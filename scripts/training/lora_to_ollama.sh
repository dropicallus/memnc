#!/usr/bin/env bash
# LoRA -> Ollama in-place hot-swap of the memory adapter.
# All tool/model paths come from the environment; nothing is hard-coded.
set -euo pipefail

# Required configuration (no defaults: the caller knows its setup)
: "${MEMNC_LORA_PY:?set MEMNC_LORA_PY to the python that has the converter deps installed}"
: "${MEMNC_LORA_CONVERT:?set MEMNC_LORA_CONVERT to the convert_lora_to_gguf.py script}"
: "${MEMNC_LORA_BASE:?set MEMNC_LORA_BASE to the base model directory (HF snapshot)}"
: "${MEMNC_LORA_TARGET_MODEL:?set MEMNC_LORA_TARGET_MODEL to the Ollama model name}"
: "${MEMNC_LORA_DOCKER:?set MEMNC_LORA_DOCKER to the Ollama docker container name}"

TARGET_MODEL="$MEMNC_LORA_TARGET_MODEL"
PY="$MEMNC_LORA_PY"
CONV="$MEMNC_LORA_CONVERT"
HF_BASE="$MEMNC_LORA_BASE"
DOCKER="$MEMNC_LORA_DOCKER"

# Arguments
ADAPTER="${1:?usage: lora_to_ollama.sh <adapter_dir>}"
ADAPTER_GGUF="$ADAPTER-lora.gguf"

echo "=== LoRA IN-PLACE -> Ollama ($TARGET_MODEL) ==="
if [ ! -d "$ADAPTER" ] || [ ! -f "$ADAPTER/adapter_model.safetensors" ]; then
  echo "ERROR: No trained memory adapter found at $ADAPTER"; exit 1
fi

# Convert LoRA to GGUF
echo "[2/3] Converting PEFT-LoRA -> GGUF adapter..."
"$PY" "$CONV" \
  --base "$HF_BASE" \
  --outtype f16 \
  --outfile "$ADAPTER_GGUF" \
  "$ADAPTER"

# Copy to the Ollama container and hot-swap the adapter in place
echo "[3/3] Hot-swapping adapter into $TARGET_MODEL ..."
docker exec "$DOCKER" mkdir -p /root/lora_load || true
docker cp "$ADAPTER_GGUF" "$DOCKER:/root/lora_load/memory.gguf"
MF=$(mktemp)
printf 'FROM %s\nADAPTER /root/lora_load/memory.gguf\n' "$TARGET_MODEL" > "$MF"
docker cp "$MF" "$DOCKER:/root/lora_load/inplace.Modelfile"
docker exec "$DOCKER" ollama create "$TARGET_MODEL" -f /root/lora_load/inplace.Modelfile
rm "$MF"

echo "=== Done: $TARGET_MODEL updated ==="
