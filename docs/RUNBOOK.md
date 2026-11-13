# Runbook: The Archivist (Cloud Run)

What to do when an alert fires, plus the few routine commands. Every alert email links here.
Run commands from the repo root after `export PROJECT_ID=archivist-790-hh`.

| | |
|---|---|
| Service | Cloud Run `archivist`, project `archivist-790-hh`, region `us-east1` |
| URL | https://archivist-7fjjipu24a-ue.a.run.app |
| Owner | Henry Halvorsen (alerts go to the owner's email) |
| Depends on | UNC AI Gateway, Secret Manager, Artifact Registry |
| Dashboard | Cloud Console, then Monitoring, then Dashboards, then "Archivist - LLM service" |
| Normal | p50 about 1 s, p95 about 3 s, no errors, about $0.0004 per question with caching ($0.0007 uncached), 1 instance |

## Routine commands

| Task | Command |
|---|---|
| Deploy a new version | `bash deploy/build_push.sh` then `bash deploy/deploy_cloudrun.sh` (traffic moves only after the new version passes its health check) |
| Roll back | `gcloud run revisions list --service archivist --region us-east1`, then `gcloud run services update-traffic archivist --region us-east1 --to-revisions <good-revision>=100` |
| Keep one instance warm | `MIN_INSTANCES=1 bash deploy/deploy_cloudrun.sh` (no cold starts, +$12.96/month) |
| Rotate the UNC key | put the new key in `.env`, then `bash deploy/setup_gcp.sh` and `bash deploy/deploy_cloudrun.sh` |
| Rotate the access token | `rm deploy/.access_token`, then the same two commands |
| Recent errors | `gcloud logging read 'resource.labels.service_name="archivist" AND severity>=ERROR' --freshness=30m --limit=10` |
| Shut everything down | `gcloud run services delete archivist --region us-east1` |

## When an alert fires

First check the dashboard to see what changed and when. If users were affected for more than 15 minutes, write a short note afterwards: what happened, when, why, and what to change.

### High latency (p95 above 8 s)
1. Look at recent questions' time per stage:
   `gcloud logging read 'resource.labels.service_name="archivist" AND jsonPayload.event="ask"' --freshness=15m --limit=20 --format='value(jsonPayload.latency_ms,jsonPayload.pipeline_ms)'`
2. `generate` or `input_guard` slow: the UNC gateway is slow or throttling. Nothing to fix on our side; wait.
3. Instances at 5: see *Scaling ceiling*.
4. Slow only after quiet periods: cold starts (8.6-10.5 s). Keep one instance warm if that's not acceptable.
5. Started right after a deploy: roll back.

### Gateway errors
1. Check the error type with the "Recent errors" command.
2. `AuthenticationError`: key expired: get a new one (Canvas Inbox) and rotate it.
3. `RateLimitError`: over the gateway quota: lower `MAX_INSTANCES` and check for abuse (*Spend spike*).
4. Timeouts / connection errors: gateway outage. Users get a clean "try again shortly" message; wait.

### Attack spike (more than 10 blocked questions in 5 minutes)
1. On the dashboard, "Blocked questions, by defense layer": which layer is firing?
2. Check whether anything got past the defenses (output filter or PII redaction events):
   `gcloud logging read 'resource.labels.service_name="archivist" AND jsonPayload.events:"output_filter"' --freshness=1h --limit=20`
3. Anything found: rotate the access token so the attacker loses access, then review which defense let it through.
4. If the classifier suddenly blocks normal-looking questions: likely false positives after a change; roll back.

Tested 2026-11-08: fired at 14 blocked questions during load test B and resolved itself 5 min 37 s later.

### 5xx errors / crash loop
1. Check the "Recent errors" command.
2. "No available instance": see *Scaling ceiling*.
3. Python error or the container won't start: roll back right away, then debug locally with `docker run --rm -p 8080:8080 -e UNC_AI_API_KEY archivist:local`.
4. "Memory limit exceeded": `MEMORY=2Gi bash deploy/deploy_cloudrun.sh`, then investigate.

### Scaling ceiling (5 instances for 5 minutes)
1. Real traffic or abuse? Check "Questions per minute"; if abuse, see *Spend spike*.
2. Real traffic: if there are no `RateLimitError`s, raise the ceiling (`MAX_INSTANCES=10 bash deploy/deploy_cloudrun.sh`). If the gateway is rate-limiting, more instances won't help.

### Spend spike (more than 300 questions in 5 minutes)
That rate is about 3,600 questions an hour, or roughly $1.50-2.50/hour of LLM spend.
1. Not expected: rotate the access token now; requests without the new token are rejected and cost nothing.
2. Expected (a demo or a grader's test): no action.
