# agentops-sandbox: runs untrusted generated code (ADR-011). Built, never run as a service.
# Containers are started per execution with network_mode=none, read-only root, cap_drop=ALL,
# no-new-privileges, tmpfs /tmp, memory/CPU/PID limits (Phase 3, tools/sandbox.py).
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# Common data libraries preinstalled so generated code never needs network access.
RUN pip install --no-cache-dir numpy==2.5.4 pandas==3.1.1 \
    && rm -rf /root/.cache \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sandbox \
    && mkdir /work && chown 10001:10001 /work
USER 10001:10001
WORKDIR /work
CMD ["python", "--version"]
