"""Bench task set for the swarm-vs-single benchmark (roadmap 14.15.9).

24 tasks, three tiers (easy / medium / hard, 8 each). Every task is
auto-verified by a deterministic checker (no LLM judge): the checker loads
the files the agent wrote and runs plain asserts. All tasks are pure stdlib,
deterministic, and require no network access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Tier = Literal["easy", "medium", "hard", "sync"]


@dataclass(frozen=True)
class BenchTask:
    """One benchmark task with a deterministic checker."""

    id: str
    tier: Tier
    # Exact instruction given to the coder role.
    prompt: str
    # File paths (relative to the workspace root) the task must produce.
    files: list[str] = field(default_factory=list)
    # Name of the check function in memnc.bench.checkers.
    checker: str = ""
    # Sync tier only: an environment update delivered AFTER the first coder
    # pass (SyncMind out-of-sync simulation). The checker verifies the
    # AMENDED spec only, so ignoring the update fails the task.
    amendment: str = ""


SYNC_PROMPTS = {
    "sync_rate_limit": (
        "Create a file named sol_rate_limit.py containing a function "
        "can_send(sent_today: int, limit: int) -> bool. It must return True "
        "only when sent_today is strictly below limit. Use only the Python "
        "standard library."
    ),
    "sync_user_label": (
        "Create a file named sol_user_label.py containing a function "
        "user_label(user: dict) -> str. It must return the user's name and "
        "the email in angle brackets, e.g. 'Ann <a@b.c>'. Use only the "
        "Python standard library."
    ),
    "sync_apply_ops": (
        "Create a file named sol_apply_ops.py containing a function "
        "apply_ops(state: dict, ops: list) -> dict. Each op is a dict with "
        "'op' ('set' or 'inc'), 'key', 'value'. 'set' writes the value, "
        "'inc' adds it to the current value (0 when the key is missing). "
        "The function must return the updated state without mutating the "
        "input. Use only the Python standard library."
    ),
    "sync_convert": (
        "Create a file named sol_convert.py containing a function "
        "convert(amount: float, rate: float) -> float. It must return "
        "amount * rate rounded to two decimals. Use only the Python "
        "standard library."
    ),
}

# Environment updates delivered AFTER the first coder pass (sync tier).
SYNC_AMENDMENTS = {
    "sync_rate_limit": (
        "A total cap was introduced: can_send now takes a third argument "
        "total_sent (int) and returns True only when sent_today < limit "
        "AND total_sent < 10."
    ),
    "sync_user_label": (
        "Privacy change: emails must no longer appear in the output. "
        "user_label(user: dict) -> str must return just the name, and the "
        "string 'unknown' when the name is missing or empty."
    ),
    "sync_apply_ops": (
        "Ops now carry an optional 'op_id'. An op whose op_id was already "
        "applied in this call must be skipped (idempotent replay); ops "
        "without an op_id always apply. Same signature and semantics "
        "otherwise."
    ),
    "sync_convert": (
        "Amounts now arrive as integer cents: convert(cents: int, "
        "rate: float) -> float must return round(cents / 100 * rate, 2)."
    ),
}

EASY_PROMPTS = {
    "easy_palindrome": (
        "Create a file named sol_palindrome.py containing a function "
        "is_palindrome(s: str) -> bool. It must return True if s is a "
        "palindrome when case is ignored and all non-alphanumeric "
        "characters are skipped, otherwise False. Empty strings count as "
        "palindromes. Use only the Python standard library."
    ),
    "easy_fizzbuzz": (
        "Create a file named sol_fizzbuzz.py containing a function "
        "fizzbuzz(n: int) -> list. For i in 1..n it must append 'FizzBuzz' "
        "when i is divisible by 3 and 5, 'Fizz' when divisible by 3 only, "
        "'Buzz' when divisible by 5 only, else str(i). Use only the "
        "Python standard library."
    ),
    "easy_count_words": (
        "Create a file named sol_count_words.py containing a function "
        "count_words(text: str) -> dict. It must lowercase the text, split "
        "it on whitespace, strip surrounding punctuation (.,!?;:) from "
        "each token, drop empty tokens, and return a dict mapping each "
        "word to how often it occurs. Use only the Python standard "
        "library."
    ),
    "easy_reverse_words": (
        "Create a file named sol_reverse_words.py containing a function "
        "reverse_words(s: str) -> str. It must return the words of s "
        "(split on whitespace) in reverse order, joined by single spaces, "
        "with leading and trailing whitespace collapsed. Use only the "
        "Python standard library."
    ),
    "easy_fibonacci": (
        "Create a file named sol_fibonacci.py containing a function "
        "fib(n: int) -> int. fib(0) == 0, fib(1) == 1, and fib(n) must "
        "equal fib(n-1) + fib(n-2) for n >= 2. It must handle n up to "
        "1000 quickly (no exponential recursion). Use only the Python "
        "standard library."
    ),
    "easy_anagram": (
        "Create a file named sol_anagram.py containing a function "
        "is_anagram(a: str, b: str) -> bool. Comparison must be "
        "case-insensitive and ignore spaces. Use only the Python standard "
        "library."
    ),
    "easy_clamp": (
        "Create a file named sol_clamp.py containing a function "
        "clamp(v: float, lo: float, hi: float) -> float. It must return v "
        "when lo <= v <= hi, lo when v < lo, and hi when v > hi. Assume "
        "lo <= hi. Use only the Python standard library."
    ),
    "easy_digit_sum": (
        "Create a file named sol_digit_sum.py containing a function "
        "digit_sum(n: int) -> int. It must return the sum of the decimal "
        "digits of abs(n), so digit_sum(-123) == 6 and digit_sum(0) == 0. "
        "Use only the Python standard library."
    ),
}

MEDIUM_PROMPTS = {
    "medium_brackets": (
        "Create a file named sol_brackets.py containing a function "
        "is_balanced(s: str) -> bool. It must return True when every "
        "bracket in s is closed correctly and in the right order for the "
        "pairs (), [], and {}, otherwise False. Any other characters are "
        "ignored. Use only the Python standard library."
    ),
    "medium_lru_cache": (
        "Create a file named sol_lru_cache.py containing a class "
        "LRUCache with __init__(self, capacity: int), get(self, key) and "
        "put(self, key, value). Both operations must run in O(1) average "
        "time (use collections.OrderedDict or a dict plus ordering "
        "bookkeeping). get must return None for missing keys. put on an "
        "existing key must refresh its recency. When capacity is exceeded "
        "the least recently used key must be evicted. Use only the Python "
        "standard library."
    ),
    "medium_moving_average": (
        "Create a file named sol_moving_average.py containing a class "
        "MovingAverage with __init__(self, size: int) and next(self, val: "
        "float) -> float. next must append val to a sliding window of at "
        "most size values and return the mean of the current window. Use "
        "only the Python standard library."
    ),
    "medium_csv_stats": (
        "Create a file named sol_csv_stats.py containing a function "
        "column_stats(csv_text: str, column: str) -> dict. csv_text is a "
        "comma-separated table whose first row is the header. The function "
        "must use the csv module to parse it and return a dict with keys "
        "'mean', 'min', 'max' holding floats for the named column. Missing "
        "columns must raise KeyError. Use only the Python standard "
        "library."
    ),
    "medium_roman": (
        "Create a file named sol_roman.py containing two functions: "
        "int_to_roman(n: int) -> str for 1 <= n <= 3999 using standard "
        "subtractive notation, and roman_to_int(s: str) -> int which "
        "parses such numerals back. roman_to_int must raise ValueError on "
        "an empty string. Use only the Python standard library."
    ),
    "medium_flatten": (
        "Create a file named sol_flatten.py containing a function "
        "flatten(nested: list) -> list. It must return a new flat list "
        "containing the non-list elements of nested in order, recursing "
        "into lists at any depth. Other types (str, int, tuple, ...) are "
        "kept as-is; tuples are not recursed into. Use only the Python "
        "standard library."
    ),
    "medium_group_anagrams": (
        "Create a file named sol_group_anagrams.py containing a function "
        "group_anagrams(words: list) -> list. It must group the words by "
        "their sorted letters (case-insensitive), and return the groups "
        "sorted by their alphabetically smallest member, with members "
        "inside each group sorted alphabetically (case-insensitive). Use "
        "only the Python standard library."
    ),
    "medium_two_sum": (
        "Create a file named sol_two_sum.py containing a function "
        "two_sum(nums: list, target: int) -> tuple. It must return the "
        "0-based indices (i, j) with i < j and nums[i] + nums[j] == "
        "target, choosing the smallest i first and then the smallest j. "
        "If no pair exists it must return None. Use only the Python "
        "standard library."
    ),
}

HARD_PROMPTS = {
    "hard_topo_sort": (
        "Create a file named sol_topo_sort.py containing a function "
        "topological_order(nodes: list, edges: list) -> list. nodes is a "
        "list of hashable items, edges is a list of (before, after) "
        "pairs. The function must return one valid topological order of "
        "nodes; when two orders are possible it must prefer smaller items "
        "first (deterministic lexicographic tie-break via a min-heap). "
        "When the graph contains a cycle it must raise ValueError. Use "
        "only the Python standard library."
    ),
    "hard_rate_limiter": (
        "Create a file named sol_rate_limiter.py containing a class "
        "TokenBucket with __init__(self, rate: float, capacity: float) "
        "and allow(self, ts: float) -> bool. rate is tokens added per "
        "second, capacity the bucket size. The bucket starts full. allow "
        "must refill proportionally based on elapsed time since the last "
        "call (capped at capacity), consume one token when available and "
        "return True, else return False. Calls may arrive with "
        "non-monotonic ts; ignore ts values older than the last seen one "
        "(return the previous decision's rule: still consume nothing and "
        "return False). Use only the Python standard library."
    ),
    "hard_dijkstra": (
        "Create a file named sol_dijkstra.py containing a function "
        "shortest_path(graph: dict, start, goal) -> tuple. graph maps "
        "each node to a dict of neighbor -> positive edge weight. The "
        "function must return (cost, path) where path is the list of "
        "nodes from start to goal; among equal-cost paths it must return "
        "the lexicographically smallest path (compare the list of node "
        "names). When goal is unreachable it must return (float('inf'), "
        "[]). Use heapq. Use only the Python standard library."
    ),
    "hard_bst": (
        "Create a file named sol_bst.py containing a class BST with "
        "__init__(self), insert(self, key), contains(self, key) -> bool "
        "and inorder(self) -> list. It must behave like a binary search "
        "tree: duplicates are ignored (insert does not add a second copy), "
        "inorder returns the keys in ascending order, and contains works "
        "on an empty tree as False. Use only the Python standard library."
    ),
    "hard_longest_substr": (
        "Create a file named sol_longest_substr.py containing a function "
        "longest_unique_substr(s: str) -> int. It must return the length "
        "of the longest contiguous substring of s without repeating "
        "characters, case-sensitive. It must run in linear time via a "
        "sliding window and handle the empty string. Use only the Python "
        "standard library."
    ),
    "hard_matrix_rotate": (
        "Create a file named sol_matrix_rotate.py containing a function "
        "rotate_cw(matrix: list) -> list. It must return a NEW matrix "
        "that is the input rotated 90 degrees clockwise; the input must "
        "not be mutated. Non-square matrices are allowed (an m x n matrix "
        "becomes n x m). Raise ValueError when the matrix is empty or has "
        "ragged rows. Use only the Python standard library."
    ),
    "hard_word_ladder": (
        "Create a file named sol_word_ladder.py containing a function "
        "ladder_length(begin: str, end: str, word_list: list) -> int. "
        "Each step changes exactly one letter and must produce a word "
        "that is in word_list (case-sensitive; begin itself may or may "
        "not be in the list). The function must return the number of "
        "words on the shortest transformation sequence from begin to end "
        "including both endpoints, or 0 when end is unreachable. Words "
        "all have equal length. Use BFS with a set. Use only the Python "
        "standard library."
    ),
    "hard_tokenizer": (
        "Create a file named sol_tokenizer.py containing a function "
        "tokenize(expr: str) -> list. It must tokenize a simple "
        "arithmetic expression: non-negative numbers with an optional "
        "decimal part become ('NUM', float_value), the symbols + - * / ( "
        ") become one-character tuples ('+', 1) style tokens where the "
        "second element is the integer 1, whitespace is skipped, and any "
        "other character must raise ValueError. Example: tokenize('1.5 + "
        "2*(3-1)') must yield [('NUM', 1.5), ('+', 1), ('NUM', 2.0), "
        "('*', 1), ('(', 1), ('NUM', 3.0), ('-', 1), ('NUM', 1.0), (')', "
        "1)]. Use only the Python standard library."
    ),
}

_PROMPTS: dict[str, dict[str, str]] = {
    "sync": SYNC_PROMPTS,
    "easy": EASY_PROMPTS,
    "medium": MEDIUM_PROMPTS,
    "hard": HARD_PROMPTS,
}

_FILES: dict[str, list[str]] = {
    task_id: [f"sol_{task_id.split('_', 1)[1]}.py"] for task_id in
    {**EASY_PROMPTS, **MEDIUM_PROMPTS, **HARD_PROMPTS, **SYNC_PROMPTS}
}


def load_tasks(tier: str | None = None) -> list[BenchTask]:
    """Return all bench tasks, optionally filtered by tier."""
    out: list[BenchTask] = []
    # The sync tier is opt-in: the default suite stays the 24-task baseline
    # so historical runs remain comparable (sync runs pass tier="sync").
    tier_names = ("sync",) if tier == "sync" else ("easy", "medium", "hard")
    for tier_name in tier_names:
        if tier is not None and tier_name != tier:
            continue
        for task_id, prompt in _PROMPTS[tier_name].items():
            out.append(
                BenchTask(
                    id=task_id,
                    tier=tier_name,  # type: ignore[arg-type]
                    prompt=prompt,
                    files=_FILES[task_id],
                    checker=f"check_{task_id}",
                    amendment=SYNC_AMENDMENTS.get(task_id, ""),
                )
            )
    return out


def get_task(task_id: str) -> BenchTask:
    """Return the task with the given id (any tier, including opt-in sync)."""
    for task in load_tasks(None) + load_tasks("sync"):
        if task.id == task_id:
            return task
    raise KeyError(f"unknown bench task: {task_id}")