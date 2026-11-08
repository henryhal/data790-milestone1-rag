"""The Archivist as a web service: the Milestone 2 RAG pipeline (rag.py + defenses.py) behind FastAPI.

    uvicorn app:app --port 8080            # local run (needs UNC_AI_API_KEY in .env and a built index)

Endpoints
    GET  /          a small page to ask questions in the browser
    POST /ask       {"question": "..."} -> answer, citations, latency, cost, which defense layer acted
    GET  /health    liveness: the process is up
    GET  /ready     readiness: the Chroma index is loaded (Kubernetes/Cloud Run only send traffic when this is 200)
    GET  /metrics   Prometheus metrics (requests, latency, tokens, cost, blocks, cache hits, in-flight requests)

Every /ask writes one JSON log line to stdout. Cloud Run sends stdout to Cloud Logging, where the
log-based metrics in monitoring/ turn those lines into LLM dashboards and alerts. The question text is
never logged (it could hold PII); only its length and a short hash are.
"""
import os

# Serving mode: keep rag.py's caches in memory instead of rewriting JSON files on every call.
# Must be set before importing rag.
os.environ.setdefault("RAG_CACHE_WRITES", "0")

import hmac
import json
import time
import uuid
import hashlib
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from prometheus_client import (CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest)

import rag

APP_VERSION = os.getenv("APP_VERSION", "dev")
GUARD_NAME = os.getenv("RAG_GUARD", "layered")          # "layered" (defended) or "none"
ACCESS_TOKEN = os.getenv("APP_ACCESS_TOKEN", "")       # if set, /ask needs the X-Access-Token header
INSTANCE_ID = uuid.uuid4().hex[:8]                     # lets the load test count how many replicas served it
_STARTED = time.time()

# ── Logging: one JSON object per line (Cloud Logging reads "severity" and "message") ─────────────
log = logging.getLogger("archivist")
log.setLevel(logging.INFO)
_h = logging.StreamHandler()
_h.setFormatter(logging.Formatter("%(message)s"))
log.addHandler(_h)
log.propagate = False


def log_json(severity, message, **fields):
    log.info(json.dumps({"severity": severity, "message": message, "instance": INSTANCE_ID,
                         "version": APP_VERSION, **fields}, default=str))


# ── Prometheus metrics ──────────────────────────────────────────────────────────────────────────
REQUESTS = Counter("archivist_requests_total", "Questions answered, by outcome",
                   ["outcome"])                         # answered / declined / blocked / error
BLOCKS = Counter("archivist_blocked_total", "Questions stopped by a defense layer", ["layer"])
LATENCY = Histogram("archivist_request_latency_seconds", "End-to-end /ask latency",
                    buckets=(0.25, 0.5, 1, 1.5, 2, 3, 5, 8, 13, 21, 30))
TOKENS = Counter("archivist_llm_tokens_total", "Generation tokens billed by the gateway", ["kind"])
COST = Counter("archivist_llm_cost_usd_total", "Estimated LLM spend (public list prices)")
CACHE_HITS = Counter("archivist_cache_hits_total", "Questions answered from the in-memory cache")
GATEWAY_ERRORS = Counter("archivist_gateway_errors_total", "Calls to the UNC AI Gateway that failed")
INFLIGHT = Gauge("archivist_inflight_requests", "Requests being processed right now")
STARTUP = Gauge("archivist_startup_seconds", "Seconds from process start to ready (cold-start cost)")

_guard = None
_ready = threading.Event()


@asynccontextmanager
async def lifespan(_app):
    global _guard
    t0 = time.time()
    _guard = rag.load_guard(GUARD_NAME)
    chunks = rag.get_collection().count()               # opens the baked-in Chroma index
    STARTUP.set(time.time() - _STARTED)
    _ready.set()
    log_json("INFO", "ready", event="startup", chunks=chunks, guard=_guard.name,
             load_seconds=round(time.time() - t0, 2), startup_seconds=round(time.time() - _STARTED, 2))
    yield


app = FastAPI(title="The Archivist - Destiny lore guide", version=APP_VERSION, lifespan=lifespan)


class Question(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)


@app.get("/health")
def health():
    return {"status": "ok", "instance": INSTANCE_ID, "version": APP_VERSION}


@app.get("/ready")
def ready():
    if not _ready.is_set():
        return JSONResponse({"status": "starting"}, status_code=503)
    return {"status": "ready", "instance": INSTANCE_ID, "guard": _guard.name,
            "uptime_seconds": round(time.time() - _STARTED, 1)}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/ask")
