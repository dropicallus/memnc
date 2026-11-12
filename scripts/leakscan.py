#!/usr/bin/env python3
"""Leak-scan a git diff range for secrets, machine details and German prose.

Usage:
    python3 scripts/leakscan.py [BASE..HEAD]     # default: origin/main..HEAD
    python3 scripts/leakscan.py --worktree       # scan tracked files as they are

Exit code 0 = clean, 1 = leaks found, 2 = usage/setup error.

Scan classes:
- secrets: token-shaped strings, private keys, .env values, cred URLs
- identity: home paths, usernames, phone-like numbers
- network: LAN/private IPs, internal hostnames
- hardware: GPU models, VRAM sizes, host aliases (kept out of the repo by design)
- german:  German prose in repo files and commit messages (repo language is English)

Per-line suppression: append `# leakscan:ok` (or `// leakscan:ok`) to a line
that is a known false positive. Rules are data below — extend, don't fork.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

# (class, regex). All anchored with word/lookaround where possible to keep
# false positives low. Keep each pattern one line, documented by the class.
# Machine-specific rules (hostnames, usernames, hardware, LAN IPs, local
# dir names) live in a LOCAL file only: scripts/leakscan_rules.local.py
# (gitignored). The repo never carries them.
try:
    from leakscan_rules_local import RULES  # type: ignore[assignment]  # noqa: F401
except ImportError:
    RULES: list[tuple[str, str]] = [
        ("secret", r"\b(sk|pk)-[A-Za-z0-9_-]{20,}\b"),
        ("secret", r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
        ("secret", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ]


# German prose detection: a scored word-frequency approach instead of a
# single regex — one stray "die" in an English sentence must not fail the scan.
GERMAN_WORDS = (
    "der die das und nicht ist sind waren wurden eine einen einem eines "
    "keine keiner für auch wir ihr unsere aber oder noch schon schon "
    "bleibt genau weiter brauchen brauchen machen macht machen okay okay "
    "zusammen trotzdem deshalb zwar jetzt heute morgen abend "
    "sowie bzw d.h v.v"
).split()
GERMAN_STRONG = {"und", "nicht", "ist", "keine", "für", "auch", "oder", "aber",
                 "bleibt", "genau", "zusammen", "trotzdem", "deshalb", "zwar"}
GERMAN_STRONG.update({"die", "der", "das", "eine", "einen", "wir", "ihr"})
# words that legitimately occur in English (die = dying, der = ?) — require
# a minimum count before flagging weak words
GERMAN_WEAK_MIN = 3

ALLOW_MARK = re.compile(r"(?:#|//)\s*leakscan:ok\s*$")


def scan_line_rules(lines: list[str]) -> list[tuple[int, str, str]]:
    """Yield (lineno, rule_class, line) for every rule hit not allow-listed."""
    compiled = []
    for cls, pat in RULES:
        try:
            compiled.append((cls, re.compile(pat)))
        except re.error:  # never let a bad rule kill the scan
            print(f"warning: invalid regex in class {cls}: {pat!r}", file=sys.stderr)
    hits = []
    for i, line in enumerate(lines, 1):
        if ALLOW_MARK.search(line):
            continue
        for cls, rx in compiled:
            if rx.search(line):
                hits.append((i, cls, line.rstrip()))
                break  # one class per line is enough
    return hits


def german_hits(lines: list[str]) -> list[tuple[int, str]]:
    """Score German function words; strong words count double."""
    counts: dict[str, int] = {}
    lines_with: dict[str, list[int]] = {}
    for i, line in enumerate(lines, 1):
        if ALLOW_MARK.search(line):
            continue
        for w in re.findall(r"[A-Za-zÄÖÜäöüß]+", line.lower()):
            if w in GERMAN_STRONG or w in GERMAN_WORDS:
                counts[w] = counts.get(w, 0) + (2 if w in GERMAN_STRONG else 1)
                lines_with.setdefault(w, []).append(i)
    score = sum(counts.values())
    if score < 4:
        return []
    offenders = sorted(counts, key=lambda w: -counts[w])[:6]
    lnums = sorted({n for w in offenders for n in lines_with.get(w, [])})[:5]
    return [(n, f"german prose (score={score}, words: {', '.join(offenders)})") for n in lnums]


def git(*args: str, cwd: str | None = None) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        print(f"git {' '.join(args)} failed:\n{r.stderr}", file=sys.stderr)
        sys.exit(2)
    return r.stdout


def added_lines(diff: str) -> tuple[list[str], list[str]]:
    """Return (added source lines, added diff-header context for files)."""
    out, files = [], []
    current_file = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            files.append(current_file)
            continue
        if line.startswith("+") and not line.startswith("+++"):
            out.append(f"[{current_file}] {line[1:]}")
    return out, files


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("range", nargs="?", default="origin/main..HEAD",
                    help="git diff range to scan (default origin/main..HEAD)")
    ap.add_argument("--worktree", action="store_true",
                    help="scan the current tracked files instead of a diff range")
    ap.add_argument("--quiet", action="store_true", help="only print the verdict")
    args = ap.parse_args()

    repo = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True).stdout.strip()
    if not repo:
        print("not inside a git repository", file=sys.stderr)
        return 2

    problems: list[str] = []

    if args.worktree:
        tracked = git("ls-files", cwd=repo).splitlines()
        # the scanner itself contains its own regex rules — skip it
        SELF = "scripts/leakscan.py"
        for f in tracked:
            if f == SELF:
                continue
            try:
                content = open(f"{repo}/{f}", encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            lines = content.splitlines()
            for i, cls, line in scan_line_rules(lines):
                problems.append(f"{f}:{i} [{cls}] {line.strip()[:100]}")
            for i, why in german_hits(lines):
                problems.append(f"{f}:{i} [{why}] {lines[i-1].strip()[:100]}")
    else:
        diff = git("diff", args.range, cwd=repo)
        added, _files = added_lines(diff)
        # the scanner itself contains its own regex rules — skip it
        added = [l for l in added if not l.startswith("[scripts/leakscan.py]")]
        for i, cls, line in scan_line_rules(added):
            problems.append(f"diff:{i} [{cls}] {line.strip()[:100]}")
        for i, why in german_hits(added):
            problems.append(f"diff:{i} [{why}] {added[i-1].strip()[:100]}")
        # commit messages in the range: same classes apply
        log = git("log", "--format=%H %s%n%b%n---", args.range, cwd=repo)
        msgs = [m for m in log.split("\n---\n") if m.strip()]
        for m in msgs:  # noqa: B007
            first = m.splitlines()[0] if m.splitlines() else ""
            for _i, cls, line in scan_line_rules(m.splitlines()):
                problems.append(f"commit {first[:12]} [{cls}] {line.strip()[:100]}")
            for _j, why in german_hits(m.splitlines()):
                problems.append(f"commit {first[:12]} [{why}] {first[:100]}")

    if args.quiet:
        print("LEAKS" if problems else "CLEAN")
        return 1 if problems else 0

    if problems:
        print(f"✗ {len(problems)} leak-scan hit(s) in {args.range if not args.worktree else 'worktree'}:")
        for p in problems:
            print("  " + p)
        print("\nsuppress a known false positive with a trailing `# leakscan:ok`")
        return 1
    print(f"✓ leak scan clean ({args.range if not args.worktree else 'worktree'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
