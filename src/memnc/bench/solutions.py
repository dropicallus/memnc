"""Reference solutions for the bench task set.

These exist to prove the checkers accept correct code and reject wrong
code (tests/test_bench.py). They are not meant to be elegant.
"""

from __future__ import annotations


def sol_palindrome():
    def is_palindrome(s):
        t = [c.lower() for c in s if c.isalnum()]
        return t == t[::-1]

    return is_palindrome


def sol_fizzbuzz():
    def fizzbuzz(n):
        out = []
        for i in range(1, n + 1):
            if i % 15 == 0:
                out.append("FizzBuzz")
            elif i % 3 == 0:
                out.append("Fizz")
            elif i % 5 == 0:
                out.append("Buzz")
            else:
                out.append(str(i))
        return out

    return fizzbuzz


def sol_count_words():
    import string

    def count_words(text):
        counts = {}
        for tok in text.lower().split():
            tok = tok.strip(".,!?;:")
            if tok:
                counts[tok] = counts.get(tok, 0) + 1
        return counts

    del string
    return count_words


def sol_reverse_words():
    def reverse_words(s):
        return " ".join(reversed(s.split()))

    return reverse_words


def sol_fibonacci():
    def fib(n):
        a, b = 0, 1
        for _ in range(n):
            a, b = b, a + b
        return a

    return fib


def sol_anagram():
    def is_anagram(a, b):
        x = [c for c in a.lower() if c != " "]
        y = [c for c in b.lower() if c != " "]
        return sorted(x) == sorted(y)

    return is_anagram


def sol_clamp():
    def clamp(v, lo, hi):
        return max(lo, min(hi, v))

    return clamp


def sol_digit_sum():
    def digit_sum(n):
        return sum(int(d) for d in str(abs(n)))

    return digit_sum


def sol_brackets():
    def is_balanced(s):
        pairs = {")": "(", "]": "[", "}": "{"}
        stack = []
        for c in s:
            if c in "([{":
                stack.append(c)
            elif c in pairs:
                if not stack or stack.pop() != pairs[c]:
                    return False
        return not stack

    return is_balanced


def sol_lru_cache():
    from collections import OrderedDict

    class LRUCache:
        def __init__(self, capacity):
            self.cap = capacity
            self.data = OrderedDict()

        def get(self, key):
            if key not in self.data:
                return None
            self.data.move_to_end(key)
            return self.data[key]

        def put(self, key, value):
            if key in self.data:
                self.data.move_to_end(key)
            self.data[key] = value
            if len(self.data) > self.cap:
                self.data.popitem(last=False)

    return LRUCache


def sol_moving_average():
    from collections import deque

    class MovingAverage:
        def __init__(self, size):
            self.size = size
            self.window = deque()

        def next(self, val):
            self.window.append(val)
            if len(self.window) > self.size:
                self.window.popleft()
            return sum(self.window) / len(self.window)

    return MovingAverage


def sol_csv_stats():
    import csv
    import io

    def column_stats(csv_text, column):
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        if column not in rows[0]:
            raise KeyError(column)
        vals = [float(r[column]) for r in rows]
        return {"mean": sum(vals) / len(vals), "min": min(vals), "max": max(vals)}

    return column_stats


