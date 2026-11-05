#!/usr/bin/env bash
# T0 E2E automation: full self-organization live test as a single command.
#
#   ./scripts/e2e.sh [--selforg] [--fresh] [--expect "text"]
#                    [--artifact PATH] [--timeout SECONDS] [--stuck-minutes N]
#                    ["task description"]
#
# What it does beyond smoke.sh (S1):
#   - stack orchestration: starts the orchestrator for the submit, then STOPS it
#     in --selforg mode so the task must be claimed and driven by workers (M14);
#   - stuck detection: if the task state does not change for --stuck-minutes,
#     FAIL immediately instead of polling until the timeout;
#   - artifact verification: after DONE, actually executes the produced script
#     in the workspace and checks its output against --expect (substring);
#   - one-line PASS/FAIL verdict with the critic's summary as evidence.
#
# Exit codes: 0 = PASS, 1 = task FAILED/CANCELLED, 2 = timeout,
#             3 = submit error, 4 = stuck (no state change), 5 = artifact check failed.
#
# Requires: meshctl on PATH (or MESHCTL), docker compose, NATS_URL/NATS_TOKEN.
# Backend-agnostic: workers decide Ollama vs OpenAI via INFERENCE_PROVIDER.

set -euo pipefail

cd "$(dirname "$0")/.."

MESHCTL="${MESHCTL:-meshctl}"
TASK=""
SELFORG=0
FRESH=0
EXPECT=""
ARTIFACT=""
TIMEOUT="${E2E_TIMEOUT:-1800}"
STUCK_MINUTES="${E2E_STUCK_MINUTES:-10}"
POLL_INTERVAL="${SMOKE_POLL_INTERVAL:-5}"

usage() { sed -n '2,17p' "$0"; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --selforg)       SELFORG=1 ;;
        --fresh)         FRESH=1 ;;
        --expect)        EXPECT="$2"; shift ;;
        --artifact)      ARTIFACT="$2"; shift ;;
        --timeout)       TIMEOUT="$2"; shift ;;
        --stuck-minutes) STUCK_MINUTES="$2"; shift ;;
        -h|--help)       usage ;;
        -*)              echo "e2e: unknown option: $1" >&2; usage ;;
        *)               if [ -z "$TASK" ]; then TASK="$1"; else echo "only one task argument" >&2; exit 2; fi ;;
    esac
    shift
done
TASK="${TASK:-Create hello.py in the workspace that prints 'hello from the mesh'}"

fail() { echo "e2e: FAIL - $1" >&2; exit "${2:-1}"; }

# --- preflight ---------------------------------------------------------------
# meshctl runs on the HOST: it would pick NATS_URL=nats://nats:4222 from .env
# (the in-compose hostname) and fail DNS. Do NOT export it here - docker compose
# would substitute ${NATS_URL} into the containers. Pass it per-call via mesh().
mesh() { NATS_URL="${E2E_NATS_URL:-nats://localhost:4222}" "$MESHCTL" "$@"; }
command -v "$MESHCTL" >/dev/null 2>&1 || fail "meshctl not on PATH (set MESHCTL=...)" 3
command -v docker >/dev/null 2>&1 || fail "docker not found" 3
docker compose ps >/dev/null 2>&1 || fail "docker compose stack not reachable" 3

# --- optional workspace reset (destructive) -----------------------------------
if [ "$FRESH" -eq 1 ]; then
    echo "e2e: resetting workspace/"
    find workspace -mindepth 1 -maxdepth 1 -exec rm -rf {} +
fi

# --- stack orchestration ------------------------------------------------------
echo "e2e: ensuring stack up (nats + workers)"
docker compose up -d --build nats worker-planner worker-coder worker-critic >/dev/null
docker compose ps >/dev/null

if [ "$SELFORG" -eq 1 ]; then
    echo "e2e: selforg mode - starting orchestrator only for the submit"
    docker compose up -d orchestrator >/dev/null
    sleep 2
fi

