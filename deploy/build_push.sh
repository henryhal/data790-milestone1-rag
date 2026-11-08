#!/usr/bin/env bash
# Build the image for Cloud Run (linux/amd64, even on an Apple Silicon Mac), scan it, and push it.
#   bash deploy/build_push.sh
set -euo pipefail
source deploy/config.sh

for f in data/corpus/docs.jsonl .cache/m2_destiny_sentence_800.npz; do
  [ -f "$f" ] || { echo "Missing $f: build the corpus/index locally first (README steps 3-4)" >&2; exit 1; }
done

echo "== Building $IMAGE"
time docker buildx build --platform linux/amd64 --build-arg APP_VERSION="$VERSION" \
  -t "$IMAGE" -t archivist:local --load .

echo "== Image size"
docker image ls archivist:local --format '{{.Repository}}:{{.Tag}}  {{.Size}}'

echo "== Security scan (Trivy: HIGH/CRITICAL vulnerabilities that have a fix available)"
mkdir -p security
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$HOME/.cache/trivy:/root/.cache/" \
  aquasec/trivy:0.58.2 image --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed \
  --format table "$IMAGE" | tee security/trivy-report.txt
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$HOME/.cache/trivy:/root/.cache/" \
  aquasec/trivy:0.58.2 image --scanners secret --format table "$IMAGE" | tee security/trivy-secrets.txt

echo "== Pushing"
docker push "$IMAGE"
echo "$IMAGE" > deploy/.last_image
echo "== Done. Next: bash deploy/deploy_cloudrun.sh"
