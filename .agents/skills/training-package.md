# Skill: the training package boundary

- `memnc.training` (src/memnc/training/) is a standalone LoRA-EWC training loop (Unsloth, Fisher consolidation against catastrophic forgetting).
- It is NOT part of the mesh runtime: it ships as the optional extra `pip install -e ".[train]"`.
- Mesh code (orchestrator, worker, bus, CLI, telegram, console, discuss) must never import `memnc.training`.
- New training features stay inside the package; the mesh consumes produced artifacts (adapters, Fisher files), not the package.
