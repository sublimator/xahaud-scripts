"""Scenario test suite runner.

Loads a YAML suite definition, runs each test with a fresh network
lifecycle (teardown -> generate -> run -> scenario), and reports results.

Usage:
    x-testnet suite .testnet/scenarios/suite.yml
    x-testnet suite .testnet/scenarios/suite.yml --no-stop-on-fail
    x-testnet suite .testnet/scenarios/suite.yml --test quorum_recovery_smoke
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.table import Table

from xahaud_scripts.testnet.config import (
    LaunchConfig,
    NetworkConfig,
    NodeInfo,
    get_bundled_genesis_file,
    prepare_genesis_file,
)
from xahaud_scripts.testnet.launcher import get_launcher
from xahaud_scripts.testnet.network import TestNetwork
from xahaud_scripts.testnet.process import UnixProcessManager
from xahaud_scripts.testnet.rpc import RequestsRPCClient
from xahaud_scripts.testnet.scenario import (
    load_scenario_variants,
    run_scenario_with_monitor,
)
from xahaud_scripts.testnet.topology import (
    Edge,
    disconnect_managed_peer,
    format_edges,
    parse_edge_specs,
    parse_node_ref,
    require_rpc_success,
    snapshot_topology,
    topology_diff,
    validate_edges_in_nodes,
)
from xahaud_scripts.utils.logging import make_logger
from xahaud_scripts.utils.quoting import validate_shell_identifier

logger = make_logger(__name__)

# Keys within ``network:`` that are merged as dicts (test values override
# defaults per-key).  All other network keys are replaced entirely.
_DICT_MERGE_KEYS = {"log_levels", "env"}


@dataclass
class TestResult:
    """Result of a single scenario test."""

    name: str
    passed: bool
    duration: float
    error: str | None = None
    snapshot_dir: Path | None = None


@dataclass
class SuiteConfig:
    """Parsed suite configuration.

    YAML structure::

        defaults:
          network:          # default network config
            node_count: 5
            env: { ... }
          params:           # default scenario params (optional)
            min_txns: 5

        tests:
          - name: my_test
            script: path/to/script.py
            network:        # per-test network overrides
              node_count: 7
            params:         # per-test scenario params
              drop_count: 3
    """

    defaults: dict[str, Any] = field(default_factory=dict)
    tests: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: Path) -> SuiteConfig:
        """Load and validate a suite YAML file."""
        with open(path) as f:
            raw = yaml.safe_load(f)

        if not isinstance(raw, dict):
            raise ValueError(f"Suite file must be a YAML mapping, got {type(raw)}")

        tests = raw.get("tests")
        if not tests or not isinstance(tests, list):
            raise ValueError("Suite file must have a non-empty 'tests' list")

        for i, test in enumerate(tests):
            if "name" not in test:
                raise ValueError(f"Test #{i + 1} missing required 'name' key")
            if "script" not in test:
                raise ValueError(f"Test '{test['name']}' missing required 'script' key")

        return cls(
            defaults=raw.get("defaults", {}),
            tests=tests,
        )

    @staticmethod
    def get_test_description(script_path: Path) -> str | None:
        """Extract description from a test script's module docstring.

        Looks for a :descr: tag first, falls back to the first line.
        """
        try:
            source = script_path.read_text()
        except OSError:
            return None
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None
        docstring = ast.get_docstring(tree)
        if not docstring:
            return None
        # Look for :descr: tag
        match = re.search(r":descr:\s*(.+)", docstring)
        if match:
            return match.group(1).strip()
        # Fall back to first line
        first_line = docstring.strip().split("\n")[0].strip()
        return first_line or None

    def effective_network(self, test: dict[str, Any]) -> dict[str, Any]:
        """Merge defaults.network with per-test network overrides."""
        base = dict(self.defaults.get("network", {}))
        for key, value in test.get("network", {}).items():
            if key in _DICT_MERGE_KEYS and isinstance(value, dict):
                existing = base.get(key, {})
                if isinstance(existing, dict):
                    base[key] = {**existing, **value}
                    continue
            base[key] = value
        return base

    def effective_params(self, test: dict[str, Any]) -> dict[str, Any] | None:
        """Merge defaults.params with per-test params.

        Returns None if neither defaults nor test define params.
        """
        base = self.defaults.get("params")
        override = test.get("params")
        if base is None and override is None:
            return None
        return {**(base or {}), **(override or {})}


def _rotate_log(log_path: Path, *, max_keep: int = 10) -> None:
    """Rotate log by timestamping, keeping at most max_keep old copies."""
    if log_path.exists():
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        stem = log_path.stem  # e.g. "scenario-test"
        archived = log_path.with_name(f"{stem}-{timestamp}.log")
        log_path.rename(archived)

    # Prune old logs beyond max_keep
    pattern = f"{log_path.stem}-*.log"
    old_logs = sorted(log_path.parent.glob(pattern))
    for stale in old_logs[:-max_keep]:
        stale.unlink()


def _test_matches(name: str, filter_str: str) -> bool:
    """Check if a test name matches a filter string.

    Exact match always works.  A filter without ``@`` also matches
    expanded matrix names: ``foo`` matches ``foo@light``.
    """
    return filter_str == name or (
        "@" not in filter_str and name.startswith(filter_str + "@")
    )


def _expand_tests(
    suite: SuiteConfig,
    xahaud_root: Path,
    *,
    params_override: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Expand variant entries into individual test entries.

    * ``params_override`` (from ``--params-json``) wins over everything.
    * ``params:`` in the suite YAML entry (merged with defaults.params)
      produces a single run.
    * ``variants`` exported by the script is expanded into ``name@label``
      entries.
    """
    expanded: list[dict[str, Any]] = []
    for test in suite.tests:
        if params_override is not None:
            expanded.append({**test, "_params": params_override})
            continue

        # Merge defaults.params + test.params
        effective = suite.effective_params(test)
        if effective is not None:
            expanded.append({**test, "_params": effective})
            continue

        script_path = Path(test["script"])
        if not script_path.is_absolute():
            script_path = xahaud_root / script_path

        variants: list[dict[str, Any]] | None = None
        if script_path.exists():
            with contextlib.suppress(Exception):
                variants = load_scenario_variants(script_path)

        if variants:
            base_name = test["name"]
            for entry in variants:
                label = entry["label"]
                params = {k: v for k, v in entry.items() if k != "label"}
                expanded.append(
                    {
                        **test,
                        "name": f"{base_name}@{label}",
                        "_params": params,
                        "_base_name": base_name,
                    }
                )
        else:
            expanded.append(test)

    return expanded


