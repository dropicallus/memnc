# AGENTS.md — rules for agents and humans working in this repo

This repository is maintained by a single owner with AI agents doing most of
the work. The rules below are binding; CI and the pre-commit hook enforce the
mechanical ones. Detailed, task-focused versions of these rules live in
`.agents/skills/` — read the one that matches your change:

- `.agents/skills/conventions.md` — language, provenance, naming
- `.agents/skills/layout.md` — where files go
- `.agents/skills/quality-gates.md` — CI, tests, pre-commit
- `.agents/skills/security.md` — secrets, gitleaks
- `.agents/skills/training-package.md` — training/mesh boundary

## Language and provenance

- **English only.** No German text in source, docs, comments, docstrings or
  commit messages.
- **No date annotations in files.** No "verified 2026-11-11" lines, no
  provenance/status stamps in code or docs. Git history is the record of
  when and by whom — files are not a memory. (arXiv IDs and version numbers
  are not dates; they stay.)
- **No personal names or local paths.** Never commit author names outside git
  metadata, usernames, or absolute home paths (`/home/...`). Use CLI args or
  env vars instead.

## Repository layout

- **No Python files in the repo root.** Everything lives in `src/` packages
  with proper entry points in `pyproject.toml`.
- **Only README.md and CONTRIBUTING.md may live in the root** (GitHub open
  source convention). Every other `.md` goes into `docs/`.
- Keep files where they are — do not create parallel copies of a module.

## Packages

- `memnc` (src/memnc/) — the mesh runtime: orchestrator, workers, bus,
  CLI (`meshctl`), telegram observer, console, discuss harness.
- `memnc.training` (src/memnc/training/) — **standalone LoRA-EWC training
  loop (Unsloth, Fisher consolidation). It is NOT part of the mesh runtime**;
  it ships as the optional extra `pip install -e ".[train]"` and must never
  be imported by mesh code.

## Quality gates

- CI runs `ruff check src tests` and `pytest tests/ -q`, plus a gitleaks
  scan over history. All three must pass.
- The pre-commit hook (`scripts/hooks/pre-commit`, enabled via
  `core.hooksPath`) runs the same checks plus gitleaks. Skip only with
  `git commit --no-verify` — and then say why in the commit message.
- Tests must be host-independent: pin `node_name` and any other
  hostname/environment-dependent values; never rely on the runner hostname.
- Branch protection on `main`: required checks `CI / test` and
  `CI / gitleaks`, strict. Do not bypass them without the owner's say-so.

## Secrets

- Gitleaks runs in CI and pre-commit. If it flags a false positive, extend
  `.gitleaks.toml` with a scoped allowlist entry and a comment — never
  disable the scan.

## Docs

- `docs/plan.md` is the current specification; `docs/roadmap.md` holds
  built/next/later items. Do not append dated status notes to either —
  update the relevant section in place.
