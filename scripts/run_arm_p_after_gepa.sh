#!/bin/bash
# Wait for the running GEPA lane2 process to exit, then run the arm P full-run
# measurement (roadmap 15.3 item 4, "Still open") on the same llama-server lane.
set +m
GEPA_PID=$1
if [ -z "$GEPA_PID" ]; then
  echo "usage: run_arm_p_after_gepa.sh <gepa_pid>" >&2
  exit 2
fi
while kill -0 "$GEPA_PID" 2>/dev/null; do
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
exec .venv/bin/python -u -m memnc.bench.runner --arms P --reps 5 --tiers hard \
  --out workspace/bench-p-hard.jsonl --report workspace/bench-p-hard.md