def _validated_env_mapping(raw: Any, *, label: str) -> dict[str, str]:
    """Validate and stringify a YAML env mapping."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be a mapping of NAME: VALUE")

    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValueError(f"{label} keys must be strings, got {key!r}")
        try:
            validate_shell_identifier(key)
        except ValueError as exc:
            raise ValueError(f"{label}.{key}: {exc}") from exc
        result[key] = str(value)
    return result


def _validated_node_env(raw: Any) -> dict[int, dict[str, str]]:
    """Validate and stringify the YAML node_env mapping."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("network.node_env must be a mapping of node_id: env")

    result: dict[int, dict[str, str]] = {}
    for node_id_raw, env_dict in raw.items():
        try:
            node_id = int(node_id_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"network.node_env key must be an integer node id: {node_id_raw!r}"
            ) from exc
        result[node_id] = _validated_env_mapping(
            env_dict,
            label=f"network.node_env.{node_id}",
        )
    return result


def _merge_env_override(
    config: dict[str, Any],
    env_override: dict[str, str] | None,
) -> None:
    """Merge suite-level CLI env overrides into a network config."""
    if not env_override:
        return
    base_env = config.get("env", {})
    if not isinstance(base_env, dict):
        raise ValueError("network.env must be a mapping of NAME: VALUE")
    config["env"] = {
        **base_env,
        **env_override,
    }


def _validate_network_env(config: dict[str, Any]) -> None:
    """Validate env-bearing network config without mutating it."""
    _validated_env_mapping(config.get("env", {}), label="network.env")
    _validated_node_env(config.get("node_env", {}))


