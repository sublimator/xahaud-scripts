"""Tests for suite ``network:`` block validators and raw-argument quoting."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from xahaud_scripts.testnet.config import LaunchConfig, NodeInfo
from xahaud_scripts.testnet.launcher.iterm import ITermLauncher
from xahaud_scripts.testnet.launcher.iterm_panes import ITermPanesLauncher
from xahaud_scripts.testnet.launcher.tmux import TmuxLauncher
from xahaud_scripts.testnet.suite import (
    _validate_network_config,
    _validated_desktop,
    _validated_extra_args,
    _validated_genesis_file,
)


def _node(tmp_path: Path) -> NodeInfo:
    return NodeInfo(
        id=0,
        public_key="pk0",
        token="tok0",
        config_path=tmp_path / "n0" / "xahaud.cfg",
        port_peer=21235,
        port_rpc=5005,
        port_ws=6005,
    )


def _launch_config(tmp_path: Path, extra_args: list[str]) -> LaunchConfig:
    return LaunchConfig(
        xahaud_root=tmp_path,
        rippled_path=tmp_path / "rippled",
        genesis_file=tmp_path / "genesis.json",
        extra_args=extra_args,
    )


# --- extra_args quoting oracle ---------------------------------------------

# A spaced value must stay ONE argument, and a substitution must stay literal.
HOSTILE_ARGS = ["--label=a b", "$(printf injected)", "semi;colon", "quote'd"]


@pytest.mark.parametrize(
    "launcher",
    [TmuxLauncher(), ITermLauncher(), ITermPanesLauncher()],
    ids=["tmux", "iterm", "iterm_panes"],
)
def test_extra_args_survive_shell_parsing_intact(launcher, tmp_path: Path):
    """Every launcher joins args into a string a shell later parses.

    Unquoted, `--label=a b` became two argv entries and `$(...)` was executed
    by the terminal. The contract the suite validator advertises is that one
    YAML list entry is one argument, so shell-splitting the built command must
    recover the list exactly.
    """
    flags = launcher._build_startup_flags(  # noqa: SLF001
        _node(tmp_path), _launch_config(tmp_path, HOSTILE_ARGS)
    )

    parsed = shlex.split(flags)
    # Drop the --ledgerfile pair the builder always emits first.
    assert parsed[0] == "--ledgerfile"
    assert parsed[2:] == HOSTILE_ARGS


# --- _validated_extra_args --------------------------------------------------


def test_extra_args_accepts_strings_and_numbers():
    assert _validated_extra_args(None) == []
    assert _validated_extra_args(["--a", 5, 1.5]) == ["--a", "5", "1.5"]


def test_extra_args_rejects_bare_string():
    """`extra_args: "--a --b"` would otherwise silently become one argument."""
    with pytest.raises(ValueError, match="not a bare string"):
        _validated_extra_args("--a --b")


def test_extra_args_rejects_bool_because_bool_is_an_int():
    with pytest.raises(ValueError, match="string or number"):
        _validated_extra_args([True])


# --- _validated_desktop -----------------------------------------------------


@pytest.mark.parametrize("value", [1, 9])
def test_desktop_accepts_boundaries(value: int):
    assert _validated_desktop(value) == value


@pytest.mark.parametrize("value", [0, 10, 99])
def test_desktop_rejects_out_of_range(value: int):
    with pytest.raises(ValueError, match="between 1 and 9"):
        _validated_desktop(value)


@pytest.mark.parametrize("value", [True, "3", 1.0])
def test_desktop_rejects_wrong_types(value: object):
    with pytest.raises(ValueError, match="must be an integer"):
        _validated_desktop(value)


# --- _validated_genesis_file ------------------------------------------------


def test_genesis_file_resolves_relative_to_root(tmp_path: Path):
    genesis = tmp_path / "custom.json"
    genesis.write_text("{}")
    assert _validated_genesis_file("custom.json", xahaud_root=tmp_path) == genesis


def test_genesis_file_rejects_missing_path(tmp_path: Path):
    with pytest.raises(ValueError, match="does not exist"):
        _validated_genesis_file("nope.json", xahaud_root=tmp_path)


def test_genesis_file_none_means_bundled(tmp_path: Path):
    assert _validated_genesis_file(None, xahaud_root=tmp_path) is None


# --- the single validation entry point --------------------------------------


@pytest.mark.parametrize(
    ("config", "match"),
    [
        ({"genesis_file": "missing.json"}, "does not exist"),
        ({"extra_args": "--bare"}, "not a bare string"),
        ({"desktop": 99}, "between 1 and 9"),
    ],
    ids=["genesis_file", "extra_args", "desktop"],
)
def test_validate_network_config_rejects_each_field(
    config: dict, match: str, tmp_path: Path
):
    """These three were previously only reached inside _build_launch_config.

    That meant --dry-run skipped them entirely, and a real run did not reject
    them until after it had torn down the previous network.
    """
    with pytest.raises(ValueError, match=match):
        _validate_network_config(config, xahaud_root=tmp_path)


def test_extra_args_rejects_nul_byte():
    """NUL is the one class quoting cannot rescue — argv cannot hold it.

    YAML "\\0" decodes to "\\x00" and would pass every other check, then fail
    deep in subprocess, which is exactly what up-front validation is for.
    """
    with pytest.raises(ValueError, match="NUL byte"):
        _validated_extra_args(["--label=a\x00b"])


def test_validate_network_config_rejects_nul_in_extra_args(tmp_path: Path):
    with pytest.raises(ValueError, match="NUL byte"):
        _validate_network_config({"extra_args": ["\x00"]}, xahaud_root=tmp_path)


# --- YAML schema shape ------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("tests: [42]\n", r"Test #1 must be a mapping, got int"),
        ("tests: 'nope'\n", r"non-empty 'tests' list"),
        (
            "defaults: []\ntests: [{name: a, script: b}]\n",
            r"'defaults' must be a mapping",
        ),
        (
            "defaults: {network: []}\ntests: [{name: a, script: b}]\n",
            r"'defaults.network' must be a mapping",
        ),
        (
            "tests: [{name: a, script: b, network: []}]\n",
            r"Test 'a' 'network' must be a mapping",
        ),
        (
            "tests: [{name: a, script: b, params: 3}]\n",
            r"Test 'a' 'params' must be a mapping",
        ),
        ("- a\n- b\n", r"Suite file must be a mapping, got list"),
    ],
    ids=[
        "test-not-mapping",
        "tests-not-list",
        "defaults-not-mapping",
        "defaults-network-not-mapping",
        "test-network-not-mapping",
        "test-params-not-mapping",
        "root-not-mapping",
    ],
)
def test_suite_yaml_shape_errors_are_value_errors(
    body: str, match: str, tmp_path: Path
):
    """Valid YAML of the wrong shape is a config mistake, not an internal crash.

    These previously reached `in` / `.items()` on the wrong type and surfaced
    as raw TypeError/AttributeError tracebacks.
    """
    from xahaud_scripts.testnet.suite import SuiteConfig

    path = tmp_path / "suite.yml"
    path.write_text(body)
    with pytest.raises(ValueError, match=match):
        SuiteConfig.from_yaml(path)


# --- required leaf fields ---------------------------------------------------


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("tests: [{name: [], script: b}]\n", r"Test #1 'name' must be a non-empty"),
        ("tests: [{name: '', script: b}]\n", r"Test #1 'name' must be a non-empty"),
        ("tests: [{name: a, script: []}]\n", r"Test 'a' 'script' must be a non-empty"),
        (
            "tests: [{name: a, script: '  '}]\n",
            r"Test 'a' 'script' must be a non-empty",
        ),
    ],
    ids=["name-list", "name-empty", "script-list", "script-blank"],
)
def test_required_leaf_fields_are_validated(body: str, match: str, tmp_path: Path):
    """`script: []` reached Path() and raised a raw TypeError; `name: []` was
    silently accepted by --dry-run and printed as the test's name."""
    from xahaud_scripts.testnet.suite import SuiteConfig

    path = tmp_path / "suite.yml"
    path.write_text(body)
    with pytest.raises(ValueError, match=match):
        SuiteConfig.from_yaml(path)


