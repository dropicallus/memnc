#!/usr/bin/env bash
# End-to-end smoke test against a running mesh.
#
#   ./scripts/smoke.sh ["task description"]
#
# Requires: meshctl on PATH (or set MESHCTL), NATS_URL/NATS_TOKEN for the mesh.

set -euo pipefail

MESHCTL="${MESHCTL:-meshctl}"
TASK="${1:-Create hello.py in the workspace that prints 'hello from the mesh'}"

echo "submitting: ${TASK}"
TASK_ID="$("${MESHCTL}" submit "${TASK}")"
echo "task id: ${TASK_ID}"

echo "tailing events (limit 60)..."
"${MESHCTL}" tail "${TASK_ID}" --limit 60 || true

echo
echo "final state:"
"${MESHCTL}" state "${TASK_ID}"