def _snapshot_test(network: TestNetwork, dest: Path) -> None:
    """Copy node dirs and network.json from live testnet into dest."""
    dest.mkdir(parents=True, exist_ok=True)

    # Copy only this run's nodes. The live testnet directory may contain stale
    # n* dirs from an earlier larger network because teardown keeps logs.
    for node in network.nodes:
        node_dir = network.base_dir / f"n{node.id}"
        if not node_dir.is_dir():
            continue
        target = dest / node_dir.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(
            node_dir,
            target,
            ignore=shutil.ignore_patterns("db"),
        )

    active = {f"n{node.id}" for node in network.nodes}
    for stale in sorted(dest.glob("n[0-9]*")):
        if stale.is_dir() and stale.name not in active:
            shutil.rmtree(stale)

    # Copy network.json
    network_json = network.base_dir / "network.json"
    if network_json.exists():
        shutil.copy2(network_json, dest / "network.json")


def _create_network(
    xahaud_root: Path,
    config: dict[str, Any],
) -> TestNetwork:
    """Create a TestNetwork from effective config."""
    node_count = config.get("node_count", 5)
    validators = config.get("validators")
    launcher_type = config.get("launcher")

    if validators is not None and validators > node_count:
        raise ValueError(
            f"validators ({validators}) cannot exceed node_count ({node_count})"
        )

    network_config = NetworkConfig(
        node_count=node_count,
        validators=validators,
        fixed_peers=config.get("fixed_peers", True),
    )
    launcher = get_launcher(launcher_type)
    rpc_client = RequestsRPCClient(network_config.base_port_rpc)
    process_manager = UnixProcessManager()

    base_dir = xahaud_root / "testnet"

    return TestNetwork(
        base_dir=base_dir,
        network_config=network_config,
        launcher=launcher,
        rpc_client=rpc_client,
        process_manager=process_manager,
    )


def _build_launch_config(
    xahaud_root: Path,
    config: dict[str, Any],
    *,
    nodes: list[NodeInfo] | None = None,
    network_config: NetworkConfig | None = None,
    rippled_path: Path | None = None,
) -> LaunchConfig:
    """Build a LaunchConfig from effective config dict."""
    if rippled_path is None:
        rippled_path = xahaud_root / "build" / "rippled"

    # Genesis file with feature/start-ledger modifications. Mirrors the
    # lower-level `x-testnet run` knobs so suites can exercise flag-ledger
    # activation paths instead of only genesis-enabled features.
    if network_config is None:
        network_config = NetworkConfig(
            node_count=config.get("node_count", 5),
            validators=config.get("validators"),
            fixed_peers=config.get("fixed_peers", True),
        )

    base_genesis = get_bundled_genesis_file()
    features = config.get("features", [])
    unl_report_keys = None
    if config.get("unl_report"):
        if nodes is None:
            raise ValueError("network unl_report requires generated node metadata")
        validator_count = network_config.validator_count
        if validator_count > len(nodes):
            raise ValueError(
                f"unl_report requires {validator_count} generated validator "
                f"nodes, got {len(nodes)}"
            )
        unl_report_keys = [node.public_key for node in nodes[:validator_count]]
    effective_genesis = prepare_genesis_file(
        base_genesis,
        features,
        start_ledger=config.get("start_ledger"),
        majority_features=config.get("majority_features"),
        unl_report_keys=unl_report_keys,
    )

    # Environment variables (simple key=value, no node-specific parsing needed)
    extra_env = _validated_env_mapping(config.get("env", {}), label="network.env")

    # Per-node environment variables: node_env: {3: {KEY: VAL}, 4: {KEY: VAL}}
    node_env = _validated_node_env(config.get("node_env", {}))

    # Suite-level rc specs use the same startup env path as `x-testnet run
    # --rc`, so delayed/dropped links are active from node launch.
    rc_specs = config.get("rc") or []
    if rc_specs:
        if nodes is None:
            raise ValueError("network rc requires generated node metadata")

        from xahaud_scripts.testnet.cli_handlers.rc import (
            RUNTIME_CONFIG_ENV,
            build_runtime_config_envs,
            merge_runtime_config_env,
            parse_rc_spec,
        )

        rc_envs = build_runtime_config_envs(
            [parse_rc_spec(spec) for spec in rc_specs],
            nodes,
        )
        for node_id, json_val in rc_envs.items():
            node_env_for_id = node_env.setdefault(node_id, {})
            if (
                RUNTIME_CONFIG_ENV in extra_env
                and RUNTIME_CONFIG_ENV not in node_env_for_id
            ):
                node_env_for_id[RUNTIME_CONFIG_ENV] = extra_env[RUNTIME_CONFIG_ENV]
            merge_runtime_config_env(
                node_env_for_id,
                json.loads(json_val)["set"],
            )

    return LaunchConfig(
        xahaud_root=xahaud_root,
        rippled_path=rippled_path,
        genesis_file=effective_genesis,
        quorum=config.get("quorum"),
        no_delays=config.get("slave_delay") is None,
        slave_delay=config.get("slave_delay", 1.0),
        extra_args=[],
        extra_env=extra_env,
        node_env=node_env,
    )


