#!/usr/bin/env bash
# One-time GCP setup: APIs, image registry, a least-privilege service account, and the secrets.
#   bash deploy/setup_gcp.sh
set -euo pipefail
source deploy/config.sh

echo "== Enabling APIs (takes a minute the first time)"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com \
  logging.googleapis.com monitoring.googleapis.com

echo "== Artifact Registry repo: $REPO (private Docker registry; the image holds copyrighted lore text)"
gcloud artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "$REPO" --repository-format=docker --location "$REGION" \
    --description="The Archivist RAG service images"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

echo "== Service account: $SA_EMAIL (can read its two secrets and write logs/metrics, nothing else)"
gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --display-name="Archivist Cloud Run runtime"
for role in roles/logging.logWriter roles/monitoring.metricWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA_EMAIL" \
    --role="$role" --condition=None --quiet >/dev/null
done

put_secret() {   # name, value
  if gcloud secrets describe "$1" >/dev/null 2>&1; then
    printf '%s' "$2" | gcloud secrets versions add "$1" --data-file=- >/dev/null
  else
    printf '%s' "$2" | gcloud secrets create "$1" --data-file=- --replication-policy=automatic >/dev/null
  fi
  gcloud secrets add-iam-policy-binding "$1" --member="serviceAccount:$SA_EMAIL" \
    --role=roles/secretmanager.secretAccessor --condition=None --quiet >/dev/null
  echo "   secret $1 stored"
}

echo "== Secrets (Secret Manager, never in the image or the repo)"
KEY=$(env_value UNC_AI_API_KEY) || { echo "UNC_AI_API_KEY not found in .env or ../.env" >&2; exit 1; }
put_secret unc-ai-api-key "$KEY"

# A shared access token so a public URL can't be used to spend the UNC gateway quota.
# Saved to deploy/.access_token (gitignored) so you can give it to the grader.
if [ ! -f deploy/.access_token ]; then
  openssl rand -hex 16 > deploy/.access_token
fi
put_secret archivist-access-token "$(cat deploy/.access_token)"
echo "   access token saved in deploy/.access_token"

echo "== Done. Next: bash deploy/build_push.sh"
