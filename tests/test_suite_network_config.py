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
    SuiteConfig,
    _contained_run_dir,
    _expand_tests,
    _validate_network_config,
    _validated_desktop,
    _validated_extra_args,
    _validated_genesis_file,
    run_suite,
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


@pytest.mark.parametrize(
    "name",
    ["/tmp/victim", "../victim", "nested/victim", ".", "has space"],
)
def test_suite_test_name_must_be_a_safe_path_component(name: str, tmp_path: Path):
    path = tmp_path / "suite.yml"
    path.write_text(f"tests: [{{name: {name!r}, script: scenario.py}}]\n")

    with pytest.raises(ValueError, match="must start with a letter or digit"):
        SuiteConfig.from_yaml(path)


def test_expanded_variant_name_grammar_remains_supported(tmp_path: Path):
    path = tmp_path / "suite.yml"
    path.write_text("tests: [{name: test-name_1.2@manual, script: scenario.py}]\n")

    suite = SuiteConfig.from_yaml(path)

    assert suite.tests[0]["name"] == "test-name_1.2@manual"


def test_run_output_path_rejects_existing_symlink_escape(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    latest_dir = runs_dir / "latest"
    outside = tmp_path / "outside"
    latest_dir.mkdir(parents=True)
    outside.mkdir()
    (latest_dir / "safe").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes"):
        _contained_run_dir(runs_dir, "latest", "safe")


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (
            "version: 1\ntests: [{name: a, script: b}]\n",
            r"Suite file has unknown key\(s\): 'version'",
        ),
        (
            "defaults: {metadata: x}\ntests: [{name: a, script: b}]\n",
            r"'defaults' has unknown key\(s\): 'metadata'",
        ),
        (
            "tests: [{name: a, script: b, skip: true}]\n",
            r"Test #1 has unknown key\(s\): 'skip'",
        ),
        (
            "defaults: {network: {node_cout: 2}}\ntests: [{name: a, script: b}]\n",
            r"'defaults.network' has unknown key\(s\): 'node_cout'",
        ),
        (
            "tests: [{name: a, script: b, network: {find_port: true}}]\n",
            r"Test 'a' 'network' has unknown key\(s\): 'find_port'",
        ),
    ],
    ids=["suite", "defaults", "test", "defaults-network", "test-network"],
)
def test_suite_schema_rejects_unknown_keys(body: str, match: str, tmp_path: Path):
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


def test_network_config_rejects_unknown_key_for_programmatic_callers(tmp_path: Path):
    with pytest.raises(ValueError, match=r"unknown key\(s\): 'node_cout'"):
        _validate_network_config({"node_cout": 2}, xahaud_root=tmp_path)


def test_network_config_enforces_cli_node_count_limit(tmp_path: Path):
    with pytest.raises(ValueError, match=r"node_count must be <= 20"):
        _validate_network_config({"node_count": 21}, xahaud_root=tmp_path)


@pytest.mark.parametrize("node_id", [True, 1.5, "1.5", -1, 2, "n2"])
def test_node_env_requires_exact_in_range_node_ids(node_id: object, tmp_path: Path):
    with pytest.raises(ValueError, match=r"node_env"):
        _validate_network_config(
            {"node_count": 2, "node_env": {node_id: {"EXAMPLE": "1"}}},
            xahaud_root=tmp_path,
        )


def test_node_env_accepts_numeric_and_n_prefixed_ids(tmp_path: Path):
    _validate_network_config(
        {
            "node_count": 2,
            "node_env": {0: {"ZERO": 0}, "n1": {"ONE": 1}},
        },
        xahaud_root=tmp_path,
    )


def test_node_env_rejects_duplicate_aliases(tmp_path: Path):
    with pytest.raises(ValueError, match=r"duplicate aliases for n1"):
        _validate_network_config(
            {
                "node_count": 2,
                "node_env": {1: {"FIRST": 1}, "n1": {"SECOND": 2}},
            },
            xahaud_root=tmp_path,
        )


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


# --- whole-suite preflight ---------------------------------------------------


def _scenario_file(path: Path) -> None:
    path.write_text("async def scenario(ctx, log):\n    pass\n")