@pytest.mark.parametrize(
    ("config", "match"),
    [
        ({"node_count": []}, r"network.node_count must be an integer, got list"),
        ({"node_count": True}, r"network.node_count must be an integer, got bool"),
        ({"node_count": 0}, r"network.node_count must be >= 1"),
        ({"validators": "3"}, r"network.validators must be an integer, got str"),
        ({"node_count": 3, "validators": 5}, r"cannot exceed"),
        ({"quorum": []}, r"network.quorum must be an integer"),
        ({"start_ledger": 1.5}, r"network.start_ledger must be an integer"),
        ({"slave_delay": "fast"}, r"network.slave_delay must be a number"),
        ({"fixed_peers": "no"}, r"network.fixed_peers must be true or false"),
        ({"features": "ConsensusEntropy"}, r"network.features must be a list"),
        ({"features": [3]}, r"network.features entry must be a non-empty string"),
        ({"rc": [""]}, r"network.rc entry must be a non-empty string"),
        ({"log_levels": []}, r"network.log_levels must be a mapping"),
    ],
    ids=[
        "node_count-list",
        "node_count-bool",
        "node_count-zero",
        "validators-str",
        "validators-exceeds",
        "quorum-list",
        "start_ledger-float",
        "slave_delay-str",
        "fixed_peers-str",
        "features-bare-str",
        "features-int-entry",
        "rc-empty-entry",
        "log_levels-list",
    ],
)
def test_core_network_leaves_are_validated(config: dict, match: str, tmp_path: Path):
    """node_count: [] used to pass preflight and only fail inside generate()'s
    range(node_count) — after teardown() had already destroyed the prior run."""
    with pytest.raises(ValueError, match=match):
        _validate_network_config(config, xahaud_root=tmp_path)


# --- deterministic semantics (round-5 findings) -----------------------------


