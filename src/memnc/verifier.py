import ast
import json
import math
import re
from dataclasses import dataclass


@dataclass
class VerityFeatures:
    file_count: int
    total_bytes: int
    total_lines: int
    ast_parsed: bool
    comment_fraction: float
    duplicate_ratio: float
    placeholder_count: int


class LearnedVerifier:
    """
    V1 Learned Verifier: A simple linear scorer (logistic regression)
    identifying 'shape' failures (fabrication, empty, stubs).
    """

    def __init__(self, weights: dict = None):
        # Initial default weights if none provided
        # Features order must be consistent:
        # [file_count, total_bytes, total_lines, ast_parsed, comment_fraction, duplicate_ratio, placeholder_count]
        self.feature_names = [
            "file_count",
            "total_bytes",
            "total_lines",
            "ast_parsed",
            "comment_fraction",
            "duplicate_ratio",
            "placeholder_count",
        ]
        if weights:
            self.weights = weights
        else:
            self.weights = {name: 0.0 for name in self.feature_names}
            self.weights["bias"] = 0.0

    def _extract_features(self, files: dict[str, str]) -> list[float]:
        if not files:
            return [0.0] * len(self.feature_names)

        total_bytes = 0
        total_lines = 0
        ast_parsed_count = 0
        comment_lines = 0
        total_non_empty_lines = 0

        all_lines = []
        placeholder_patterns = [
            re.compile(r"\bTODO\b", re.I),
            re.compile(r"\bpass\b"),
            re.compile(r"\bempty\b", re.I),
            re.compile(r"raise NotImplementedError", re.I),
        ]
        placeholder_count = 0

        for path, content in files.items():
            lines = content.splitlines()
            file_len = len(lines)
            total_lines += file_len
            total_bytes += len(content.encode("utf-8"))

            if file_len > 0:
                total_non_empty_lines += 1
                all_lines.extend(lines)

            # AST parse check for .py files
            if path.endswith(".py"):
                try:
                    ast.parse(content)
                    ast_parsed_count += 1
                except Exception:
                    pass

            # Comment fraction and placeholder detection
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue

                # Simple comment detection
                if stripped.startswith("#"):
                    comment_lines += 1

                # Placeholder check
                for pattern in placeholder_patterns:
                    if pattern.search(stripped):
                        placeholder_count += 1
                        break

        # Duplicate ratio (very simple: unique lines vs total non-empty lines)
        unique_lines = set(line.strip() for line in all_lines if line.strip())
        duplicate_ratio = 1.0 - (
            len(unique_lines) / total_non_empty_lines if total_non_empty_lines > 0 else 1.0
        )

        ast_parsed = 1.0 if ast_parsed_count > 0 else 0.0
        comment_fraction = (
            comment_lines / total_non_empty_lines if total_non_empty_lines > 0 else 0.0
        )

        return [
            float(len(files)),
            float(total_bytes),
            float(total_lines),
            ast_parsed,
            comment_fraction,
            duplicate_ratio,
            float(placeholder_count),
        ]

    def _sigmoid(self, x: float) -> float:
        return 1.0 / (1.0 + math.exp(-x))

    def predict_score(self, features: list[float]) -> float:
        """Logistic regression inference."""
        z = self.weights["bias"]
        for name, val in zip(self.feature_names, features, strict=False):
            z += val * self.weights[name]
        return self._sigmoid(z)

    def verify(self, files: dict[str, str]) -> float:
        """Public API: returns a score from 0..1."""
        if not files:
            return 0.0
        features = self._extract_features(files)
        return self.predict_score(features)

    def train(self, corpus: list[tuple[dict[str, str], int]]):
        """
        Simple Gradient Descent for Logistic Regression.
        corpus: List of (files_dict, label) where label is 0 or 1.
        """
        learning_rate = 0.000001  # Very low due to raw values like total_bytes
        iterations = 100  # Not enough for convergence on unscaled data, but enough for demo

        # Optimization loop (stochastic gradient descent)
        for _ in range(iterations):
            for files, label in corpus:
                features = self._extract_features(files)
                # Predictions
                z = self.weights["bias"]
                for name, val in zip(self.feature_names, features, strict=False):
                    z += val * self.weights[name]
                pred = self._sigmoid(z)

                # Gradient
                error = pred - label
                self.weights["bias"] -= learning_rate * error
                for i, name in enumerate(self.feature_names):
                    self.weights[name] -= learning_rate * error * features[i]

    def save_weights(self, path: str):
        with open(path, "w") as f:
            json.dump(self.weights, f)

    def load_weights(self, path: str):
        with open(path) as f:
            self.weights = json.load(f)
