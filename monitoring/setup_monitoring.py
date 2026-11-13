"""Create the Cloud Monitoring side of the observability stack (run once after the first deploy).

    python3 monitoring/setup_monitoring.py --project YOUR_PROJECT_ID --email you@example.com

What it creates (safe to re-run: existing items are updated or skipped):
  1. Log-based metrics: turn the app's one-JSON-line-per-question logs into LLM metrics
     (latency, cost, tokens, outcomes, blocks by defense layer, cache hits, gateway errors).
  2. An email notification channel.
  3. 6 alert policies (ALERTS below).
  4. A dashboard (dashboard() below) with the LLM metrics next to Cloud Run's own metrics.

Uses only the Python standard library plus your logged-in gcloud (for an access token).
"""
import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request

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


# Alert policies
RUN = f'resource.type="cloud_run_revision" AND resource.labels.service_name="{SERVICE}"'


def user_metric(name):
    return f'metric.type="logging.googleapis.com/user/{name}" AND {RUN}'


def alert(name, severity, condition, metric_filter, aligner, reducer, threshold, doc,
          period="300s", duration="0s"):
    return {"displayName": name, "severity": severity, "combiner": "OR",
            "conditions": [{"displayName": condition, "conditionThreshold": {
                "filter": metric_filter,
                "aggregations": [{"alignmentPeriod": period, "perSeriesAligner": aligner,
                                  "crossSeriesReducer": reducer}],
                "comparison": "COMPARISON_GT", "thresholdValue": threshold,
                "duration": duration, "trigger": {"count": 1}}}],
            "alertStrategy": {"autoClose": "1800s"},
            "documentation": {"mimeType": "text/markdown", "content": doc + "\n\nRunbook: docs/RUNBOOK.md"}}


ALERTS = [
    alert("Archivist: p95 latency above 8 s", "WARNING", "p95 /ask latency > 8000 ms (5 min)",
          user_metric("archivist_latency"), "ALIGN_DELTA", "REDUCE_PERCENTILE_95", 8000,
          "Players are waiting more than 8 s for answers (normal p95 is about 3 s). Usually the UNC gateway is slow or rate-limiting, or instances are saturated. See runbook section **High latency**."),
    alert("Archivist: gateway errors", "ERROR", "failed questions > 3 (5 min)",
          user_metric("archivist_errors"), "ALIGN_SUM", "REDUCE_SUM", 3,
          "Calls to the UNC AI Gateway are failing (key expired/revoked, quota exceeded, or gateway outage). See runbook section **Gateway errors**."),
    alert("Archivist: attack spike", "WARNING", "questions blocked by defenses > 10 (5 min)",
          user_metric("archivist_blocked"), "ALIGN_SUM", "REDUCE_SUM", 10,
          "More than 10 questions were blocked in 5 minutes: someone is probably probing the defenses. Check which layer is firing (dashboard) and the logs. See runbook section **Attack spike**."),
    alert("Archivist: HTTP 5xx responses", "ERROR", "5xx responses > 5 (5 min)",
          f'metric.type="run.googleapis.com/request_count" AND {RUN} AND metric.labels.response_code_class="5xx"',
          "ALIGN_SUM", "REDUCE_SUM", 5,
          "The service is returning server errors. See runbook section **5xx errors / crash loop**."),
    alert("Archivist: running at max instances", "WARNING", "instances >= 5 for 5 min",
          f'metric.type="run.googleapis.com/container/instance_count" AND {RUN}', "ALIGN_MAX", "REDUCE_SUM", 4,
          "Autoscaling has hit its ceiling (max-instances=5) for 5 minutes, so new requests queue. Decide whether to raise max-instances (cost) or whether the gateway quota is the real limit. See runbook section **Scaling ceiling**.",
          period="60s", duration="300s"),
    alert("Archivist: question volume spike (LLM spend guard)", "WARNING", "questions > 300 (5 min)",
          user_metric("archivist_requests"), "ALIGN_SUM", "REDUCE_SUM", 300,
          "Over 300 questions in 5 minutes (about $0.20 of LLM spend per 5 min at $0.00064/question, 10x normal). Could be real traffic or abuse. See runbook section **Spend spike**."),
]


# Dashboard: 12 charts in a 3-column grid under a short text tile
def series(metric_filter, aligner, reducer, group=None, legend=None, plot="LINE"):
    agg = {"alignmentPeriod": "60s", "perSeriesAligner": aligner, "crossSeriesReducer": reducer}
    if group:
        agg["groupByFields"] = group
    d = {"timeSeriesQuery": {"timeSeriesFilter": {"filter": metric_filter, "aggregation": agg}},
         "plotType": plot, "minAlignmentPeriod": "60s"}
    if legend:
        d["legendTemplate"] = legend
    return d


