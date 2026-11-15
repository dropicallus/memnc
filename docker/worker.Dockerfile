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

# 15.4.1: ship the opencode coding agent binary so --harness opencode can
# spawn inside the worker. Installer drops into $HOME/.opencode/bin; HOME is
# redirected so the fixed path survives the ${UID:-1000} runtime user
# (rootfs is read_only, /tmp is tmpfs — nothing may live under $HOME then).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && HOME=/usr/local sh -c 'curl -fsSL https://opencode.ai/install | bash' \
    && ln -s /usr/local/.opencode/bin/opencode /usr/local/bin/opencode \
    && chmod -R a+rX /usr/local/.opencode

USER 10001:10001

ENV WORKSPACE_ROOT=/workspace

CMD ["python", "-m", "memnc.worker"]
