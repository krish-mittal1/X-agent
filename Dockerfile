FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EMBEDDING_DEVICE=cpu \
    HF_HOME=/opt/huggingface

WORKDIR /app

# Install a Chromium browser for headless Brave publishing. CPU-only torch
# keeps the image reproducible and avoids pulling CUDA into a CPU deployment.
COPY requirements.txt requirements-embeddings.txt ./
RUN pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r requirements.txt -r requirements-embeddings.txt && \
    apt-get update && apt-get install -y --no-install-recommends \
        chromium ca-certificates fonts-liberation ffmpeg && \
    rm -rf /var/lib/apt/lists/* # noqa: ERA001

# Bake the default embedding model into the image so startup needs no download.
ARG EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p "$HF_HOME" /app/.temp_media \
    && python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}')" \
    && chown -R appuser:appuser "$HF_HOME" /app

# The model above is intentionally baked into the image.  Runtime workers must
# use that revision and must not stall the newsroom cycle on Hub metadata calls.
ENV HF_HUB_OFFLINE=1

COPY alembic.ini pyproject.toml ./
COPY migrations ./migrations
COPY app ./app
COPY scripts ./scripts

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
