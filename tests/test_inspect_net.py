"""Tests for inspect_net pure logic (no network access)."""

from __future__ import annotations

from xahaud_scripts.inspect_net import amendments as amd
from xahaud_scripts.inspect_net.crawl import (
    Crawler,
    CrawlResult,
    Node,
    coerce_port,
    parse_seed,
    release_date,
    short_version,
)

# --- crawl: seed/version helpers ---


def test_parse_seed_variants():
    assert parse_seed("host", 21337) == ("host", 21337)
    assert parse_seed("host:1234", 21337) == ("host", 1234)
    assert parse_seed("host 5555", 21337) == ("host", 5555)
    assert parse_seed("[2001:db8::1]:9999", 21337) == ("2001:db8::1", 9999)
    assert parse_seed("[2001:db8::1]", 21337) == ("2001:db8::1", 21337)


def test_coerce_port_handles_int_str_and_garbage():
    assert coerce_port(21337, 1) == 21337
    assert coerce_port("21337", 1) == 21337
    assert coerce_port(None, 42) == 42
    assert coerce_port("not-a-port", 42) == 42


def test_short_version_strips_prefix():
    assert short_version("xahaud-2026.6.21-release+3350") == "2026.6.21-release+3350"
    assert short_version("rippled-2.0.0") == "2.0.0"
    assert short_version(None) == "(unknown)"


def test_release_date_extracts_date_portion():
    assert release_date("xahaud-2026.6.21-release+3350") == "2026.6.21"
    assert release_date("rippled-2.4.0") == "2.4.0"  # no YYYY.M.D -> short form
    assert release_date(None) == "(unknown)"


# --- crawl: merge / aggregation ---


def _crawler() -> Crawler:
    return Crawler(default_port=21337, probe_default_port=True)


def test_merge_unions_nodes_by_pubkey_and_returns_endpoints():
    c = _crawler()
    new = c._merge(
        CrawlResult(
            server_header=None,
            peers=[
                {
                    "public_key": "A",
                    "version": "xahaud-2026.6.21-release",
                    "ip": "1.1.1.1",
                    "port": 21337,
                },
                {"public_key": "B", "version": "xahaud-2026.5.1-release"},  # no ip
                {"version": "nokey"},  # dropped: no public_key
            ],
        )
    )
    assert new == [("1.1.1.1", 21337)]
    assert set(c.nodes) == {"A", "B"}
    assert c.nodes["A"].has_endpoint is True
    assert c.nodes["B"].has_endpoint is False


def test_merge_keeps_first_version_seen():
    c = _crawler()
    c._merge(CrawlResult(None, [{"public_key": "A", "version": "v1"}]))
    c._merge(CrawlResult(None, [{"public_key": "A", "version": "v2"}]))
    assert c.nodes["A"].version == "v1"


def test_merge_probes_default_port_when_hidden():
    c = _crawler()
    new = c._merge(CrawlResult(None, [{"public_key": "A", "ip": "2.2.2.2"}]))
    assert new == [("2.2.2.2", 21337)]


def test_merge_skips_hidden_port_when_probe_disabled():
    c = Crawler(default_port=21337, probe_default_port=False)
    new = c._merge(CrawlResult(None, [{"public_key": "A", "ip": "2.2.2.2"}]))
    assert new == []
    assert c.nodes["A"].has_endpoint is True  # still counted, just not contactable


def test_enqueue_respects_visited_and_cap():
    c = Crawler(default_port=21337, max_nodes=2)
    assert c._enqueue(("a", 1)) is True
    assert c._enqueue(("a", 1)) is False  # already visited
    assert c._enqueue(("b", 1)) is True
    assert c._enqueue(("c", 1)) is False  # cap reached


def test_version_counts_and_rollup():
    c = _crawler()
    c.nodes = {
        "A": Node("A", version="xahaud-2026.6.21-release+1"),
        "B": Node("B", version="xahaud-2026.6.21-release+1"),
        "C": Node("C", version="xahaud-2026.5.1-release+9"),
        "D": Node("D", version=None),
    }
    counts = c.version_counts()
    assert counts["2026.6.21-release+1"] == 2
    assert counts["(unknown)"] == 1
    rollup = c.release_rollup()
    # newest release first
    assert rollup[0] == ("2026.6.21", 2)
    assert ("2026.5.1", 1) in rollup
    assert c.contactable == 0


# --- amendments: normalize / status / vote_detail ---


def _features() -> dict:
    return {
        "H1": {"name": "Live", "supported": True, "enabled": True},
        "H2": {
            "name": "Pending",
            "supported": True,
            "enabled": False,
            "count": 2,
            "validations": 5,
            "threshold": 4,
        },
        "H3": {"name": "Vetoed", "supported": True, "enabled": False, "vetoed": True},
        "H4": {
            "name": "Old",
            "supported": True,
            "enabled": False,
            "vetoed": "Obsolete",
        },
        "H5": {
            "name": "Majority",
            "supported": True,
            "enabled": False,
            "majority": 12345,
        },
        "H6": {"name": "Unknown", "supported": False, "enabled": False},
    }


def test_normalize_sorts_and_buckets_status():
    recs = amd.normalize(_features())
    assert [r.name for r in recs] == sorted([r.name for r in recs], key=str.lower)
    by_name = {r.name: r for r in recs}
    assert by_name["Live"].status() == amd.STATUS_ENABLED
    assert by_name["Pending"].status() == amd.STATUS_PENDING
    assert by_name["Vetoed"].status() == amd.STATUS_VETOED
    assert by_name["Old"].status() == amd.STATUS_OBSOLETE
    assert by_name["Majority"].status() == amd.STATUS_MAJORITY
    assert by_name["Unknown"].status() == amd.STATUS_UNSUPPORTED


def test_obsolete_is_not_vetoed():
    old = next(a for a in amd.normalize(_features()) if a.name == "Old")
    assert old.is_obsolete is True
    assert old.is_vetoed is False


def test_vote_detail_renders_tally_and_majority():
    recs = {r.name: r for r in amd.normalize(_features())}
    assert recs["Pending"].vote_detail() == "votes 2/5 need 4"
    assert "majority reached" in recs["Majority"].vote_detail()
    assert "unsupported-by-node" in recs["Unknown"].vote_detail()
    assert recs["Live"].vote_detail() == ""


def test_normalize_handles_unnamed_amendment():
    recs = amd.normalize({"DEADBEEF" + "0" * 56: {"enabled": True}})
    assert recs[0].name.startswith("(unknown DEADBEEF")
