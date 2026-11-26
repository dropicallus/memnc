from memnc.roadmap import missing, open_tasks, parse_roadmap

SAMPLE = """# Roadmap

## E1 Substrate and mesh core

| ID | Task | Status | Evidence / log |
|---|---|---|---|
| T1.1 | Bus and FSM | ✅ | M8 |
| T1.4 | node-c back on the bus | 🟡 | 15.4.2 |
| T1.6 | VRAM as a schedulable resource | ☐ | design note |

## E2 Harness layer

| ID | Task | Status | Evidence / log |
|---|---|---|---|
| T2.6 | Foreign-harness approval routing | ☐ | P5.5 (`§19.2`) |
"""


def test_parse_reads_every_row_with_its_epic():
    rows = parse_roadmap(SAMPLE)
    assert [r.task_id for r in rows] == ["T1.1", "T1.4", "T1.6", "T2.6"]
    assert rows[0].status == "✅"
    assert rows[1].epic == "E1 Substrate and mesh core"
    assert rows[3].epic == "E2 Harness layer"
    assert rows[2].evidence == "design note"


def test_open_tasks_skips_done_rows():
    assert [r.task_id for r in open_tasks(SAMPLE)] == ["T1.4", "T1.6", "T2.6"]


def test_prompt_carries_epic_and_evidence():
    row = open_tasks(SAMPLE)[2]
    prompt = row.backlog_prompt()
    assert "E2 Harness layer" in prompt
    assert "Foreign-harness approval routing" in prompt
    assert "§19.2" in prompt
    assert "Context from the roadmap" in prompt
    assert row.backlog_title().startswith("[T2.6] ")


def test_missing_is_idempotent_by_marker():
    rows = open_tasks(SAMPLE)
    existing = ["[T1.4] node-c back on the bus"]
    assert [r.task_id for r in missing(rows, existing)] == ["T1.6", "T2.6"]
    assert missing(rows, ["[T1.4] x", "[T1.6] y", "[T2.6] z"]) == []


def test_header_and_separator_rows_are_ignored():
    rows = parse_roadmap(SAMPLE)
    assert all(r.task_id.startswith("T") for r in rows)
    assert len(rows) == 4