def chart(title, data_sets, y_label):
    return {"title": title, "xyChart": {"dataSets": data_sets, "timeshiftDuration": "0s",
                                        "yAxis": {"label": y_label, "scale": "LINEAR"}}}


def dashboard():
    run_metric = lambda m: f'metric.type="run.googleapis.com/{m}" AND {RUN}'
    charts = [
        chart("Questions per minute, by outcome", [series(user_metric("archivist_requests"), "ALIGN_SUM", "REDUCE_SUM", ["metric.label.outcome"], "${metric.labels.outcome}", "STACKED_BAR")], "questions"),
        chart("Answer latency p50 / p95 (ms)", [series(user_metric("archivist_latency"), "ALIGN_DELTA", "REDUCE_PERCENTILE_50", legend="p50"),
                                                series(user_metric("archivist_latency"), "ALIGN_DELTA", "REDUCE_PERCENTILE_95", legend="p95")], "ms"),
        chart("LLM cost per question (USD, mean)", [series(user_metric("archivist_cost"), "ALIGN_DELTA", "REDUCE_MEAN", legend="mean cost")], "USD"),
        chart("Tokens per answer (mean)", [series(user_metric("archivist_prompt_tokens"), "ALIGN_DELTA", "REDUCE_MEAN", legend="prompt"),
                                           series(user_metric("archivist_completion_tokens"), "ALIGN_DELTA", "REDUCE_MEAN", legend="completion")], "tokens"),
        chart("Blocked questions, by defense layer", [series(user_metric("archivist_blocked"), "ALIGN_SUM", "REDUCE_SUM", ["metric.label.blocked_by"], "${metric.labels.blocked_by}", "STACKED_BAR")], "blocked"),
        chart("Cache hits vs. gateway calls", [series(user_metric("archivist_requests"), "ALIGN_SUM", "REDUCE_SUM", ["metric.label.cache_hit"], "cache_hit=${metric.labels.cache_hit}", "STACKED_BAR")], "questions"),
        chart("Instances (autoscaling)", [series(run_metric("container/instance_count"), "ALIGN_MAX", "REDUCE_SUM", ["metric.label.state"], "${metric.labels.state}", "STACKED_AREA")], "instances"),
        chart("Cold starts: container startup latency (ms)", [series(run_metric("container/startup_latencies"), "ALIGN_DELTA", "REDUCE_PERCENTILE_95", legend="p95 startup")], "ms"),
        chart("Cloud Run request latency p95 (ms)", [series(run_metric("request_latencies"), "ALIGN_DELTA", "REDUCE_PERCENTILE_95", legend="p95")], "ms"),
        chart("Requests by HTTP status class", [series(run_metric("request_count"), "ALIGN_SUM", "REDUCE_SUM", ["metric.label.response_code_class"], "${metric.labels.response_code_class}", "STACKED_BAR")], "requests"),
        chart("Container CPU utilization p95", [series(run_metric("container/cpu/utilizations"), "ALIGN_DELTA", "REDUCE_PERCENTILE_95", legend="CPU p95")], "fraction"),
        chart("Container memory utilization p95", [series(run_metric("container/memory/utilizations"), "ALIGN_DELTA", "REDUCE_PERCENTILE_95", legend="memory p95")], "fraction"),
    ]
    intro = {"title": "About this dashboard", "text": {"format": "MARKDOWN", "content":
             "**The Archivist (Destiny lore RAG) on Cloud Run.** Top rows: LLM metrics from the app's structured logs (log-based metrics). Bottom rows: Cloud Run's built-in container metrics. Alerts and this dashboard are defined in `monitoring/setup_monitoring.py`. Runbook: `docs/RUNBOOK.md`."}}
    tiles = [{"xPos": 0, "yPos": 0, "width": 12, "height": 2, "widget": intro}]
    tiles += [{"xPos": (i % 3) * 4, "yPos": 2 + (i // 3) * 4, "width": 4, "height": 4, "widget": c}
              for i, c in enumerate(charts)]
    return {"displayName": "Archivist - LLM service", "mosaicLayout": {"columns": 12, "tiles": tiles}}


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
    for pol in ALERTS:
        pol = dict(pol, notificationChannels=channels)
        if pol["displayName"] in existing:
            call("DELETE", f"https://monitoring.googleapis.com/v3/{existing[pol['displayName']]}")
        call("POST", f"{mon}/alertPolicies", pol)
        print(f"   {pol['displayName']}")

    print("== Dashboard")
    dash = dashboard()
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
