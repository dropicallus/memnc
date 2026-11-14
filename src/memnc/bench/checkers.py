"""Deterministic checkers for the bench task set.

Every check_<task_id> function receives the workspace directory containing
the files the agent wrote, returns a list of failure strings, and passes
when the list is empty. Reference solutions live in memnc.bench.solutions
and are exercised by tests/test_bench.py so the checkers are provably
correct.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load(workdir: Path, filename: str) -> ModuleType:
    """Import the produced module; raise FileNotFoundError when missing."""
    path = Path(workdir) / filename
    if not path.is_file():
        raise FileNotFoundError(filename)
    spec = importlib.util.spec_from_file_location(filename[:-3], path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(checker_name: str, workdir: Path) -> list[str]:
    """Run the named check_* function and normalize its outcome."""
    func = globals().get(checker_name)
    if func is None:
        return [f"unknown checker: {checker_name}"]
    try:
        failures = func(Path(workdir))
    except FileNotFoundError as exc:
        return [f"missing file: {exc}"]
    except Exception as exc:  # checker import/runtime errors are failures
        return [f"checker error: {type(exc).__name__}: {exc}"]
    return list(failures)


# ---------------------------------------------------------------- easy ----


def check_easy_palindrome(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_palindrome.py")
    f = mod.is_palindrome
    cases = [
        ("", True),
        ("a", True),
        ("aa", True),
        ("ab", False),
        ("racecar", True),
        ("A man, a plan, a canal: Panama", True),
        ("No lemon, no melon", True),
        ("Hello, World", False),
        ("0P", False),
        ("Able was I, ere I saw Elba", True),
    ]
    return [
        f"is_palindrome({s!r}) == {want}, got {f(s)!r}"
        for s, want in cases
        if f(s) is not want and f(s) != want
    ]


def check_easy_fizzbuzz(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_fizzbuzz.py")
    f = mod.fizzbuzz
    if f(0) != []:
        return ["fizzbuzz(0) must be []"]
    if f(1) != ["1"]:
        return ["fizzbuzz(1) must be ['1']"]
    got = f(15)
    want = [
        "1", "2", "Fizz", "4", "Buzz", "Fizz", "7", "8", "Fizz", "Buzz",
        "11", "Fizz", "13", "14", "FizzBuzz",
    ]
    if got != want:
        return [f"fizzbuzz(15) mismatch: got {got!r}"]
    return []


def check_easy_count_words(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_count_words.py")
    f = mod.count_words
    got = f("The cat and the Cat, sat! sat? sat.")
    want = {"the": 2, "cat": 2, "and": 1, "sat": 3}
    if got != want:
        return [f"count_words mismatch: got {got!r}, want {want!r}"]
    if f("") != {}:
        return ["count_words('') must be {}"]
    return []


def check_easy_reverse_words(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_reverse_words.py")
    f = mod.reverse_words
    cases = [
        ("hello world", "world hello"),
        ("  spaced   out  ", "out spaced"),
        ("one", "one"),
        ("", ""),
    ]
    return [
        f"reverse_words({s!r}) == {want!r}, got {f(s)!r}"
        for s, want in cases
        if f(s) != want
    ]


def check_easy_fibonacci(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_fibonacci.py")
    f = mod.fib
    want = [0, 1, 1, 2, 3, 5, 8, 13, 21, 34, 55]
    got = [f(n) for n in range(11)]
    if got != want:
        return [f"fib sequence mismatch: got {got!r}"]
    if f(100) != 354224848179261915075:
        return ["fib(100) wrong — solution is not linear/poly time"]
    return []


def check_easy_anagram(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_anagram.py")
    f = mod.is_anagram
    cases = [
        ("Listen", "Silent", True),
        ("Dormitory", "Dirty room", True),
        ("abc", "abd", False),
        ("a", "a a", False),
        ("", "", True),
    ]
    return [
        f"is_anagram({a!r}, {b!r}) == {want}, got {f(a, b)!r}"
        for a, b, want in cases
        if f(a, b) != want
    ]


def check_easy_clamp(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_clamp.py")
    f = mod.clamp
    cases = [(5, 0, 10, 5), (-1, 0, 10, 0), (11, 0, 10, 10), (0, 0, 10, 0)]
    return [
        f"clamp{args} == {want}, got {f(*args)!r}"
        for args, want in [(c[:3], c[3]) for c in cases]
        if f(*args) != want
    ]


def check_easy_digit_sum(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_digit_sum.py")
    f = mod.digit_sum
    cases = [(0, 0), (5, 5), (123, 6), (-123, 6), (999999, 54)]
    return [
        f"digit_sum({n}) == {want}, got {f(n)!r}"
        for n, want in cases
        if f(n) != want
    ]


# -------------------------------------------------------------- medium ----


def check_medium_brackets(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_brackets.py")
    f = mod.is_balanced
    cases = [
        ("", True),
        ("()", True),
        ("([]{})", True),
        ("([)]", False),
        ("(((", False),
        (")", False),
        ("a(b[c]d)e", True),
        ("}{", False),
    ]
    return [
        f"is_balanced({s!r}) == {want}, got {f(s)!r}"
        for s, want in cases
        if f(s) != want
    ]


def check_medium_lru_cache(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_lru_cache.py")
    cache = mod.LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    if cache.get("a") != 1:
        return ["get('a') must be 1 after puts"]
    cache.put("c", 3)  # evicts 'b'
    if cache.get("b") is not None:
        return ["least recently used key 'b' was not evicted"]
    if cache.get("a") != 1 or cache.get("c") != 3:
        return ["keys 'a'/'c' lost after eviction"]
    cache.put("a", 9)  # refresh existing key
    cache.put("c", 4)  # evicts nothing new, 'a' was refreshed
    if cache.get("a") != 9:
        return ["put on existing key must refresh and overwrite"]
    small = mod.LRUCache(1)
    small.put("x", 1)
    small.put("y", 2)
    if small.get("x") is not None or small.get("y") != 2:
        return ["capacity-1 cache evicts wrong key"]
    return []


def check_medium_moving_average(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_moving_average.py")
    m = mod.MovingAverage(3)
    got = [m.next(v) for v in (1, 2, 3, 4)]
    want = [1.0, 1.5, 2.0, 3.0]
    if got != want:
        return [f"moving average mismatch: got {got!r}, want {want!r}"]
    m1 = mod.MovingAverage(1)
    if m1.next(7) != 7.0:
        return ["size-1 window must echo the value"]
    return []


def check_medium_csv_stats(workdir: Path) -> list[str]:
    import csv
    import io

    mod = _load(workdir, "sol_csv_stats.py")
    f = mod.column_stats
    text = "name,score,count\nalpha,10,1\nbeta,20,2\ngamma,30,3\n"
    got = f(text, "score")
    want = {"mean": 20.0, "min": 10.0, "max": 30.0}
    if got != want:
        return [f"column_stats mismatch: got {got!r}, want {want!r}"]
    if f(text, "count") != {"mean": 2.0, "min": 1.0, "max": 3.0}:
        return ["column 'count' stats wrong"]
    # csv module must be used: a quoted field containing a comma breaks
    # naive splits.
    quoted = 'name,score\n"x,y",5\n'
    next(csv.reader(io.StringIO(quoted)))
    if f(quoted, "score") != {"mean": 5.0, "min": 5.0, "max": 5.0}:
        return ["quoted comma fields not handled — use the csv module"]
    try:
        f(text, "missing")
    except KeyError:
        return []
    return ["missing column must raise KeyError"]


def check_medium_roman(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_roman.py")
    to_roman, from_roman = mod.int_to_roman, mod.roman_to_int
    cases = [(1, "I"), (4, "IV"), (9, "IX"), (14, "XIV"), (40, "XL"),
             (90, "XC"), (400, "CD"), (1994, "MCMXCIV"), (3999, "MMMCMXCIX")]
    fails = [
        f"int_to_roman({n}) == {r}, got {to_roman(n)!r}"
        for n, r in cases
        if to_roman(n) != r
    ]
    fails += [
        f"roman_to_int({r!r}) == {n}, got {from_roman(r)!r}"
        for n, r in cases
        if from_roman(r) != n
    ]
    for n in range(1, 4000):
        if from_roman(to_roman(n)) != n:
            fails.append(f"roundtrip failed for {n}")
            break
    try:
        from_roman("")
    except ValueError:
        return fails
    fails.append("roman_to_int('') must raise ValueError")
    return fails


def check_medium_flatten(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_flatten.py")
    f = mod.flatten
    cases = [
        ([1, [2, [3, 4]], 5], [1, 2, 3, 4, 5]),
        ([], []),
        ([[[[1]]]], [1]),
        ([1, ("a", "b")], [1, ("a", "b")]),
        (["x", ["y", ["z"]]], ["x", "y", "z"]),
    ]
    fails = [
        f"flatten({n!r}) == {want!r}, got {f(n)!r}"
        for n, want in cases
        if f(n) != want
    ]
    nested = [1, [2]]
    f(nested)
    if nested != [1, [2]]:
        fails.append("flatten must not mutate its input")
    return fails


def check_medium_group_anagrams(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_group_anagrams.py")
    f = mod.group_anagrams
    got = f(["eat", "Tea", "tan", "ate", "nat", "bat"])
    want = [["ate", "eat", "Tea"], ["bat"], ["nat", "tan"]]
    if got != want:
        return [f"group_anagrams mismatch: got {got!r}, want {want!r}"]
    if f([]) != []:
        return ["group_anagrams([]) must be []"]
    return []


def check_medium_two_sum(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_two_sum.py")
    f = mod.two_sum
    cases = [
        ([2, 7, 11, 15], 9, (0, 1)),
        ([3, 2, 4], 6, (1, 2)),
        ([3, 3], 6, (0, 1)),
        ([1, 2, 3], 100, None),
        ([5, 1, 5, 1], 10, (0, 2)),
    ]
    return [
        f"two_sum({n!r}, {t}) == {want!r}, got {f(n, t)!r}"
        for n, t, want in cases
        if f(n, t) != want
    ]


# ---------------------------------------------------------------- hard ----


def check_hard_topo_sort(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_topo_sort.py")
    f = mod.topological_order
    got = f(["b", "a", "c"], [("a", "b"), ("a", "c")])
    if got != ["a", "b", "c"]:
        return [f"lexicographic order required: got {got!r}"]
    got = f(["c", "b", "a"], [("b", "c"), ("a", "c")])
    if got != ["a", "b", "c"]:
        return [f"topological constraint violated: got {got!r}"]
    if f(["x"], []) != ["x"]:
        return ["single node graph must return [node]"]
    try:
        f(["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "a")])
    except ValueError:
        return []
    return ["cycle must raise ValueError"]


def check_hard_rate_limiter(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_rate_limiter.py")
    # rate 1/s, capacity 2, starts full.
    tb = mod.TokenBucket(rate=1.0, capacity=2.0)
    got = [tb.allow(0.0), tb.allow(0.0), tb.allow(0.0)]
    if got != [True, True, False]:
        return [f"burst of 2 expected [True, True, False], got {got!r}"]
    if not tb.allow(1.0):
        return ["after 1s at rate 1 one token must have refilled"]
    if tb.allow(1.5):
        return ["only one token can refill between calls at t=1.0/1.5"]
    if not tb.allow(5.0):
        return ["refill must cap at capacity, but 4s must refill >= 1"]
    # Non-monotonic ts: older than last seen must be refused.
    tb2 = mod.TokenBucket(rate=10.0, capacity=10.0)
    tb2.allow(100.0)
    if tb2.allow(50.0):
        return ["ts older than the last seen call must return False"]
    return []


def check_hard_dijkstra(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_dijkstra.py")
    f = mod.shortest_path
    g = {
        "a": {"b": 1, "c": 4},
        "b": {"c": 2, "d": 5},
        "c": {"d": 1},
        "d": {},
        "e": {},
    }
    cost, path = f(g, "a", "d")
    if cost != 4 or path != ["a", "b", "c", "d"]:
        return [f"shortest a->d must be (4, a-b-c-d), got {(cost, path)!r}"]
    cost, path = f(g, "a", "e")
    if cost != float("inf") or path != []:
        return [f"unreachable must be (inf, []), got {(cost, path)!r}"]
    cost, path = f(g, "a", "a")
    if cost != 0 or path != ["a"]:
        return [f"start == goal must be (0, [start]), got {(cost, path)!r}"]
    # Lexicographic tie-break: a->b (1) and a->z (1) both reach goal.
    g2 = {"s": {"b": 1, "a": 1}, "b": {"t": 1}, "a": {"t": 1}, "t": {}}
    _, path = f(g2, "s", "t")
    if path != ["s", "a", "t"]:
        return [f"equal-cost tie must prefer lexicographic path, got {path!r}"]
    return []


def check_hard_bst(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_bst.py")
    t = mod.BST()
    for k in (5, 3, 7, 3, 8, 5):
        t.insert(k)
    if t.inorder() != [3, 5, 7, 8]:
        return [f"inorder must dedupe and sort: got {t.inorder()!r}"]
    if not (t.contains(5) and t.contains(8) and not t.contains(1)):
        return ["contains wrong on populated tree"]
    empty = mod.BST()
    if empty.inorder() != [] or empty.contains(1):
        return ["empty tree must have [] and contains() == False"]
    return []


def check_hard_longest_substr(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_longest_substr.py")
    f = mod.longest_unique_substr
    cases = [
        ("", 0),
        ("a", 1),
        ("abcabcbb", 3),
        ("bbbbb", 1),
        ("pwwkew", 3),
        ("AbBa", 4),
        ("dvdf", 3),
        ("abba", 2),
    ]
    return [
        f"longest_unique_substr({s!r}) == {want}, got {f(s)!r}"
        for s, want in cases
        if f(s) != want
    ]


def check_hard_matrix_rotate(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_matrix_rotate.py")
    f = mod.rotate_cw
    got = f([[1, 2, 3], [4, 5, 6]])
    if got != [[4, 1], [5, 2], [6, 3]]:
        return [f"rotate 2x3 wrong: got {got!r}"]
    got = f([[1]])
    if got != [[1]]:
        return ["1x1 rotation changed the matrix"]
    src = [[1, 2], [3, 4]]
    _ = f(src)
    if src != [[1, 2], [3, 4]]:
        return ["input matrix must not be mutated"]
    for bad in ([], [[1, 2], [3]]):
        try:
            f(bad)
        except ValueError:
            continue
        return [f"ragged/empty input must raise ValueError: {bad!r}"]
    return []


def check_hard_word_ladder(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_word_ladder.py")
    f = mod.ladder_length
    words = ["hot", "dot", "dog", "lot", "log", "cog"]
    if f("hit", "cog", words) != 5:
        return ["hit->hot->dot->dog->cog is 5 words"]
    if f("hit", "cog", ["hot", "dot", "dog", "lot", "log"]) != 0:
        return ["unreachable end must return 0"]
    if f("hit", "hot", words) != 2:
        return ["direct one-letter neighbor must be 2"]
    return []


def check_hard_tokenizer(workdir: Path) -> list[str]:
    mod = _load(workdir, "sol_tokenizer.py")
    f = mod.tokenize
    got = f("1.5 + 2*(3-1)")
    want = [
        ("NUM", 1.5), ("+", 1), ("NUM", 2.0), ("*", 1), ("(", 1),
        ("NUM", 3.0), ("-", 1), ("NUM", 1.0), (")", 1),
    ]
    if got != want:
        return [f"tokenize('1.5 + 2*(3-1)') mismatch: got {got!r}"]
    if f("42") != [("NUM", 42.0)]:
        return ["integer literal must become ('NUM', 42.0)"]
    if f(" \t ") != []:
        return ["whitespace-only input must be an empty token list"]
    try:
        f("1 # 2")
    except ValueError:
        return []
    return ["illegal characters must raise ValueError"]