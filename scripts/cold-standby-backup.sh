#!/usr/bin/env bash
# Cold-standby backup (Duo topology): dump durable KV buckets and prune
# old dumps. Run from cron on the active NATS host. See
# docs/operations.md "Cold-standby backup".
set -euo pipefail

# Since the NKey migration the server rejects legacy tokens ("Authorization
# Violation") — authentication uses the operator NKey seed from secrets/.
# Pick NATS_URL from the repo .env when the shell does not have it (cron
# shells do not inherit the docker-compose environment); .env contains
# unquoted values (model names with spaces), so never source it.
_pick_env_var() {
  # $1=env file  $2=variable name; prints the value or nothing
  awk -F= -v k="$2" '$1 == k {sub(/^[^=]*=/,""); gsub(/^"|"$/,""); print; exit}' "$1" 2>/dev/null
}
unset NATS_TOKEN
# meshctl lives at <repo>/.venv/bin/meshctl — climb three levels
REPO="$(cd "$(dirname "${MESHCTL:-$HOME/memnc/.venv/bin/meshctl}")/../.." && pwd)"
if [ -z "${NATS_NKEYS_SEED:-}" ] && [ -f "$REPO/secrets/operator.nk" ]; then
  export NATS_NKEYS_SEED="$REPO/secrets/operator.nk"
fi

MESHCTL="${MESHCTL:-$HOME/memnc/.venv/bin/meshctl}"
NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/.local/share/memnc-backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%F-%H%M)"
OUT="$BACKUP_DIR/memnc-$STAMP.json"

cd "$(dirname "$MESHCTL")/.."
env -u PYTHONPATH NATS_URL="$NATS_URL" "$MESHCTL" backup --out "$OUT"

# prune dumps older than KEEP_DAYS
find "$BACKUP_DIR" -name 'memnc-*.json' -mtime "+$KEEP_DAYS" -delete

echo "backup: $OUT ($(du -h "$OUT" | cut -f1))"
