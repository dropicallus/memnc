# Design: ACE-style automated experience store (roadmap 15.3 item 5)

Source evidence: ACE (ICLR 2026) — context as an append-only, itemized
bullet store with a deterministic merge; two named failure modes it guards:
**brevity bias** (an incremental compression step over-compresses, losing
operational detail) and **context collapse** (duplication accumulates until
the context drowns in redundant bullets). In memnc, `docs/experience.md` is
the manual first instance of the Erfahrungsspeicher; this slice makes it a
mechanism a program can write to.

## Shape

- `src/memnc/experience.py` — pure-logic store, no NATS dependency. File
  backed (JSON) so it works on any node without a broker; KV wiring is a
  later step.
- Entries are grouped by **topic** (e.g. `bench-arm-A`, `bench-arm-B`). Each
  bullet is one self-contained sentence with a stable id
  (`{topic}-{seq}`), an optional source tag, and an append position. The
  store is append-only: bullets are never rewritten, only added or
  (explicitly) merged away by the guards below.

## Deterministic merge with the two ACE guards

`merge(topic, bullets)` folds new bullets into a topic:

1. **Normalize** each bullet (strip, collapse whitespace, lowercase for
   comparison only — stored text is preserved verbatim).
2. **Context-collapse guard**: a new bullet is a near-duplicate when its
   Jaccard token overlap with an existing bullet is >= 0.8. The *existing*
   bullet wins, and when both candidates differ in length the longer one is
   kept (a longer phrasing carries more operational detail). Exact
   duplicates are always dropped.
3. **Monotone merge**: merge never shrinks a topic — near-duplicate
   resolution is one-for-one (a longer new bullet replaces the existing
   one; a shorter/equal new bullet is folded away). Real compression is
   the separate explicit step `compress(topic, bullets)`, which replaces a
   whole topic and is guarded: rejected unless it keeps at least
   `(1 - MAX_PRUNE_RATIO)` of the old bullet count (brevity-bias guard —
   compression in small deliberate steps, never one sweep). An LLM
   summarization step sits outside the store and calls `compress`.
4. New bullets are appended in the order given, ids continue the topic's
   sequence. Merge is a pure function of (existing, new): same inputs, same
   output, no timestamps involved.

## Producers (this slice)

- `record_bench_run(jsonl_path, store)` turns a bench JSONL into per-arm
  bullets (success counts per tier, mean tokens/elapsed) and appends one
  failure bullet per failed task (`task_id` + issues/error text). This is
  the first automated producer; the roadmap wires further producers
  (observation deposits, task failures) later.

## Non-goals

- No LLM calls in the store itself — merging is deterministic text
  operations only. An LLM summarization step would sit *outside* the store
  and must respect the brevity-bias guard ratio.
- No KV/NATS wiring in this slice (later: a `meshctl experience-list` read
  verb once a node-agnostic location is chosen).
- `docs/experience.md` stays human-curated and English-only; the automated
  store is a separate artifact, not a rewrite of that file.
