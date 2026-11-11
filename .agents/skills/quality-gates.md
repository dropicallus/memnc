# Skill: quality gates

What must pass before a change can be merged.

- `ruff check src tests` and `pytest tests/ -q` must pass; CI runs the same plus a gitleaks scan.
- A pre-commit hook mirrors CI (`scripts/hooks/pre-commit`, enabled via `core.hooksPath`). Skip only with `git commit --no-verify` and state why in the commit message.
- Tests must be host-independent: pin `node_name` and every other hostname/environment-dependent value; never depend on the runner hostname.
- Branch protection on `main` requires `CI / test` and `CI / gitleaks` (strict). Do not try to bypass it.
