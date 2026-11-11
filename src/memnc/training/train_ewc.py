"""EWC memory trainer (layer 2): continuous LoRA learning with
Elastic Weight Consolidation against catastrophic forgetting.

Idea ("memory through weights"):
  * New daily knowledge is trained into a LoRA adapter (Unsloth, GPU).
  * The FISHER information of past days protects old, important LoRA
    weights from being overwritten by the new day's knowledge.
  * Fisher is updated exponentially: F_new = l * F_old + (1 - l) * F_day.

Directory layout under MEMORY_DIR:
  adapter_model.safetensors          -> current LoRA adapter
  fisher.pt                          -> Fisher diagonal (running update)
  old_adapter/                       -> backup of the previous adapter

Usage (requires the GPU override on AMD):
  HSA_OVERRIDE_GFX_VERSION=11.0.0 python -m memnc.training.train_ewc \
     --data training_data.jsonl \
     --model Qwen/Qwen3-4B --steps 12 --ewc-lambda 1000 --memory-dir ./memory
"""

from __future__ import annotations

import argparse
import json
import logging
import os

import torch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ewc")


def sanitize_msgs(msgs):
    """Normalize a chat-message list for templating (string content only)."""
    out = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        m = dict(m)
        if not m.get("content"):
            m["content"] = ""
        if m.get("role") == "tool" and not isinstance(m.get("content"), str):
            m["content"] = json.dumps(m.get("content") or "", ensure_ascii=False)
        out.append(m)
    return out


