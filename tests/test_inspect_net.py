"""Tests for inspect_net pure logic (no network access)."""

from __future__ import annotations

from datetime import timedelta

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
    assert recs["Pending"].vote_detail() == "votes 2/5 (need 4)"
    # Majority with a numeric close time -> activation ETA, not bare "reached".
    assert "enables ~" in recs["Majority"].vote_detail()
    assert "unsupported-by-node" in recs["Unknown"].vote_detail()
    assert recs["Live"].vote_detail() == ""


def test_vote_fraction():
    recs = {r.name: r for r in amd.normalize(_features())}
    assert recs["Pending"].vote_fraction == "2/5 (need 4)"
    assert recs["Live"].vote_fraction is None  # no tally reported


def test_activation_eta_is_majority_plus_the_xahau_hold():
    """Xahau holds a majority for five days, not the two weeks XRPL uses.

    xahaud:include/xrpl/protocol/SystemParameters.h:81 —
    `defaultAmendmentMajorityTime = std::chrono::days{5}`. Assuming XRPL's
    default put every activation date nine days late, which is the kind of
    wrong an operator plans an upgrade window around.
    """
    a = amd.normalize({"H": {"name": "M", "enabled": False, "majority": 835701281}})[0]
    eta = a.activation_eta()
    assert eta is not None
    # 835701281 (ripple epoch) reached 2026-06-25 11:14Z, +5d -> 2026-06-30.
    assert eta.strftime("%Y-%m-%d") == "2026-06-30"


def test_the_hold_matches_xahaud_not_xrpl():
    """Pinned as a value so a silent revert to `weeks=2` fails here."""
    assert timedelta(days=5) == amd.AMENDMENT_HOLD


def test_activation_eta_none_without_numeric_majority():
    a = amd.normalize({"H": {"name": "M", "enabled": False}})[0]
    assert a.activation_eta() is None


def test_normalize_handles_unnamed_amendment():
    recs = amd.normalize({"DEADBEEF" + "0" * 56: {"enabled": True}})
    assert recs[0].name.startswith("(unknown DEADBEEF")


def test_server_definitions_features_fail_closed_on_malformed_map():
    for result in (
        {},
        {"features": []},
        {"features": {}},
        {"features": {"BAD": {}}},
        {"features": {"BAD": []}},
        {"features": {1: {"enabled": True}}},
    ):
        try:
            amd._server_definition_features(result)
        except ValueError as exc:
            assert "feature" in str(exc)
        else:
            raise AssertionError("expected malformed server_definitions to fail")


def test_server_definitions_features_accepts_non_empty_map():
    features = {"ABC": {"name": "Live", "enabled": True}}
    assert amd._server_definition_features({"features": features}) is features


# --- amendments: sample aggregation / cross-referencing ---


def _sample(node, **flags):
    """One backend reading: flags maps amendment name -> features dict."""
    feats = {f"H_{n}": {"name": n, "supported": True, **v} for n, v in flags.items()}
    return amd._Sample(amd.normalize(feats), node=node, build="b1", ledger_seq=100)


def test_aggregate_single_sample_is_stable():
    agg = amd._aggregate([_sample("n1", Foo={"enabled": True})])
    assert agg.samples == 1
    assert agg.nodes == ["n1"]
    assert agg.enabled_unstable == set()
    assert agg.nodeview_varied == set()
    assert agg.enabled_of("Foo") is True


def test_aggregate_flags_nodeview_variance_but_not_enabled():
    # Same `enabled` on both reads, but veto/vote differs (node-local) -> ~ only.
    agg = amd._aggregate(
        [
            _sample("n1", Foo={"enabled": False, "vetoed": True}),
            _sample("n2", Foo={"enabled": False, "count": 1, "validations": 4}),
        ]
    )
    assert agg.nodes == ["n1", "n2"]  # load-balanced across 2 backends
    assert agg.nodeview_varied == {"Foo"}
    assert agg.enabled_unstable == set()  # enabled agreed -> network truth intact
    assert agg.enabled_of("Foo") is False


