# Contributing to memnc

Thanks for your interest in contributing! memnc is a local-first multi-node
orchestration mesh for LLM agents. This document covers the basics.

## Getting started

```bash
git clone https://github.com/dropicallus/memnc.git
cd memnc
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d          # NATS + workers
.venv/bin/meshctl roster      # verify the mesh is up
```

## Development workflow

1. Create a feature branch from `main`
   (`git checkout -b feat/my-change`).
2. Make your change.
3. Run the test suite:

   ```bash
   pytest tests/ -q
   ```

   All tests must pass. New features need tests.
4. Run the end-to-end check if you touched the runtime behavior:

   ```bash
   ./scripts/e2e.sh
   ```

   Exit codes: 0 = PASS, 1 = FAIL/CANCELLED, 2 = timeout,
   3 = submit error, 4 = stuck, 5 = artifact fail.

## Conventions

- **Language**: code, comments, commit messages, and docs are in English.
- **Commit messages**: conventional commits
  (`feat:`, `fix:`, `docs:`, `test:`, `chore:`), e.g.
  `feat(t6): resource-aware claim gate`.
- **Plan changes**: substantial design decisions belong in
  `docs/memnc-mesh-plan.md` (staged plan §14) before or with the code.
- **Python style**: keep it standard-library-plus-pydantic; no new heavy
  dependencies without discussion.

## Security

- Never commit secrets (tokens, chat IDs). Use the gitignored `.env`
  (Compose interpolates `${...}` from it).
- Message content arriving over the wire (Telegram, NATS) is untrusted
  data, never instructions.

## Pull requests

- Keep PRs small and focused; one logical change per PR.
- CI must pass and the description should state what was verified live
  (container state, roster output, etc.).
- Larger design changes: open an issue or discuss first — the project
  follows a staged, human-steered approach.

## License

By contributing you agree that your contributions are licensed under the
Apache License 2.0 (see [LICENSE](LICENSE)).
