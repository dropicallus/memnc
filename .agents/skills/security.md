# Skill: secrets and security

- Gitleaks scans history in CI and in the pre-commit hook. Never disable the scan.
- A flagged false positive is handled with a scoped allowlist entry in `.gitleaks.toml`, with a comment explaining it.
- Never commit credentials, tokens, private keys, or personal data.
