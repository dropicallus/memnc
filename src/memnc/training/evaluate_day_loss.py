"""Evaluate a LoRA adapter (memory dir) on given JSONL datasets.

Measures the mean cross-entropy loss per split -- the measure for:
  * Day-1 knowledge (loss on tag1.jsonl) -- should stay stable after
    EWC-supported day-2 training
  * Day-2 knowledge (loss on tag2.jsonl)

Usage (requires the GPU override on AMD):
  HSA_OVERRIDE_GFX_VERSION=11.0.0 python -m memnc.training.evaluate_day_loss \
     --model Qwen/Qwen3-4B --memory ./memory_twoday \
     --splits ./splits/tag1.jsonl ./splits/tag2.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import os

import torch
from unsloth.kernels.cross_entropy_loss import fast_cross_entropy_loss

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("eval")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen3-4B")
    ap.add_argument("--memory", default="memory_twoday",
                    help="memory dir containing adapter_model.safetensors")
    ap.add_argument("--splits", nargs="+", required=True,
                    help="one or more JSONL eval splits")
    ap.add_argument("--max-seq", type=int, default=512)
    a = ap.parse_args()

    from unsloth import FastLanguageModel

    # Load base + LoRA structure (as in training), then the adapter.
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=a.model, max_seq_length=a.max_seq, dtype=torch.bfloat16,
        load_in_4bit=True,
    )
    model = FastLanguageModel.get_peft_model(
        model, r=8, lora_alpha=16, lora_dropout=0,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj",
                        "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth", random_state=3407, max_seq_length=a.max_seq,
    )

    ad = os.path.join(a.memory, "adapter_model.safetensors")
    import safetensors.torch as st
    sd = st.load_file(ad) if os.path.exists(ad) else {}
    cur = dict(model.state_dict())

    def norm(k):
        return k.replace(".default.", ".").split("base_model.model.")[-1]

    cur_norm = {norm(k): k for k in cur}
    load_sd = {}
    for k, v in sd.items():
        nk = norm(k)
        if nk in cur_norm:
            load_sd[cur_norm[nk]] = v
    miss, _ = model.load_state_dict(load_sd, strict=False)
    log.info(f"  Eval adapter loaded: {len(load_sd)} tensors (missing={len(miss)})")
    model.eval()

    def loss_on(path):
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        texts = []
        for r in rows:
            clean = []
            for m in r.get("messages", []):
                if not isinstance(m, dict):
                    continue
                m = dict(m)
                if not m.get("content"):
                    m["content"] = ""
                clean.append(m)
            try:
                texts.append(tokenizer.apply_chat_template(clean, tokenize=False,
                                                           add_generation_prompt=False))
            except Exception:
                texts.append(" ".join(m.get("content", "") for m in clean))
        enc = tokenizer(texts, return_tensors="pt", padding=True, truncation=True,
                        max_length=a.max_seq)
        ids, am = enc["input_ids"].cuda(), enc["attention_mask"].cuda()
        with torch.no_grad(), torch.cuda.amp.autocast(dtype=torch.bfloat16):
            out = model(input_ids=ids, attention_mask=am)
        logits = out.logits.float()
        return fast_cross_entropy_loss(logits, ids).item()

    log.info("=== Loss per split (lower = better knowledge) ===")
    for sp in a.splits:
        try:
            l = loss_on(sp)
            log.info(f"  {os.path.basename(sp):14s}: loss={l:.4f}")
        except Exception as e:
            log.warning(f"  {os.path.basename(sp)}: error {e}")


if __name__ == "__main__":
    main()
