#!/usr/bin/env bash
# S1 Smoke-Gate: end-to-end smoke test against a running mesh.
#
#   ./scripts/smoke.sh ["task description"] [--claimable] [--fanout]
#                      [--node NAME] [--timeout SECONDS] [--tail]
#
# Submits a task, then POLLS `meshctl state` until a terminal state
# (DONE/FAILED/CANCELLED) or timeout. Asserts the final state is DONE.
# Exit codes: 0 = DONE, 1 = FAILED/CANCELLED, 2 = timeout, 3 = submit error.
#
# Backend-agnostic: the worker decides Ollama vs OpenAI (INFERENCE_PROVIDER).
# Requires: meshctl on PATH (or MESHCTL), NATS_URL/NATS_TOKEN for the mesh.

set -euo pipefail

MESHCTL="${MESHCTL:-meshctl}"
TASK=""
CLAIMABLE=0
FANOUT=0
NODE=""
TIMEOUT="${SMOKE_TIMEOUT:-600}"
POLL_INTERVAL="${SMOKE_POLL_INTERVAL:-5}"
TAIL=0

usage() { sed -n '2,10p' "$0"; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --claimable) CLAIMABLE=1 ;;
        --fanout)    FANOUT=1 ;;
        --node)      NODE="$2"; shift ;;
        --timeout)   TIMEOUT="$2"; shift ;;
        --tail)      TAIL=1 ;;
        -h|--help)   usage ;;
        -*)          echo "unknown option: $1" >&2; usage ;;
        *)           if [ -z "$TASK" ]; then TASK="$1"; else echo "only one task argument" >&2; exit 2; fi ;;
    esac
    shift
done
TASK="${TASK:-Create hello.py in the workspace that prints 'hello from the mesh'}"

# --- submit ----------------------------------------------------------------
ARGS=(submit "$TASK")
[ "$CLAIMABLE" -eq 1 ] && ARGS+=(--claimable)
[ "$FANOUT" -eq 1 ] && ARGS+=(--fanout)
[ -n "$NODE" ] && ARGS+=(--node "$NODE")

echo "smoke: submitting: ${TASK}"
[ "$CLAIMABLE" -eq 1 ] && echo "smoke: mode=claimable node=${NODE:-local}"
[ "$FANOUT" -eq 1 ] && echo "smoke: mode=fanout"

if ! TASK_ID="$("${MESHCTL}" "${ARGS[@]}")"; then
    echo "smoke: FAIL - submit failed" >&2
    exit 3
fi
TASK_ID="$(echo "$TASK_ID" | tail -n1)"
echo "smoke: task_id=${TASK_ID}"

# --- optional live tail in parallel ----------------------------------------
TAIL_LOG=""
TAIL_PID=""
if [ "$TAIL" -eq 1 ]; then
    TAIL_LOG="$(mktemp)"
    "${MESHCTL}" tail "${TASK_ID}" --limit 200 >"$TAIL_LOG" 2>&1 &
    TAIL_PID=$!
fi
cleanup() { if [ -n "$TAIL_PID" ]; then kill "$TAIL_PID" 2>/dev/null || true; fi; }
trap cleanup EXIT

# --- poll state until terminal or timeout -----------------------------------
deadline=$(( $(date +%s) + TIMEOUT ))
echo "smoke: polling state (timeout ${TIMEOUT}s, interval ${SMOKE_POLL_INTERVAL:-5}s)..."
while :; do
    SNAPSHOT="$("${MESHCTL}" state "${TASK_ID}" 2>/dev/null || true)"
    STATE="$(printf '%s' "$SNAPSHOT" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("state", ""))
except Exception: print("")' 2>/dev/null || true)"
    if [ -n "$STATE" ]; then
        echo "smoke: state=${STATE}"
    else
        echo "smoke: state=<unavailable, retrying>"
    fi
    case "$STATE" in
        DONE)
            echo "smoke: PASS - task DONE"
            if [ -n "$TAIL_LOG" ]; then echo "--- event tail ---"; cat "$TAIL_LOG"; fi
            printf '%s\n' "$SNAPSHOT"
            exit 0
            ;;
        FAILED|CANCELLED)
            echo "smoke: FAIL - task ${STATE}" >&2
            printf '%s\n' "$SNAPSHOT" >&2
            exit 1
            ;;
    esac
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "smoke: FAIL - timeout after ${TIMEOUT}s (last state: ${STATE:-unknown})" >&2
        printf '%s\n' "$SNAPSHOT" >&2
        exit 2
    fi
    sleep "${SMOKE_POLL_INTERVAL:-5}"
done
