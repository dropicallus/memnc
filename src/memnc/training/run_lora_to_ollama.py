"""Run the LoRA -> Ollama hot-swap wrapper (scripts/training/lora_to_ollama.sh).

Thin subprocess wrapper: locates the shell script, sets the AMD GPU override
if missing, and passes the adapter directory through. All converter/model
paths are configured via MEMNC_LORA_* environment variables (see the script).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys


def main():
    ap = argparse.ArgumentParser(description="Hot-swap a trained LoRA adapter into Ollama")
    ap.add_argument("--adapter", required=True, help="Directory with the trained LoRA adapter")
    args = ap.parse_args()

    env = os.environ.copy()
    if "HSA_OVERRIDE_GFX_VERSION" not in env:
        env["HSA_OVERRIDE_GFX_VERSION"] = "11.0.0"

    # Resolve the wrapper script: MEMNC_LORA_SCRIPT wins, else look for the
    # checked-in scripts/training/lora_to_ollama.sh relative to this package
    # (works from a source checkout; in a wheel install set the env var).
    script_path = env.get("MEMNC_LORA_SCRIPT")
    if not script_path:
        cand = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))))),
            "scripts", "training", "lora_to_ollama.sh",
        )
        script_path = cand
    if not os.path.exists(script_path):
        print(f"ERROR: wrapper script not found at {script_path} "
              "(set MEMNC_LORA_SCRIPT)", file=sys.stderr)
        sys.exit(1)

    print(f"Running LoRA -> Ollama hot-swap for adapter: {args.adapter}")
    try:
        result = subprocess.run(
            ["bash", script_path, args.adapter],
            env=env, capture_output=True, text=True, check=True,
        )
        print(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"Error during conversion: {e.stderr}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