def test_real_run_preflights_all_configs_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    first_script = tmp_path / "first.py"
    second_script = tmp_path / "second.py"
    _scenario_file(first_script)
    _scenario_file(second_script)
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(
        "tests:\n"
        f"  - {{name: first, script: {first_script}}}\n"
        f"  - name: second\n    script: {second_script}\n"
        "    network: {node_count: 0}\n"
    )
    dispatched: list[str] = []

    def record_dispatch(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        dispatched.append(args[2]["name"])
        raise AssertionError("no test may dispatch before whole-suite preflight")

    monkeypatch.setattr("xahaud_scripts.testnet.suite._run_one_test", record_dispatch)

    with pytest.raises(ValueError, match=r"node_count must be >= 1"):
        run_suite(suite_file, tmp_path)

    assert dispatched == []
    assert not (tmp_path / ".testnet").exists()


def test_real_run_preflights_all_scripts_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    first_script = tmp_path / "first.py"
    _scenario_file(first_script)
    missing_script = tmp_path / "missing.py"
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(
        "tests:\n"
        f"  - {{name: first, script: {first_script}}}\n"
        f"  - {{name: second, script: {missing_script}}}\n"
    )
    dispatched = False

    def record_dispatch(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal dispatched
        dispatched = True
        raise AssertionError("no test may dispatch before whole-suite preflight")

    monkeypatch.setattr("xahaud_scripts.testnet.suite._run_one_test", record_dispatch)

    with pytest.raises(ValueError, match=r"Script is not a file"):
        run_suite(suite_file, tmp_path)

    assert dispatched is False
    assert not (tmp_path / ".testnet").exists()


def test_real_run_preflights_scenario_contract_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    script = tmp_path / "not_async.py"
    script.write_text("def scenario(ctx, log):\n    pass\n")
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: bad, script: {script}}}]\n")

    monkeypatch.setattr(
        "xahaud_scripts.testnet.suite._run_one_test",
        lambda *args, **kwargs: pytest.fail("preflight must reject before dispatch"),
    )

    with pytest.raises(ValueError, match=r"must define 'async def scenario'"):
        run_suite(suite_file, tmp_path)

    assert not (tmp_path / ".testnet").exists()


def test_preflight_does_not_execute_scenario_module(tmp_path: Path):
    marker = tmp_path / "module-executed"
    script = tmp_path / "scenario_with_side_effect.py"
    script.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "async def scenario(ctx, log):\n"
        "    pass\n"
    )
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: side-effect, script: {script}}}]\n")

    assert run_suite(suite_file, tmp_path, dry_run=True) == []
    assert not marker.exists()


@pytest.mark.parametrize(
    "replacement",
    [
        "scenario = None\n",
        "def scenario(ctx, log):\n    pass\n",
        "del scenario\n",
        "holder = lambda value=(scenario := None): None\n",
        "class Holder:\n    global scenario\n    scenario = None\n",
        "class Outer:\n"
        "    class Inner:\n"
        "        global scenario\n"
        "        scenario = None\n",
    ],
    ids=[
        "assignment",
        "sync-redefinition",
        "delete",
        "lambda-default",
        "class-global",
        "nested-class-global",
    ],
)
def test_preflight_rejects_overwritten_async_scenario(replacement: str, tmp_path: Path):
    script = tmp_path / "overwritten.py"
    script.write_text("async def scenario(ctx, log):\n    pass\n" + replacement)
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: overwritten, script: {script}}}]\n")

    with pytest.raises(ValueError, match=r"must define 'async def scenario'"):
        run_suite(suite_file, tmp_path, dry_run=True)

    assert not (tmp_path / ".testnet").exists()


def test_preflight_allows_annotation_without_overwriting_scenario(tmp_path: Path):
    script = tmp_path / "annotated.py"
    script.write_text("async def scenario(ctx, log):\n    pass\nscenario: object\n")
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: annotated, script: {script}}}]\n")

    assert run_suite(suite_file, tmp_path, dry_run=True) == []


def test_preflight_rejects_compile_invalid_module_without_dispatch(tmp_path: Path):
    script = tmp_path / "compile_invalid.py"
    script.write_text("async def scenario(ctx, log):\n    pass\nreturn\n")
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: compile-invalid, script: {script}}}]\n")

    with pytest.raises(ValueError, match=r"'return' outside function"):
        run_suite(suite_file, tmp_path, dry_run=True)

    assert not (tmp_path / ".testnet").exists()


