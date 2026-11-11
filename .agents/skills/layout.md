# Skill: repository layout

Where files go — and where they must not go.

- No Python files in the repo root. All code lives in `src/` packages with entry points in `pyproject.toml`.
- Only README.md and CONTRIBUTING.md live in the repo root (GitHub open-source convention); every other markdown file belongs in `docs/`.
- Do not create parallel copies of a module; move it and fix imports.