def load_fisher(path):
    """Load a previous Fisher diagonal plus the matching old LoRA weights."""
    if not os.path.exists(path):
        return None, None
    d = torch.load(path, map_location="cpu", weights_only=False)
    return d.get("fisher"), d.get("theta_old")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen3-4B")
    ap.add_argument("--data", required=True)
    ap.add_argument("--steps", type=int, default=12)
    ap.add_argument("--rank", type=int, default=8)
    ap.add_argument("--alpha", type=int, default=16)
    ap.add_argument("--max-seq", type=int, default=512)
    ap.add_argument("--learning-rate", type=float, default=1e-4)
    # EWC regularization strength (0 = disabled).
    ap.add_argument("--ewc-lambda", type=float, default=1000.0)
    # Fisher update: l (old) vs (1 - l) (new).
    ap.add_argument("--fisher-decay", type=float, default=0.7)
    ap.add_argument("--memory-dir", default="./memory")
    args = ap.parse_args()

    from unsloth import FastLanguageModel
    from datasets import Dataset
    from trl import SFTTrainer, SFTConfig

    os.makedirs(args.memory_dir, exist_ok=True)
    fisher_path = os.path.join(args.memory_dir, "fisher.pt")
    adapter_path = os.path.join(args.memory_dir, "adapter_model.safetensors")

    # ---------- 1) Model + LoRA ----------
    log.info(f"[1/6] Loading {args.model} (4bit) ...")
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=args.model, max_seq_length=args.max_seq,
        dtype=torch.bfloat16, load_in_4bit=True,
    )
    model = FastLanguageModel.get_peft_model(
        model, r=args.rank, lora_alpha=args.alpha, lora_dropout=0,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj",
                        "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth", random_state=3407, max_seq_length=args.max_seq,
    )

    # ---------- 2) Load previous memory (for EWC + continuity) ----------
    old_F, old_theta = load_fisher(fisher_path)
    ewc_on = args.ewc_lambda > 0 and old_F is not None and old_theta is not None

    # Load an existing old adapter (LoRA continuity). The official PEFT way
    # is set_peft_model_state_dict (it handles the name mapping).
    if os.path.exists(adapter_path):
        from peft import set_peft_model_state_dict
        try:
            sd_loaded = torch.load(adapter_path, map_location="cpu", weights_only=False)
            if "state_dict" in sd_loaded:
                sd_loaded = sd_loaded["state_dict"]
            imported, _ = set_peft_model_state_dict(model, sd_loaded, adapter_name="default")
            log.info(f"  Loaded previous adapter: {len(imported)} tensors (continuity)")
        except Exception as e:
            log.warning(
                "  Adapter load via set_peft_model_state_dict failed: %s; "
                "falling back to raw state_dict keys.", e,
            )
            import safetensors.torch as st
            sd = st.load_file(adapter_path)
            cur = dict(model.state_dict())
            load_sd = {}

            def norm(k):
                return k.replace(".default.", ".").split("base_model.model.")[-1]

            cur_norm = {norm(k): k for k in cur}
            for k, v in sd.items():
                nk = norm(k)
                if nk in cur_norm:
                    load_sd[cur_norm[nk]] = v
            miss, _unexp = model.load_state_dict(load_sd, strict=False)
            log.info(f"  Fallback adapter loaded: {len(load_sd)} tensors (missing={len(miss)})")

    if ewc_on:
        log.info(f"  EWC ACTIVE: previous Fisher present ({len(old_F)} tensors). lambda={args.ewc_lambda}")
    else:
        log.info("  No previous memory/Fisher -> EWC disabled for now (first run).")

    # ---------- 3) Data ----------
    log.info("[3/6] Data ...")
    with open(args.data, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    texts = []
    for r in rows:
        msgs = sanitize_msgs(r.get("messages"))
        if not msgs:
            continue
        try:
            texts.append(tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=False))
        except Exception:
            texts.append(" ".join(m.get("content") or "" for m in msgs))
    log.info(f"  {len(texts)} samples")
    ds = Dataset.from_dict({"text": texts})

    # ---------- 4) EWC loss via custom compute_loss ----------
    # Recommended by Unsloth development: manual forward + bf16 autocast +
    # own cross-entropy (fast_cross_entropy_loss) instead of
    # super().compute_loss (which routes gradients through Unsloth's patched
    # path and does not let the EWC term through).
    from unsloth.kernels.cross_entropy_loss import fast_cross_entropy_loss

    class EWCTrainer(SFTTrainer):
        def __init__(self, *a, ewc=None, **k):
            super().__init__(*a, **k)
            self._ewc = ewc
            self._fast_ce = fast_cross_entropy_loss

        def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None):
            # Manual forward WITHOUT labels so Unsloth returns the logits
            # (the empty-logits branch only triggers with labels != None and
            # no UNSLOTH_RETURN_LOGITS).
            labels = inputs.get("labels")
            with torch.cuda.amp.autocast(dtype=torch.bfloat16):
                outputs = model(input_ids=inputs["input_ids"],
                                attention_mask=inputs.get("attention_mask"))
            logits = outputs.logits.float()
            loss = self._fast_ce(logits, labels)
            ewc = self._ewc
            total = loss
            multiplier = max(1, inputs["input_ids"].numel())
            if ewc and ewc["lambda"] > 0:
                term = torch.tensor(0.0, device=loss.device)
                self._ewc_matched = 0
                for name, p in model.named_parameters():
                    if not p.requires_grad or "lora_" not in name:
                        continue
                    nkey = name.replace(".default.", ".")
                    if nkey not in ewc["F"] or nkey not in ewc["theta"]:
                        continue
                    self._ewc_matched += 1
                    Fi = ewc["F"][nkey].to(p.device).float()
                    fmax = Fi.max()
                    if fmax > 0:
                        Fi = Fi / fmax
                    th_old = ewc["theta"][nkey].to(p.device).float()
                    term = term + (Fi * (p.float() - th_old).pow(2)).sum()
                total = total + (ewc["lambda"] / (2.0 * multiplier)) * term
                if not getattr(self, "_ewc_reported", False):
                    self._ewc_reported = True
                    log.info(f"  [EWC] manual forward: matched={self._ewc_matched}, "
                             f"CE={loss.item():.4f}, +EWC={total.item():.4f}")
            return (total, outputs) if return_outputs else total

    ewc = None
    if ewc_on:
        ewc = {"lambda": args.ewc_lambda, "F": old_F, "theta": old_theta}

    trainer = EWCTrainer(
        model=model, tokenizer=tokenizer, train_dataset=ds, dataset_text_field="text",
        args=SFTConfig(
            per_device_train_batch_size=1, gradient_accumulation_steps=1,
            max_steps=args.steps, learning_rate=args.learning_rate, warmup_steps=1,
            output_dir=os.path.join(args.memory_dir, "ckpt"), report_to="none",
            logging_steps=1, optim="adamw_8bit", seed=3407, max_seq_length=args.max_seq,
        ),
        ewc=ewc,
    )
    log.info(f"[4/6] Training ({args.steps} steps) {'with EWC' if ewc else 'without EWC'} ...")
    trainer.train()

    # ---------- 5) Save adapter + tokenizer ----------
    log.info("[5/6] Saving adapter ...")
    model.save_pretrained(args.memory_dir)
    tokenizer.save_pretrained(args.memory_dir)
    log.info(f"  Adapter -> {args.memory_dir} (adapter_model.safetensors)")

    # ---------- 6) Update Fisher and store it back ----------
    log.info("[6/6] Computing/updating Fisher ...")
    import safetensors.torch as st

    def _norm_name(k):
        # state-dict names '...lora_A.default.weight' -> '...lora_A.weight',
        # matching the names in adapter_model.safetensors (save_pretrained).
        return k.replace(".default.", ".")

    lora_params = {_norm_name(n): p for n, p in model.named_parameters() if p.requires_grad}
    F_new = {n: torch.zeros_like(p.float(), device="cpu") for n, p in lora_params.items()}
    theta_new = {n: p.detach().float().cpu().clone() for n, p in lora_params.items()}
    model.zero_grad()
    enc = tokenizer(texts, return_tensors="pt", padding=True, truncation=True, max_length=args.max_seq)
    ids = enc["input_ids"].cuda()
    am = enc["attention_mask"].cuda()
    B = 4
    model.train()
    for i in range(0, len(ids), B):
        model.zero_grad()
        out = model(input_ids=ids[i:i + B], attention_mask=am[i:i + B], labels=ids[i:i + B])
        out.loss.backward()
        for name, p in model.named_parameters():
            if p.requires_grad and p.grad is not None:
                n = _norm_name(name)
                F_new[n] += p.grad.float().cpu().square()
    for n in F_new:
        F_new[n].div_(max(1, (len(ids) + B - 1) // B))
    # Update: F = l * F_old + (1 - l) * F_day ; theta = theta_new
    l = args.fisher_decay
    if old_F is not None:
        for n in F_new:
            if n in old_F and old_F[n] is not None:
                F_new[n] = l * old_F[n].to(torch.float32) + (1 - l) * F_new[n]
    torch.save({"fisher": F_new, "theta_old": theta_new, "decay": l}, fisher_path)
    log.info(f"  Fisher updated -> {fisher_path} ({len(F_new)} tensors)")

    # --- EWC effect check: is the deviation from theta_old damped more
    #     strongly where Fisher is high? (high F -> smaller |delta| is
    #     evidence that EWC is working) ---
    try:
        import numpy as np
        # Reference = the old weights loaded BEFORE training (if available).
        ref = old_theta if old_theta is not None else theta_new
        deltas, fs = [], []
        cur_final = {_norm_name(n): p.detach().float().cpu()
                     for n, p in model.named_parameters()
                     if p.requires_grad and "lora_" in n}
        for n, Fv in F_new.items():
            if n not in cur_final or n not in ref:
                continue
            d = (cur_final[n] - ref[n]).abs().mean().item()
            deltas.append(d)
            fs.append(Fv.float().mean().item())
        deltas = np.array(deltas)
        fs = np.array(fs)
        pos = fs > 0
        if pos.sum() > 5:
            med = np.median(fs[pos])
            lo = deltas[pos][fs[pos] <= med].mean()
            hi = deltas[pos][fs[pos] > med].mean()
            log.info(f"  [EWC-CHECK] avg|dTheta_old| at LOW Fisher={lo:.3e}, "
                     f"at HIGH Fisher={hi:.3e}")
            if hi < lo:
                log.info("  => EWC is working: high-Fisher params moved less.")
            else:
                log.info("  => No clear EWC effect in this sample.")
    except Exception as e:
        log.warning(f"  EWC-CHECK skipped: {e}")
    log.info("=== DONE: memory adapter + Fisher saved ===")


if __name__ == "__main__":
    main()