def test_preflight_honors_python_source_encoding_cookie(tmp_path: Path):
    script = tmp_path / "latin1_scenario.py"
    script.write_bytes(
        b"# coding: latin-1\n"
        b'"""caf\xe9 scenario"""\n'
        b"description = 'caf\xe9'\n"
        b"async def scenario(ctx, log):\n"
        b"    pass\n"
    )
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: latin1, script: {script}}}]\n")

    assert run_suite(suite_file, tmp_path, dry_run=True) == []
    assert SuiteConfig.get_test_description(script) == "caf\xe9 scenario"


def test_preflight_honors_scenario_future_syntax_flags(tmp_path: Path):
    script = tmp_path / "future_syntax.py"
    script.write_text(
        "from __future__ import barry_as_FLUFL\n"
        "async def scenario(ctx, log):\n"
        "    pass\n"
        "legacy_not_equal = 1 <> 2\n"
    )
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: future-syntax, script: {script}}}]\n")

    assert run_suite(suite_file, tmp_path, dry_run=True) == []


def test_preflight_ignores_binding_deferred_in_generator_body(tmp_path: Path):
    script = tmp_path / "deferred_generator.py"
    script.write_text(
        "async def scenario(ctx, log):\n"
        "    pass\n"
        "deferred = ((scenario := None) for _ in ())\n"
    )
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: deferred, script: {script}}}]\n")

    assert run_suite(suite_file, tmp_path, dry_run=True) == []


@pytest.mark.parametrize(
    "consumption",
    [
        "eager = list((scenario := None) for _ in [0])\n",
        "deferred = ((scenario := None) for _ in [0])\nnext(deferred)\n",
    ],
    ids=["immediate-consumer", "later-consumer"],
)
def test_preflight_rejects_generator_binding_consumed_during_import(
    consumption: str, tmp_path: Path
):
    script = tmp_path / "consumed_generator.py"
    script.write_text("async def scenario(ctx, log):\n    pass\n" + consumption)
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: consumed, script: {script}}}]\n")

    with pytest.raises(ValueError, match=r"must define 'async def scenario'"):
        run_suite(suite_file, tmp_path, dry_run=True)


def test_preflight_attributes_deferred_generator_binding_to_later_consumer(
    tmp_path: Path,
):
    script = tmp_path / "late_consumed_generator.py"
    script.write_text(
        "deferred = ((scenario := None) for _ in [0])\n"
        "async def scenario(ctx, log):\n"
        "    pass\n"
        "next(deferred)\n"
    )
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: consumed, script: {script}}}]\n")

    with pytest.raises(ValueError, match=r"must define 'async def scenario'"):
        run_suite(suite_file, tmp_path, dry_run=True)


@pytest.mark.parametrize(
    "helper",
    [
        "def helper():\n    next(deferred)\n",
        "async def helper():\n    next(deferred)\n",
        "helper = lambda: next(deferred)\n",
    ],
    ids=["function", "async-function", "lambda"],
)
def test_preflight_ignores_generator_load_in_deferred_helper_body(
    helper: str, tmp_path: Path
):
    script = tmp_path / "deferred_helper.py"
    script.write_text(
        "deferred = ((scenario := None) for _ in [0])\n"
        "async def scenario(ctx, log):\n"
        "    pass\n" + helper
    )
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: helper, script: {script}}}]\n")

    assert run_suite(suite_file, tmp_path, dry_run=True) == []


@pytest.mark.parametrize(
    "consumer",
    [
        "def helper(value=next(deferred)):\n    pass\n",
        "class Helper:\n    next(deferred)\n",
    ],
    ids=["function-default", "class-body"],
)
def test_preflight_rejects_generator_load_in_definition_time_expression(
    consumer: str, tmp_path: Path
):
    script = tmp_path / "definition_time_consumer.py"
    script.write_text(
        "deferred = ((scenario := None) for _ in [0])\n"
        "async def scenario(ctx, log):\n"
        "    pass\n" + consumer
    )
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: consumer, script: {script}}}]\n")

    with pytest.raises(ValueError, match=r"must define 'async def scenario'"):
        run_suite(suite_file, tmp_path, dry_run=True)


