#!/bin/bash
# Wait for the running arm-P bench process to exit, then run the GEPA gen1
# coder-prompt validation (roadmap item-4 next step) on the same lane.
set +m
ARM_P_PID=$1
if [ -z "$ARM_P_PID" ]; then
  echo "usage: run_gepa_validate_after_arm_p.sh <arm_p_pid>" >&2
  exit 2
fi
while kill -0 "$ARM_P_PID" 2>/dev/null; do
  sleep 120
done
cd "$(cd "$(dirname "$0")/.." && pwd)" || exit 1
export INFERENCE_PROVIDER=openai
export INFERENCE_BASE_URL=http://127.0.0.1:8098/v1
export INFERENCE_TIMEOUT_S=7200
export MODEL_PLANNER=gemma4-31b
export MODEL_CODER=gemma4-31b
export MODEL_CRITIC=gemma4-31b
unset NATS_TOKEN
exec .venv/bin/python -u /tmp/gepa-validate-launch.py
