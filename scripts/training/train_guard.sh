#!/usr/bin/env bash
# Training-loop guard: run the EWC training only if enough new data has arrived.
# Configuration comes from the environment; nothing is hard-coded.
set -e

# Configuration
MIN_NEW="${MIN_NEW:-3}"
MIN_TOKENS="${MIN_TOKENS:-5000}"
: "${TRACE_DB_PATH:?set TRACE_DB_PATH to the trace-proxy SQLite database}"
DB_PATH="$TRACE_DB_PATH"
STATE_FILE="${TRACE_STATE_FILE:-$HOME/.cache/memnc/train_guard_last_count}"
STATE_TOKENS="${TRACE_STATE_TOKENS:-$HOME/.cache/memnc/train_guard_last_tokens}"
LOG_FILE="${TRAIN_LOG_FILE:-$HOME/.cache/memnc/train_guard.log}"
LOCK_FILE="${TRAIN_LOCK_FILE:-$HOME/.cache/memnc/train_guard.lock}"
mkdir -p "$(dirname "$STATE_FILE")"

# Skip if training is already running
if pgrep -f "memnc.training.train_ewc" >/dev/null 2>&1 \
   || ([ -f "$LOCK_FILE" ] && kill -0 "$(cat "$LOCK_FILE" 2>/dev/null || echo 0) 2>/dev/null"); then
    echo "[$(date '+%F %T')] TRAINING ALREADY RUNNING -> Aborting." >> "$LOG_FILE"
    exit 0
fi

# Lock file management
LOCKPID="$$"
cleanup() {
    [ -f "$LOCK_FILE" ] && [ "$(cat "$LOCK_FILE" 2>/dev/null)" = "$LOCKPID" ] && rm -f "$LOCK_FILE"
}
trap cleanup EXIT
echo "$LOCKPID" > "$LOCK_FILE"

if [ ! -f "$DB_PATH" ]; then
    echo "[$(date '+%F %T')] ERROR: Database not found at $DB_PATH" >> "$LOG_FILE"
    exit 0
fi

# Count new samples and tokens
CURRENT=$(python3 -c "import sqlite3; c=sqlite3.connect('$DB_PATH'); print(c.execute(\"SELECT COUNT(*) FROM calls WHERE endpoint='/v1/chat/completions' AND status='success'\").fetchone()[0])" 2>/dev/null || echo 0)
CURRENT_TOKENS=$(python3 -c "import sqlite3; c=sqlite3.connect('$DB_PATH'); print(c.execute(\"SELECT COALESCE(SUM(total_tokens),0) FROM calls WHERE endpoint='/v1/chat/completions' AND status='success'\").fetchone()[0])" 2>/dev/null || echo 0)

PREV=0; [ -f "$STATE_FILE" ] && PREV=$(cat "$STATE_FILE")
NEW=$(( CURRENT - PREV ))

PREV_TOKENS=0; [ -f "$STATE_TOKENS" ] && PREV_TOKENS=$(cat "$STATE_TOKENS")
NEW_TOKENS=$(( CURRENT_TOKENS - PREV_TOKENS ))

echo "[$(date '+%F %T')] Check: $NEW new samples, $NEW_TOKENS new tokens (Min: $MIN_NEW, $MIN_TOKENS)" >> "$LOG_FILE"

# The training command is provided by the caller (GPU/venv setup is
# environment-specific). Example:
#   TRAIN_CMD="python -m memnc.training.train_ewc --data day.jsonl ..."
: "${TRAIN_CMD:?set TRAIN_CMD to the full training command to run}"

if [ "$NEW" -ge "$MIN_NEW" ] && [ "$NEW_TOKENS" -ge "$MIN_TOKENS" ]; then
    echo "[$(date '+%F %T')] DATA THRESHOLD MET -> Starting EWC training ..." >> "$LOG_FILE"
    if eval "$TRAIN_CMD" 2>&1 | tail -30 >> "$LOG_FILE"; then
        echo "$CURRENT" > "$STATE_FILE"
        echo "$CURRENT_TOKENS" > "$STATE_TOKENS"
        echo "[$(date '+%F %T')] SUCCESS: State updated ($CURRENT samples, $CURRENT_TOKENS tokens)." >> "$LOG_FILE"
        # Optional post-step (e.g. LoRA -> Ollama hot-swap)
        if [ -n "${TRAIN_POST_CMD:-}" ]; then
            eval "$TRAIN_POST_CMD" 2>&1 | tail -10 >> "$LOG_FILE" || true
        fi
    else
        echo "[$(date '+%F %T')] ERROR: Training failed. State not updated." >> "$LOG_FILE"
    fi
else
    echo "[$(date '+%F %T')] THRESHOLD NOT MET -> Skipping." >> "$LOG_FILE"
fi