def _node_by_id(nodes: list[NodeInfo], node_id: int) -> NodeInfo:
    for node in nodes:
        if node.id == node_id:
            return node
    raise ValueError(f"Unknown node id: n{node_id}")


def _parse_topology_nodes(nodes: Any) -> list[int] | None:
    if nodes is None:
        return None
    return [parse_node_ref(node) for node in nodes]


def _wait_for_rpc(network: TestNetwork, *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    pending = {node.id for node in network.nodes}
    while time.monotonic() < deadline:
        pending = {
            node_id
            for node_id in pending
            if network.rpc_client.server_info(node_id) is None
        }
        if not pending:
            return
        time.sleep(0.5)
    raise TimeoutError(f"Timed out waiting for RPC on nodes: {sorted(pending)}")


def _wait_for_topology(
    network: TestNetwork,
    expected: set[Edge],
    *,
    nodes: list[int] | None = None,
    exact: bool = True,
    timeout: float = 60,
    poll_interval: float = 1.0,
    stable_for: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout
    stable_since: float | None = None
    last_message = "not checked"

    while time.monotonic() < deadline:
        snapshot = snapshot_topology(
            network.rpc_client, network.nodes, include_nodes=nodes
        )
        ok, message = topology_diff(snapshot, expected, nodes=nodes, exact=exact)
        last_message = message
        if ok:
            now = time.monotonic()
            stable_since = now if stable_since is None else stable_since
            if now - stable_since >= stable_for:
                return
        else:
            stable_since = None
        time.sleep(poll_interval)

    raise TimeoutError(
        f"Timed out waiting for topology {format_edges(expected)}: {last_message}"
    )


def _apply_runtime_topology(network: TestNetwork, config: dict[str, Any]) -> None:
    """Apply suite-level runtime topology before scenario execution."""
    topo = config.get("topology") or config.get("runtime_topology")
    if not topo:
        return
    if not isinstance(topo, dict):
        raise ValueError("network topology must be a mapping")

    _wait_for_rpc(network, timeout=float(topo.get("rpc_timeout", 30)))

    nodes = _parse_topology_nodes(topo.get("nodes"))
    if nodes is not None:
        for node_id in nodes:
            _node_by_id(network.nodes, node_id)

    bidirectional = bool(topo.get("bidirectional", False))
    exact = bool(topo.get("exact", True))
    timeout_value = topo.get("settle_timeout")
    if timeout_value is None:
        timeout_value = topo.get("timeout", 60)
    timeout = float(timeout_value)
    poll_interval = float(topo.get("poll_interval", 1.0))
    stable_for = float(topo.get("stable_for", 2.0))

    if "edges" in topo:
        expected = parse_edge_specs(
            topo.get("edges") or [], bidirectional=bidirectional
        )
        target_nodes = (
            nodes if nodes is not None else [node.id for node in network.nodes]
        )
        validate_edges_in_nodes(expected, target_nodes)
        if exact and network.config.fixed_peers:
            raise ValueError(
                "network.topology exact shaping requires fixed_peers: false; "
                "generated [ips_fixed] peers may reconnect omitted edges"
            )
        logger.info(
            "Applying runtime topology: "
            f"expected={format_edges(expected)} exact={exact} nodes={target_nodes}"
        )
        current = snapshot_topology(
            network.rpc_client,
            network.nodes,
            include_nodes=nodes,
        ).outbound_edges
        logger.info(f"Runtime topology before apply: {format_edges(current)}")
        if exact:
            for source, target in sorted(current - expected):
                logger.info(f"Runtime topology disconnect n{source}->n{target}")
                result = disconnect_managed_peer(
                    network.rpc_client,
                    network.nodes,
                    source=source,
                    target=target,
                )
                require_rpc_success(result, f"n{source}->n{target} disconnect")
        for source, target in sorted(expected - current):
            target_node = _node_by_id(network.nodes, target)
            logger.info(f"Runtime topology connect n{source}->n{target}")
            result = network.rpc_client.connect(
                source, "127.0.0.1", target_node.port_peer
            )
            require_rpc_success(result, f"n{source}->n{target} connect")
        _wait_for_topology(
            network,
            expected,
            nodes=nodes,
            exact=exact,
            timeout=timeout,
            poll_interval=poll_interval,
            stable_for=stable_for,
        )
        logger.info(f"Applied runtime topology: {format_edges(expected)}")
        return

    for spec in topo.get("disconnect", []) or []:
        source, target = parse_edge_specs([spec]).pop()
        logger.info(f"Runtime topology disconnect n{source}->n{target}")
        result = disconnect_managed_peer(
            network.rpc_client,
            network.nodes,
            source=source,
            target=target,
        )
        require_rpc_success(result, f"n{source}->n{target} disconnect")
    for spec in topo.get("connect", []) or []:
        source, target = parse_edge_specs([spec]).pop()
        target_node = _node_by_id(network.nodes, target)
        logger.info(f"Runtime topology connect n{source}->n{target}")
        result = network.rpc_client.connect(source, "127.0.0.1", target_node.port_peer)
        require_rpc_success(result, f"n{source}->n{target} connect")


def _run_one_test(
    xahaud_root: Path,
    suite: SuiteConfig,
    test: dict[str, Any],
    *,
    combined_log: Path,
    snapshot_on_fail: bool = True,
    env_override: dict[str, str] | None = None,
    py_log_specs: list[str] | None = None,
    fast_bootstrap: bool = True,
    rippled_path: Path | None = None,
) -> TestResult:
    """Run a single test with full network lifecycle."""
    name = test["name"]
    script_path = Path(test["script"])
    if not script_path.is_absolute():
        script_path = xahaud_root / script_path

    if not script_path.exists():
        return TestResult(
            name=name,
            passed=False,
            duration=0,
            error=f"Script not found: {script_path}",
        )

    config = suite.effective_network(test)
    _merge_env_override(config, env_override)
    _validate_network_env(config)

    # --fast-bootstrap: inject global.bootstrap_fast_start=true unless already
    # set in XAHAUD_RUNTIME_TEST_CONFIG.
    if fast_bootstrap:
        env = config.setdefault("env", {})
        if isinstance(env, dict):
            from xahaud_scripts.testnet.cli_handlers.rc import (
                merge_runtime_config_env,
            )

            merge_runtime_config_env(
                env,
                {"global": {"bootstrap_fast_start": True}},
                overwrite=False,
            )

    start = time.monotonic()

    network = _create_network(xahaud_root, config)

    # Prepare output dirs
    runs_dir = xahaud_root / ".testnet" / "output" / "runs"
    latest_dir = runs_dir / "latest" / name
    latest_dir.mkdir(parents=True, exist_ok=True)

    per_test_log = latest_dir / "scenario.log"

    try:
        # 1. Kill any prior node processes (generate() handles dir cleanup)
        network.teardown(keep_dirs=True)

        # 2. Generate fresh configs
        log_levels = config.get("log_levels")
        find_ports = config.get("find_ports", False)
        rc_specs = config.get("rc")
        network.generate(
            log_levels=log_levels,
            find_ports=find_ports,
            rc_specs=rc_specs,
        )

        # 3. Build launch config and run
        launch_config = _build_launch_config(
            xahaud_root,
            config,
            nodes=network.nodes,
            network_config=network.config,
            rippled_path=rippled_path,
        )
        # 4. Set up dual file logging before launch/topology setup so setup
        # failures leave the same paper trail as scenario failures.
        from xahaud_scripts.utils.logging import scenario_file_logging

        with scenario_file_logging(
            (combined_log, "a"),
            (per_test_log, "w"),
            py_log_specs=py_log_specs,
        ) as handlers:
            # Write separator to combined log
            combined_handler = handlers[0]
            sep = f"\n{'=' * 60}\n  Test: {name}\n{'=' * 60}\n"
            combined_handler.stream.write(sep)
            combined_handler.stream.flush()

            network.run(launch_config)
            _apply_runtime_topology(network, config)

            # 5. Execute scenario
            tracked = config.get("track_features")
            params = test.get("_params")
            passed = asyncio.run(
                run_scenario_with_monitor(
                    script_path=script_path,
                    network=network,
                    tracked_features=tracked,
                    params=params,
                )
            )

        duration = time.monotonic() - start

        # 6. Always snapshot to latest/
        _snapshot_test(network, latest_dir)

        snapshot_dir = None
        if not passed and snapshot_on_fail:
            # Preserve a timestamped copy for failure investigation
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            fail_dir = runs_dir / f"{timestamp}-{name}"
            shutil.copytree(latest_dir, fail_dir)
            logger.info(f"Failure snapshot: {fail_dir}")
            snapshot_dir = fail_dir

        # Kill processes but keep dirs — the next test's pre-test
        # teardown (or the user) handles cleanup.
        network.teardown(keep_dirs=True)
        return TestResult(
            name=name,
            passed=passed,
            duration=duration,
            error="Scenario failed" if not passed else None,
            snapshot_dir=snapshot_dir,
        )

    except KeyboardInterrupt:
        duration = time.monotonic() - start
        network.teardown(keep_dirs=True)
        logger.info(f"Test {name} interrupted — killed processes, kept dirs")
        raise
    except Exception as e:
        duration = time.monotonic() - start
        snapshot_dir = None
        if snapshot_on_fail:
            with contextlib.suppress(Exception):
                _snapshot_test(network, latest_dir)
                timestamp = time.strftime("%Y%m%d-%H%M%S")
                fail_dir = runs_dir / f"{timestamp}-{name}"
                shutil.copytree(latest_dir, fail_dir)
                snapshot_dir = fail_dir
        network.teardown(keep_dirs=True)
        return TestResult(
            name=name,
            passed=False,
            duration=duration,
            error=str(e),
            snapshot_dir=snapshot_dir,
        )


def run_suite(
    suite_path: Path,
    xahaud_root: Path,
    *,
    stop_on_fail: bool = True,
    snapshot_on_fail: bool = True,
    test_filter: list[str] | None = None,
    test_n: int = 1,
    params_override: dict[str, Any] | None = None,
    env_override: dict[str, str] | None = None,
    dry_run: bool = False,
    py_log_specs: list[str] | None = None,
    fast_bootstrap: bool = True,
    rippled_path: Path | None = None,
) -> list[TestResult]:
    """Run a scenario test suite.

    Args:
        suite_path: Path to the suite YAML file.
        xahaud_root: Path to the xahaud repository root.
        stop_on_fail: Stop suite on first failure.
        snapshot_on_fail: Snapshot logs on failure.
        test_filter: If set, only run tests matching these names.
            Supports ``name[label]`` for exact match, or ``name``
            to match all variants.
        test_n: Run each test this many times (default 1).
        params_override: If set, override all variant/params with these
            values (from ``--params-json``).
        env_override: If set, merge these env vars into every test config
            (overrides both defaults and per-test env).
        dry_run: Print plan without executing.
        py_log_specs: If set, enable extra Python loggers to file at
            requested levels (format: ``logger.name=LEVEL``).
        fast_bootstrap: If True (default), inject
            XAHAUD_RUNTIME_TEST_CONFIG global.bootstrap_fast_start=true unless
            explicitly set in suite config or --env.
        rippled_path: If set, use this binary instead of
            ``$xahaud_root/build/rippled``.

    Returns:
        List of TestResult for all executed tests.
    """
    suite = SuiteConfig.from_yaml(suite_path)

    # Expand matrix entries before filtering
    tests = _expand_tests(suite, xahaud_root, params_override=params_override)

    if test_filter:
        tests = [
            t for t in tests if any(_test_matches(t["name"], f) for f in test_filter)
        ]
        if not tests:
            available = [t["name"] for t in _expand_tests(suite, xahaud_root)]
            raise ValueError(
                f"No tests match filter {test_filter}. Available: {available}"
            )

    if dry_run:
        console = Console(stderr=True)
        console.print(f"\n[bold]Suite:[/bold] {suite_path}")
        tests_label = str(len(tests))
        if test_n > 1:
            tests_label += f" x {test_n} runs each"
        console.print(f"[bold]Tests:[/bold] {tests_label}")
        for i, test in enumerate(tests, 1):
            config = suite.effective_network(test)
            _merge_env_override(config, env_override)
            _validate_network_env(config)
            console.print(f"\n[bold cyan]  {i}. {test['name']}[/bold cyan]")
            console.print(f"     script: {test['script']}")
            params = test.get("_params")
            if params:
                console.print(f"     params: {params}")
            console.print(f"     node_count: {config.get('node_count', 5)}")
            if config.get("fixed_peers") is False:
                console.print("     fixed_peers: false")
            features = config.get("features", [])
            if features:
                console.print(f"     features: {', '.join(features)}")
            env = config.get("env", {})
            if env:
                console.print(f"     env: {env}")
            rc = config.get("rc", [])
            if rc:
                console.print(f"     rc: {rc}")
            topology = config.get("topology") or config.get("runtime_topology")
            if topology:
                console.print(f"     topology: {topology}")
            log_levels = config.get("log_levels", {})
            if log_levels:
                console.print(f"     log_levels: {log_levels}")
        console.print()
        return []

    # Rotate combined log before starting suite
    combined_log = xahaud_root / ".testnet" / "output" / "logs" / "scenario-test.log"
    combined_log.parent.mkdir(parents=True, exist_ok=True)
    _rotate_log(combined_log)

    results: list[TestResult] = []
    total_runs = len(tests) * test_n
    run_label = (
        f"{len(tests)} test(s)"
        if test_n == 1
        else f"{len(tests)} test(s) x {test_n} run(s) = {total_runs} run(s)"
    )

    logger.info(f"Running suite: {suite_path} ({run_label})")
    logger.info(f"  tail -F {combined_log}")

    run_num = 0
    stopped = False
    for test in tests:
        name = test["name"]
        for attempt in range(1, test_n + 1):
            run_num += 1
            run_suffix = f" (run {attempt}/{test_n})" if test_n > 1 else ""
            logger.info(f"\n{'=' * 60}")
            logger.info(f"Test {run_num}/{total_runs}: {name}{run_suffix}")
            logger.info(f"{'=' * 60}")

            try:
                result = _run_one_test(
                    xahaud_root,
                    suite,
                    test,
                    combined_log=combined_log,
                    snapshot_on_fail=snapshot_on_fail,
                    env_override=env_override,
                    py_log_specs=py_log_specs,
                    fast_bootstrap=fast_bootstrap,
                    rippled_path=rippled_path,
                )
            except KeyboardInterrupt:
                logger.info("Suite interrupted — network left in place for inspection")
                stopped = True
                break

            result_name = f"{name}{run_suffix}" if test_n > 1 else name
            result = TestResult(
                name=result_name,
                passed=result.passed,
                duration=result.duration,
                error=result.error,
                snapshot_dir=result.snapshot_dir,
            )
            results.append(result)

            status = "[green]PASS[/green]" if result.passed else "[red]FAIL[/red]"
            Console(stderr=True).print(
                f"  {result_name}: {status} ({result.duration:.1f}s)"
            )

            if not result.passed and stop_on_fail:
                logger.info("Stopping suite (--stop-on-fail)")
                stopped = True
                break

        if stopped:
            break

    return results


def print_summary(results: list[TestResult]) -> None:
    """Print a Rich table summarizing suite results."""
    if not results:
        return

    console = Console(stderr=True)
    table = Table(title="Suite Results")
    table.add_column("Test", style="cyan", no_wrap=True)
    table.add_column("Result", justify="center")
    table.add_column("Duration", justify="right", style="white")
    table.add_column("Error", style="dim")

    for r in results:
        status = "[green]PASS[/green]" if r.passed else "[red]FAIL[/red]"
        error = r.error or ""
        if r.snapshot_dir:
            error += f" (snapshot: {r.snapshot_dir.name})"
        table.add_row(r.name, status, f"{r.duration:.1f}s", error)

    console.print()
    console.print(table)

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    console.print(f"\n{passed}/{total} passed")