def test_aggregate_flags_enabled_instability():
    # A backend disagrees on `enabled` -> out-of-sync node; majority wins.
    agg = amd._aggregate(
        [
            _sample("n1", Foo={"enabled": True}),
            _sample("n2", Foo={"enabled": True}),
            _sample("n3", Foo={"enabled": False}),  # stale/blocked backend
        ]
    )
    assert agg.enabled_unstable == {"Foo"}
    assert agg.enabled_of("Foo") is True  # most-common across samples


def test_aggregate_dedupes_nodes_and_builds():
    agg = amd._aggregate(
        [_sample("n1", Foo={"enabled": True}), _sample("n1", Foo={"enabled": True})]
    )
    assert agg.nodes == ["n1"]
    assert agg.builds == ["b1"]
    assert agg.samples == 2


# --------------------------------------------------------------------------- #
#  Network truth vs the queried node's opinion
# --------------------------------------------------------------------------- #

def test_majority_outranks_a_nodes_veto():
    """A majority activates on a known date whatever one node votes.

    Reporting "vetoed" for an amendment two weeks from activating hides the
    only part a reader can act on — and `vetoed` is that node's own config,
    not a network decision.
    """
    a = amd.normalize({"H": {"name": "M", "enabled": False, "vetoed": True,
                             "majority": 835701281}})[0]
    assert a.is_vetoed is True
    assert a.status() == amd.STATUS_MAJORITY


def test_veto_still_wins_without_a_majority():
    a = amd.normalize({"H": {"name": "V", "enabled": False, "vetoed": True}})[0]
    assert a.status() == amd.STATUS_VETOED


def test_enabled_outranks_everything():
    a = amd.normalize({"H": {"name": "E", "enabled": True, "vetoed": True,
                             "majority": 835701281}})[0]
    assert a.status() == amd.STATUS_ENABLED


def test_aggregate_does_not_mutate_the_samples_it_read():
    """`rep` holds references to the first sample's objects."""
    first = amd.normalize({"H": {"name": "A", "enabled": True}})
    second = amd.normalize({"H": {"name": "A", "enabled": False}})
    amd._aggregate([
        amd._Sample(first, "n1", "b", 1),
        amd._Sample(second, "n2", "b", 1),
        amd._Sample(second, "n3", "b", 1),
    ])
    assert first[0].enabled is True, "the first sample was rewritten"


def test_a_majority_disagreement_is_ledger_drift_not_node_opinion():
    """`majority` comes from the ledger, so backends disagreeing means one is
    out of sync — not the node-local noise nodeview_varied is for.

    Tracked apart from `enabled_unstable` because the two justify different
    actions: one says "we are unsure whether this is live", the other says
    "we are unsure when it goes live". Consumers treat the first as a reason
    to require the amendment.
    """
    a = amd.normalize({"H": {"name": "A", "enabled": False, "majority": 100}})
    b = amd.normalize({"H": {"name": "A", "enabled": False, "majority": 200}})
    agg = amd._aggregate([amd._Sample(a, "n1", "b", 1), amd._Sample(b, "n2", "b", 1)])
    assert "A" in agg.majority_unstable
    assert "A" not in agg.enabled_unstable
    assert "A" not in agg.nodeview_varied


def test_majority_drift_does_not_make_an_amendment_a_requirement():
    """zombies calls a version incompatible when it cannot support an amendment
    that is *live*. An amendment nobody has enabled yet is not one.

    Folding majority drift into `enabled_unstable` made every version missing
    it INCOMPATIBLE for an amendment no ledger has activated.
    """
    from xahaud_scripts.inspect_net import zombies as zmb

    a = amd.normalize({"H": {"name": "A", "enabled": False, "majority": 100}})
    b = amd.normalize({"H": {"name": "A", "enabled": False, "majority": 200}})
    agg = amd._aggregate([amd._Sample(a, "n1", "b", 1), amd._Sample(b, "n2", "b", 1)])

    assert [x.name for x in zmb.enabled_amendments(agg)] == []