@pytest.mark.parametrize(
    ("config", "match"),
    [
        ({"slave_delay": float("nan")}, r"slave_delay must be a finite number"),
        ({"slave_delay": float("inf")}, r"slave_delay must be a finite number"),
        ({"rc": ["not-a-spec"]}, r"network.rc 'not-a-spec'"),
        ({"node_count": 2, "rc": ["n9:delay=1"]}, r"references n9, outside"),
        ({"node_count": 2, "rc": ["n0->n7:drop=1"]}, r"references n7, outside"),
        ({"log_levels": {"Overlay": []}}, r"log_levels\['Overlay'\] must be a string"),
        ({"log_levels": {"": "debug"}}, r"log_levels key must be a non-empty"),
        ({"launcher": "imaginary"}, r"launcher must be one of"),
        ({"topology": ["bad"]}, r"topology must be a mapping"),
        (
            {"node_count": 2, "topology": {"edges": ["n0->n9"]}},
            r"references n9, outside",
        ),
        ({"topology": {"edges": "n0->n1"}}, r"topology.edges must be a list"),
        ({"topology": {"edges": ["nope"]}}, r"topology.edges 'nope'"),
        ({"topology": {"exact": "yes"}}, r"topology.exact must be true or false"),
        (
            {"topology": {"settle_timeout": float("nan")}},
            r"settle_timeout must be a finite",
        ),
    ],
    ids=[
        "slave_delay-nan",
        "slave_delay-inf",
        "rc-malformed",
        "rc-node-oob",
        "rc-peer-oob",
        "log_levels-value",
        "log_levels-key",
        "launcher-unknown",
        "topology-not-mapping",
        "topology-edge-oob",
        "topology-edges-str",
        "topology-edge-malformed",
        "topology-exact-str",
        "topology-nan-timeout",
    ],
)
def test_deterministic_semantics_rejected_at_preflight(
    config: dict, match: str, tmp_path: Path
):
    """All knowable from config alone, yet each used to fail after teardown,
    config generation, or node launch."""
    with pytest.raises(ValueError, match=match):
        _validate_network_config(config, xahaud_root=tmp_path)


def test_valid_semantics_still_pass(tmp_path: Path):
    """Guard against the validators being too strict to express real suites."""
    _validate_network_config(
        {
            "node_count": 3,
            "validators": 2,
            "slave_delay": 0.2,
            "launcher": "tmux",
            "rc": ["rng_poll_ms=333", "n0->n2:drop=100,msg=proposal"],
            "log_levels": {"Overlay": "debug", "TxQ": ""},
            "fixed_peers": False,
            "topology": {
                "edges": ["n0->n1", "n1->n2"],
                "exact": True,
                "settle_timeout": 30,
            },
        },
        xahaud_root=tmp_path,
    )


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("tests: [{name: a, script: b, params: {1: v}}]\n", r"keys must be non-empty"),
        ("tests: [{name: a, script: b, params: []}]\n", r"'params' must be a mapping"),
        (
            "defaults: {params: {1: v}}\ntests: [{name: a, script: b}]\n",
            r"keys must be non-empty",
        ),
    ],
    ids=["test-params-int-key", "test-params-list", "defaults-params-int-key"],
)
def test_params_keys_must_be_strings(body: str, match: str, tmp_path: Path):
    """params are expanded as **kwargs; a non-string key cannot be expanded,
    and previously only failed once the network was already up."""
    from xahaud_scripts.testnet.suite import SuiteConfig

    path = tmp_path / "suite.yml"
    path.write_text(body)
    with pytest.raises(ValueError, match=match):
        SuiteConfig.from_yaml(path)


# --- topology cross-field invariants ----------------------------------------


def test_topology_edge_outside_explicit_node_subset(tmp_path: Path):
    """An edge to a node not in topology.nodes was only rejected after launch.

    _apply_runtime_topology calls validate_edges_in_nodes once the network is
    up and RPC is answering; the same check is deterministic from config.
    """
    with pytest.raises(ValueError, match=r"topology.edges:.*n2"):
        _validate_network_config(
            {
                "node_count": 3,
                "fixed_peers": False,
                "topology": {"nodes": [0, 1], "edges": ["n0->n2"], "exact": True},
            },
            xahaud_root=tmp_path,
        )


@pytest.mark.parametrize(
    "topology",
    [
        {"edges": ["n0->n1"], "exact": True},
        {"edges": ["n0->n1"]},  # exact defaults true
    ],
    ids=["exact-explicit", "exact-default"],
)
def test_exact_shaping_requires_no_fixed_peers(topology: dict, tmp_path: Path):
    """fixed_peers also defaults true, so the omitted-field pairing must fail."""
    with pytest.raises(ValueError, match="requires fixed_peers: false"):
        _validate_network_config(
            {"node_count": 2, "topology": topology}, xahaud_root=tmp_path
        )


def test_non_exact_shaping_is_allowed_with_fixed_peers(tmp_path: Path):
    """Additive (exact: false) shaping stays legal on a fixed-peer mesh."""
    _validate_network_config(
        {"node_count": 2, "topology": {"edges": ["n0->n1"], "exact": False}},
        xahaud_root=tmp_path,
    )
