# Incident Triage Engine — API + dashboard image.
# Build:  docker build -t incident-triage-engine .
# Run:    docker compose up            (see docker-compose.yml)

FROM python:3.12-slim AS runtime

# Python writes line-buffered output and skips .pyc files, so container logs appear
# immediately instead of only when the buffer fills.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ChromaDB's default embedder downloads its ONNX model to ~/.cache/chroma the first
# time a query runs. Warming it in a build stage means a container start never waits
# on that download, and the final image carries no pip or build tooling.
FROM python:3.12-slim AS warm-cache
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install -r requirements.txt
RUN python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction as E; E()(['warmup'])"

FROM runtime

# Dependencies first: this layer is reused whenever only source changes.
COPY requirements.txt .
RUN python -m pip install -r requirements.txt

# Pre-warmed ONNX embedding model, so retrieval works with no outbound request.
COPY --from=warm-cache /root/.cache/chroma /root/.cache/chroma

# Application source, runbooks (the retrieval corpus) and the ingest script.
COPY src ./src
COPY scripts ./scripts
COPY docs/runbooks ./docs/runbooks

# Migrations ship with the image so the container can bring the reports table up to
# date at start; alembic.ini keeps no URL, env.py reads DATABASE_URL like the app does.
COPY alembic.ini .
COPY alembic ./alembic

# The embedder caches under the *runtime user's* home, so warm it as that user.
COPY --from=warm-cache /root/.cache/chroma /home/app/.cache/chroma

# Run as a non-root user; it owns the app tree and the ChromaDB volume.
RUN useradd --create-home --shell /bin/bash app && chown -R app:app /app /home/app
USER app

# Docker creates a named volume as root-owned, so the non-root app user cannot write
# to it on first start; chown runs as root before dropping privileges.
USER root
RUN mkdir -p /app/.chroma_db && chown -R app:app /app/.chroma_db
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4)"

# Migrations run before the service starts, and only when a database is configured:
# report storage is opt-in, so a stack without DATABASE_URL still comes up offline.
# A failed migration stops the container rather than serving reports from a mismatched
# schema. The served app enables runbook retrieval, so the index must exist before
# requests arrive; otherwise every triage returns zero matches. Upsert is idempotent,
# so this re-ingests on each start and picks up runbooks mounted from the volume.
CMD ["sh", "-c", "if [ -n \"$DATABASE_URL\" ]; then python -m alembic upgrade head || exit 1; fi && python scripts/ingest_runbooks.py && python -m uvicorn src.main:app --host 0.0.0.0 --port 8000"]
