#!/usr/bin/env bash
# Deploy (or update) the Cloud Run service. Every deploy is a new revision; traffic moves only
# when the new revision passes its startup check, so updates have no downtime.
#   bash deploy/deploy_cloudrun.sh                     # scale-to-zero (default)
#   MIN_INSTANCES=1 bash deploy/deploy_cloudrun.sh     # keep one instance warm (no cold starts)
set -euo pipefail
source deploy/config.sh
IMAGE="${DEPLOY_IMAGE:-$(cat deploy/.last_image 2>/dev/null || echo "$IMAGE")}"

echo "== Deploying $IMAGE  (min=$MIN_INSTANCES max=$MAX_INSTANCES concurrency=$CONCURRENCY cpu=$CPU mem=$MEMORY)"
gcloud run deploy "$SERVICE" \
  --image "$IMAGE" \
  --region "$REGION" \
  --service-account "$SA_EMAIL" \
  --set-secrets "UNC_AI_API_KEY=unc-ai-api-key:latest,APP_ACCESS_TOKEN=archivist-access-token:latest" \
  --set-env-vars "RAG_GUARD=layered,APP_VERSION=${IMAGE##*:},RAG_CACHE_MAX=5000" \
  --port 8080 \
  --cpu "$CPU" --memory "$MEMORY" \
  --concurrency "$CONCURRENCY" \
  --min-instances "$MIN_INSTANCES" --max-instances "$MAX_INSTANCES" \
  --cpu-boost \
  --timeout 60 \
  --startup-probe "httpGet.path=/ready,initialDelaySeconds=0,periodSeconds=2,failureThreshold=30,timeoutSeconds=2" \
  --liveness-probe "httpGet.path=/health,periodSeconds=30,failureThreshold=3,timeoutSeconds=5" \
  --labels "app=archivist,course=data790" \
  --allow-unauthenticated \
  --quiet

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')
echo "$URL" > deploy/.service_url
mkdir -p cloudrun
gcloud run services describe "$SERVICE" --region "$REGION" --format export > cloudrun/service.yaml
echo "== Live at: $URL"
echo "   Declarative config saved to cloudrun/service.yaml (Knative format, same API as Kubernetes)"
echo "== Smoke test"
curl -s "$URL/health"; echo
curl -s -X POST "$URL/ask" -H "Content-Type: application/json" \
  -H "X-Access-Token: $(cat deploy/.access_token)" \
  -d '{"question": "Who are Oryx'"'"'s sisters?"}' | python3 -m json.tool | head -30
