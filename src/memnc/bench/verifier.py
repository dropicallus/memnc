"""Learned verifier V1: a linear scorer over deterministic solution features.

Design: docs/design-learned-verifier.md. V1 is a pure-stdlib logistic
regression trained on DPO pairs (see bench.dpo.build_pairs). Its scope is
deliberately narrow: it learns to separate checker-passing solutions from
checker-failing ones by SHAPE (empty files, placeholder stubs, syntax
errors — the fabrication failure mode), never semantic correctness. It
complements the checkers and the critic; it replaces neither.
"""

from __future__ import annotations

import ast
import json
import math
from pathlib import Path

# Deterministic feature order — the JSON artifact stores the names it was
# trained with, so a stored model can never misinterpret a reordered vector.
FEATURE_NAMES = (
    "file_count",
    "total_bytes",
    "line_count",
    "comment_line_ratio",
    "blank_line_ratio",
    "placeholder_count",
    "empty_file_count",
    "syntax_error_count",
    "duplicate_line_ratio",
    "avg_line_len",
)

_PLACEHOLDER_TOKENS = ("TODO", "FIXME", "placeholder", "not implemented")


def _lines(text: str) -> list[str]:
    return text.splitlines()


def extract_features(solutions: dict[str, str]) -> list[float]:
    """Deterministic feature vector over a files dict ({path: content})."""
    file_count = len(solutions)
    total_bytes = 0
    line_count = 0
    comment_lines = 0
    blank_lines = 0
    placeholders = 0
    empty_files = 0
    syntax_errors = 0
    all_lines: list[str] = []
    for _name, content in solutions.items():
        total_bytes += len(content)
        lines = _lines(content)
        line_count += len(lines)
        if not content.strip():
            empty_files += 1
            continue
        lower = content.lower()
        for token in _PLACEHOLDER_TOKENS:
            placeholders += lower.count(token.lower())
        comment_lines += sum(
            1 for ln in lines if ln.strip().startswith("#") or ln.strip().startswith("//")
        )
        blank_lines += sum(1 for ln in lines if not ln.strip())
        all_lines.extend(ln.strip() for ln in lines if ln.strip())
        try:
            ast.parse(content)
        except SyntaxError:
            syntax_errors += 1
    duplicates = len(all_lines) - len(set(all_lines)) if all_lines else 0
    duplicate_ratio = duplicates / len(all_lines) if all_lines else 0.0
    return [
        float(file_count),
        float(total_bytes),
        float(line_count),
        comment_lines / line_count if line_count else 0.0,
        blank_lines / line_count if line_count else 0.0,
        float(placeholders),
        float(empty_files),
        float(syntax_errors),
        duplicate_ratio,
        total_bytes / line_count if line_count else 0.0,
    ]


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


class Verifier:
    """Logistic scorer trained on (chosen, rejected) solution pairs.

    Loss per pair: -log(sigmoid(w . (x_chosen - x_rejected) + b)) — a pure
    pairwise preference objective, no per-sample labels needed.
    """

    def __init__(self) -> None:
        self.feature_names: list[str] = list(FEATURE_NAMES)
        self.weights: list[float] = [0.0] * len(FEATURE_NAMES)
        self.bias: float = 0.0
        self.means: list[float] = [0.0] * len(FEATURE_NAMES)
        self.stds: list[float] = [1.0] * len(FEATURE_NAMES)
        self.trained_on: int = 0

    # -- training --------------------------------------------------------
    def fit(
        self, pairs: list[dict], epochs: int = 300, lr: float = 0.1
    ) -> Verifier:
        if not pairs:
            raise ValueError("no pairs to train on")
        chosen = [extract_features(p["chosen"]) for p in pairs]
        rejected = [extract_features(p["rejected"]) for p in pairs]
        n = len(self.feature_names)
        self.means = [sum(x[i] for x in chosen + rejected) / (2 * len(pairs)) for i in range(n)]
        self.stds = [
            max(1e-9, math.sqrt(sum((x[i] - self.means[i]) ** 2 for x in chosen + rejected) / (2 * len(pairs))))
            for i in range(n)
        ]
        ch = [self._standardize(x) for x in chosen]
        rj = [self._standardize(x) for x in rejected]
        self.weights = [0.0] * n
        self.bias = 0.0
        for _ in range(epochs):
            grad_w = [0.0] * n
            grad_b = 0.0
            for xc, xr in zip(ch, rj, strict=True):
                p = _sigmoid(
                    sum(w * (a - b) for w, a, b in zip(self.weights, xc, xr, strict=True)) + self.bias
                )
                d = p - 1.0  # gradient of -log p wrt the margin (score_chosen - score_rejected)
                for i in range(n):
                    grad_w[i] += d * (xc[i] - xr[i])
                grad_b += d
            self.weights = [w - lr * g / len(pairs) for w, g in zip(self.weights, grad_w, strict=True)]
            self.bias -= lr * grad_b / len(pairs)
        self.trained_on = len(pairs)
        return self

    def _standardize(self, vec: list[float]) -> list[float]:
        return [(v - m) / s for v, m, s in zip(vec, self.means, self.stds, strict=True)]

    # -- scoring ---------------------------------------------------------
    def score(self, solutions: dict[str, str]) -> float:
        """Probability the scorer assigns to 'this is a good solution'."""
        x = self._standardize(extract_features(solutions))
        z = sum(w * v for w, v in zip(self.weights, x, strict=True)) + self.bias
        return _sigmoid(z)

    def accuracy(self, pairs: list[dict]) -> float:
        """Fraction of pairs where chosen outranks rejected."""
        if not pairs:
            raise ValueError("no pairs to evaluate")
        good = sum(
            1
            for p in pairs
            if self.score(p["chosen"]) > self.score(p["rejected"])
        )
        return good / len(pairs)

    # -- artifact --------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "feature_names": self.feature_names,
            "weights": self.weights,
            "bias": self.bias,
            "means": self.means,
            "stds": self.stds,
            "trained_on": self.trained_on,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Verifier:
        v = cls()
        if data.get("feature_names") != v.feature_names:
            raise ValueError("feature mismatch: artifact was trained with a different extractor")
        v.weights = [float(x) for x in data["weights"]]
        v.bias = float(data["bias"])
        v.means = [float(x) for x in data["means"]]
        v.stds = [float(x) for x in data["stds"]]
        v.trained_on = int(data.get("trained_on", 0))
        return v

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: str | Path) -> Verifier:
        return cls.from_dict(json.loads(Path(path).read_text()))


# Convenience name per the design note's API.
def verify(solutions: dict[str, str], model: Verifier) -> float:
    return model.score(solutions)
