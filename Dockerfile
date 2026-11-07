# api + worker image (same code, different command)
FROM ghcr.io/astral-sh/uv:0.12.9 AS uv
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
# Embedding model baked in at build time (ADR-010/038): the running containers never download
# it. The only network access is this build step (Hugging Face, or Qdrant's mirror).
ENV FASTEMBED_CACHE_PATH=/opt/fastembed
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/opt/fastembed')" \
    && chmod -R a+rX /opt/fastembed
COPY agentops ./agentops
COPY mcp_servers ./mcp_servers
COPY config ./config
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

RUN useradd --uid 10000 --create-home --shell /usr/sbin/nologin agentops \
    && mkdir -p /var/lib/agentops/mcp /var/lib/agentops/notes /workspaces \
    && chown -R agentops:agentops /var/lib/agentops /workspaces
# Runtime is offline for models: fail loudly rather than download.
ENV HF_HUB_OFFLINE=1
USER agentops
EXPOSE 8000
CMD ["uvicorn", "agentops.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
