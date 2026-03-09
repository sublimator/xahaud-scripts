"""Generate scenario test guide dynamically from scenario.py source."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

_SCENARIO_PY = Path(__file__).parent / "scenario.py"

# Classes to show as full source (small dataclasses / exceptions)
_FULL_SOURCE_CLASSES = (
    "Marker",
    "Range",
    "Operation",
    "LogSearchResult",
    "AssertionError",
)

# The class whose public methods we extract
_API_CLASS = "ScenarioContext"


def _has_docstring(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return (
        bool(node.body)
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    )


def _extract_class_source(node: ast.ClassDef, lines: list[str]) -> str:
    """Extract full class source."""
    start = node.lineno - 1
    end = node.end_lineno or node.lineno
    return "\n".join(lines[start:end])


def _extract_method_stub(
    node: ast.FunctionDef | ast.AsyncFunctionDef, lines: list[str]
) -> str:
    """Extract method signature + docstring (skip implementation body)."""
    # Include decorators (e.g. @property) if present
    if node.decorator_list:
        start = node.decorator_list[0].lineno - 1
    else:
        start = node.lineno - 1
    if _has_docstring(node):
        end = node.body[0].end_lineno or node.lineno
    else:
        # No docstring — just the def line(s) up to the colon
        for i in range(start, min(start + 20, len(lines))):
            stripped = lines[i].rstrip()
            if stripped.endswith(":"):
                end = i + 1
                break
        else:
            end = start + 1
    return "\n".join(lines[start:end])


def _extract_api(source: str) -> dict[str, list[str]]:
    """Parse scenario.py and extract ScenarioContext methods by section.

    Returns dict of section_name -> list of method stubs.
    """
    tree = ast.parse(source)
    lines = source.splitlines()

    sections: dict[str, list[str]] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == _API_CLASS:
            current_section = "General"
            for item in node.body:
                # Detect section comments: lines like "# -- Timing ---"
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Check for section comment above this method
                    for line_idx in range(item.lineno - 2, max(item.lineno - 5, 0), -1):
                        line = lines[line_idx].strip()
                        if line.startswith("# --") and line.endswith("-" * 3 + "-"):
                            section_name = line.strip("# -").strip()
                            if section_name:
                                current_section = section_name
                            break
                        if line and not line.startswith("#"):
                            break

                    # Skip private methods
                    if item.name.startswith("_"):
                        continue

                    stub = _extract_method_stub(item, lines)
                    # Dedent to remove class indentation
                    stub = textwrap.dedent(stub)
                    sections.setdefault(current_section, []).append(stub)
            break

    return sections


def _extract_data_classes(source: str) -> dict[str, str]:
    """Extract full source for data type classes."""
    tree = ast.parse(source)
    lines = source.splitlines()
    result: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in _FULL_SOURCE_CLASSES:
            result[node.name] = _extract_class_source(node, lines)

    return result


def generate_scenario_guide() -> str:
    """Generate the full scenario test guide from live source."""
    source = _SCENARIO_PY.read_text()
    api_sections = _extract_api(source)
    data_classes = _extract_data_classes(source)

    parts: list[str] = []

    # -- Header --
    parts.append("""\
# Scenario Test Guide

## Overview

Scenario scripts test network-level behavior — amendment activation, node
crashes, consensus transitions, log assertion, and similar integration
scenarios. They operate at a higher level than test scripts (which use
xrpl-py for transaction-level testing).

## Running

    x-testnet run --scenario-script my_scenario.py
    x-testnet run --scenario-script my_scenario.py --test-script-teardown

Flags:
- `--scenario-script PATH` — Run a scenario script after network launch
- `--test-script-teardown` — Kill nodes after the scenario finishes
- `--feature HASH` — Enable/disable amendments (prefix `-` to disable)
- `--launcher tmux` — Use tmux (required for node lifecycle control)

The scenario runner:
1. Launches the network and waits for the first ledger
2. Calls your `async def scenario(ctx, log)` function
3. Reports pass/fail and exits (non-zero on failure)
4. Logs to `.testnet/scenario-test.log`

## Script Format

A scenario script is a Python file defining:

```python
async def scenario(ctx, log):
    \"\"\"Docstring shown in logs.\"\"\"
    await ctx.wait_for_ledger_close()
    log("Network is up")
    # ... your scenario logic ...
```

- `ctx` — `ScenarioContext` instance (API documented below)
- `log` — shortcut for `ctx.log()`, logs to console + file

Raise `AssertionError` (or let `ctx.assert_*` raise it) to indicate failure.\
""")

    # -- Timing primitives --
    parts.append("\n## Timing Primitives\n")
    for name in ("Marker", "Range", "Operation"):
        if name in data_classes:
            parts.append(f"### {name}\n")
            parts.append(f"```python\n{data_classes[name]}\n```\n")

    # -- ScenarioContext API --
    parts.append("## ScenarioContext API\n")
    parts.append(
        "Methods available on `ctx` inside your scenario function.\n"
        "All multi-node methods accept `nodes=[1,2]` and/or `exclude_nodes=[4]`.\n"
    )

    for section_name, stubs in api_sections.items():
        parts.append(f"### {section_name}\n")
        for stub in stubs:
            parts.append(f"```python\n{stub}\n```\n")

    # -- Log types --
    parts.append("## Log Search Types\n")
    for name in ("LogSearchResult", "AssertionError"):
        if name in data_classes:
            parts.append(f"### {name}\n")
            parts.append(f"```python\n{data_classes[name]}\n```\n")

    # -- Node targeting --
    parts.append("""\
## Node Targeting

Most multi-node methods accept two keyword arguments:

- `nodes=[0, 1, 2]` — target specific nodes (default: all)
- `exclude_nodes=[4]` — exclude specific nodes

These compose: `nodes=[0,1,2,3,4], exclude_nodes=[4]` targets 0-3.
When neither is given, all nodes in the network are targeted.
""")

    # -- Example --
    parts.append("""\
## Example: Amendment Crash Scenario

```python
\"\"\"Scenario: ConsensusEntropy amendment crashes non-supporting node.

Votes ConsensusEntropy accept on all nodes except n4, then waits for n4
to crash as the amendment activates without its support.

    x-testnet run --scenario-script consensus_entropy_crash.py
\"\"\"


async def scenario(ctx, log):
    await ctx.wait_for_ledger_close()
    ctx.feature("ConsensusEntropy", vetoed=False, exclude_nodes=[4])

    log("Waiting for ConsensusEntropy to be voted for...")
    await ctx.wait_for_feature(
        "ConsensusEntropy",
        check=lambda s: not s.get("vetoed"),
        exclude_nodes=[4],
        timeout=60,
    )

    log("Waiting for n4 to crash...")
    op = await ctx.wait_for_nodes_down(nodes=[4], timeout=600)

    ctx.assert_log("unsupported amendments activated", since=op.started, nodes=[4])
    ctx.assert_exit_status(0, nodes=[4])
    log("PASS: n4 shut down due to unsupported amendment")
```
""")

    return "\n".join(parts)
