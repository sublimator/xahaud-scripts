from __future__ import annotations

import hashlib

import pytest

from xahaud_scripts.binary_features import (
    DEFAULT_TRACK,
    OBSERVED_XAHAU_REFS,
    FeatureParser,
    parse_track_args,
    render_markdown_summary,
    resolve_refs,
)


def _id(name: str) -> str:
    return hashlib.sha512(name.encode()).digest()[:32].hex().upper()


def test_parse_new_features_macro_shape():
    source = """
XRPL_FIX    (HookMap,          Supported::yes, VoteBehavior::DefaultYes)
XRPL_FEATURE(Export,           Supported::yes, VoteBehavior::DefaultNo)
XRPL_FEATURE(ConsensusEntropy, Supported::no,  VoteBehavior::DefaultNo)
XRPL_RETIRE(MultiSign)
"""
    decls = FeatureParser().parse(source, "HEAD:features.macro")
    by_name = {d.name: d for d in decls}

    assert by_name["fixHookMap"].kind == "fix"
    assert by_name["fixHookMap"].supported is True
    assert by_name["fixHookMap"].vote == "DefaultYes"
    assert by_name["fixHookMap"].amendment_id == _id("fixHookMap")

    assert by_name["Export"].kind == "feature"
    assert by_name["Export"].compact() == "yes/DefaultNo"

    assert by_name["ConsensusEntropy"].supported is False
    assert by_name["ConsensusEntropy"].status == "unsupported"

    assert by_name["MultiSign"].kind == "retired"
    assert by_name["MultiSign"].compact() == "retired"


def test_parse_old_feature_cpp_shape():
    source = r"""
REGISTER_FEATURE(Cron,               Supported::yes, VoteBehavior::DefaultNo);
REGISTER_FIX    (fixCronStacking,    Supported::yes, VoteBehavior::DefaultYes);
REGISTER_FEATURE(OldThing,           Supported::yes, VoteBehavior::Obsolete);

[[deprecated("The referenced amendment has been retired"), maybe_unused]]
uint256 const
    retiredMultiSign    = retireFeature("MultiSign"),
    retiredFix1201      = retireFeature("fix1201");
"""
    decls = FeatureParser().parse(source, "2025:Feature.cpp")
    by_name = {d.name: d for d in decls}

    assert by_name["Cron"].kind == "feature"
    assert by_name["Cron"].compact() == "yes/DefaultNo"

    assert by_name["fixCronStacking"].kind == "fix"
    assert by_name["fixCronStacking"].compact() == "yes/DefaultYes"

    assert by_name["OldThing"].status == "obsolete"
    assert by_name["OldThing"].compact() == "obsolete"

    assert by_name["MultiSign"].status == "retired"
    assert by_name["fix1201"].status == "retired"


def test_parse_errors_are_not_silent_partial_successes():
    with pytest.raises(ValueError, match="tree-sitter parse error"):
        FeatureParser().parse("XRPL_FEATURE(", "bad:features.macro")


def test_summary_renders_missing_and_compact_statuses():
    parser = FeatureParser()
    head = parser.parse(
        """
XRPL_FEATURE(Export, Supported::yes, VoteBehavior::DefaultNo)
XRPL_FIX(HookMap, Supported::yes, VoteBehavior::DefaultYes)
""",
        "HEAD:features.macro",
    )
    old = parser.parse(
        "REGISTER_FEATURE(Cron, Supported::yes, VoteBehavior::DefaultNo);",
        "old:Feature.cpp",
    )

    from xahaud_scripts.binary_features import RefFeatures

    rendered = render_markdown_summary(
        [
            RefFeatures("HEAD", "features.macro", head),
            RefFeatures("old", "Feature.cpp", old),
        ],
        ["Export", "fixHookMap", "Cron"],
    )

    assert "| HEAD | features.macro |" in rendered
    assert "| old | Feature.cpp |" in rendered
    assert "yes/DefaultNo" in rendered
    assert "yes/DefaultYes" in rendered
    assert (
        "| old | Feature.cpp | 1 | 1 | 0 | 0 | 0 | - | - | yes/DefaultNo |" in rendered
    )


def test_resolve_refs_observed_preset_dedupes():
    refs = resolve_refs(["HEAD", OBSERVED_XAHAU_REFS[0]], include_observed=True)
    assert refs[0] == "HEAD"
    assert refs.count(OBSERVED_XAHAU_REFS[0]) == 1
    assert refs[-1] == OBSERVED_XAHAU_REFS[-1]


def test_parse_track_args_keeps_order_and_dedupes():
    assert parse_track_args(
        ["Export,ConsensusEntropy", "Custom,Export"], include_defaults=False
    ) == ["Export", "ConsensusEntropy", "Custom"]


def test_default_track_uses_registered_fix_names():
    assert "fixHookMap" in DEFAULT_TRACK
    assert "HookMap" not in DEFAULT_TRACK
