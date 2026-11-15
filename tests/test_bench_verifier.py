"""Tests for the learned verifier V1 (bench verifier).

Pure-stdlib training on synthetic DPO pairs: a checker-passing style
solution vs empty/placeholder/syntax-broken files. No network, no models.
"""

from __future__ import annotations

import json

from memnc.bench.verifier import (
    FEATURE_NAMES,
    Verifier,
    extract_features,
    verify,
)

GOOD_SOLUTION = {
    "main.py": (
        "#!/usr/bin/env python3\n"
        "def is_palindrome(s: str) -> bool:\n"
        "    t = [c.lower() for c in s if c.isalnum()]\n"
        "    return t == t[::-1]\n"
    ),
    "util.py": "def normalize(s: str) -> str:\n    return s.strip().lower()\n",
}

EMPTY_SOLUTION = {"main.py": "", "util.py": ""}

PLACEHOLDER_SOLUTION = {
    "main.py": "def is_palindrome(s):\n    # TODO: implement\n    pass\n",
    "util.py": "# placeholder\n# not implemented\n",
}

SYNTAX_ERROR_SOLUTION = {
    "main.py": "def broken(:\n    return\n",
}

MORE_CODE_SOLUTION = {
    "main.py": "\n".join(f"x{i} = {i}" for i in range(200)) + "\n",
    "util.py": "def helper() -> int:\n    return 42\n",
}


def make_pairs() -> list[dict]:
    return [
        {"chosen": GOOD_SOLUTION, "rejected": EMPTY_SOLUTION},
        {"chosen": GOOD_SOLUTION, "rejected": PLACEHOLDER_SOLUTION},
        {"chosen": GOOD_SOLUTION, "rejected": SYNTAX_ERROR_SOLUTION},
        {"chosen": MORE_CODE_SOLUTION, "rejected": EMPTY_SOLUTION},
        {"chosen": MORE_CODE_SOLUTION, "rejected": PLACEHOLDER_SOLUTION},
    ]


def test_extract_features_is_deterministic_and_complete():
    a = extract_features(GOOD_SOLUTION)
    b = extract_features(dict(reversed(list(GOOD_SOLUTION.items()))))
    assert a == b
    assert len(a) == len(FEATURE_NAMES)
    empty = extract_features(EMPTY_SOLUTION)
    assert empty[FEATURE_NAMES.index("empty_file_count")] == 2
    assert empty[FEATURE_NAMES.index("total_bytes")] == 0
    ph = extract_features(PLACEHOLDER_SOLUTION)
    assert ph[FEATURE_NAMES.index("placeholder_count")] > 0
    assert ph[FEATURE_NAMES.index("comment_line_ratio")] > 0
    se = extract_features(SYNTAX_ERROR_SOLUTION)
    assert se[FEATURE_NAMES.index("syntax_error_count")] == 1


def test_fit_separates_good_from_degenerate():
    pairs = make_pairs()
    model = Verifier().fit(pairs, epochs=200, lr=0.2)
    assert model.accuracy(pairs) == 1.0
    assert verify(GOOD_SOLUTION, model) > 0.5
    assert verify(EMPTY_SOLUTION, model) < 0.5
    assert verify(PLACEHOLDER_SOLUTION, model) < 0.5
    assert verify(SYNTAX_ERROR_SOLUTION, model) < 0.5


def test_untrained_verifier_scores_neutral():
    model = Verifier()
    assert model.score(GOOD_SOLUTION) == 0.5


def test_fit_rejects_empty_pair_list():
    try:
        Verifier().fit([])
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_json_roundtrip_preserves_scores():
    model = Verifier().fit(make_pairs(), epochs=100, lr=0.2)
    data = json.loads(json.dumps(model.to_dict()))
    loaded = Verifier.from_dict(data)
    for sol in (GOOD_SOLUTION, EMPTY_SOLUTION, PLACEHOLDER_SOLUTION):
        assert loaded.score(sol) == model.score(sol)
    assert loaded.accuracy(make_pairs()) == model.accuracy(make_pairs())


def test_artifact_feature_mismatch_is_rejected():
    model = Verifier().fit(make_pairs(), epochs=10)
    data = model.to_dict()
    data["feature_names"] = ["bogus"] * len(FEATURE_NAMES)
    try:
        Verifier.from_dict(data)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_save_and_load(tmp_path):
    model = Verifier().fit(make_pairs(), epochs=50)
    path = tmp_path / "verifier.json"
    model.save(path)
    loaded = Verifier.load(path)
    assert loaded.score(GOOD_SOLUTION) == model.score(GOOD_SOLUTION)
    assert loaded.trained_on == len(make_pairs())