def test_an_enabled_flip_still_makes_it_a_requirement():
    """The safety net that split is not allowed to drop: if the samples cannot
    agree whether it is live, requiring it is the side that fails safe."""
    from xahaud_scripts.inspect_net import zombies as zmb

    a = amd.normalize({"H": {"name": "A", "enabled": True}})
    b = amd.normalize({"H": {"name": "A", "enabled": False}})
    agg = amd._aggregate([amd._Sample(a, "n1", "b", 1), amd._Sample(b, "n2", "b", 1)])

    assert "A" in agg.enabled_unstable
    assert [x.name for x in zmb.enabled_amendments(agg)] == ["A"]


def test_a_veto_disagreement_is_node_opinion():
    a = amd.normalize({"H": {"name": "A", "enabled": False, "vetoed": True}})
    b = amd.normalize({"H": {"name": "A", "enabled": False, "vetoed": False}})
    agg = amd._aggregate([amd._Sample(a, "n1", "b", 1), amd._Sample(b, "n2", "b", 1)])
    assert "A" in agg.nodeview_varied
    assert "A" not in agg.enabled_unstable


# --------------------------------------------------------------------------- #
#  The compare cell prefers a number to a word
# --------------------------------------------------------------------------- #

def test_compare_cell_shows_the_tally_for_a_vetoed_amendment():
    """The number says whether a veto is one node's opinion or a settled
    outcome; the word cannot."""
    from xahaud_scripts.inspect_net.cli import _cell

    a = amd.normalize({"H": {"name": "V", "enabled": False, "vetoed": True,
                             "count": 0, "validations": 4, "threshold": 3}})[0]
    assert "0/4" in _cell(a).plain


def test_compare_cell_marks_a_vetoed_tally(self=None):
    from xahaud_scripts.inspect_net.cli import _cell

    a = amd.normalize({"H": {"name": "V", "enabled": False, "vetoed": True,
                             "count": 0, "validations": 4, "threshold": 3}})[0]
    assert _cell(a).plain.endswith("\u2298")


def test_compare_cell_falls_back_to_the_word_without_a_tally(self=None):
    from xahaud_scripts.inspect_net.cli import _cell

    a = amd.normalize({"H": {"name": "V", "enabled": False, "vetoed": True}})[0]
    assert _cell(a).plain == "vetoed"


def test_compare_cell_prefers_an_activation_date():
    from xahaud_scripts.inspect_net.cli import _cell

    a = amd.normalize({"H": {"name": "M", "enabled": False, "majority": 835701281,
                             "count": 4, "validations": 4, "threshold": 3}})[0]
    assert _cell(a).plain.startswith("\u2192")


# --------------------------------------------------------------------------- #
#  The --json manifest, as a contract a consumer pins to
# --------------------------------------------------------------------------- #

def _agg(**flags):
    return amd._aggregate([_sample("n1", **flags)])


def test_a_manifest_carries_the_provenance_it_is_for():
    """Without these a pin cannot say which network or ledger it described."""
    agg = amd._aggregate(
        [amd._Sample(amd.normalize({"H": {"name": "A", "enabled": True}}),
                     node="n1", build="b1", ledger_seq=4242, network_id=21337)]
    )
    entry = amd.as_manifest("https://example.invalid", agg)

    assert entry["network_id"] == 21337
    assert entry["ledger_seq"] == 4242
    assert entry["queried_at"]
    assert entry["url"] == "https://example.invalid"


def test_top_level_enabled_matches_the_per_amendment_flags():
    """The field a consumer actually reads must not drift from the rows."""
    agg = _agg(Live={"enabled": True}, Off={"enabled": False},
               Also={"enabled": True})
    entry = amd.as_manifest("u", agg)

    assert entry["enabled"] == ["Also", "Live"]
    assert entry["enabled"] == sorted(
        a["name"] for a in entry["amendments"] if a["enabled"]
    )


