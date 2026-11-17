#!/bin/sh
# Wait for the arm-D retry lane (rerun_failed_d.py) to finish, then run the
# GEPA live evolution on the merged arm-D results.
while pgrep -f 'scripts/rerun_failed_d.py' > /dev/null 2>&1; do
    sleep 60
done
cd "$(dirname "$0")/.." || exit 1
PYTHONPATH=
export PYTHONPATH
INFERENCE_BASE_URL=http://127.0.0.1:11434
export INFERENCE_BASE_URL
exec .venv/bin/python -m memnc.bench.gepa_run \
    --arm D --role coder --tier hard \
    --jsonl workspace/bench-d-hard-merged.jsonl \
    --out workspace/gepa-d-coder-frontier.jsonl \
    --generations 1 > workspace/gepa-d-live.log 2>&1
