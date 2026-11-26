from memnc.schema import resolve_request_id

IDS = ["mreq-aaaa1111", "mreq-aaaa2222", "mreq-bbbb3333"]


def test_exact_id_wins():
    assert resolve_request_id("mreq-bbbb3333", IDS) == ("mreq-bbbb3333", [])


def test_unique_prefix_resolves():
    assert resolve_request_id("mreq-bbbb", IDS) == ("mreq-bbbb3333", [])


def test_empty_prefix_never_matches_the_first_request():
    assert resolve_request_id("", IDS) == ("", [])
    assert resolve_request_id("#", IDS) == ("", [])


def test_short_prefix_is_refused():
    assert resolve_request_id("m", IDS) == ("", [])
    assert resolve_request_id("mreq-", IDS) == ("", [])


def test_ambiguous_prefix_lists_candidates():
    rid, candidates = resolve_request_id("mreq-aaaa", IDS)
    assert rid == ""
    assert candidates == ["mreq-aaaa1111", "mreq-aaaa2222"]
