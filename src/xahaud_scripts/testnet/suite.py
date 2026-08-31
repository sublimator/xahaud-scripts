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
import inspect
import json
import math
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.table import Table

from xahaud_scripts.binary_registry import resolve_binary_spec
from xahaud_scripts.testnet.config import (
    MAX_NODE_COUNT,
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
    connect_managed_peer,
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
_DICT_MERGE_KEYS = {"log_levels", "env", "node_binaries"}

# Closed suite schema. ``runtime_topology`` is the legacy spelling retained as
# an intentional compatibility alias for ``topology``.
_SUITE_KEYS = {"defaults", "tests"}
_DEFAULT_KEYS = {"network", "params"}
_TEST_KEYS = {"name", "script", "network", "params"}
_NETWORK_KEYS = {
    "desktop",
    "env",
    "extra_args",
    "features",
    "find_ports",
    "fixed_peers",
    "genesis_file",
    "launcher",
    "lldb",
    "log_levels",
    "majority_features",
    "node_binaries",
    "node_count",
    "node_env",
    "quorum",
    "rc",
    "runtime_topology",
    "slave_delay",
    "start_ledger",
    "topology",
    "track_features",
    "unl_report",
    "validators",
}
_TOPOLOGY_KEYS = {
    "bidirectional",
    "connect",
    "disconnect",
    "edges",
    "exact",
    "nodes",
    "poll_interval",
    "rpc_timeout",
    "settle_timeout",
    "stable_for",
    "timeout",
}

# Test names become directory names below .testnet/output/runs. Keep the
# grammar deliberately filesystem-portable; ``@`` is needed by expanded
# variant names (``base@label``).
_TEST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]*$")
_MISSING = object()


def _require_non_empty_str(value: Any, label: str) -> str:
    """Require a non-empty string leaf."""
    if not isinstance(value, str) or not value.strip():
        got = type(value).__name__
        raise ValueError(f"{label} must be a non-empty string, got {got}")
    return value


