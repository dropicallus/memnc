#!/usr/bin/env bash
# Backlog driver daemon launcher (ExecStart for the systemd user unit).
# Loop: claim one backlog entry -> submit claimable to the mesh -> watch
# the JetStream task events -> mark done/failed; sleeps when idle.
#
# Rules encoded here (see docs/roadmap.md 14.15):
# - never `source` .env (unquoted values with spaces abort the shell);
#   extract single vars with the plain-match awk pattern instead.
# - host-side clients need a loopback URL; the .env NATS_URL carries the
#   compose-internal hostname (`nats`), which does not resolve on the host.
# - never export an empty NATS_TOKEN (Authorization Violation looks like
#   wrong credentials); only export when non-empty.
# - strip PYTHONPATH: a leaked value from another env shadows the venv's
#   site-packages and produces misleading import errors.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
MESHCTL="$REPO/.venv/bin/meshctl"

nats_token="$(awk -F= -v k=NATS_TOKEN '$1 == k {sub(/^[^=]*=/,""); gsub(/^"|"$/,""); print; exit}' "$REPO/.env")"
export NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"
if [ -n "$nats_token" ]; then
  export NATS_TOKEN="$nats_token"
fi

exec env -u PYTHONPATH "$MESHCTL" backlog-run
