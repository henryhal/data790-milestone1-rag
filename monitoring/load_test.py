"""Load-test the deployed Archivist and log the run to MLflow.

    python monitoring/load_test.py --label scale-to-zero --requests 120 --concurrency 12
    mlflow ui --backend-store-uri sqlite:///mlflow.db     # then open http://127.0.0.1:5000

The URL and access token default to the files deploy/deploy_cloudrun.sh writes (deploy/.service_url,
deploy/.access_token). Questions come from the gold set (data/gold_set.csv); --attack-share mixes in
prompt-injection attacks from data/attacks.json so the defense metrics and alerts get exercised too.

Each run records, in MLflow:
  params   label, url, concurrency, requests, attack share, deployed version, min instances (from --label/--min-instances)
  metrics  client latency p50/p95/p99, server latency, error rate, throughput, cost per question,
           cache-hit rate, blocked rate, distinct instances that served traffic (autoscaling evidence),
           first-request latency (cold start when the service was scaled to zero), projected monthly cost
  steps    latency_ms per request, in the order they finished (MLflow draws it as a chart)
  files    per-request CSV and the run summary

Results are also saved as CSV (like the Milestone 2 results/ files):
  results/load_tests.csv                    one row per measurement, one column per load test
  results/load_test_<label>_requests.csv    one row per question in that run
"""
import argparse
import csv
import io
import json
import random
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(path, default=""):
    p = ROOT / path
    return p.read_text().strip() if p.exists() else default


def post(url, token, question, timeout=90):
    body = json.dumps({"question": question}).encode()
    req = urllib.request.Request(f"{url}/ask", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Access-Token": token})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
            status = r.status
    except urllib.error.HTTPError as e:
        d, status = {}, e.code
    except Exception as e:                     # timeout, connection reset
        d, status = {"error": type(e).__name__}, 0
    return {"status": status, "client_ms": (time.perf_counter() - t0) * 1000,
            "server_ms": d.get("latency_ms"), "outcome": d.get("outcome", "error"),
            "blocked_by": d.get("blocked_by") or "", "cost_usd": d.get("cost_usd", 0.0),
            "cache_hit": d.get("cache_hit", False), "instance": d.get("instance", ""),
            "finished_at": time.time()}


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q / 100 * (len(xs) - 1))))] if xs else float("nan")


# What each row of results/load_tests.csv means (rows = measurements, one column per load test).
METRICS = [
    ("version", "Deployed app version (git commit) that was tested"),
    ("requests", "Questions sent"),
    ("concurrency", "Questions in flight at once (simulated simultaneous users)"),
    ("instances_seen", "Cloud Run instances that answered at least one question (autoscaling)"),
    ("wall_seconds", "How long the whole test took (s)"),
    ("throughput_rps", "Questions answered per second"),
    ("error_rate", "Share of questions that failed (0.005 = 0.5%)"),
    ("errors", "Number of failed questions"),
    ("latency_p50_ms", "Median wait for an answer, measured by the client (ms)"),
    ("latency_p95_ms", "95% of answers arrived within this time (ms)"),
    ("latency_p99_ms", "99% of answers arrived within this time (ms)"),
    ("server_latency_mean_ms", "Average time spent inside the app per question (ms)"),
    ("first_request_ms", "Time for the very first question of the test (ms)"),
    ("cache_hit_rate", "Share of questions answered from the in-memory cache"),
    ("cost_total_usd", "LLM cost of the whole test (USD)"),
    ("cost_per_question_usd", "Average LLM cost per question (USD)"),
    ("projected_llm_cost_per_month_usd", "That cost at 10,000 questions/day for 30 days (USD)"),
    ("answered", "Questions answered"),
    ("declined", "Questions the app declined (not in the lore)"),
    ("blocked", "Questions stopped by a defense layer"),
    ("blocked_by_pattern", "...stopped by the regex pattern layer"),
    ("blocked_by_classifier", "...stopped by the LLM injection classifier"),
    ("blocked_by_gateway", "...stopped by Azure's content filter at the UNC gateway"),
]


def flatten(summary):
    """One value per measurement in METRICS (nested counts become their own rows)."""
    outcomes, blocks = summary.get("outcomes", {}), summary.get("blocked_by", {})
    flat = dict(summary)
    flat["errors"] = outcomes.get("error", 0)
    for k in ("answered", "declined", "blocked"):
        flat[k] = outcomes.get(k, 0)
    for k in ("pattern", "classifier", "gateway"):
        flat[f"blocked_by_{k}"] = blocks.get(k, 0)
    return {m: flat.get(m, "") for m, _ in METRICS}