# --- submit -------------------------------------------------------------------
echo "e2e: submitting: ${TASK}"
SUBMIT_ARGS=(submit "$TASK")
[ "$SELFORG" -eq 1 ] && SUBMIT_ARGS+=(--claimable)
if ! TASK_ID="$(mesh "${SUBMIT_ARGS[@]}")"; then
    fail "submit failed" 3
fi
TASK_ID="$(echo "$TASK_ID" | tail -n1)"
echo "e2e: task_id=${TASK_ID}"

if [ "$SELFORG" -eq 1 ]; then
    echo "e2e: stopping orchestrator - workers must self-organize now"
    docker compose stop orchestrator >/dev/null
fi

# --- poll with stuck detection -------------------------------------------------
deadline=$(( $(date +%s) + TIMEOUT ))
stuck_seconds=$(( STUCK_MINUTES * 60 ))
last_state=""
last_change=$(date +%s)

snapshot_field() { # $1 = json, $2 = python expr on parsed dict
    printf '%s' "$1" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    print($2)
except Exception:
    print('')
" 2>/dev/null || true
}

while :; do
    SNAPSHOT="$(mesh state "$TASK_ID" 2>/dev/null || true)"
    STATE="$(snapshot_field "$SNAPSHOT" "d.get('state','')")"

    if [ -n "$STATE" ]; then
        if [ "$STATE" != "$last_state" ]; then
            echo "e2e: state=${STATE} claimed_by=$(snapshot_field "$SNAPSHOT" "d.get('claimed_by','')")"
            last_state="$STATE"
            last_change=$(date +%s)
        fi
    else
        echo "e2e: state=<unavailable, retrying>"
    fi

    case "$STATE" in
        DONE) break ;;
        FAILED|CANCELLED)
            echo "--- critic verdict ---"
            snapshot_field "$SNAPSHOT" "(d.get('critique') or {}).get('summary','')"
            fail "task ${STATE}" 1
            ;;
    esac

    now=$(date +%s)
    if [ $((now - last_change)) -ge "$stuck_seconds" ]; then
        echo "--- last snapshot ---"; printf '%s\n' "$SNAPSHOT" >&2
        fail "stuck: state=${STATE:-unknown} unchanged for ${STUCK_MINUTES} min" 4
    fi
    if [ "$now" -ge "$deadline" ]; then
        fail "timeout after ${TIMEOUT}s (last state: ${STATE:-unknown})" 2
    fi
    sleep "$POLL_INTERVAL"
done

echo "e2e: task DONE"
VERDICT="$(snapshot_field "$SNAPSHOT" "(d.get('critique') or {}).get('summary','')")"

# --- artifact verification -----------------------------------------------------
# Pick the artifact: explicit --artifact, else the newest .py in workspace/.
CANDIDATE="${ARTIFACT}"
if [ -z "$CANDIDATE" ]; then
    CANDIDATE="$(find workspace -name '*.py' -type f -printf '%T@ %p\n' 2>/dev/null \
        | sort -rn | head -1 | cut -d' ' -f2- || true)"
fi

if [ -z "$CANDIDATE" ] || [ ! -f "$CANDIDATE" ]; then
    echo "e2e: PASS (state only) - no artifact found to execute; critic verdict: ${VERDICT}"
    exit 0
fi

echo "e2e: verifying artifact: ${CANDIDATE}"
python3 -m py_compile "$CANDIDATE" || fail "artifact does not compile: ${CANDIDATE}" 5

OUTPUT="$(python3 "$CANDIDATE" 2>&1)" || fail "artifact crashed: ${OUTPUT}" 5
echo "e2e: artifact output: ${OUTPUT}"

if [ -n "$EXPECT" ]; then
    printf '%s' "$OUTPUT" | grep -qF "$EXPECT" \
        || fail "artifact output does not contain expected text: '${EXPECT}'" 5
fi

echo "e2e: PASS - task DONE, artifact verified (output check: ${EXPECT:-executed only})"
echo "e2e: critic verdict: ${VERDICT}"