def _require_int(
    value: Any,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Require an integer leaf. bool is an int in Python, so exclude it."""
    if isinstance(value, bool) or not isinstance(value, int):
        got = type(value).__name__
        raise ValueError(f"{label} must be an integer, got {got}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} must be <= {maximum}, got {value}")
    return value


def _require_number(value: Any, label: str, *, minimum: float | None = None) -> float:
    """Require a finite numeric leaf (int or float), excluding bool.

    NaN and infinity have to be rejected explicitly: `nan < 0` is False, so a
    plain minimum check waves them through, and `time.sleep(nan)` then raises
    only after the nodes have been launched.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        got = type(value).__name__
        raise ValueError(f"{label} must be a number, got {got}")
    if not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number, got {value}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be >= {minimum}, got {value}")
    return float(value)


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        got = type(value).__name__
        raise ValueError(f"{label} must be true or false, got {got}")
    return value


def _require_str_list(value: Any, label: str) -> list[str]:
    """Require a list of non-empty strings (features, rc specs, ...)."""
    if isinstance(value, str) or not isinstance(value, list):
        got = type(value).__name__
        raise ValueError(
            f"{label} must be a list of strings, got {got} "
            "(each entry is a separate list item)"
        )
    return [_require_non_empty_str(entry, f"{label} entry") for entry in value]


def _require_mapping(value: Any, label: str) -> None:
    """Reject valid YAML of the wrong shape with a ValueError, not a TypeError.

    ``tests: [42]`` and ``network: []`` parse fine as YAML and only fail later
    on ``in``/``.items()``, where they read as an internal crash rather than the
    configuration mistake they are.
    """
    if not isinstance(value, dict):
        got = type(value).__name__
        raise ValueError(f"{label} must be a mapping, got {got}")


def _reject_unknown_keys(value: dict[Any, Any], allowed: set[str], label: str) -> None:
    """Reject misspelled/unsupported schema keys instead of ignoring them."""
    unknown = sorted((key for key in value if key not in allowed), key=repr)
    if unknown:
        rendered = ", ".join(repr(key) for key in unknown)
        raise ValueError(f"{label} has unknown key(s): {rendered}")


def _validated_test_name(value: Any, label: str = "Test name") -> str:
    """Validate one filesystem-safe suite/expanded test name."""
    name = _require_non_empty_str(value, label)
    if not _TEST_NAME_RE.fullmatch(name):
        raise ValueError(
            f"{label} must start with a letter or digit and use only letters, "
            f"digits, '.', '_', '@', or '-', got {name!r}"
        )
    return name


def _contained_run_dir(runs_dir: Path, *parts: str) -> Path:
    """Build a run-output path and reject existing symlink escapes."""
    candidate = runs_dir.joinpath(*parts)
    root = runs_dir.resolve()
    if not candidate.resolve().is_relative_to(root):
        raise ValueError(f"Run output path escapes {runs_dir}: {candidate}")
    return candidate


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
              node_binaries:
                2: "@old-release"
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
        """Load and validate a suite YAML file.

        Every nested shape is checked here rather than where it is consumed.
        Valid YAML of the wrong shape (``tests: [42]``, ``network: []``) used to
        reach ``.items()``/``in`` on the wrong type and surface as a raw
        TypeError or AttributeError traceback.
        """
        with open(path) as f:
            raw = yaml.safe_load(f)

        _require_mapping(raw, "Suite file")
        _reject_unknown_keys(raw, _SUITE_KEYS, "Suite file")

        defaults = raw.get("defaults", {})
        _require_mapping(defaults, "'defaults'")
        _reject_unknown_keys(defaults, _DEFAULT_KEYS, "'defaults'")
        if "network" in defaults:
            _require_mapping(defaults["network"], "'defaults.network'")
            _reject_unknown_keys(
                defaults["network"],
                _NETWORK_KEYS,
                "'defaults.network'",
            )
        _validated_params(defaults.get("params"), "'defaults.params'")

        tests = raw.get("tests")
        if not tests or not isinstance(tests, list):
            raise ValueError("Suite file must have a non-empty 'tests' list")

        for i, test in enumerate(tests):
            _require_mapping(test, f"Test #{i + 1}")
            _reject_unknown_keys(test, _TEST_KEYS, f"Test #{i + 1}")
            if "name" not in test:
                raise ValueError(f"Test #{i + 1} missing required 'name' key")
            _validated_test_name(test["name"], f"Test #{i + 1} 'name'")
            if "script" not in test:
                raise ValueError(f"Test '{test['name']}' missing required 'script' key")
            _require_non_empty_str(test["script"], f"Test '{test['name']}' 'script'")
            if "network" in test:
                _require_mapping(test["network"], f"Test '{test['name']}' 'network'")
                _reject_unknown_keys(
                    test["network"],
                    _NETWORK_KEYS,
                    f"Test '{test['name']}' 'network'",
                )
            _validated_params(test.get("params"), f"Test '{test['name']}' 'params'")

        return cls(
            defaults=defaults,
            tests=tests,
        )

    @staticmethod
    def get_test_description(script_path: Path) -> str | None:
        """Extract description from a test script's module docstring.

        Looks for a :descr: tag first, falls back to the first line.
        """
        try:
            tree = ast.parse(script_path.read_bytes(), filename=str(script_path))
        except (OSError, UnicodeError, SyntaxError):
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


class _ScopeGlobalFinder(ast.NodeVisitor):
    """Find a global declaration in one class code block."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.found = False

    def visit_Global(self, node: ast.Global) -> None:  # noqa: N802
        if self.name in node.names:
            self.found = True

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return

    def visit_AsyncFunctionDef(  # noqa: N802
        self, node: ast.AsyncFunctionDef
    ) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return


def _scope_declares_global(statements: list[ast.stmt], name: str) -> bool:
    finder = _ScopeGlobalFinder(name)
    for statement in statements:
        finder.visit(statement)
    return finder.found


class _ImportTimeBindingFinder(ast.NodeVisitor):
    """Find whether a module-level statement can bind or delete one name.

    Compound statements execute in module scope, so their bodies are visited.
    Function/lambda bodies and comprehension targets have their own scopes and
    are skipped, while expressions evaluated while defining them are retained.
    """

    def __init__(
        self,
        name: str,
        *,
        deferred_generators: set[int] | None = None,
    ) -> None:
        self.name = name
        self.found = False
        self.deferred_generators = deferred_generators or set()

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if node.id == self.name and isinstance(node.ctx, (ast.Store, ast.Del)):
            self.found = True

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        # ``name: Type`` updates __annotations__ but does not bind or replace
        # ``name``. Only an annotated assignment with a value does so.
        if node.value is not None:
            self.visit(node.target)
            self.visit(node.value)
        self.visit(node.annotation)

    def _visit_definition_expressions(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        if node.name == self.name:
            self.found = True
        self._visit_definition_expressions(node)

    def visit_AsyncFunctionDef(  # noqa: N802
        self, node: ast.AsyncFunctionDef
    ) -> None:
        if node.name == self.name:
            self.found = True
        self._visit_definition_expressions(node)

    def _visit_class_definition(
        self,
        node: ast.ClassDef,
        *,
        enclosing_binds_module: bool,
    ) -> None:
        if enclosing_binds_module and node.name == self.name:
            self.found = True
        if enclosing_binds_module:
            for expression in (*node.decorator_list, *node.bases):
                self.visit(expression)
            for keyword in node.keywords:
                self.visit(keyword.value)
        # A class body normally binds its own namespace, but a ``global``
        # declaration redirects every matching store/delete in that class code
        # block to the module. Class bodies execute immediately during import.
        if _scope_declares_global(node.body, self.name):
            for statement in node.body:
                self.visit(statement)
        else:
            # Even when this class keeps ordinary local bindings, a nested
            # class body executes and may carry its own module-level ``global``
            # declaration. Traverse those executing class scopes without
            # treating their names or enclosing-class expressions as globals.
            for statement in node.body:
                self._visit_nested_class_scopes(statement)

    def _visit_nested_class_scopes(self, node: ast.AST) -> None:
        if isinstance(node, ast.ClassDef):
            self._visit_class_definition(node, enclosing_binds_module=False)
            return
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
        ):
            return
        for child in ast.iter_child_nodes(node):
            self._visit_nested_class_scopes(child)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._visit_class_definition(node, enclosing_binds_module=True)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        # The body is deferred, but defaults execute when the lambda object is
        # created and assignment expressions there bind the enclosing scope.
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)

    def _visit_comprehension(
        self,
        value_nodes: tuple[ast.AST, ...],
        generators: list[ast.comprehension],
    ) -> None:
        for value in value_nodes:
            self.visit(value)
        for generator in generators:
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)

    def visit_ListComp(self, node: ast.ListComp) -> None:  # noqa: N802
        self._visit_comprehension((node.elt,), node.generators)

    def visit_SetComp(self, node: ast.SetComp) -> None:  # noqa: N802
        self._visit_comprehension((node.elt,), node.generators)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:  # noqa: N802
        if id(node) in self.deferred_generators:
            # Creating a generator evaluates only the outermost iterable. Its
            # element, filters, and nested iterables remain deferred until
            # iteration; a later consumer is recorded as a separate binding event.
            self.visit(node.generators[0].iter)
        else:
            # Calls and later module statements may iterate a generator during
            # import. Conservatively retain bindings from bodies that can escape.
            self._visit_comprehension((node.elt,), node.generators)

    def visit_DictComp(self, node: ast.DictComp) -> None:  # noqa: N802
        self._visit_comprehension((node.key, node.value), node.generators)

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            bound = alias.asname or alias.name.split(".", 1)[0]
            if bound == self.name:
                self.found = True

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        for alias in node.names:
            if alias.name == "*" or (alias.asname or alias.name) == self.name:
                self.found = True

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        if node.name == self.name:
            self.found = True
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:  # noqa: N802
        if node.name == self.name:
            self.found = True
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:  # noqa: N802
        if node.name == self.name:
            self.found = True

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:  # noqa: N802
        if node.rest == self.name:
            self.found = True
        self.generic_visit(node)


class _NameLoadFinder(ast.NodeVisitor):
    """Find a later reference that may make a stored generator escape."""

    def __init__(self, names: set[str]) -> None:
        self.names = names
        self.found = False

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if node.id in self.names and isinstance(node.ctx, ast.Load):
            self.found = True

    def _visit_assignment_receiver(self, node: ast.AST) -> None:
        """Visit executable receiver parts without treating its root as a read."""
        if isinstance(node, ast.Name):
            return
        if isinstance(node, ast.Attribute):
            self._visit_assignment_receiver(node.value)
            return
        if isinstance(node, ast.Subscript):
            self._visit_assignment_receiver(node.value)
            self.visit(node.slice)
            return
        self.visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        # Evaluating ``holder.value = ...`` loads ``holder`` but does not read,
        # escape, or consume a generator previously stored on it. A load of the
        # attribute itself still visits the root name. Complex receivers still
        # execute: ``next(generator).value = ...`` consumes the generator.
        if isinstance(node.ctx, ast.Load):
            self.generic_visit(node)
        else:
            self._visit_assignment_receiver(node.value)

    def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: N802
        if isinstance(node.ctx, ast.Load):
            self.generic_visit(node)
        else:
            # The container root is storage-only, but computed receivers and
            # indices execute before the assignment and may consume a handle.
            self._visit_assignment_receiver(node.value)
            self.visit(node.slice)

    def _visit_definition_expressions(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        # Decorators and defaults execute while the function is defined; its
        # body remains deferred until a later call.
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_definition_expressions(node)

    def visit_AsyncFunctionDef(  # noqa: N802
        self, node: ast.AsyncFunctionDef
    ) -> None:
        self._visit_definition_expressions(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)


def _stored_generator_nodes(node: ast.AST) -> list[ast.GeneratorExp]:
    """Return generators whose value is retained without an immediate consumer."""
    if isinstance(node, ast.GeneratorExp):
        return [node]
    if isinstance(node, ast.Lambda):
        result: list[ast.GeneratorExp] = []
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                result.extend(_stored_generator_nodes(default))
        return result
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [
            generator
            for value in node.elts
            for generator in _stored_generator_nodes(value)
        ]
    if isinstance(node, ast.BoolOp):
        return [
            generator
            for value in node.values
            for generator in _stored_generator_nodes(value)
        ]
    if isinstance(node, ast.Dict):
        result = []
        for key, value in zip(node.keys, node.values, strict=True):
            if key is not None:
                result.extend(_stored_generator_nodes(key))
            result.extend(_stored_generator_nodes(value))
        return result
    if isinstance(node, ast.IfExp):
        return [
            *_stored_generator_nodes(node.body),
            *_stored_generator_nodes(node.orelse),
        ]
    if isinstance(node, ast.NamedExpr):
        return _stored_generator_nodes(node.value)
    if isinstance(node, ast.Starred):
        # Starred displays consume their operand while building the container;
        # a direct generator here is not deferred storage. Unpacking a literal
        # container retains any generator values inside that container.
        if isinstance(node.value, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
            return _stored_generator_nodes(node.value)
        return []
    return []


def _target_root_names(node: ast.AST) -> set[str]:
    """Return module-visible roots capable of retaining an assigned value."""
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Attribute, ast.Subscript, ast.Starred)):
        return _target_root_names(node.value)
    if isinstance(node, (ast.Tuple, ast.List)):
        return set().union(*(_target_root_names(item) for item in node.elts))
    return set()


def _target_requires_unpack(node: ast.AST) -> bool:
    """Return whether assignment to this target iterates the source value."""
    return isinstance(node, (ast.Tuple, ast.List))


def _direct_generator_value(node: ast.AST) -> bool:
    """Return whether an expression itself can produce a generator value."""
    if isinstance(node, ast.GeneratorExp):
        return True
    if isinstance(node, ast.NamedExpr):
        return _direct_generator_value(node.value)
    if isinstance(node, ast.BoolOp):
        return any(_direct_generator_value(value) for value in node.values)
    if isinstance(node, ast.IfExp):
        return _direct_generator_value(node.body) or _direct_generator_value(
            node.orelse
        )
    return False


def _named_storage_handle_names(node: ast.AST) -> set[str]:
    """Find assignment-expression names that retain a stored generator value."""
    if isinstance(node, ast.NamedExpr):
        return _target_root_names(node.target) | _named_storage_handle_names(node.value)
    if isinstance(node, ast.GeneratorExp):
        return set()
    if isinstance(node, ast.Lambda):
        return set().union(
            *(
                _named_storage_handle_names(default)
                for default in (*node.args.defaults, *node.args.kw_defaults)
                if default is not None
            )
        )
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return set().union(*(_named_storage_handle_names(item) for item in node.elts))
    if isinstance(node, ast.BoolOp):
        return set().union(
            *(_named_storage_handle_names(value) for value in node.values)
        )
    if isinstance(node, ast.Dict):
        return set().union(
            *(
                _named_storage_handle_names(item)
                for item in (*node.keys, *node.values)
                if item is not None
            )
        )
    if isinstance(node, ast.IfExp):
        return _named_storage_handle_names(node.body) | _named_storage_handle_names(
            node.orelse
        )
    if isinstance(node, ast.Starred):
        return _named_storage_handle_names(node.value)
    return set()


class _GeneratorStorageFinder(ast.NodeVisitor):
    """Find import-time generator storage within one module statement."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.generators: set[int] = set()
        self.handles: set[str] = set()
        self.same_statement_handles: set[str] = set()
        self.immediate_consumer = False

    def _record(
        self,
        values: tuple[ast.AST, ...],
        *,
        handles: set[str],
        same_statement_handles: set[str],
    ) -> bool:
        matched = False
        for value in values:
            for generator in _stored_generator_nodes(value):
                finder = _ImportTimeBindingFinder(self.name)
                finder.visit(generator)
                if finder.found:
                    self.generators.add(id(generator))
                    matched = True
        if matched:
            self.handles.update(handles)
            self.same_statement_handles.update(same_statement_handles)
        return matched

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        if any(_target_requires_unpack(target) for target in node.targets) and (
            _direct_generator_value(node.value)
        ):
            # Iterable assignment immediately consumes a direct generator RHS.
            return
        handles = set().union(
            *(_target_root_names(target) for target in node.targets)
        ) | _named_storage_handle_names(node.value)
        self._record(
            (node.value,),
            handles=handles,
            same_statement_handles=handles,
        )

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        if node.value is None:
            return
        handles = _target_root_names(node.target) | _named_storage_handle_names(
            node.value
        )
        self._record(
            (node.value,),
            handles=handles,
            same_statement_handles=handles,
        )

    def _visit_definition_defaults(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        defaults = tuple(
            default
            for default in (*node.args.defaults, *node.args.kw_defaults)
            if default is not None
        )
        matched = self._record(
            defaults,
            handles={node.name}.union(
                *(_named_storage_handle_names(default) for default in defaults)
            ),
            same_statement_handles=set(),
        )
        # Decorators receive the newly created function after its defaults have
        # been installed, so arbitrary decorator code can immediately inspect
        # and consume a generator retained in those defaults.
        if matched and node.decorator_list:
            self.immediate_consumer = True

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_definition_defaults(node)

    def visit_AsyncFunctionDef(  # noqa: N802
        self, node: ast.AsyncFunctionDef
    ) -> None:
        self._visit_definition_defaults(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        # Class bodies execute, but assignment expressions in comprehensions
        # are prohibited in a class scope. Stored module generators loaded by a
        # class body are handled by _NameLoadFinder instead.
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:  # noqa: N802
        return

    def visit_Expr(self, node: ast.Expr) -> None:  # noqa: N802
        # A bare generator is created and discarded without running its body.
        self._record(
            (node.value,),
            handles=_named_storage_handle_names(node.value),
            same_statement_handles=_named_storage_handle_names(node.value),
        )


class _AssignedHandleFinder(ast.NodeVisitor):
    """Collect names that may retain an already tracked generator handle."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if isinstance(node.ctx, ast.Store):
            self.names.add(node.id)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        for target in node.targets:
            self.names.update(_target_root_names(target))
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        self.names.update(_target_root_names(node.target))
        if node.value is not None:
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:  # noqa: N802
        self.names.update(_target_root_names(node.target))
        self.visit(node.value)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self.names.add(node.name)

    def visit_AsyncFunctionDef(  # noqa: N802
        self, node: ast.AsyncFunctionDef
    ) -> None:
        self.names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self.names.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return

    def _visit_comprehension(
        self,
        value_nodes: tuple[ast.AST, ...],
        generators: list[ast.comprehension],
    ) -> None:
        for value in value_nodes:
            self.visit(value)
        for generator in generators:
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)

    def visit_ListComp(self, node: ast.ListComp) -> None:  # noqa: N802
        self._visit_comprehension((node.elt,), node.generators)

    def visit_SetComp(self, node: ast.SetComp) -> None:  # noqa: N802
        self._visit_comprehension((node.elt,), node.generators)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:  # noqa: N802
        self.visit(node.generators[0].iter)

    def visit_DictComp(self, node: ast.DictComp) -> None:  # noqa: N802
        self._visit_comprehension((node.key, node.value), node.generators)


def _combine_handle_states(states: list[tuple[bool, bool]]) -> tuple[bool, bool]:
    """Combine ``(loads_handle, storage_only)`` expression classifications."""
    found = any(loads for loads, _storage_only in states)
    storage_only = all(not loads or safe for loads, safe in states)
    return found, storage_only


def _storage_expression_handle_state(
    node: ast.AST, handles: set[str]
) -> tuple[bool, bool]:
    """Classify whether an expression only retains tracked generator handles."""
    if isinstance(node, ast.Name):
        found = node.id in handles and isinstance(node.ctx, ast.Load)
        return found, True
    if isinstance(node, (ast.Constant, ast.Slice)):
        return False, True
    if isinstance(node, (ast.Attribute, ast.NamedExpr)):
        return _storage_expression_handle_state(node.value, handles)
    if isinstance(node, ast.Starred):
        # Starred displays unpack their operand immediately. A direct tracked
        # handle is therefore iterated rather than merely retained. Unpacking
        # a literal container merely copies the tracked values it contains.
        if isinstance(node.value, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
            return _storage_expression_handle_state(node.value, handles)
        finder = _NameLoadFinder(handles)
        finder.visit(node.value)
        return finder.found, not finder.found
    if isinstance(node, ast.Subscript):
        value_found, value_storage_only = _storage_expression_handle_state(
            node.value, handles
        )
        slice_finder = _NameLoadFinder(handles)
        slice_finder.visit(node.slice)
        return (
            value_found or slice_finder.found,
            value_storage_only and not slice_finder.found,
        )
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return _combine_handle_states(
            [_storage_expression_handle_state(item, handles) for item in node.elts]
        )
    if isinstance(node, ast.Dict):
        return _combine_handle_states(
            [
                _storage_expression_handle_state(item, handles)
                for item in (*node.keys, *node.values)
                if item is not None
            ]
        )
    if isinstance(node, ast.BoolOp):
        # Every operand except the last is truth-tested. A tracked root can be
        # an ordinary generator (whose truth value is harmless) or a holder
        # with user-defined __bool__/__len__ that consumes the retained value;
        # without executing it, the latter must be treated as a consumer.
        truth_test_states: list[tuple[bool, bool]] = []
        for value in node.values[:-1]:
            finder = _NameLoadFinder(handles)
            finder.visit(value)
            truth_test_states.append((finder.found, not finder.found))
        return _combine_handle_states(
            [
                *truth_test_states,
                _storage_expression_handle_state(node.values[-1], handles),
            ]
        )
    if isinstance(node, ast.IfExp):
        test_finder = _NameLoadFinder(handles)
        test_finder.visit(node.test)
        return _combine_handle_states(
            [
                (test_finder.found, not test_finder.found),
                _storage_expression_handle_state(node.body, handles),
                _storage_expression_handle_state(node.orelse, handles),
            ]
        )
    if isinstance(node, ast.Lambda):
        return _combine_handle_states(
            [
                _storage_expression_handle_state(default, handles)
                for default in (*node.args.defaults, *node.args.kw_defaults)
                if default is not None
            ]
        )
    if isinstance(node, ast.GeneratorExp):
        # Creating a generator evaluates only its outermost iterable. If that
        # iterable consumes a tracked handle, this expression is not storage.
        # Otherwise the body remains deferred and the new generator retains
        # anything it references.
        if _generator_creation_consumes_handle(node, handles):
            return True, False
        finder = _NameLoadFinder(handles)
        finder.visit(node)
        return finder.found, True

    finder = _NameLoadFinder(handles)
    finder.visit(node)
    return finder.found, not finder.found


def _direct_handle_value(node: ast.AST, handles: set[str]) -> bool:
    """Return whether an expression may itself produce one tracked handle."""
    if isinstance(node, ast.Name):
        return node.id in handles and isinstance(node.ctx, ast.Load)
    if isinstance(node, (ast.Attribute, ast.Subscript, ast.NamedExpr)):
        return _direct_handle_value(node.value, handles)
    if isinstance(node, ast.BoolOp):
        return any(_direct_handle_value(value, handles) for value in node.values)
    if isinstance(node, ast.IfExp):
        return _direct_handle_value(node.body, handles) or _direct_handle_value(
            node.orelse, handles
        )
    return False


def _generator_creation_consumes_handle(node: ast.AST, handles: set[str]) -> bool:
    """Return whether evaluating ``node`` creates a consuming generator."""
    if isinstance(node, ast.GeneratorExp):
        # Creating a generator evaluates only its outermost iterable. Recurse
        # through generators created by that expression without inspecting
        # their deferred elements, filters, or later iterable clauses.
        outer_iterable = node.generators[0].iter
        found, storage_only = _storage_expression_handle_state(
            outer_iterable,
            handles,
        )
        return (found and not storage_only) or _generator_creation_consumes_handle(
            outer_iterable,
            handles,
        )
    if isinstance(node, ast.Lambda):
        return any(
            _generator_creation_consumes_handle(default, handles)
            for default in (*node.args.defaults, *node.args.kw_defaults)
            if default is not None
        )
    return any(
        _generator_creation_consumes_handle(child, handles)
        for child in ast.iter_child_nodes(node)
    )


def _call_positional_values(node: ast.AST) -> tuple[ast.AST, ...]:
    """Return values a call may receive from one argument expression."""
    if isinstance(node, ast.Starred):
        return _starred_call_values(node.value)
    return (node,)


_UNKNOWN_SUBSCRIPT = object()


def _static_subscript(slice_node: ast.AST) -> object:
    """Return a literal index, key, or slice, or the unknown sentinel."""
    if isinstance(slice_node, ast.Slice):
        parts: list[object] = []
        for bound in (slice_node.lower, slice_node.upper, slice_node.step):
            if bound is None:
                parts.append(None)
                continue
            try:
                parts.append(ast.literal_eval(bound))
            except (ValueError, TypeError):
                return _UNKNOWN_SUBSCRIPT
        return slice(*parts)
    try:
        return ast.literal_eval(slice_node)
    except (ValueError, TypeError):
        return _UNKNOWN_SUBSCRIPT


def _literal_sequence_items(container: ast.AST) -> tuple[ast.AST, ...] | None:
    """Expand an exact builtin tuple/list display into its runtime items."""
    if isinstance(container, ast.NamedExpr):
        return _literal_sequence_items(container.value)
    if isinstance(container, ast.IfExp):
        truth = _static_truth_value(container.test)
        if truth is None:
            return None
        return _literal_sequence_items(container.body if truth else container.orelse)
    if isinstance(container, ast.BoolOp):
        selected = _static_boolop_value(container)
        if selected is None:
            return None
        return _literal_sequence_items(selected)
    if not isinstance(container, (ast.Tuple, ast.List)):
        return None
    items: list[ast.AST] = []
    for item in container.elts:
        if not isinstance(item, ast.Starred):
            items.append(item)
            continue
        expanded = _literal_sequence_items(item.value)
        if expanded is None:
            return None
        items.extend(expanded)
    return tuple(items)


def _index_literal_sequence(
    items: tuple[ast.AST, ...], index: object
) -> tuple[ast.AST, ...] | None:
    """Index or slice a resolved sequence of AST items."""
    if isinstance(index, slice):
        try:
            return tuple(items[index])
        except (TypeError, ValueError):
            return None
    if isinstance(index, int):
        try:
            return (items[index],)
        except IndexError:
            return ()
    return None


def _literal_dict_entries(
    container: ast.AST,
) -> tuple[tuple[object, ast.AST], ...] | None:
    """Resolve entries from an exact builtin dict display, in update order."""
    if isinstance(container, ast.NamedExpr):
        return _literal_dict_entries(container.value)
    if isinstance(container, ast.BoolOp):
        selected = _static_boolop_value(container)
        if selected is None:
            return None
        return _literal_dict_entries(selected)
    if isinstance(container, ast.IfExp):
        truth = _static_truth_value(container.test)
        if truth is None:
            return None
        return _literal_dict_entries(container.body if truth else container.orelse)
    if isinstance(container, ast.Subscript):
        subscript = _static_subscript(container.slice)
        if subscript is _UNKNOWN_SUBSCRIPT or isinstance(subscript, slice):
            return None
        selected_values = _literal_subscript_values(container)
        if selected_values is None or len(selected_values) != 1:
            return None
        return _literal_dict_entries(selected_values[0])
    if not isinstance(container, ast.Dict):
        return None
    entries: list[tuple[object, ast.AST]] = []
    for key, mapped in zip(container.keys, container.values, strict=True):
        if key is None:
            expanded = _literal_dict_entries(mapped)
            if expanded is None:
                return None
            entries.extend(expanded)
            continue
        try:
            literal_key = ast.literal_eval(key)
            hash(literal_key)
        except (ValueError, TypeError):
            return None
        entries.append((literal_key, mapped))
    return tuple(entries)


def _literal_dict_lookup(node: ast.Dict, index: object) -> tuple[ast.AST, ...] | None:
    """Return the last equal-key value from an exact literal dict display."""
    if isinstance(index, slice):
        return None
    try:
        hash(index)
    except TypeError:
        return None
    entries = _literal_dict_entries(node)
    if entries is None:
        return None
    selected: tuple[ast.AST, ...] = ()
    for literal_key, mapped in entries:
        if literal_key == index:
            selected = (mapped,)
    return selected


def _static_truth_value(node: ast.AST) -> bool | None:
    """Return the truth of an exact literal, or None when it is not static."""
    if isinstance(node, ast.NamedExpr):
        return _static_truth_value(node.value)
    # ``ast.literal_eval`` accepts ``set()`` as an empty-set representation,
    # but real source resolves it as a shadowable function call.
    if isinstance(node, ast.Call):
        return None
    try:
        return bool(ast.literal_eval(node))
    except (ValueError, TypeError):
        return None


def _static_boolop_value(node: ast.BoolOp) -> ast.AST | None:
    """Return the operand selected by an exact builtin boolean expression."""
    for operand in node.values[:-1]:
        truth = _static_truth_value(operand)
        if truth is None:
            return None
        if (isinstance(node.op, ast.Or) and truth) or (
            isinstance(node.op, ast.And) and not truth
        ):
            return operand
    return node.values[-1]


def _index_literal_container(
    container: ast.AST, index: object
) -> tuple[ast.AST, ...] | None:
    """Index a builtin literal tuple/list/dict, including wrapper bases."""
    if isinstance(container, ast.NamedExpr):
        return _index_literal_container(container.value, index)
    if isinstance(container, ast.IfExp):
        truth = _static_truth_value(container.test)
        if truth is not None:
            return _index_literal_container(
                container.body if truth else container.orelse, index
            )
        left = _index_literal_container(container.body, index)
        right = _index_literal_container(container.orelse, index)
        if left is None or right is None:
            return None
        return (*left, *right)
    if isinstance(container, ast.BoolOp):
        selected = _static_boolop_value(container)
        if selected is None:
            return None
        return _index_literal_container(selected, index)
    if isinstance(container, ast.Subscript):
        inner = _literal_subscript_values(container)
        if inner is None:
            return None
        # A slice produces a new top-level sequence. A later subscript indexes
        # that result, not the contents of its sole selected AST item.
        if isinstance(_static_subscript(container.slice), slice):
            return _index_literal_sequence(inner, index)
        if len(inner) == 1 and isinstance(
            inner[0],
            (ast.Tuple, ast.List, ast.Dict, ast.NamedExpr, ast.IfExp, ast.BoolOp),
        ):
            return _index_literal_container(inner[0], index)
        return _index_literal_sequence(inner, index)
    if isinstance(container, (ast.Tuple, ast.List)):
        items = _literal_sequence_items(container)
        if items is None:
            return None
        return _index_literal_sequence(items, index)
    if isinstance(container, ast.Dict):
        return _literal_dict_lookup(container, index)
    return None


def _literal_subscript_values(
    node: ast.Subscript,
) -> tuple[ast.AST, ...] | None:
    """Return a statically selected literal container item, when exact."""
    index = _static_subscript(node.slice)
    if index is _UNKNOWN_SUBSCRIPT:
        return None
    return _index_literal_container(node.value, index)


def _starred_call_values(value: ast.AST) -> tuple[ast.AST, ...]:
    """Return values yielded by starring one expression as a call argument."""
    if isinstance(value, ast.NamedExpr):
        return _starred_call_values(value.value)
    if isinstance(value, ast.IfExp):
        truth = _static_truth_value(value.test)
        if truth is not None:
            return _starred_call_values(value.body if truth else value.orelse)
        return (
            *_starred_call_values(value.body),
            *_starred_call_values(value.orelse),
        )
    if isinstance(value, ast.BoolOp):
        boolop_selected = _static_boolop_value(value)
        if boolop_selected is not None:
            return _starred_call_values(boolop_selected)
        return tuple(
            positional
            for operand in value.values
            for positional in _starred_call_values(operand)
        )
    if isinstance(value, ast.Subscript):
        selected = _literal_subscript_values(value)
        if selected is not None:
            # A slice result is itself starred exactly once: its top-level
            # elements become arguments, but nested literal containers do not.
            if isinstance(_static_subscript(value.slice), slice):
                return selected
            return tuple(
                positional
                for item in selected
                for positional in _starred_call_values(item)
            )
    if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
        return tuple(
            positional
            for item in value.elts
            for positional in _call_positional_values(item)
        )
    if isinstance(value, ast.Dict):
        positional: list[ast.AST] = []
        for key, mapped in zip(value.keys, value.values, strict=True):
            if key is not None:
                positional.extend(_call_positional_values(key))
            elif isinstance(
                mapped,
                (ast.Dict, ast.NamedExpr, ast.IfExp, ast.BoolOp, ast.Subscript),
            ):
                positional.extend(_starred_call_values(mapped))
        return tuple(positional)
    return (value,)


class _FilterLoadFinder(_NameLoadFinder):
    """Find immediate handle use while respecting nested generator deferral."""

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:  # noqa: N802
        # A generator object's truth value does not iterate it, but creating it
        # can recursively create generators in its outermost iterable.
        if _generator_creation_consumes_handle(node, self.names):
            self.found = True

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        # A bare generator in a filter is only truth-tested. A generator
        # value passed as a call argument may be evaluated by any/list/next and
        # similar, while a starred argument is iterated before the call. Handle
        # direct generators and the wrappers that can preserve their value.
        # Star-unpacking a literal container yields its elements as call
        # arguments; iterating the container itself does not consume a
        # generator stored inside it.
        self.visit(node.func)
        for raw in (*node.args, *(keyword.value for keyword in node.keywords)):
            for item in _call_positional_values(raw):
                value = item.value if isinstance(item, ast.Starred) else item
                if _iterable_unpack_consumes_handle(value, self.names):
                    self.found = True
                    return
            self.visit(raw)


def _iterable_unpack_consumes_handle(node: ast.AST, handles: set[str]) -> bool:
    """Return whether iterating an expression may consume one tracked handle."""
    found, storage_only = _storage_expression_handle_state(node, handles)
    if found and not storage_only:
        return True
    if _direct_handle_value(node, handles):
        return True
    if isinstance(node, ast.GeneratorExp):
        found, storage_only = _storage_expression_handle_state(node.elt, handles)
        if found and not storage_only:
            return True
        for generator in node.generators:
            if _iterable_unpack_consumes_handle(generator.iter, handles):
                return True
            for condition in generator.ifs:
                finder = _FilterLoadFinder(handles)
                finder.visit(condition)
                if finder.found:
                    return True
        return False
    if isinstance(node, ast.NamedExpr):
        return _iterable_unpack_consumes_handle(node.value, handles)
    if isinstance(node, ast.BoolOp):
        return any(
            _iterable_unpack_consumes_handle(value, handles) for value in node.values
        )
    if isinstance(node, ast.IfExp):
        return _iterable_unpack_consumes_handle(
            node.body, handles
        ) or _iterable_unpack_consumes_handle(node.orelse, handles)
    return False


def _pure_handle_storage(statement: ast.stmt, handles: set[str]) -> set[str] | None:
    """Return new aliases if a statement stores handles without consuming them."""
    values: tuple[ast.AST, ...]
    extra_handles: set[str] = set()
    if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Expr)):
        if statement.value is None:
            return None
        if (
            isinstance(statement, ast.Assign)
            and any(_target_requires_unpack(target) for target in statement.targets)
            and _iterable_unpack_consumes_handle(statement.value, handles)
        ):
            return None
        values = (statement.value,)
    elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        defaults = tuple(
            default
            for default in (*statement.args.defaults, *statement.args.kw_defaults)
            if default is not None
        )
        if statement.decorator_list:
            # Decorator callables receive the completed function and can inspect
            # any tracked generators retained in its defaults. A tracked value
            # used as the decorator is itself invoked as well.
            decorator_finder = _NameLoadFinder(handles)
            for decorator in statement.decorator_list:
                decorator_finder.visit(decorator)
            default_finder = _NameLoadFinder(handles)
            for default in defaults:
                default_finder.visit(default)
            if decorator_finder.found or default_finder.found:
                return None
        values = tuple(statement.decorator_list) + defaults
        extra_handles = {statement.name}.union(
            *(_named_storage_handle_names(value) for value in values)
        )
    else:
        return None

    found, storage_only = _combine_handle_states(
        [_storage_expression_handle_state(value, handles) for value in values]
    )
    if not found or not storage_only:
        return None
    assigned = _AssignedHandleFinder()
    assigned.visit(statement)
    return assigned.names | extra_handles


def _direct_target_names(node: ast.AST) -> set[str]:
    """Return names definitely rebound by assigning or deleting one target."""
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Starred):
        return _direct_target_names(node.value)
    if isinstance(node, (ast.Tuple, ast.List)):
        return set().union(*(_direct_target_names(item) for item in node.elts))
    return set()


def _definitely_rebound_handle_names(statement: ast.stmt) -> set[str]:
    """Find simple module bindings that replace an earlier handle on success."""
    if isinstance(statement, ast.Assign):
        return set().union(
            *(_direct_target_names(target) for target in statement.targets)
        )
    if isinstance(statement, ast.AnnAssign):
        return (
            _direct_target_names(statement.target)
            if statement.value is not None
            else set()
        )
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {statement.name}
    if isinstance(statement, ast.Delete):
        return set().union(
            *(_direct_target_names(target) for target in statement.targets)
        )
    if isinstance(statement, ast.Import):
        return {
            alias.asname or alias.name.split(".", 1)[0] for alias in statement.names
        }
    if isinstance(statement, ast.ImportFrom):
        return {
            alias.asname or alias.name for alias in statement.names if alias.name != "*"
        }
    return set()


def _deferred_generator_bindings(
    tree: ast.Module, name: str
) -> tuple[set[int], set[int]]:
    """Find stored generators and later statements that may consume them."""
    deferred_generators: set[int] = set()
    consumer_statements: set[int] = set()
    storages: list[_GeneratorStorageFinder] = []
    for statement in tree.body:
        storage = _GeneratorStorageFinder(name)
        storage.visit(statement)
        storages.append(storage)
        deferred_generators.update(storage.generators)

    tracked_handles: set[str] = set()
    for statement, storage in zip(tree.body, storages, strict=True):
        rebound_handles = _definitely_rebound_handle_names(statement)
        load_finder = _NameLoadFinder(tracked_handles)
        load_finder.visit(statement)
        local_load_finder = _NameLoadFinder(storage.same_statement_handles)
        local_load_finder.visit(statement)
        aliases = (
            _pure_handle_storage(statement, tracked_handles)
            if load_finder.found
            else None
        )
        if aliases is not None:
            tracked_handles.difference_update(rebound_handles)
            tracked_handles.update(aliases)
        elif storage.immediate_consumer or load_finder.found or local_load_finder.found:
            consumer_statements.add(id(statement))
            assigned = _AssignedHandleFinder()
            assigned.visit(statement)
            tracked_handles.difference_update(rebound_handles)
            tracked_handles.update(assigned.names)
        else:
            tracked_handles.difference_update(rebound_handles)
        tracked_handles.update(storage.handles)
    return deferred_generators, consumer_statements


def _statement_binds_name(
    statement: ast.stmt,
    name: str,
    *,
    deferred_generators: set[int] | None = None,
) -> bool:
    finder = _ImportTimeBindingFinder(
        name,
        deferred_generators=deferred_generators,
    )
    finder.visit(statement)
    return finder.found


def _script_may_define_variants(script_path: Path) -> bool:
    """Return whether importing a script could expose ``variants``.

    Most scenarios have no variants. Avoid executing their module bodies just
    to prove an attribute is absent; direct and dynamically-computed assignment
    forms such as ``variants = make_variants()`` are still discovered.
    """
    try:
        # Parse bytes so Python honors a PEP 263 encoding cookie exactly as the
        # import loader will when it eventually executes the scenario.
        tree = ast.parse(script_path.read_bytes(), filename=str(script_path))
    except (OSError, UnicodeError, SyntaxError):
        # Preserve the existing load error for unreadable/malformed scripts;
        # whole-suite preflight will still stop before lifecycle mutation.
        return True
    deferred_generators, generator_consumers = _deferred_generator_bindings(
        tree, "variants"
    )
    return any(
        id(statement) in generator_consumers
        or _statement_binds_name(
            statement, "variants", deferred_generators=deferred_generators
        )
        for statement in tree.body
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
        if script_path.exists() and _script_may_define_variants(script_path):
            try:
                variants = load_scenario_variants(script_path)
            except Exception as exc:
                raise ValueError(
                    f"Could not load scenario variants from {script_path}: {exc}"
                ) from exc

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


def _validated_node_env(
    raw: Any,
    *,
    node_count: int,
) -> dict[int, dict[str, str]]:
    """Validate and stringify the YAML node_env mapping."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("network.node_env must be a mapping of node_id: env")

    result: dict[int, dict[str, str]] = {}
    for node_id_raw, env_dict in raw.items():
        try:
            node_id = parse_node_ref(node_id_raw)
        except ValueError as exc:
            raise ValueError(
                f"network.node_env key must be a node id: {node_id_raw!r}"
            ) from exc
        if node_id < 0 or node_id >= node_count:
            raise ValueError(
                f"network.node_env n{node_id} is outside this {node_count}-node network"
            )
        if node_id in result:
            raise ValueError(
                f"network.node_env contains duplicate aliases for n{node_id}"
            )
        result[node_id] = _validated_env_mapping(
            env_dict,
            label=f"network.node_env.{node_id}",
        )
    return result


def _validated_node_binaries(raw: Any, *, node_count: int) -> dict[int, Path]:
    """Validate and resolve the YAML node_binaries mapping."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("network.node_binaries must be a mapping of node_id: binary")

    result: dict[int, Path] = {}
    for node_id_raw, spec_raw in raw.items():
        try:
            node_id = parse_node_ref(node_id_raw)
        except ValueError as exc:
            raise ValueError(
                f"network.node_binaries key must be a node id: {node_id_raw!r}"
            ) from exc

        if node_id < 0 or node_id >= node_count:
            raise ValueError(
                f"network.node_binaries n{node_id} is outside this "
                f"{node_count}-node network"
            )

        if not isinstance(spec_raw, (str, Path)):
            raise ValueError(
                f"network.node_binaries.n{node_id} must be an @alias or path"
            )
        spec = str(spec_raw)
        if not spec:
            raise ValueError(
                f"network.node_binaries.n{node_id} must be an @alias or path"
            )

        try:
            binary_path = resolve_binary_spec(spec)
        except (OSError, ValueError) as exc:
            raise ValueError(f"network.node_binaries.n{node_id}: {exc}") from exc

        binary_path = binary_path.expanduser().resolve()
        if not binary_path.is_file() or not os.access(binary_path, os.X_OK):
            raise ValueError(
                f"network.node_binaries.n{node_id}: binary is not an "
                f"executable file: {spec} -> {binary_path}"
            )
        result[node_id] = binary_path

    return result


def _validated_genesis_file(raw: Any, *, xahaud_root: Path) -> Path | None:
    """Resolve the YAML ``genesis_file`` override, or None for the bundled one."""
    if raw is None:
        return None
    if not isinstance(raw, (str, Path)) or not str(raw):
        raise ValueError("network.genesis_file must be a path")
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = xahaud_root / path
    if not path.is_file():
        raise ValueError(f"network.genesis_file does not exist: {path}")
    return path


def _validated_extra_args(raw: Any) -> list[str]:
    """Validate the YAML ``extra_args`` list of raw daemon arguments."""
    if raw is None:
        return []
    if isinstance(raw, str) or not isinstance(raw, list):
        raise ValueError(
            "network.extra_args must be a list of strings, not a bare string "
            "(each argument is a separate list entry)"
        )
    args: list[str] = []
    for entry in raw:
        # bool is an int, so reject it explicitly: `- true` would otherwise be
        # handed to the daemon as the string "True".
        if isinstance(entry, bool) or not isinstance(entry, (str, int, float)):
            raise ValueError(
                f"network.extra_args entry must be a string or number: {entry!r}"
            )
        text = str(entry)
        # Quoting makes every other value transport-safe, but a Unix argv
        # cannot hold a NUL at all — YAML "\0" would sail through validation
        # and only blow up in subprocess, defeating the up-front check.
        if "\x00" in text:
            raise ValueError(
                "network.extra_args entry contains a NUL byte, which cannot "
                f"appear in a command argument: {entry!r}"
            )
        args.append(text)
    return args


def _validated_desktop(raw: Any) -> int | None:
    """Validate the YAML ``desktop`` macOS space number."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError("network.desktop must be an integer 1-9")
    if not 1 <= raw <= 9:
        raise ValueError(f"network.desktop must be between 1 and 9, got {raw}")
    return raw


def _validated_lldb_nodes(raw: Any, *, node_count: int) -> set[int]:
    """Validate the YAML ``lldb`` spec into a set of node ids.

    Accepts ``all``/``true`` for every node, or a list of node ids (``0`` or
    ``n0``). Nodes listed here are launched under lldb so a crash leaves a
    backtrace in the pane (see ``x-testnet node-output``).
    """
    if raw is None or raw is False:
        return set()
    if raw is True or (isinstance(raw, str) and raw.strip().lower() == "all"):
        return set(range(node_count))
    if not isinstance(raw, list):
        raise ValueError("network.lldb must be 'all' or a list of node ids")

    result: set[int] = set()
    for entry in raw:
        try:
            node_id = parse_node_ref(entry)
        except ValueError as exc:
            raise ValueError(
                f"network.lldb entry must be a node id: {entry!r}"
            ) from exc
        if node_id < 0 or node_id >= node_count:
            raise ValueError(
                f"network.lldb n{node_id} is outside this {node_count}-node network"
            )
        result.add(node_id)
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


def _validate_network_env(config: dict[str, Any], *, node_count: int) -> None:
    """Validate env-bearing network config without mutating it."""
    _validated_env_mapping(config.get("env", {}), label="network.env")
    _validated_node_env(config.get("node_env", {}), node_count=node_count)


def _validated_params(raw: Any, label: str) -> dict[str, Any] | None:
    """Require scenario params to be a mapping with non-empty string keys.

    They are expanded as ``scenario(ctx, log, **params)``, and Python cannot
    expand a non-string key — which otherwise surfaces only after the network
    has been launched.
    """
    if raw is None:
        return None
    _require_mapping(raw, label)
    for key in raw:
        if not isinstance(key, str) or not key.strip():
            raise ValueError(
                f"{label} keys must be non-empty strings (they become keyword "
                f"arguments), got {key!r}"
            )
    return dict(raw)


def _validated_launcher(raw: Any) -> str | None:
    """Validate the launcher name against the registry."""
    if raw is None:
        return None
    from xahaud_scripts.testnet.launcher import LAUNCHER_TYPES

    name = _require_non_empty_str(raw, "network.launcher")
    if name not in LAUNCHER_TYPES:
        known = ", ".join(sorted(LAUNCHER_TYPES))
        raise ValueError(f"network.launcher must be one of {known}, got {name!r}")
    return name


def _validate_rc_specs(specs: list[str], *, node_count: int) -> None:
    """Parse every rc spec and bound its node references.

    Both the DSL parse and the peer-address resolution otherwise happen in
    _build_launch_config, i.e. after teardown and config regeneration.
    """
    from xahaud_scripts.testnet.cli_handlers.rc import parse_rc_spec

    for spec in specs:
        try:
            parsed = parse_rc_spec(spec)
        except Exception as exc:  # click.BadParameter, ValueError, ...
            raise ValueError(f"network.rc {spec!r}: {exc}") from exc
        for attr in ("node_id", "peer_id"):
            node_id = getattr(parsed, attr)
            if node_id is not None and not 0 <= node_id < node_count:
                raise ValueError(
                    f"network.rc {spec!r} references n{node_id}, outside this "
                    f"{node_count}-node network"
                )


def _validate_log_levels(raw: Any) -> None:
    """Validate the partition -> severity mapping.

    An empty severity is the documented way to remove a default partition, so
    it stays legal; a non-string value is not, and would be interpolated
    straight into the generated daemon config.
    """
    _require_mapping(raw, "network.log_levels")
    for partition, severity in raw.items():
        _require_non_empty_str(partition, "network.log_levels key")
        if not isinstance(severity, str):
            got = type(severity).__name__
            raise ValueError(
                f"network.log_levels['{partition}'] must be a string "
                f"(empty removes the partition), got {got}"
            )


def _selected_topology(config: dict[str, Any]) -> Any:
    """Return the configured topology block, preserving malformed falsy values."""
    present = [key for key in ("topology", "runtime_topology") if key in config]
    if len(present) > 1:
        raise ValueError(
            "network may define only one of 'topology' or 'runtime_topology'"
        )
    return config[present[0]] if present else _MISSING


def _validate_topology(raw: Any, *, node_count: int, fixed_peers: bool) -> None:
    """Validate statically-checkable topology fields before launch.

    Deliberately does not predict live state — only the shape and the node/edge
    references, which are knowable from the config alone yet otherwise fail
    after the nodes are running. The two cross-field invariants at the end are
    the same ones `_apply_runtime_topology` enforces; they are reproduced here
    rather than referenced so a config cannot get as far as a launched network
    before being rejected.
    """
    _require_mapping(raw, "network.topology")
    _reject_unknown_keys(raw, _TOPOLOGY_KEYS, "network.topology")

    selected: list[int] | None = None
    nodes = raw.get("nodes")
    if nodes is not None:
        if not isinstance(nodes, list):
            raise ValueError("network.topology.nodes must be a list of node ids")
        selected = []
        for entry in nodes:
            try:
                node_id = parse_node_ref(entry)
            except ValueError as exc:
                raise ValueError(f"network.topology.nodes: {exc}") from exc
            if not 0 <= node_id < node_count:
                raise ValueError(
                    f"network.topology.nodes references n{node_id}, outside "
                    f"this {node_count}-node network"
                )
            selected.append(node_id)

    for key in ("edges", "connect", "disconnect"):
        specs = raw.get(key)
        if specs is None:
            continue
        if isinstance(specs, str) or not isinstance(specs, list):
            raise ValueError(f"network.topology.{key} must be a list of 'n0->n1'")
        for spec in specs:
            _require_non_empty_str(spec, f"network.topology.{key} entry")
            try:
                source, target = parse_edge_specs([spec]).pop()
            except ValueError as exc:
                raise ValueError(f"network.topology.{key} {spec!r}: {exc}") from exc
            for node_id in (source, target):
                if not 0 <= node_id < node_count:
                    raise ValueError(
                        f"network.topology.{key} {spec!r} references n{node_id}, "
                        f"outside this {node_count}-node network"
                    )

    for key in ("bidirectional", "exact"):
        if key in raw:
            _require_bool(raw[key], f"network.topology.{key}")
    for key in (
        "settle_timeout",
        "timeout",
        "poll_interval",
        "stable_for",
        "rpc_timeout",
    ):
        if key in raw:
            _require_number(raw[key], f"network.topology.{key}", minimum=0)

    # Cross-field invariants, mirroring _apply_runtime_topology. Both default
    # the same way it does: exact is true unless set, fixed_peers is true
    # unless set.
    if raw and raw.get("rpc_timeout", 30) <= 0:
        raise ValueError("network.topology.rpc_timeout must be > 0")
    if "edges" not in raw:
        return

    timeout_value = raw.get("settle_timeout")
    if timeout_value is None:
        timeout_value = raw.get("timeout", 60)
    stable_for = raw.get("stable_for", 2)
    if timeout_value <= 0:
        raise ValueError("network.topology settle timeout must be > 0")
    if stable_for >= timeout_value:
        raise ValueError(
            "network.topology.stable_for must be less than its settle timeout "
            f"({stable_for} >= {timeout_value})"
        )

    exact = bool(raw.get("exact", True))
    expected = parse_edge_specs(
        raw.get("edges") or [],
        bidirectional=bool(raw.get("bidirectional", False)),
    )
    target_nodes = selected if selected is not None else list(range(node_count))
    try:
        validate_edges_in_nodes(expected, target_nodes)
    except ValueError as exc:
        raise ValueError(f"network.topology.edges: {exc}") from exc
    if exact and fixed_peers:
        raise ValueError(
            "network.topology exact shaping requires fixed_peers: false; "
            "generated [ips_fixed] peers may reconnect omitted edges"
        )


def _validate_network_config(config: dict[str, Any], *, xahaud_root: Path) -> None:
    """Run every ``network:`` validator without building anything.

    One entry point on purpose. Validators used to be invoked only where their
    value was needed — inside ``_build_launch_config`` — which meant ``--dry-run``
    silently skipped several of them, and a real run did not reject a bad field
    until after it had already torn down the previous network and regenerated
    node directories. Rejecting up front keeps a typo cheap.
    """
    _reject_unknown_keys(config, _NETWORK_KEYS, "network")

    # Core sizing first: everything below is expressed relative to node_count,
    # and a bad value here used to survive preflight only to blow up inside
    # generate()'s range(node_count) — after teardown() had already run.
    node_count = config.get("node_count", 5)
    _require_int(
        node_count,
        "network.node_count",
        minimum=1,
        maximum=MAX_NODE_COUNT,
    )

    validators = config.get("validators")
    if validators is not None:
        _require_int(validators, "network.validators", minimum=1)
        if validators > node_count:
            raise ValueError(
                f"network.validators ({validators}) cannot exceed "
                f"network.node_count ({node_count})"
            )

    for key in ("quorum", "start_ledger"):
        if config.get(key) is not None:
            _require_int(config[key], f"network.{key}", minimum=1)
    if config.get("slave_delay") is not None:
        _require_number(config["slave_delay"], "network.slave_delay", minimum=0)
    for key in ("fixed_peers", "find_ports", "unl_report"):
        if config.get(key) is not None:
            _require_bool(config[key], f"network.{key}")
    for key in ("features", "majority_features", "track_features", "rc"):
        if config.get(key) is not None:
            _require_str_list(config[key], f"network.{key}")
    if config.get("rc") is not None:
        _validate_rc_specs(config["rc"], node_count=node_count)
    if config.get("log_levels") is not None:
        _validate_log_levels(config["log_levels"])
    _validated_launcher(config.get("launcher"))
    topology = _selected_topology(config)
    if topology is not _MISSING:
        _validate_topology(
            topology,
            node_count=node_count,
            fixed_peers=config.get("fixed_peers", True),
        )

    _validate_network_env(config, node_count=node_count)
    _validated_node_binaries(config.get("node_binaries", {}), node_count=node_count)
    _validated_lldb_nodes(config.get("lldb"), node_count=node_count)
    _validated_genesis_file(config.get("genesis_file"), xahaud_root=xahaud_root)
    _validated_extra_args(config.get("extra_args"))
    _validated_desktop(config.get("desktop"))


def _snapshot_test(network: TestNetwork, dest: Path) -> None:
    """Copy node dirs and network.json from live testnet into dest."""
    network.copy_snapshot_to(dest, active_nodes_only=True)


def _unique_failure_dir(runs_dir: Path, name: str) -> Path:
    _validated_test_name(name)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = _contained_run_dir(runs_dir, f"{timestamp}-{name}")
    suffix = 2
    while candidate.exists():
        candidate = _contained_run_dir(runs_dir, f"{timestamp}-{name}-{suffix}")
        suffix += 1
    return candidate


def archive_failed_run(
    network: TestNetwork,
    name: str,
    *,
    runs_dir: Path | None = None,
    scenario_log: Path | None = None,
) -> Path:
    """Archive a failed run under output/runs/latest and a timestamped dir."""
    _validated_test_name(name)
    runs_dir = runs_dir or (network.base_dir.parent / ".testnet" / "output" / "runs")
    latest_dir = _contained_run_dir(runs_dir, "latest", name)
    latest_dir.mkdir(parents=True, exist_ok=True)

    _snapshot_test(network, latest_dir)
    if scenario_log is not None and scenario_log.exists():
        dest_log = latest_dir / "scenario.log"
        # Guard against a self-copy: suite runs pass their own
        # latest/<name>/scenario.log here, which shutil.copy2 rejects with
        # SameFileError (previously swallowed → failed suite tests lost their
        # timestamped archive).
        if scenario_log.resolve() != dest_log.resolve():
            shutil.copy2(scenario_log, dest_log)

    fail_dir = _unique_failure_dir(runs_dir, name)
    shutil.copytree(latest_dir, fail_dir)
    logger.info(f"Failure snapshot: {fail_dir}")
    return fail_dir


def _create_network(
    xahaud_root: Path,
    config: dict[str, Any],
    testnet_dir: Path | None = None,
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

    base_dir = testnet_dir or (xahaud_root / "testnet")

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

    base_genesis = (
        _validated_genesis_file(config.get("genesis_file"), xahaud_root=xahaud_root)
        or get_bundled_genesis_file()
    )
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
    node_env = _validated_node_env(
        config.get("node_env", {}),
        node_count=network_config.node_count,
    )
    node_rippled_paths = _validated_node_binaries(
        config.get("node_binaries", {}),
        node_count=network_config.node_count,
    )
    lldb_nodes = _validated_lldb_nodes(
        config.get("lldb"),
        node_count=network_config.node_count,
    )

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
        extra_args=_validated_extra_args(config.get("extra_args")),
        extra_env=extra_env,
        node_env=node_env,
        node_rippled_paths=node_rippled_paths,
        lldb_nodes=lldb_nodes,
        desktop=_validated_desktop(config.get("desktop")),
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
    topo = _selected_topology(config)
    if topo is _MISSING:
        return
    if not isinstance(topo, dict):
        raise ValueError("network topology must be a mapping")
    if not topo:
        return

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
            logger.info(f"Runtime topology connect n{source}->n{target}")
            result = connect_managed_peer(
                network.rpc_client,
                network.nodes,
                source=source,
                target=target,
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
        logger.info(f"Runtime topology connect n{source}->n{target}")
        result = connect_managed_peer(
            network.rpc_client,
            network.nodes,
            source=source,
            target=target,
        )
        require_rpc_success(result, f"n{source}->n{target} connect")


def _resolved_script_path(test: dict[str, Any], xahaud_root: Path) -> Path:
    """Resolve a suite script path relative to the xahaud checkout."""
    script_path = Path(test["script"])
    return script_path if script_path.is_absolute() else xahaud_root / script_path


def _ast_scenario_signature(
    definition: ast.AsyncFunctionDef,
) -> inspect.Signature:
    """Build the callable signature relevant to the scenario runner."""
    args = definition.args
    positional = [*args.posonlyargs, *args.args]
    default_start = len(positional) - len(args.defaults)
    parameters: list[inspect.Parameter] = []

    for index, arg in enumerate(positional):
        kind = (
            inspect.Parameter.POSITIONAL_ONLY
            if index < len(args.posonlyargs)
            else inspect.Parameter.POSITIONAL_OR_KEYWORD
        )
        default = None if index >= default_start else inspect.Parameter.empty
        parameters.append(inspect.Parameter(arg.arg, kind, default=default))

    if args.vararg is not None:
        parameters.append(
            inspect.Parameter(args.vararg.arg, inspect.Parameter.VAR_POSITIONAL)
        )
    for arg, default_node in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        default = None if default_node is not None else inspect.Parameter.empty
        parameters.append(
            inspect.Parameter(arg.arg, inspect.Parameter.KEYWORD_ONLY, default=default)
        )
    if args.kwarg is not None:
        parameters.append(
            inspect.Parameter(args.kwarg.arg, inspect.Parameter.VAR_KEYWORD)
        )
    return inspect.Signature(parameters)


class _FunctionYieldFinder(ast.NodeVisitor):
    """Find yields in one function body without descending into nested scopes."""

    def __init__(self) -> None:
        self.found = False

    def visit_Yield(self, node: ast.Yield) -> None:  # noqa: N802
        self.found = True

    def visit_YieldFrom(self, node: ast.YieldFrom) -> None:  # noqa: N802
        self.found = True

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return

    def visit_AsyncFunctionDef(  # noqa: N802
        self, node: ast.AsyncFunctionDef
    ) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return


def _is_async_generator(definition: ast.AsyncFunctionDef) -> bool:
    finder = _FunctionYieldFinder()
    for statement in definition.body:
        finder.visit(statement)
    return finder.found


def _validate_scenario_contract(
    script_path: Path,
    *,
    test_name: str,
    params: dict[str, Any] | None,
) -> None:
    """Validate the scenario entry point without executing user code.

    Importing a scenario for preflight runs its module body. The real scenario
    loader imports it again, so doing that here repeats arbitrary top-level
    side effects before the first lifecycle mutation. The documented contract
    requires a top-level ``async def scenario`` and can be checked from the
    syntax tree instead.
    """
    try:
        # Keep the encoded source so compile/AST parsing honor PEP 263 in the
        # same way as the import loader.
        source = script_path.read_bytes()
        # ``ast.parse`` accepts some constructs that cannot become a module
        # code object (for example ``return`` at module scope or a late
        # ``global`` declaration). Compile as well, without executing, so those
        # failures cannot be deferred until after the network is launched.
        # Do not inherit this module's future flags: the scenario's own future
        # imports must be authoritative.
        compile(source, str(script_path), "exec", dont_inherit=True)
        tree = ast.parse(source, filename=str(script_path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ValueError(
            f"Test '{test_name}' script failed preflight ({script_path}): {exc}"
        ) from exc

    deferred_generators, generator_consumers = _deferred_generator_bindings(
        tree, "scenario"
    )
    active_definition: ast.AsyncFunctionDef | None = None
    for statement in tree.body:
        if isinstance(statement, ast.AsyncFunctionDef) and statement.name == "scenario":
            active_definition = statement
        elif id(statement) in generator_consumers or _statement_binds_name(
            statement, "scenario", deferred_generators=deferred_generators
        ):
            # The final import-time binding is what load_scenario_script sees.
            # A direct assignment, sync definition, import, delete, loop target,
            # or conditional binding after the async def invalidates it.
            active_definition = None

    if active_definition is None:
        raise ValueError(
            f"Test '{test_name}' script must define 'async def scenario': {script_path}"
        )
    if active_definition.decorator_list:
        # A decorator can replace an async function with any object. Determining
        # whether the final binding remains a coroutine would require executing
        # user code, defeating side-effect-free preflight.
        raise ValueError(
            f"Test '{test_name}' scenario must not be decorated because its "
            f"async contract cannot be checked without importing {script_path}"
        )
    if _is_async_generator(active_definition):
        raise ValueError(
            f"Test '{test_name}' scenario must be an awaitable coroutine, not an "
            f"async generator: {script_path}"
        )

    try:
        _ast_scenario_signature(active_definition).bind(
            object(),
            object(),
            **{key: object() for key in params or {}},
        )
    except TypeError as exc:
        raise ValueError(
            f"Test '{test_name}' scenario cannot be called as "
            f"scenario(ctx, log, **params): {exc}"
        ) from exc


def _preflight_selected_tests(
    suite: SuiteConfig,
    tests: list[dict[str, Any]],
    *,
    xahaud_root: Path,
    env_override: dict[str, str] | None,
) -> None:
    """Validate every selected test before the first lifecycle mutation."""
    runs_dir = xahaud_root / ".testnet" / "output" / "runs"
    for test in tests:
        name = _validated_test_name(test["name"])
        _contained_run_dir(runs_dir, "latest", name)

        params = _validated_params(
            test.get("_params"), f"Test '{name}' effective params"
        )
        config = suite.effective_network(test)
        _merge_env_override(config, env_override)
        _validate_network_config(config, xahaud_root=xahaud_root)

        script_path = _resolved_script_path(test, xahaud_root)
        if not script_path.is_file():
            raise ValueError(f"Script is not a file: {script_path}")
        _validate_scenario_contract(script_path, test_name=name, params=params)


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
    ai_sandboxed: bool = False,
    rippled_path: Path | None = None,
    testnet_dir: Path | None = None,
) -> TestResult:
    """Run a single test with full network lifecycle."""
    name = _validated_test_name(test["name"])
    script_path = _resolved_script_path(test, xahaud_root)

    if not script_path.exists():
        return TestResult(
            name=name,
            passed=False,
            duration=0,
            error=f"Script not found: {script_path}",
        )

    config = suite.effective_network(test)
    _merge_env_override(config, env_override)
    _validate_network_config(config, xahaud_root=xahaud_root)

    # --fast-bootstrap: inject global.bootstrap_fast_start=true unless already
    # set in XAHAUD_RUNTIME_TEST_CONFIG. This daemon hook exists only on
    # feature-export-rng branches; stock binaries ignore it.
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

    network = _create_network(xahaud_root, config, testnet_dir=testnet_dir)

    # Prepare output dirs
    runs_dir = xahaud_root / ".testnet" / "output" / "runs"
    latest_dir = _contained_run_dir(runs_dir, "latest", name)
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
                    ai_sandboxed=ai_sandboxed,
                )
            )

        duration = time.monotonic() - start

        snapshot_dir = None
        if not passed and snapshot_on_fail:
            snapshot_dir = archive_failed_run(
                network,
                name,
                runs_dir=runs_dir,
                scenario_log=per_test_log,
            )
        else:
            # 6. Always snapshot to latest/
            _snapshot_test(network, latest_dir)

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
                snapshot_dir = archive_failed_run(
                    network,
                    name,
                    runs_dir=runs_dir,
                    scenario_log=per_test_log,
                )
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
    ai_sandboxed: bool = False,
    rippled_path: Path | None = None,
    testnet_dir: Path | None = None,
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
            explicitly set in suite config or --env. Supported only by
            feature-export-rng branches; inert elsewhere.
        ai_sandboxed: Skip optional monitor host-process introspection. This
            does not soften RPC, lifecycle, topology, assertion, or teardown
            failures.
        rippled_path: If set, use this binary instead of
            ``$xahaud_root/build/rippled``.

    Returns:
        List of TestResult for all executed tests.
    """
    _require_int(test_n, "test_n", minimum=1)
    suite = SuiteConfig.from_yaml(suite_path)
    _validated_params(params_override, "params_override")

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

    _preflight_selected_tests(
        suite,
        tests,
        xahaud_root=xahaud_root,
        env_override=env_override,
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
            _validate_network_config(config, xahaud_root=xahaud_root)
            node_binaries = _validated_node_binaries(
                config.get("node_binaries", {}),
                node_count=config.get("node_count", 5),
            )
            lldb_nodes = _validated_lldb_nodes(
                config.get("lldb"),
                node_count=config.get("node_count", 5),
            )
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
            if node_binaries:
                console.print(f"     node_binaries: {node_binaries}")
            if lldb_nodes:
                console.print(f"     lldb: {sorted(lldb_nodes)}")
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
                    ai_sandboxed=ai_sandboxed,
                    rippled_path=rippled_path,
                    testnet_dir=testnet_dir,
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