def save_csv(summary, rows, label):
    """results/load_tests.csv: one row per measurement, one column per load test (re-running a label
    replaces its column). results/load_test_<label>_requests.csv: one row per question."""
    results = ROOT / "results"
    results.mkdir(exist_ok=True)
    runs_file = results / "load_tests.csv"
    runs = {}
    if runs_file.exists():
        table = list(csv.reader(open(runs_file)))
        for j, run_label in enumerate(table[0][1:-1], start=1):
            runs[run_label] = {r[0]: r[j] for r in table[1:]}
    runs[label] = flatten(summary)
    with open(runs_file, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric"] + list(runs) + ["meaning"])
        for m, meaning in METRICS:
            w.writerow([m] + [runs[r].get(m, "") for r in runs] + [meaning])
    req_file = results / f"load_test_{label}_requests.csv"
    with open(req_file, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Saved {runs_file.relative_to(ROOT)} and {req_file.relative_to(ROOT)}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=read("deploy/.service_url"))
    p.add_argument("--token", default=read("deploy/.access_token"))
    p.add_argument("--label", required=True, help="name for this run, e.g. scale-to-zero or warm-min1")
    p.add_argument("--min-instances", default="", help="recorded as a param (what the service was deployed with)")
    p.add_argument("--requests", type=int, default=120)
    p.add_argument("--concurrency", type=int, default=12)
    p.add_argument("--attack-share", type=float, default=0.1, help="fraction of requests that are attacks")
    p.add_argument("--questions-per-day", type=int, default=10_000, help="for the monthly cost projection")
    p.add_argument("--seed", type=int, default=790)
    p.add_argument("--no-mlflow", action="store_true")
    a = p.parse_args()
    assert a.url, "No URL: pass --url or run deploy/deploy_cloudrun.sh first"

    rng = random.Random(a.seed)
    with open(ROOT / "data" / "gold_set.csv") as f:
        gold = [row["question"] for row in csv.DictReader(f)]
    attacks = [x["payload"] for x in json.loads((ROOT / "data" / "attacks.json").read_text())]
    questions = [rng.choice(attacks) if rng.random() < a.attack_share else rng.choice(gold)
                 for _ in range(a.requests)]

    health = json.loads(urllib.request.urlopen(f"{a.url}/health", timeout=120).read())
    # The /health call above may itself have paid the cold start; time a real question separately.
    first = post(a.url, a.token, gold[0])
    print(f"Service {a.url} version {health.get('version')} | first question {first['client_ms']:.0f} ms")

    print(f"Sending {a.requests} questions at concurrency {a.concurrency}...")
    t0 = time.time()
    rows = []
    with ThreadPoolExecutor(max_workers=a.concurrency) as pool:
        futures = [pool.submit(post, a.url, a.token, q) for q in questions]
        for i, fut in enumerate(as_completed(futures), 1):
            rows.append(fut.result())
            if i % 20 == 0:
                print(f"  {i}/{a.requests}")
    wall = time.time() - t0

    ok = [r for r in rows if r["status"] == 200]
    lat = [r["client_ms"] for r in ok]
    cost = sum(r["cost_usd"] for r in ok)
    per_q = cost / len(ok) if ok else 0.0
    summary = {
        "label": a.label, "url": a.url, "version": health.get("version"), "requests": a.requests,
        "concurrency": a.concurrency, "wall_seconds": round(wall, 1),
        "throughput_rps": round(len(ok) / wall, 2) if wall else 0,
        "error_rate": round(1 - len(ok) / len(rows), 4),
        "status_counts": {str(s): sum(r["status"] == s for r in rows) for s in sorted({r["status"] for r in rows})},
        "latency_p50_ms": round(pct(lat, 50), 1), "latency_p95_ms": round(pct(lat, 95), 1),
        "latency_p99_ms": round(pct(lat, 99), 1),
        "server_latency_mean_ms": round(statistics.mean([r["server_ms"] for r in ok if r["server_ms"]]), 1) if ok else None,
        "first_request_ms": round(first["client_ms"], 1),
        "cost_total_usd": round(cost, 6), "cost_per_question_usd": round(per_q, 6),
        "cache_hit_rate": round(sum(r["cache_hit"] for r in ok) / len(ok), 3) if ok else 0,
        "blocked_rate": round(sum(r["outcome"] == "blocked" for r in ok) / len(ok), 3) if ok else 0,
        "blocked_by": {k: sum(r["blocked_by"] == k for r in ok) for k in sorted({r["blocked_by"] for r in ok} - {""})},
        "outcomes": {k: sum(r["outcome"] == k for r in rows) for k in sorted({r["outcome"] for r in rows})},
        "instances_seen": len({r["instance"] for r in ok if r["instance"]}),
        "projected_llm_cost_per_month_usd": round(per_q * a.questions_per_day * 30, 2),
    }
    print(json.dumps(summary, indent=2))
    save_csv(summary, rows, a.label)

    if a.no_mlflow:
        return
    import mlflow
    # SQLite backend (newer MLflow versions refuse the old ./mlruns folder store). Both files are small and
    # committed, so a grader can run `mlflow ui --backend-store-uri sqlite:///mlflow.db` on a fresh clone.
    mlflow.set_tracking_uri(f"sqlite:///{ROOT / 'mlflow.db'}")
    name = "archivist-cloud-deployment"
    if mlflow.get_experiment_by_name(name) is None:
        mlflow.create_experiment(name, artifact_location=(ROOT / "mlartifacts").as_uri())
    mlflow.set_experiment(name)
    with mlflow.start_run(run_name=a.label):
        mlflow.set_tags({"service": "archivist", "platform": "gcp-cloud-run", "kind": "load-test"})
        mlflow.log_params({"label": a.label, "url": a.url, "version": health.get("version"),
                           "requests": a.requests, "concurrency": a.concurrency,
                           "attack_share": a.attack_share, "min_instances": a.min_instances or "unknown",
                           "questions_per_day_assumed": a.questions_per_day})
        mlflow.log_metrics({k: float(v) for k, v in summary.items()
                            if isinstance(v, (int, float)) and k not in ("requests", "concurrency")})
        for step, r in enumerate(sorted(rows, key=lambda r: r["finished_at"])):
            mlflow.log_metric("latency_ms", r["client_ms"], step=step)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
        mlflow.log_text(buf.getvalue(), "requests.csv")
        mlflow.log_dict(summary, "summary.json")
    print("Logged to MLflow: run `mlflow ui --backend-store-uri sqlite:///mlflow.db` in the repo folder")


if __name__ == "__main__":
    main()