def test_preflight_rejects_decorated_scenario_without_executing_decorator(
    tmp_path: Path,
):
    marker = tmp_path / "decorator-executed"
    script = tmp_path / "decorated.py"
    script.write_text(
        "from pathlib import Path\n"
        "def replace(fn):\n"
        f"    Path({str(marker)!r}).write_text('executed')\n"
        "    return lambda ctx, log: None\n"
        "@replace\n"
        "async def scenario(ctx, log):\n"
        "    pass\n"
    )
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: decorated, script: {script}}}]\n")

    with pytest.raises(ValueError, match=r"scenario must not be decorated"):
        run_suite(suite_file, tmp_path, dry_run=True)

    assert not marker.exists()


def test_preflight_rejects_async_generator_scenario(tmp_path: Path):
    script = tmp_path / "async_generator.py"
    script.write_text("async def scenario(ctx, log):\n    yield 1\n")
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: async-generator, script: {script}}}]\n")

    with pytest.raises(ValueError, match=r"not an async generator"):
        run_suite(suite_file, tmp_path, dry_run=True)


def test_preflight_allows_nested_generator_helper(tmp_path: Path):
    script = tmp_path / "nested_generator.py"
    script.write_text(
        "async def scenario(ctx, log):\n"
        "    def values():\n"
        "        yield 1\n"
        "    list(values())\n"
    )
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: nested-generator, script: {script}}}]\n")

    assert run_suite(suite_file, tmp_path, dry_run=True) == []


@pytest.mark.parametrize(
    ("signature", "params"),
    [
        ("", "{}"),
        ("ctx, log, required", "{}"),
        ("ctx, log", "{unexpected: 1}"),
        ("ctx, log", "{ctx: duplicate}"),
    ],
    ids=["no-context", "missing-param", "unexpected-param", "duplicate-context"],
)
def test_preflight_rejects_scenario_call_signature_mismatch(
    signature: str, params: str, tmp_path: Path
):
    script = tmp_path / "bad_signature.py"
    script.write_text(f"async def scenario({signature}):\n    pass\n")
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(
        f"tests:\n  - name: bad-signature\n    script: {script}\n    params: {params}\n"
    )

    with pytest.raises(ValueError, match=r"cannot be called as scenario"):
        run_suite(suite_file, tmp_path, dry_run=True)

    assert not (tmp_path / ".testnet").exists()


def test_dynamic_variants_still_execute_for_expansion(tmp_path: Path):
    marker = tmp_path / "variants-executed"
    script = tmp_path / "dynamic_variants.py"
    script.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "variants = list([{'label': 'dynamic', 'value': 1}])\n"
        "async def scenario(ctx, log, **params):\n"
        "    pass\n"
    )
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: dynamic, script: {script}}}]\n")

    assert run_suite(suite_file, tmp_path, dry_run=True) == []
    assert marker.read_text() == "executed"


def test_generator_binding_consumed_during_import_exposes_variants(tmp_path: Path):
    script = tmp_path / "generator_variants.py"
    script.write_text(
        "deferred = ((variants := [{'label': 'generated'}]) for _ in [0])\n"
        "next(deferred)\n"
        "async def scenario(ctx, log, **params):\n"
        "    pass\n"
    )
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: dynamic, script: {script}}}]\n")

    suite = SuiteConfig.from_yaml(suite_file)
    assert [test["name"] for test in _expand_tests(suite, tmp_path)] == [
        "dynamic@generated"
    ]


def test_lambda_default_variants_are_discovered(tmp_path: Path):
    script = tmp_path / "lambda_variants.py"
    script.write_text(
        "holder = lambda value=(variants := "
        "[{'label': 'lambda', 'value': 1}]): None\n"
        "async def scenario(ctx, log, **params):\n"
        "    pass\n"
    )
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: dynamic, script: {script}}}]\n")

    suite = SuiteConfig.from_yaml(suite_file)
    assert [test["name"] for test in _expand_tests(suite, tmp_path)] == [
        "dynamic@lambda"
    ]


def test_nested_class_global_variants_are_discovered(tmp_path: Path):
    script = tmp_path / "nested_class_variants.py"
    script.write_text(
        "class Outer:\n"
        "    class Inner:\n"
        "        global variants\n"
        "        variants = [{'label': 'nested', 'value': 1}]\n"
        "async def scenario(ctx, log, **params):\n"
        "    pass\n"
    )
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: dynamic, script: {script}}}]\n")

    suite = SuiteConfig.from_yaml(suite_file)
    assert [test["name"] for test in _expand_tests(suite, tmp_path)] == [
        "dynamic@nested"
    ]


