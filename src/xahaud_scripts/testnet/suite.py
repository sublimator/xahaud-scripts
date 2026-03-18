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
from xahaud_scripts.utils.logging import make_logger

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


def _snapshot_test(network: TestNetwork, dest: Path) -> None:
    """Copy node dirs and network.json from live testnet into dest."""
    dest.mkdir(parents=True, exist_ok=True)

    # Copy each node directory (excluding db/)
    for node_dir in sorted(network.base_dir.glob("n*")):
        if node_dir.is_dir():
            target = dest / node_dir.name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(
                node_dir,
                target,
                ignore=shutil.ignore_patterns("db"),
            )

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

    network_config = NetworkConfig(node_count=node_count, validators=validators)
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
) -> LaunchConfig:
    """Build a LaunchConfig from effective config dict."""
    rippled_path = xahaud_root / "build" / "rippled"

    # Genesis file with features
    base_genesis = get_bundled_genesis_file()
    features = config.get("features", [])
    effective_genesis = prepare_genesis_file(base_genesis, features)

    # Environment variables (simple key=value, no node-specific parsing needed)
    extra_env: dict[str, str] = {}
    for key, value in config.get("env", {}).items():
        extra_env[key] = str(value)

    # Per-node environment variables: node_env: {3: {KEY: VAL}, 4: {KEY: VAL}}
    node_env: dict[int, dict[str, str]] = {}
    for node_id_str, env_dict in config.get("node_env", {}).items():
        node_env[int(node_id_str)] = {k: str(v) for k, v in env_dict.items()}

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
    if env_override:
        base_env = config.get("env", {})
        config["env"] = {
            **(base_env if isinstance(base_env, dict) else {}),
            **env_override,
        }

    # --fast-bootstrap: inject XAHAUD_BOOTSTRAP_FAST_START=1 unless already set
    if fast_bootstrap:
        env = config.setdefault("env", {})
        if isinstance(env, dict) and "XAHAUD_BOOTSTRAP_FAST_START" not in env:
            env["XAHAUD_BOOTSTRAP_FAST_START"] = "1"

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
        launch_config = _build_launch_config(xahaud_root, config)
        network.run(launch_config)

        # 4. Set up dual file logging for scenario + txn_generator etc.
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
    params_override: dict[str, Any] | None = None,
    env_override: dict[str, str] | None = None,
    dry_run: bool = False,
    py_log_specs: list[str] | None = None,
    fast_bootstrap: bool = True,
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
        params_override: If set, override all variant/params with these
            values (from ``--params-json``).
        env_override: If set, merge these env vars into every test config
            (overrides both defaults and per-test env).
        dry_run: Print plan without executing.
        py_log_specs: If set, enable extra Python loggers to file at
            requested levels (format: ``logger.name=LEVEL``).
        fast_bootstrap: If True (default), inject XAHAUD_BOOTSTRAP_FAST_START=1
            unless explicitly set in suite config or --env.

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
        console.print(f"[bold]Tests:[/bold] {len(tests)}")
        for i, test in enumerate(tests, 1):
            config = suite.effective_network(test)
            console.print(f"\n[bold cyan]  {i}. {test['name']}[/bold cyan]")
            console.print(f"     script: {test['script']}")
            params = test.get("_params")
            if params:
                console.print(f"     params: {params}")
            console.print(f"     node_count: {config.get('node_count', 5)}")
            features = config.get("features", [])
            if features:
                console.print(f"     features: {', '.join(features)}")
            env = config.get("env", {})
            if env:
                console.print(f"     env: {env}")
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

    logger.info(f"Running suite: {suite_path} ({len(tests)} test(s))")
    logger.info(f"  tail -F {combined_log}")

    for i, test in enumerate(tests, 1):
        name = test["name"]
        logger.info(f"\n{'=' * 60}")
        logger.info(f"Test {i}/{len(tests)}: {name}")
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
            )
        except KeyboardInterrupt:
            logger.info("Suite interrupted — network left in place for inspection")
            break

        results.append(result)

        status = "[green]PASS[/green]" if result.passed else "[red]FAIL[/red]"
        Console(stderr=True).print(f"  {name}: {status} ({result.duration:.1f}s)")

        if not result.passed and stop_on_fail:
            logger.info("Stopping suite (--stop-on-fail)")
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
