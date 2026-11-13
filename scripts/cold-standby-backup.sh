#!/usr/bin/env bash
# Cold-standby backup (Duo topology): dump durable KV buckets and prune
# old dumps. Run from cron on the active NATS host. See
# docs/operations.md "Cold-standby backup".
set -euo pipefail

# load NATS_TOKEN / NATS_URL from the repo .env when the shell does not have
# them (cron shells do not inherit the docker-compose environment)
# .env contains unquoted values (e.g. model names with spaces), so do not
# source it — pick out just the two NATS variables.
_pick_env_var() {
  # $1=env file  $2=variable name; prints the value or nothing
  awk -F= -v k="$2" '$1 == k {sub(/^[^=]*=/,""); gsub(/^"|"$/,""); print; exit}' "$1" 2>/dev/null
}
if [ -z "${NATS_TOKEN:-}" ]; then
  for _env in "$(dirname "${MESHCTL:-$HOME/memnc/.venv/bin/meshctl}")/../.env" "$HOME/memnc/.env"; do
    if [ -f "$_env" ]; then
      _tok="$(_pick_env_var "$_env" NATS_TOKEN)"
      if [ -n "$_tok" ]; then export NATS_TOKEN="$_tok"; fi
      break
    fi
  done
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
