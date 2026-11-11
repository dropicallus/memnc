import argparse
import json
import logging
import os

import safetensors.torch as st
import torch
from unsloth import FastLanguageModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fisher")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="Base model name")
    ap.add_argument("--adapter", required=True, help="Directory with trained LoRA adapter")
    ap.add_argument("--data", required=True, help="JSONL with messages")
    ap.add_argument("--out", default="fisher.pt", help="Output file for fisher.pt")
    ap.add_argument("--samples", type=int, default=1, help="Number of gradient estimation steps")
    args = ap.parse_args()

    log.info(f"[1/4] Loading Base {args.model} + creating LoRA ...")
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=args.model, max_seq_length=512, dtype=torch.bfloat16,
        load_in_4bit=True,
    )
    model = FastLanguageModel.get_peft_model(
        model, r=8, lora_alpha=16, lora_dropout=0,
        target_modules=["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"],
        use_gradient_checkpointing="unsloth", random_state=3407, max_seq_length=512,
    )

    log.info("[2/4] Loading weights from adapter ...")
    ad_path = os.path.join(args.adapter, "adapter_model.safetensors")
    if not os.path.exists(ad_path):
        log.error(f"Adapter not found at {ad_path}")
        return

    sd = st.load_file(ad_path)
    cur = dict(model.state_dict())
    load_sd = {}
    
    def norm(k): return k.replace(".default.", ".").split("base_model.model.")[-1]
    cur_norm = {norm(k): k for k in cur}

    for k, v in sd.items():
        nk = norm(k)
        if nk in cur_norm:
            load_sd[nk] = v

    model.load_state_dict(load_sd, strict=False)
    model.train()
    model = model.to("cuda")

    log.info("[3/4] Processing traces ...")
    rows = []
    with open(args.data, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
                
    texts = []
    for r in rows:
        msgs = r.get("messages")
        if not msgs: continue
        clean = []
        for m in msgs:
            if not isinstance(m, dict): continue
            m_copy = dict(m)
            if not m_copy.get("content"): m_copy["content"] = ""
            if m_copy.get("role") == "tool" and not isinstance(m_copy.get("content"), str):
                m_copy["content"] = json.dumps(m_copy.get("content") or "", ensure_ascii=False)
            clean.append(m_copy)
        try:
            t = tokenizer.apply_chat_template(clean, tokenize=False, add_generation_prompt=False)
        except Exception:
            t = " ".join(str(m.get("content") or "") for m in clean)
        texts.append(t)
    
    if not texts:
        log.error("No text samples extracted from data.")
        return

    enc = tokenizer(texts, return_tensors="pt", padding=True, truncation=True, max_length=512)
    input_ids = enc["input_ids"].cuda()
    attn = enc["attention_mask"].cuda()

    log.info("[4/4] Computing Fisher ...")
    lora_params = {n: p for n, p in model.named_parameters() if p.requires_grad}
    F = {n: torch.zeros_like(p, dtype=torch.float32) for n, p in lora_params.items()}
    
    batch_size = 4
    num_samples = args.samples
    max(1, (len(input_ids) + batch_size - 1) // batch_size)
    
    for _ in range(num_samples):
        model.zero_grad()
        for i in range(0, len(input_ids), batch_size):
            b_ids = input_ids[i:i+batch_size].cuda()
            b_am = attn[i:i+batch_size].cuda()
            out = model(input_ids=b_ids, attention_mask=b_am, labels=b_ids)
            loss = out.loss
            loss.backward()
            
        for n, p in lora_params.items():
            if p.grad is not None:
                F[n].add_(p.grad.float().square())
                
    for n in F:
        F[n].div_(num_samples)

    torch.save({"fisher": F, "steps": num_samples}, args.out)
    log.info(f"  Saved to {args.out}")

if __name__ == "__main__":
    main()