def test_malformed_variants_are_not_silently_ignored(tmp_path: Path):
    script = tmp_path / "bad_variants.py"
    script.write_text(
        "variants = [{'label': '../escape'}]\nasync def scenario(ctx, log):\n    pass\n"
    )
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(f"tests: [{{name: bad, script: {script}}}]\n")

    with pytest.raises(ValueError, match=r"Could not load scenario variants"):
        run_suite(suite_file, tmp_path)

    assert not (tmp_path / ".testnet").exists()


def test_preflight_only_applies_to_selected_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    selected_script = tmp_path / "selected.py"
    _scenario_file(selected_script)
    suite_file = tmp_path / "suite.yml"
    suite_file.write_text(
        "tests:\n"
        f"  - {{name: selected, script: {selected_script}}}\n"
        "  - name: ignored\n    script: missing.py\n"
        "    network: {node_count: 0}\n"
    )

    monkeypatch.setattr(
        "xahaud_scripts.testnet.suite._run_one_test",
        lambda *args, **kwargs: pytest.fail("dry-run must not dispatch"),
    )

    assert (
        run_suite(
            suite_file,
            tmp_path,
            test_filter=["selected"],
            dry_run=True,
        )
        == []
    )


@pytest.mark.parametrize("test_n", [0, -1, True])
def test_run_suite_rejects_non_positive_or_boolean_test_count(
    test_n: object, tmp_path: Path
):
    with pytest.raises(ValueError, match=r"test_n"):
        run_suite(tmp_path / "does-not-need-to-exist.yml", tmp_path, test_n=test_n)  # type: ignore[arg-type]


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


@pytest.mark.parametrize("topology", [None, [], "", 0, False])
def test_present_topology_must_be_a_mapping(topology: object, tmp_path: Path):
    with pytest.raises(ValueError, match=r"topology must be a mapping"):
        _validate_network_config({"topology": topology}, xahaud_root=tmp_path)


def test_topology_rejects_unknown_key(tmp_path: Path):
    with pytest.raises(ValueError, match=r"topology has unknown key\(s\): 'wait'"):
        _validate_network_config(
            {"topology": {"wait": 3}},
            xahaud_root=tmp_path,
        )


def test_topology_aliases_are_mutually_exclusive(tmp_path: Path):
    with pytest.raises(ValueError, match=r"only one of 'topology'"):
        _validate_network_config(
            {"topology": {}, "runtime_topology": {}},
            xahaud_root=tmp_path,
        )


def test_legacy_runtime_topology_alias_remains_supported(tmp_path: Path):
    _validate_network_config(
        {
            "fixed_peers": False,
            "runtime_topology": {"edges": [], "exact": True},
        },
        xahaud_root=tmp_path,
    )


@pytest.mark.parametrize(
    "topology",
    [
        {"timeout": 1, "stable_for": 2},
        {"settle_timeout": 1, "timeout": 10, "stable_for": 2},
        {"timeout": 2, "stable_for": 2},
    ],
)
def test_topology_stability_must_be_less_than_effective_timeout(
    topology: dict, tmp_path: Path
):
    with pytest.raises(ValueError, match=r"stable_for must be less than"):
        _validate_network_config(
            {"fixed_peers": False, "topology": {**topology, "edges": []}},
            xahaud_root=tmp_path,
        )


def test_topology_requires_positive_effective_timeout_for_edges(tmp_path: Path):
    with pytest.raises(ValueError, match=r"settle timeout must be > 0"):
        _validate_network_config(
            {
                "fixed_peers": False,
                "topology": {"edges": [], "timeout": 0, "stable_for": 0},
            },
            xahaud_root=tmp_path,
        )


def test_non_empty_topology_requires_positive_rpc_timeout(tmp_path: Path):
    with pytest.raises(ValueError, match=r"rpc_timeout must be > 0"):
        _validate_network_config(
            {"topology": {"connect": [], "rpc_timeout": 0}},
            xahaud_root=tmp_path,
        )


def test_unused_stability_fields_do_not_constrain_connect_only_topology(
    tmp_path: Path,
):
    _validate_network_config(
        {"topology": {"connect": [], "timeout": 1, "stable_for": 2}},
        xahaud_root=tmp_path,
    )
