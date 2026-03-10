"""Scenario test suite runner.

Loads a YAML suite definition, runs each test with a fresh network
lifecycle (teardown -> generate -> run -> scenario), and reports results.

Usage:
    x-testnet suite .testnet/suite.yml
    x-testnet suite .testnet/suite.yml --no-stop-on-fail
    x-testnet suite .testnet/suite.yml --test quorum_recovery_smoke
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import logging
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
    load_scenario_matrix,
    run_scenario_with_monitor,
)
from xahaud_scripts.utils.logging import make_logger

logger = make_logger(__name__)

# Keys that are merged as dicts (test values override defaults per-key).
# All other keys are replaced entirely when overridden.
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
    """Parsed suite configuration."""

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

    def effective_config(self, test: dict[str, Any]) -> dict[str, Any]:
        """Merge defaults with per-test overrides."""
        # Internal keys injected by matrix expansion — skip them.
        skip = {"name", "script", "_params", "_base_name"}
        merged = dict(self.defaults)
        for key, value in test.items():
            if key in skip:
                continue
            if key in _DICT_MERGE_KEYS and isinstance(value, dict):
                base = merged.get(key, {})
                if isinstance(base, dict):
                    merged[key] = {**base, **value}
                    continue
            merged[key] = value
        return merged


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
    tests: list[dict[str, Any]],
    xahaud_root: Path,
    *,
    params_override: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Expand matrix entries into individual test entries.

    * ``params_override`` (from ``--params-json``) wins over everything.
    * ``params:`` in the suite YAML entry produces a single run.
    * ``matrix`` exported by the script is expanded into ``name[label]`` entries.
    """
    expanded: list[dict[str, Any]] = []
    for test in tests:
        if params_override is not None:
            expanded.append({**test, "_params": params_override})
            continue

        if "params" in test:
            expanded.append({**test, "_params": test["params"]})
            continue

        script_path = Path(test["script"])
        if not script_path.is_absolute():
            script_path = xahaud_root / script_path

        matrix: list[dict[str, Any]] | None = None
        if script_path.exists():
            with contextlib.suppress(Exception):
                matrix = load_scenario_matrix(script_path)

        if matrix:
            base_name = test["name"]
            for entry in matrix:
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
    launcher_type = config.get("launcher")

    network_config = NetworkConfig(node_count=node_count)
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

    return LaunchConfig(
        xahaud_root=xahaud_root,
        rippled_path=rippled_path,
        genesis_file=effective_genesis,
        quorum=config.get("quorum"),
        no_delays=config.get("slave_delay") is None,
        slave_delay=config.get("slave_delay", 1.0),
        extra_args=[],
        extra_env=extra_env,
    )


def _run_one_test(
    xahaud_root: Path,
    suite: SuiteConfig,
    test: dict[str, Any],
    *,
    combined_log: Path,
    snapshot_on_fail: bool = True,
    env_override: dict[str, str] | None = None,
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

    config = suite.effective_config(test)
    if env_override:
        base_env = config.get("env", {})
        config["env"] = {
            **(base_env if isinstance(base_env, dict) else {}),
            **env_override,
        }
    start = time.monotonic()

    network = _create_network(xahaud_root, config)

    # Prepare output dirs
    runs_dir = xahaud_root / ".testnet" / "scenario-tests" / "runs"
    latest_dir = runs_dir / "latest" / name
    latest_dir.mkdir(parents=True, exist_ok=True)

    per_test_log = latest_dir / "scenario.log"

    try:
        # 1. Teardown any prior state
        network.teardown()

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
        scenario_logger = logging.getLogger("xahaud_scripts.testnet")
        formatter = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")

        # Combined log (append) - the stable tail -F target
        combined_handler = logging.FileHandler(combined_log, mode="a")
        combined_handler.setFormatter(formatter)
        scenario_logger.addHandler(combined_handler)

        # Write separator to combined log
        sep = f"\n{'=' * 60}\n  Test: {name}\n{'=' * 60}\n"
        combined_handler.stream.write(sep)
        combined_handler.stream.flush()

        # Per-test log (fresh each run)
        test_handler = logging.FileHandler(per_test_log, mode="w")
        test_handler.setFormatter(formatter)
        scenario_logger.addHandler(test_handler)

        # 5. Execute scenario
        tracked = config.get("track_features")
        params = test.get("_params")
        try:
            passed = asyncio.run(
                run_scenario_with_monitor(
                    script_path=script_path,
                    network=network,
                    tracked_features=tracked,
                    params=params,
                )
            )
        finally:
            combined_handler.close()
            test_handler.close()
            scenario_logger.removeHandler(combined_handler)
            scenario_logger.removeHandler(test_handler)

        duration = time.monotonic() - start

        # 6. Always snapshot to latest/
        _snapshot_test(network, latest_dir)

        if not passed and snapshot_on_fail:
            # Preserve a timestamped copy for failure investigation
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            fail_dir = runs_dir / f"{timestamp}-{name}"
            shutil.copytree(latest_dir, fail_dir)
            logger.info(f"Failure snapshot: {fail_dir}")
            return TestResult(
                name=name,
                passed=False,
                duration=duration,
                error="Scenario failed",
                snapshot_dir=fail_dir,
            )

        return TestResult(name=name, passed=passed, duration=duration)

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
        return TestResult(
            name=name,
            passed=False,
            duration=duration,
            error=str(e),
            snapshot_dir=snapshot_dir,
        )
    else:
        network.teardown()


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
) -> list[TestResult]:
    """Run a scenario test suite.

    Args:
        suite_path: Path to the suite YAML file.
        xahaud_root: Path to the xahaud repository root.
        stop_on_fail: Stop suite on first failure.
        snapshot_on_fail: Snapshot logs on failure.
        test_filter: If set, only run tests matching these names.
            Supports ``name[label]`` for exact match, or ``name``
            to match all matrix variants.
        params_override: If set, override all matrix/params with these
            values (from ``--params-json``).
        env_override: If set, merge these env vars into every test config
            (overrides both defaults and per-test env).
        dry_run: Print plan without executing.

    Returns:
        List of TestResult for all executed tests.
    """
    suite = SuiteConfig.from_yaml(suite_path)

    # Expand matrix entries before filtering
    tests = _expand_tests(suite.tests, xahaud_root, params_override=params_override)

    if test_filter:
        tests = [
            t for t in tests if any(_test_matches(t["name"], f) for f in test_filter)
        ]
        if not tests:
            available = [t["name"] for t in _expand_tests(suite.tests, xahaud_root)]
            raise ValueError(
                f"No tests match filter {test_filter}. Available: {available}"
            )

    if dry_run:
        console = Console(stderr=True)
        console.print(f"\n[bold]Suite:[/bold] {suite_path}")
        console.print(f"[bold]Tests:[/bold] {len(tests)}")
        for i, test in enumerate(tests, 1):
            config = suite.effective_config(test)
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
    combined_log = xahaud_root / ".testnet" / "scenario-test.log"
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
