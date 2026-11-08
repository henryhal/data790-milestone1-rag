# Shared settings for the deploy scripts. Edit PROJECT_ID once; everything else has a sensible default.
# Every script does `source deploy/config.sh`, so run them from the repo root:  bash deploy/<script>.sh
PROJECT_ID="${PROJECT_ID:-YOUR_PROJECT_ID}"
REGION="${REGION:-us-east1}"                      # South Carolina: closest Cloud Run region to UNC
SERVICE="${SERVICE:-archivist}"
REPO="${REPO:-archivist}"                         # Artifact Registry repository name
SA_NAME="${SA_NAME:-archivist-run}"               # the identity the service runs as (least privilege)
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
VERSION="${VERSION:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}:${VERSION}"

# Scaling settings (see DEPLOYMENT_REPORT.md for why these numbers)
MIN_INSTANCES="${MIN_INSTANCES:-0}"     # 0 = scale to zero (cheapest, cold starts); 1 = always warm
MAX_INSTANCES="${MAX_INSTANCES:-5}"     # cost ceiling; the gateway's tokens-per-minute quota limits useful scale anyway
CONCURRENCY="${CONCURRENCY:-20}"        # requests per instance at once (custom scaling metric: requests, not CPU)
CPU="${CPU:-1}"
MEMORY="${MEMORY:-512Mi}"               # measured ~175 MB peak; 512Mi is the minimum Cloud Run allows at 1 vCPU

if [ "$PROJECT_ID" = "YOUR_PROJECT_ID" ]; then
  echo "Set PROJECT_ID first:  export PROJECT_ID=your-gcp-project-id   (or edit deploy/config.sh)" >&2
  exit 1
fi
gcloud config set project "$PROJECT_ID" >/dev/null 2>&1 || true

# Read one key from .env (this folder or a parent) without `source`-ing the whole file.
env_value() {
  for f in .env ../.env; do
    if [ -f "$f" ]; then
      v=$(grep -E "^$1=" "$f" | tail -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//')
      if [ -n "$v" ]; then printf '%s' "$v"; return 0; fi
    fi
  done
  return 1
}
