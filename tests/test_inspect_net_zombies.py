from __future__ import annotations

from collections import Counter

from xahaud_scripts.binary_features import FeatureParser, RefFeatures
from xahaud_scripts.inspect_net import amendments as amd
from xahaud_scripts.inspect_net import cli as inspect_cli
from xahaud_scripts.inspect_net import zombies as zmb
from xahaud_scripts.inspect_net.crawl import Node


def _features(source: str) -> RefFeatures:
    return RefFeatures(
        ref="test",
        source_path="features.macro",
        declarations=FeatureParser().parse(source, "test:features.macro"),
    )


def test_compare_enabled_amendments_by_hash_not_name():
    ref = _features(
        """
XRPL_FEATURE(Export, Supported::yes, VoteBehavior::DefaultNo)
"""
    )
    export = ref.by_name()["Export"]

    missing, unsupported = zmb.compare_ref_to_enabled(
        ref,
        (
            zmb.EnabledAmendment(
                name="RenamedDisplay",
                amendment_id=export.amendment_id,
            ),
        ),
    )

    assert missing == ()
    assert unsupported == ()


def test_compare_flags_missing_and_explicitly_unsupported_enabled_amendments():
    ref = _features(
        """
XRPL_FEATURE(UnsupportedLive, Supported::no, VoteBehavior::DefaultNo)
"""
    )
    unsupported_live = ref.by_name()["UnsupportedLive"]

    missing, unsupported = zmb.compare_ref_to_enabled(
        ref,
        (
            zmb.EnabledAmendment(name="MissingLive", amendment_id="00" * 32),
            zmb.EnabledAmendment(
                name="UnsupportedLive",
                amendment_id=unsupported_live.amendment_id,
            ),
        ),
    )

    assert missing == ("MissingLive",)
    assert unsupported == ("UnsupportedLive",)


def test_enabled_amendments_include_unstable_enabled_requirements():
    amendments = amd.NetworkAmendments(
        amendments=[
            amd.Amendment(
                hash="AB" * 32,
                name="NewlyLive",
                supported=True,
                enabled=False,
                vetoed=None,
                count=None,
                validations=None,
                threshold=None,
                majority=None,
            )
        ],
        ledger_seq=1,
        enabled_unstable={"NewlyLive"},
    )

    enabled = zmb.enabled_amendments(amendments)

    assert enabled == (zmb.EnabledAmendment("NewlyLive", "AB" * 32),)


def test_amendment_evidence_links_missing_source_and_unsupported_line():
    ref = _features(
        """
XRPL_FEATURE(UnsupportedLive, Supported::no, VoteBehavior::DefaultNo)
"""
    )
    unsupported_live = ref.by_name()["UnsupportedLive"]

    evidence = zmb.amendment_evidence(
        ref,
        (
            zmb.EnabledAmendment(name="MissingLive", amendment_id="00" * 32),
            zmb.EnabledAmendment(
                name="UnsupportedLive",
                amendment_id=unsupported_live.amendment_id,
            ),
        ),
        "https://github.com/Xahau/xahaud/blob/abc/features.macro",
    )

    by_name = {e.name: e for e in evidence}
    assert by_name["MissingLive"].issue == "missing"
    assert by_name["MissingLive"].evidence_url is not None
    assert by_name["MissingLive"].evidence_url.endswith("/features.macro")
    assert by_name["UnsupportedLive"].issue == "unsupported"
    assert by_name["UnsupportedLive"].evidence_url is not None
    assert by_name["UnsupportedLive"].evidence_url.endswith("/features.macro#L2")


def test_github_base_from_remote_normalizes_common_github_remotes():
    assert (
        zmb.github_base_from_remote("git@github.com:Xahau/xahaud.git")
        == "https://github.com/Xahau/xahaud"
    )
    assert (
        zmb.github_base_from_remote("https://github.com/Xahau/xahaud.git")
        == "https://github.com/Xahau/xahaud"
    )
    assert zmb.github_base_from_remote("git@example.com:Xahau/xahaud.git") is None


def test_version_ref_treats_unknowns_as_inconclusive():
    assert zmb.version_ref("(unknown)") is None
    assert zmb.version_ref("") is None
    assert zmb.version_ref("rippled-2.4.0") is None
    assert zmb.version_ref("2026.6.21-release+3350") == "2026.6.21-release+3350"
    assert (
        zmb.version_ref(
            "xahaud-2026.6.21-release+3350",
            {"xahaud-2026.6.21-release+3350": "tag"},
        )
        == "tag"
    )


def test_visible_version_counts_preserves_non_xahaud_versions():
    counts = zmb.visible_version_counts(
        [
            "xahaud-2026.6.21-release+3350",
            "rippled-2.4.0",
            "catl-peer-client-1.0",
            None,
        ]
    )

    assert counts["2026.6.21-release+3350"] == 1
    assert counts["rippled-2.4.0"] == 1
    assert counts["catl-peer-client-1.0"] == 1
    assert counts["(unknown)"] == 1


def test_analyze_versions_marks_missing_tag_unknown(tmp_path):
    reports = zmb.analyze_versions(
        repo=tmp_path,
        version_counts=Counter({"does-not-exist": 2, "(unknown)": 1}),
        enabled=(),
    )

    by_version = {r.version: r for r in reports}
    assert by_version["does-not-exist"].status == "unknown"
    assert by_version["(unknown)"].status == "unknown"


def test_zombie_node_json_reports_public_key_raw_version_and_status():
    report = zmb.VersionCompatibility(
        version="2026.6.21-release+3350",
        nodes=1,
        ref="2026.6.21-release+3350",
        parsed=None,
        missing_enabled=("fixHookMap",),
        unsupported_enabled=(),
    )
    node = Node(
        public_key="node-pubkey",
        version="xahaud-2026.6.21-release+3350",
        has_endpoint=True,
        endpoints={("127.0.0.1", 21337)},
    )

    row = inspect_cli._zombie_node_json(
        node,
        {"2026.6.21-release+3350": report},
    )

    assert row == {
        "public_key": "node-pubkey",
        "version": "xahaud-2026.6.21-release+3350",
        "version_key": "2026.6.21-release+3350",
        "status": "incompatible",
        "ref": "2026.6.21-release+3350",
        "missing_enabled_count": 1,
        "unsupported_enabled_count": 0,
        "has_endpoint": True,
        "endpoints": ["127.0.0.1:21337"],
    }
