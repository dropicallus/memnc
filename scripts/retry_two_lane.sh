#!/bin/bash
# Two-lane bench-D retry: lane 1 = ollama :11434 (card1), lane 2 = llauncher/llama-server :8098 (openai backend).
cd /home/frank/memnc
unset NATS_TOKEN
L1_PID=0
(
  export INFERENCE_BASE_URL=http://127.0.0.1:11434
  .venv/bin/python scripts/rerun_failed_d.py > workspace/bench-d-hard-retry-lane1.log 2>&1
) &
L1_PID=$!
(
  export INFERENCE_PROVIDER=openai
  export INFERENCE_BASE_URL=http://127.0.0.1:8098/v1
  export MODEL_PLANNER=gemma4-31b
  export MODEL_CODER=gemma4-31b
  export MODEL_CRITIC=gemma4-31b
  .venv/bin/python scripts/rerun_failed_d_lane2.py > workspace/bench-d-hard-retry-lane2.log 2>&1
) &
L2_PID=$!
wait $L1_PID; echo "lane1 exit $?"
wait $L2_PID; echo "lane2 exit $?"
wc -l workspace/bench-d-hard-retry.jsonl workspace/bench-d-hard-retry-lane2.jsonl 2>/dev/null