def test_the_same_reading_serializes_to_the_same_bytes():
    """Canonical output, so a regenerated manifest diffs only on real change."""
    agg = _agg(B={"enabled": True}, A={"enabled": False})
    first = amd.manifest_bytes({"mainnet": amd.as_manifest("u", agg)})
    second = amd.manifest_bytes({"mainnet": amd.as_manifest("u", agg)})

    assert first == second
    assert first.endswith("\n"), "a missing trailing newline diffs the last line"


def test_keys_are_sorted_throughout():
    agg = _agg(A={"enabled": True})
    text = amd.manifest_bytes({"z": amd.as_manifest("u", agg),
                               "a": amd.as_manifest("u", agg)})
    import json

    parsed = json.loads(text)
    assert list(parsed) == ["a", "z"]
    assert text.index('"a"') < text.index('"z"'), "written in sorted order too"
    assert list(parsed["a"]) == sorted(parsed["a"])


def test_node_and_build_lists_are_ordered():
    """They come from a set-like dedupe, so their order is otherwise arbitrary."""
    agg = amd._aggregate([
        amd._Sample(amd.normalize({"H": {"name": "A", "enabled": True}}),
                    node=n, build=b, ledger_seq=1)
        for n, b in (("n2", "b2"), ("n1", "b1"))
    ])
    entry = amd.as_manifest("u", agg)

    assert entry["backend_nodes"] == ["n1", "n2"]
    assert entry["builds"] == ["b1", "b2"]


def test_amendments_are_ordered_by_name_then_hash():
    """Names are not unique in principle; the hash breaks the tie so two runs
    against one ledger cannot order the list differently."""
    agg = amd._aggregate([amd._Sample(
        amd.normalize({
            "FF": {"name": "Same", "enabled": True},
            "AA": {"name": "Same", "enabled": True},
            "BB": {"name": "Other", "enabled": True},
        }), node="n", build="b", ledger_seq=1)])
    entry = amd.as_manifest("u", agg)

    assert [(a["name"], a["hash"]) for a in entry["amendments"]] == [
        ("Other", "BB"), ("Same", "AA"), ("Same", "FF"),
    ]


def test_two_amendments_the_build_cannot_name_both_survive():
    """Aggregation is keyed by hash, not by the label a build puts on it.

    A node reports an amendment it does not know with no name, and `normalize`
    calls every one of those "(unknown <hash>…)". Keyed by name they would be
    distinct only by accident of that string; keyed by hash they are distinct
    because they are.
    """
    feats = {"AA" + "0" * 62: {"enabled": True}, "BB" + "0" * 62: {"enabled": False}}
    agg = amd._aggregate([amd._Sample(amd.normalize(feats), "n", "b", 1)])

    assert len(agg.amendments) == 2
    assert {a.hash for a in agg.amendments} == set(feats)


def test_json_always_fetches_the_provenance_fields(monkeypatch, tmp_path):
    """`--json` without `--net` queries two networks, and the cheap path skips
    server_info — which is where network_id and ledger_seq come from.

    A manifest written that way says `network_id: null`, and a consumer pinned
    to it cannot tell which network it describes.
    """
    import json as _json

    from click.testing import CliRunner

    from xahaud_scripts.inspect_net import cli as inspect_cli

    seen: list[bool] = []

    def fake_fetch(url, timeout, samples=1, *, want_seq=True):
        seen.append(want_seq)
        return amd._aggregate([
            amd._Sample(amd.normalize({"H": {"name": "A", "enabled": True}}),
                        node="n", build="b", ledger_seq=99, network_id=21337)
        ])

    monkeypatch.setattr(inspect_cli.amd, "fetch_sampled", fake_fetch)
    out = tmp_path / "m.json"
    result = CliRunner().invoke(inspect_cli.amendments, ["--json", str(out)])

    assert result.exit_code == 0, result.output
    assert seen and all(seen), "every network must be asked for its ledger/network id"
    written = _json.loads(out.read_text())
    for entry in written.values():
        assert entry["network_id"] == 21337
        assert entry["ledger_seq"] == 99
