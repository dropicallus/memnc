# Security Policy

## Supported versions

Only the latest `main` branch is supported. There are no release branches yet.

## Reporting a vulnerability

Use GitHub private vulnerability reporting: **Security tab → Report a vulnerability**. Do not open a public issue for security problems.

Reports are handled by the maintainer. Secrets in reported material never appear in public tickets.

## Scanning

The repository is scanned for secrets on every push (gitleaks in CI and pre-commit) and dependencies are updated weekly (dependabot).
