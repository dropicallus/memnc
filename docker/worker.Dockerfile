FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && groupadd -g 10001 mesh \
    && useradd -u 10001 -g 10001 -s /usr/sbin/nologin mesh \
    && mkdir -p /workspace \
    && chown -R mesh:mesh /workspace

USER 10001:10001

ENV WORKSPACE_ROOT=/workspace

CMD ["python", "-m", "memnc.worker"]