def sol_roman():
    VALUES = [
        (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
        (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
        (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
    ]

    def int_to_roman(n):
        out = []
        for val, sym in VALUES:
            while n >= val:
                out.append(sym)
                n -= val
        return "".join(out)

    def roman_to_int(s):
        if not s:
            raise ValueError("empty roman numeral")
        lookup = {sym: val for val, sym in VALUES}
        total = 0
        for i, c in enumerate(s):
            v = lookup[c]
            if i + 1 < len(s) and lookup[s[i + 1]] > v:
                total -= v
            else:
                total += v
        return total

    return int_to_roman, roman_to_int


def sol_flatten():
    def flatten(nested):
        out = []
        for item in nested:
            if isinstance(item, list):
                out.extend(flatten(item))
            else:
                out.append(item)
        return out

    return flatten


def sol_group_anagrams():
    def group_anagrams(words):
        groups = {}
        for w in words:
            key = "".join(sorted(w.lower()))
            groups.setdefault(key, []).append(w)
        result = [sorted(g, key=str.lower) for g in groups.values()]
        result.sort(key=lambda g: g[0].lower())
        return result

    return group_anagrams


def sol_two_sum():
    def two_sum(nums, target):
        seen = {}
        for j, v in enumerate(nums):
            i = seen.get(target - v)
            if i is not None:
                return (i, j)
            if v not in seen:
                seen[v] = j
        return None

    return two_sum


def sol_topo_sort():
    import heapq

    def topological_order(nodes, edges):
        indeg = {n: 0 for n in nodes}
        adj = {n: [] for n in nodes}
        for before, after in edges:
            adj[before].append(after)
            indeg[after] += 1
        heap = [n for n in nodes if indeg[n] == 0]
        heapq.heapify(heap)
        out = []
        while heap:
            n = heapq.heappop(heap)
            out.append(n)
            for m in sorted(adj[n]):
                indeg[m] -= 1
                if indeg[m] == 0:
                    heapq.heappush(heap, m)
        if len(out) != len(nodes):
            raise ValueError("cycle detected")
        return out

    return topological_order


def sol_rate_limiter():
    class TokenBucket:
        def __init__(self, rate, capacity):
            self.rate = rate
            self.capacity = capacity
            self.tokens = capacity
            self.last = None

        def allow(self, ts):
            if self.last is not None and ts < self.last:
                return False
            if self.last is not None:
                self.tokens = min(self.capacity, self.tokens + (ts - self.last) * self.rate)
            self.last = ts
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False

    return TokenBucket


def sol_dijkstra():
    import heapq

    def shortest_path(graph, start, goal):
        best = {start: 0}
        # heap entries: (cost, path) so ties break lexicographically
        heap = [(0, [start])]
        while heap:
            cost, path = heapq.heappop(heap)
            node = path[-1]
            if node == goal:
                return (cost, path)
            for nxt, w in graph.get(node, {}).items():
                nc = cost + w
                if nc < best.get(nxt, float("inf")):
                    best[nxt] = nc
                    heapq.heappush(heap, (nc, path + [nxt]))
        return (float("inf"), [])

    return shortest_path


def sol_bst():
    class BST:
        def __init__(self):
            self.root = None

        def insert(self, key):
            def _ins(node, key):
                if node is None:
                    return [key, None, None]
                if key < node[0]:
                    node[1] = _ins(node[1], key)
                elif key > node[0]:
                    node[2] = _ins(node[2], key)
                return node

            self.root = _ins(self.root, key)

        def contains(self, key):
            node = self.root
            while node is not None:
                if key == node[0]:
                    return True
                node = node[1] if key < node[0] else node[2]
            return False

        def inorder(self):
            out = []

            def _walk(node):
                if node is None:
                    return
                _walk(node[1])
                out.append(node[0])
                _walk(node[2])

            _walk(self.root)
            return out

    return BST


def sol_longest_substr():
    def longest_unique_substr(s):
        last = {}
        start = 0
        best = 0
        for i, c in enumerate(s):
            if c in last and last[c] >= start:
                start = last[c] + 1
            last[c] = i
            best = max(best, i - start + 1)
        return best

    return longest_unique_substr


def sol_matrix_rotate():
    def rotate_cw(matrix):
        if not matrix:
            raise ValueError("empty matrix")
        width = len(matrix[0])
        if any(len(row) != width for row in matrix):
            raise ValueError("ragged rows")
        return [list(row) for row in zip(*matrix[::-1], strict=True)]

    return rotate_cw


def sol_word_ladder():
    from collections import deque

    def ladder_length(begin, end, word_list):
        words = set(word_list)
        if end not in words:
            return 0
        queue = deque([(begin, 1)])
        seen = {begin}
        while queue:
            word, depth = queue.popleft()
            if word == end:
                return depth
            for i in range(len(word)):
                for c in "abcdefghijklmnopqrstuvwxyz":
                    nxt = word[:i] + c + word[i + 1:]
                    if nxt in words and nxt not in seen:
                        seen.add(nxt)
                        queue.append((nxt, depth + 1))
        return 0

    return ladder_length


def sol_tokenizer():
    def tokenize(expr):
        tokens = []
        i = 0
        while i < len(expr):
            c = expr[i]
            if c.isspace():
                i += 1
                continue
            if c.isdigit() or c == ".":
                j = i
                while j < len(expr) and (expr[j].isdigit() or expr[j] == "."):
                    j += 1
                tokens.append(("NUM", float(expr[i:j])))
                i = j
            elif c in "+-*/()":
                tokens.append((c, 1))
                i += 1
            else:
                raise ValueError(f"illegal character {c!r} at {i}")
        return tokens

    return tokenize


# ----------------------------------------------------------------- sync ----


def sol_rate_limit():
    def can_send(sent_today, limit, total_sent):
        return sent_today < limit and total_sent < 10

    return can_send


def sol_user_label():
    def user_label(user):
        name = user.get("name") or ""
        return name if name else "unknown"

    return user_label


def sol_apply_ops():
    def apply_ops(state, ops):
        out = dict(state)
        seen = set()
        for op in ops:
            op_id = op.get("op_id")
            if op_id is not None:
                if op_id in seen:
                    continue
                seen.add(op_id)
            if op["op"] == "set":
                out[op["key"]] = op["value"]
            else:
                out[op["key"]] = out.get(op["key"], 0) + op["value"]
        return out

    return apply_ops


def sol_convert():
    def convert(cents, rate):
        return round(cents / 100 * rate, 2)

    return convert


# Public names each solution module must expose, by task id.
SOL_NAMES: dict[str, tuple[str, ...]] = {
    "easy_palindrome": ("is_palindrome",),
    "easy_fizzbuzz": ("fizzbuzz",),
    "easy_count_words": ("count_words",),
    "easy_reverse_words": ("reverse_words",),
    "easy_fibonacci": ("fib",),
    "easy_anagram": ("is_anagram",),
    "easy_clamp": ("clamp",),
    "easy_digit_sum": ("digit_sum",),
    "medium_brackets": ("is_balanced",),
    "medium_lru_cache": ("LRUCache",),
    "medium_moving_average": ("MovingAverage",),
    "medium_csv_stats": ("column_stats",),
    "medium_roman": ("int_to_roman", "roman_to_int"),
    "medium_flatten": ("flatten",),
    "medium_group_anagrams": ("group_anagrams",),
    "medium_two_sum": ("two_sum",),
    "hard_topo_sort": ("topological_order",),
    "hard_rate_limiter": ("TokenBucket",),
    "hard_dijkstra": ("shortest_path",),
    "hard_bst": ("BST",),
    "hard_longest_substr": ("longest_unique_substr",),
    "hard_matrix_rotate": ("rotate_cw",),
    "hard_word_ladder": ("ladder_length",),
    "hard_tokenizer": ("tokenize",),
    "sync_rate_limit": ("can_send",),
    "sync_user_label": ("user_label",),
    "sync_apply_ops": ("apply_ops",),
    "sync_convert": ("convert",),
}