def ask(body: Question, request: Request, x_access_token: str = Header(default="")):
    # Plain "def": FastAPI runs it in a thread pool, so one instance serves several questions at once
    # while each waits on the gateway (that's what Cloud Run's concurrency setting relies on).
    if ACCESS_TOKEN and not hmac.compare_digest(x_access_token, ACCESS_TOKEN):
        raise HTTPException(status_code=401, detail="missing or wrong X-Access-Token")
    if not _ready.is_set():
        raise HTTPException(status_code=503, detail="index still loading")

    request_id = request.headers.get("x-cloud-trace-context", "").split("/")[0] or uuid.uuid4().hex[:16]
    q_hash = hashlib.sha256(body.question.encode()).hexdigest()[:12]
    INFLIGHT.inc()
    t0 = time.perf_counter()
    try:
        r = rag.ask(body.question, guard=_guard)
    except Exception as e:                                  # gateway down, quota exceeded, timeout...
        wall = time.perf_counter() - t0
        GATEWAY_ERRORS.inc()
        REQUESTS.labels("error").inc()
        LATENCY.observe(wall)
        log_json("ERROR", "ask failed", event="ask", request_id=request_id, question_hash=q_hash,
                 error_type=type(e).__name__, error=str(e)[:300], latency_ms=round(wall * 1000, 1))
        raise HTTPException(status_code=502, detail="the lore service is unavailable, try again shortly")
    finally:
        INFLIGHT.dec()

    wall = time.perf_counter() - t0
    cache_hit = bool(r.get("cache_hit"))
    billed = 0.0 if cache_hit else r["cost_usd"]          # a cache hit makes no gateway calls
    outcome = "blocked" if r["blocked_by"] else ("declined" if r["declined"] else "answered")

    REQUESTS.labels(outcome).inc()
    LATENCY.observe(wall)
    COST.inc(billed)
    if cache_hit:
        CACHE_HITS.inc()
    else:
        TOKENS.labels("prompt").inc(r.get("prompt_tokens", 0))
        TOKENS.labels("completion").inc(r.get("completion_tokens", 0))
    if r["blocked_by"]:
        BLOCKS.labels(r["blocked_by"]).inc()

    log_json("WARNING" if r["blocked_by"] else "INFO", f"ask {outcome}", event="ask",
             request_id=request_id, question_hash=q_hash, question_chars=len(body.question),
             outcome=outcome, blocked_by=r["blocked_by"] or "", guard=r["guard"],
             latency_ms=round(wall * 1000, 1), pipeline_ms=r["timings_ms"],
             prompt_tokens=r.get("prompt_tokens", 0), completion_tokens=r.get("completion_tokens", 0),
             cost_usd=billed, cache_hit=cache_hit, gateway_flags=r["gateway_flags"],
             events=r["events"], top_distance=r["retrieved"][0]["distance"] if r["retrieved"] else None)

    return {"answer": r["answer"],
            "sources": [{"n": i, "title": c["title"], "chunk_id": c["chunk_id"], "distance": c["distance"]}
                        for i, c in enumerate(r["retrieved"], 1) if c["chunk_id"] in r["used"]],
            "outcome": outcome, "blocked_by": r["blocked_by"], "latency_ms": round(wall * 1000, 1),
            "cost_usd": billed, "cache_hit": cache_hit, "instance": INSTANCE_ID, "version": APP_VERSION}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>The Archivist</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 16px;color:#1d1d1f}
h1{font-size:1.6rem;margin-bottom:0}p.sub{color:#666;margin-top:4px}
textarea,input{width:100%;box-sizing:border-box;font:inherit;padding:10px;border:1px solid #ccc;border-radius:8px}
button{margin-top:10px;padding:10px 18px;font:inherit;border:0;border-radius:8px;background:#2b4c7e;color:#fff;cursor:pointer}
#out{white-space:pre-wrap;margin-top:24px}.meta{color:#666;font-size:.85rem;margin-top:12px}</style></head>
<body><h1>The Archivist</h1><p class="sub">Ask about the story and lore of Destiny and Destiny 2.</p>
<textarea id="q" rows="3" placeholder="Who are Oryx's sisters?"></textarea>
__TOKEN_FIELD__<button id="b">Ask</button><div id="out"></div><div class="meta" id="meta"></div>
<script>
const b=document.getElementById('b');
b.onclick=async()=>{const q=document.getElementById('q').value.trim();if(!q)return;
b.disabled=true;document.getElementById('out').textContent='Searching the archive...';document.getElementById('meta').textContent='';
const t=document.getElementById('tok');
try{const r=await fetch('/ask',{method:'POST',headers:{'Content-Type':'application/json','X-Access-Token':t?t.value:''},body:JSON.stringify({question:q})});
const d=await r.json();if(!r.ok){document.getElementById('out').textContent='Error: '+(d.detail||r.status);return;}
document.getElementById('out').textContent=d.answer+(d.sources.length?'\\n\\nSources:\\n'+d.sources.map(s=>'['+s.n+'] '+s.title).join('\\n'):'');
document.getElementById('meta').textContent=`${d.latency_ms.toFixed(0)} ms - $${d.cost_usd.toFixed(5)} - ${d.cache_hit?'cache hit - ':''}${d.blocked_by?'blocked by '+d.blocked_by+' - ':''}instance ${d.instance} - ${d.version}`;}
catch(e){document.getElementById('out').textContent='Error: '+e;}finally{b.disabled=false;}};
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    token_field = ('<input id="tok" type="password" placeholder="Access token" style="margin-top:10px">'
                   if ACCESS_TOKEN else "")
    return PAGE.replace("__TOKEN_FIELD__", token_field)
