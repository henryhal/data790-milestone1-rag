# syntax=docker/dockerfile:1.7
# The Archivist (Destiny lore RAG) - production image.
#
#   docker buildx build --platform linux/amd64 -t archivist:local .
#   docker run --rm -p 8080:8080 --env-file .env archivist:local
#
# Two stages: "builder" has pip and the raw corpus and builds the Chroma index; "runtime" gets only the
# installed packages, the finished index and three .py files. Nothing from .env is ever copied in
# (.dockerignore is an allowlist); the API key is passed at run time.

ARG PYTHON_IMAGE=python:3.10-slim-bookworm

# ── Stage 1: install dependencies and build the search index ────────────────────────────────────
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# Layer caching: requirements change rarely, so they get their own layer before any source code.
# The cache mount keeps pip's download cache between builds without putting it in the image.
COPY requirements-serve.txt .
# Upgrade the packaging tools first: the versions bundled with the base image had HIGH CVEs (Trivy scan v1).
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade "pip>=25" "setuptools>=84" "wheel>=0.46.2" \
 && pip install -r requirements-serve.txt

# Rebuild the Chroma index from the saved embeddings in .cache/ (no API key, no API calls).
# If the chunks didn't match the saved vectors, rag.py would try to call the gateway and the
# build would fail on the missing key, which is the behaviour we want.
COPY rag.py defenses.py ./
COPY data/corpus/docs.jsonl data/corpus/docs.jsonl
COPY .cache/m2_destiny_sentence_800.npz .cache/m2_destiny_sentence_800.npz
RUN RAG_CACHE_WRITES=0 python rag.py index

# ── Stage 2: the slim runtime image ─────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="archivist" \
      org.opencontainers.image.description="Destiny lore RAG service (DATA 790 cloud deployment)" \
      org.opencontainers.image.source="https://github.com/henryhal/data790-milestone1-rag"

# Patch OS packages, then run as an unprivileged user (uid 10001), never root.
# The base image's own pip/setuptools/wheel are never used (the app runs from /opt/venv) and carried
# HIGH CVEs, so they are removed: less code in the image, less to patch.
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/* \
 && python -m pip uninstall -y pip setuptools wheel \
 && useradd --create-home --uid 10001 --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
# pip and wheel are only needed to install packages, not to run the app
RUN /opt/venv/bin/python -m pip uninstall -y pip wheel
COPY --from=builder --chown=app:app /app/chroma_db ./chroma_db
COPY --chown=app:app rag.py defenses.py app.py ./
RUN mkdir -p .cache results && chown app:app .cache results

ARG APP_VERSION=dev
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    RAG_CACHE_WRITES=0 RAG_GUARD=layered \
    PROMETHEUS_DISABLE_CREATED_SERIES=True \
    ANONYMIZED_TELEMETRY=False \
    APP_VERSION=${APP_VERSION} \
    PORT=8080

USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8080'), timeout=4)"

# One uvicorn worker per container: scale by adding containers (Cloud Run / the HPA), not processes.
# PORT is set by Cloud Run; exec makes uvicorn PID 1 so it receives SIGTERM and shuts down cleanly.
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-graceful-shutdown 20"]
