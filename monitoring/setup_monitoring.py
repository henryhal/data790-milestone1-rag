"""Create the Cloud Monitoring side of the observability stack (run once after the first deploy).

    python3 monitoring/setup_monitoring.py --project YOUR_PROJECT_ID --email you@example.com

What it creates (safe to re-run: existing items are updated or skipped):
  1. Log-based metrics: turn the app's one-JSON-line-per-question logs into LLM metrics
     (latency, cost, tokens, outcomes, blocks by defense layer, cache hits, gateway errors).
  2. An email notification channel.
  3. Alert policies (alert_policies.json).
  4. A dashboard (dashboard.json) with the LLM metrics next to Cloud Run's own metrics.

Uses only the Python standard library plus your logged-in gcloud (for an access token).
"""
import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICE = "archivist"
BASE_FILTER = (f'resource.type="cloud_run_revision" AND resource.labels.service_name="{SERVICE}" '
               f'AND jsonPayload.event="ask"')


def dist(name, desc, field, unit, scale, growth=1.6, n=24, labels=()):
    m = {"name": name, "description": desc, "filter": BASE_FILTER,
         "valueExtractor": f"EXTRACT(jsonPayload.{field})",
         "metricDescriptor": {"metricKind": "DELTA", "valueType": "DISTRIBUTION", "unit": unit,
                              "labels": [{"key": k, "valueType": "STRING"} for k in labels]},
         "labelExtractors": {k: f"EXTRACT(jsonPayload.{k})" for k in labels},
         "bucketOptions": {"exponentialBuckets": {"numFiniteBuckets": n, "growthFactor": growth,
                                                  "scale": scale}}}
    return m


def counter(name, desc, extra_filter="", labels=()):
    return {"name": name, "description": desc, "filter": BASE_FILTER + extra_filter,
            "metricDescriptor": {"metricKind": "DELTA", "valueType": "INT64", "unit": "1",
                                 "labels": [{"key": k, "valueType": "STRING"} for k in labels]},
            "labelExtractors": {k: f"EXTRACT(jsonPayload.{k})" for k in labels}}


LOG_METRICS = [
    dist("archivist_latency", "End-to-end /ask latency (ms)", "latency_ms", "ms", 50, labels=("outcome",)),
    dist("archivist_cost", "Estimated LLM cost per question (USD, public list prices)", "cost_usd", "USD", 0.00001),
    dist("archivist_prompt_tokens", "Prompt tokens per generated answer", "prompt_tokens", "1", 50, 1.5),
    dist("archivist_completion_tokens", "Completion tokens per generated answer", "completion_tokens", "1", 5, 1.5),
    counter("archivist_requests", "Questions, by outcome and cache hit", labels=("outcome", "cache_hit")),
    counter("archivist_blocked", "Questions stopped by a defense layer", ' AND jsonPayload.outcome="blocked"',
            labels=("blocked_by",)),
    counter("archivist_errors", "Failed questions (gateway errors, timeouts)", ' AND severity="ERROR"'),
]


def token():
    return subprocess.run(["gcloud", "auth", "print-access-token"], check=True, capture_output=True,
                          text=True).stdout.strip()


def call(method, url, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {token()}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        if e.code == 409:
            return {"_exists": True}
        sys.exit(f"{method} {url} failed ({e.code}):\n{detail}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True)
    p.add_argument("--email", help="where alerts go (skip to create policies with no notifications)")
    a = p.parse_args()
    proj = a.project
    logging_api = f"https://logging.googleapis.com/v2/projects/{proj}/metrics"
    mon = f"https://monitoring.googleapis.com/v3/projects/{proj}"

    print("== Log-based metrics")
    for m in LOG_METRICS:
        res = call("POST", logging_api, m)
        if res.get("_exists"):
            call("PUT", f"{logging_api}/{m['name']}", m)
            print(f"   updated {m['name']}")
        else:
            print(f"   created {m['name']}")

    channels = []
    if a.email:
        print("== Email notification channel")
        existing = call("GET", f"{mon}/notificationChannels").get("notificationChannels", [])
        match = [c for c in existing if c.get("labels", {}).get("email_address") == a.email]
        ch = match[0] if match else call("POST", f"{mon}/notificationChannels", {
            "type": "email", "displayName": f"Archivist alerts ({a.email})",
            "labels": {"email_address": a.email}})
        channels = [ch["name"]]
        print(f"   {ch['name']}")

    print("== Alert policies")
    existing = {pol["displayName"]: pol["name"] for pol in
                call("GET", f"{mon}/alertPolicies").get("alertPolicies", [])}
    for pol in json.loads((HERE / "alert_policies.json").read_text()):
        pol["notificationChannels"] = channels
        if pol["displayName"] in existing:
            call("DELETE", f"https://monitoring.googleapis.com/v3/{existing[pol['displayName']]}")
        call("POST", f"{mon}/alertPolicies", pol)
        print(f"   {pol['displayName']}")

    print("== Dashboard")
    dash = json.loads((HERE / "dashboard.json").read_text())
    dashboards = call("GET", f"https://monitoring.googleapis.com/v1/projects/{proj}/dashboards").get("dashboards", [])
    for d in dashboards:
        if d.get("displayName") == dash["displayName"]:
            call("DELETE", f"https://monitoring.googleapis.com/v1/{d['name']}")
    d = call("POST", f"https://monitoring.googleapis.com/v1/projects/{proj}/dashboards", dash)
    dash_id = d["name"].split("/")[-1]
    print(f"   https://console.cloud.google.com/monitoring/dashboards/builder/{dash_id}?project={proj}")
    print("\nNote: log-based metrics only count log lines written after they were created, "
          "so send some traffic (monitoring/load_test.py) before expecting charts.")


if __name__ == "__main__":
    main()
