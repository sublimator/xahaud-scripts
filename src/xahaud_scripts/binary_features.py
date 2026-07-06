#!/usr/bin/env python3
"""Inspect amendment support encoded in a xahaud binary/source ref.

The live crawler can tell us which build strings are visible on the network.
This tool answers the matching source question: for a given git ref/tag, which
amendments did that binary know how to register and vote for?
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tree_sitter_cpp as tscpp
from tree_sitter import Language, Node, Parser

from xahaud_scripts.utils.paths import get_xahaud_root

SOURCE_PATHS = (
    "include/xrpl/protocol/detail/features.macro",
    "src/ripple/protocol/impl/Feature.cpp",
    "src/libxrpl/protocol/Feature.cpp",
    "src/xrpl/protocol/impl/Feature.cpp",
)

MACRO_NAMES = {
    "XRPL_FEATURE",
    "XRPL_FIX",
    "XRPL_RETIRE",
    "REGISTER_FEATURE",
    "REGISTER_FIX",
    "retireFeature",
}
SEMICOLONLESS_MACROS = {"XRPL_FEATURE", "XRPL_FIX", "XRPL_RETIRE"}

DEFAULT_TRACK = [
    "Export",
    "ConsensusEntropy",
    "fixHookMap",
    "fixGuardDepth32",
    "Cron",
    "ExtendedHookState",
    "DeepFreeze",
]

# Versions observed in the public network crawl on 2026-07-06.
OBSERVED_XAHAU_REFS = [
    "2026.6.21-release+3350",
    "2025.12.1-release+2609",
    "2025.10.27-release+2405",
    "2025.7.9-release+1951",
    "2025.2.6-release+1299",
]


@dataclass(frozen=True)
class FeatureDecl:
    """One registered amendment declaration from Feature.cpp/features.macro."""

    name: str
    kind: str
    supported: bool
    vote: str
    amendment_id: str
    source: str
    line: int

    @property
    def status(self) -> str:
        if self.kind == "retired":
            return "retired"
        if self.vote == "Obsolete":
            return "obsolete"
        if not self.supported:
            return "unsupported"
        return "supported"

    def compact(self) -> str:
        if self.kind == "retired":
            return "retired"
        if self.vote == "Obsolete":
            return "obsolete"
        return f"{'yes' if self.supported else 'no'}/{self.vote}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "supported": self.supported,
            "vote": self.vote,
            "status": self.status,
            "amendment_id": self.amendment_id,
            "source": self.source,
            "line": self.line,
        }


@dataclass(frozen=True)
class RefFeatures:
    """Feature declarations extracted from one git ref."""

    ref: str
    source_path: str
    declarations: tuple[FeatureDecl, ...]

    def by_name(self) -> dict[str, FeatureDecl]:
        return {d.name: d for d in self.declarations}

    def counts(self) -> dict[str, int]:
        return {
            "registered": len(self.declarations),
            "supported": sum(1 for d in self.declarations if d.status == "supported"),
            "unsupported": sum(
                1 for d in self.declarations if d.status == "unsupported"
            ),
            "obsolete": sum(1 for d in self.declarations if d.status == "obsolete"),
            "retired": sum(1 for d in self.declarations if d.status == "retired"),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "source_path": self.source_path,
            "counts": self.counts(),
            "declarations": [d.as_dict() for d in self.declarations],
        }


class FeatureParser:
    """Small tree-sitter parser for xahaud amendment registration macros."""

    def __init__(self) -> None:
        self.language = Language(tscpp.language())
        self.parser = Parser(self.language)

    def parse(self, source: str, source_label: str) -> tuple[FeatureDecl, ...]:
        prepared = _prepare_macro_source(source)
        source_bytes = prepared.encode()
        tree = self.parser.parse(source_bytes)
        if tree.root_node.has_error:
            raise ValueError(f"{source_label}: tree-sitter parse error")
        declarations: list[FeatureDecl] = []

        def visit(node: Node) -> None:
            if node.type == "call_expression":
                decl = self._parse_call(node, source_bytes, source_label)
                if decl:
                    declarations.append(decl)
            for child in node.children:
                visit(child)

        visit(tree.root_node)
        return tuple(declarations)

    def _parse_call(
        self, node: Node, source: bytes, source_label: str
    ) -> FeatureDecl | None:
        name_node = node.child_by_field_name("function")
        if name_node is None:
            return None

        macro = _node_text(name_node, source)
        if macro not in MACRO_NAMES:
            return None

        args_node = node.child_by_field_name("arguments")
        args = _argument_texts(args_node, source) if args_node else []
        line = node.start_point.row + 1

        if macro == "XRPL_FEATURE":
            if len(args) < 3:
                return None
            return _decl(
                name=_identifier_arg(args[0]),
                kind="feature",
                supported=_supported_arg(args[1]),
                vote=_vote_arg(args[2]),
                source=source_label,
                line=line,
            )
        if macro == "XRPL_FIX":
            if len(args) < 3:
                return None
            return _decl(
                name=f"fix{_identifier_arg(args[0])}",
                kind="fix",
                supported=_supported_arg(args[1]),
                vote=_vote_arg(args[2]),
                source=source_label,
                line=line,
            )
        if macro == "XRPL_RETIRE":
            if not args:
                return None
            return _retired(_identifier_arg(args[0]), source_label, line)
        if macro == "REGISTER_FEATURE":
            if len(args) < 3:
                return None
            return _decl(
                name=_identifier_arg(args[0]),
                kind="feature",
                supported=_supported_arg(args[1]),
                vote=_vote_arg(args[2]),
                source=source_label,
                line=line,
            )
        if macro == "REGISTER_FIX":
            if len(args) < 3:
                return None
            return _decl(
                name=_identifier_arg(args[0]),
                kind="fix",
                supported=_supported_arg(args[1]),
                vote=_vote_arg(args[2]),
                source=source_label,
                line=line,
            )
        if macro == "retireFeature":
            if not args:
                return None
            return _retired(_string_or_identifier_arg(args[0]), source_label, line)

        return None


def _prepare_macro_source(source: str) -> str:
    """Make semicolon-less features.macro calls parse as C++ statements."""
    out: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if (
            any(stripped.startswith(name) for name in SEMICOLONLESS_MACROS)
            and ")" in stripped
            and not stripped.endswith(";")
        ):
            out.append(f"{line};")
        else:
            out.append(line)
    return "\n".join(out)


def _node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _argument_texts(args_node: Node, source: bytes) -> list[str]:
    args: list[str] = []
    current: list[str] = []
    for child in args_node.children:
        if child.type == "(":
            continue
        if child.type == ",":
            args.append("".join(current).strip())
            current = []
            continue
        if child.type == ")":
            if current:
                args.append("".join(current).strip())
            continue
        current.append(_node_text(child, source))
    return args


def _identifier_arg(arg: str) -> str:
    return arg.strip()


def _string_or_identifier_arg(arg: str) -> str:
    value = arg.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _enum_tail(arg: str, enum_name: str) -> str:
    value = arg.strip()
    prefix = f"{enum_name}::"
    if value.startswith(prefix):
        return value[len(prefix) :]
    return value


def _supported_arg(arg: str) -> bool:
    return _enum_tail(arg, "Supported") == "yes"


def _vote_arg(arg: str) -> str:
    return _enum_tail(arg, "VoteBehavior")


def _amendment_id(name: str) -> str:
    return hashlib.sha512(name.encode("utf-8")).digest()[:32].hex().upper()


def _decl(
    *,
    name: str,
    kind: str,
    supported: bool,
    vote: str,
    source: str,
    line: int,
) -> FeatureDecl:
    return FeatureDecl(
        name=name,
        kind=kind,
        supported=supported,
        vote=vote,
        amendment_id=_amendment_id(name),
        source=source,
        line=line,
    )


def _retired(name: str, source: str, line: int) -> FeatureDecl:
    return _decl(
        name=name,
        kind="retired",
        supported=True,
        vote="Obsolete",
        source=source,
        line=line,
    )


def git_show(repo: Path, ref: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def load_ref_features(repo: Path, ref: str, parser: FeatureParser) -> RefFeatures:
    for path in SOURCE_PATHS:
        source = git_show(repo, ref, path)
        if source is not None:
            return RefFeatures(
                ref=ref,
                source_path=path,
                declarations=parser.parse(source, f"{ref}:{path}"),
            )
    searched = ", ".join(SOURCE_PATHS)
    raise RuntimeError(f"{ref}: could not find amendment source ({searched})")


def render_markdown_summary(refs: list[RefFeatures], tracked: list[str]) -> str:
    headers = [
        "ref",
        "source",
        "registered",
        "supported",
        "unsupported",
        "obsolete",
        "retired",
        *tracked,
    ]
    rows = []
    for ref in refs:
        counts = ref.counts()
        by_name = ref.by_name()
        rows.append(
            [
                ref.ref,
                ref.source_path,
                str(counts["registered"]),
                str(counts["supported"]),
                str(counts["unsupported"]),
                str(counts["obsolete"]),
                str(counts["retired"]),
                *[
                    by_name[name].compact() if name in by_name else "-"
                    for name in tracked
                ],
            ]
        )
    return _markdown_table(headers, rows)


def render_markdown_details(refs: list[RefFeatures]) -> str:
    parts: list[str] = []
    for ref in refs:
        parts.append(f"\n### {ref.ref}\n")
        rows = [
            [
                d.name,
                d.kind,
                d.compact(),
                d.amendment_id,
                f"{d.source}:{d.line}",
            ]
            for d in ref.declarations
        ]
        parts.append(
            _markdown_table(["name", "kind", "support", "amendment_id", "source"], rows)
        )
    return "\n".join(parts)


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    def esc(value: str) -> str:
        return value.replace("|", "\\|")

    lines = [
        "| " + " | ".join(esc(h) for h in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(esc(c) for c in row) + " |")
    return "\n".join(lines)


def render_csv_summary(refs: list[RefFeatures], tracked: list[str]) -> str:
    from io import StringIO

    out = StringIO()
    fieldnames = [
        "ref",
        "source",
        "registered",
        "supported",
        "unsupported",
        "obsolete",
        "retired",
        *tracked,
    ]
    writer = csv.DictWriter(out, fieldnames=fieldnames)
    writer.writeheader()
    for ref in refs:
        counts = ref.counts()
        by_name = ref.by_name()
        row = {
            "ref": ref.ref,
            "source": ref.source_path,
            **counts,
            **{
                name: by_name[name].compact() if name in by_name else ""
                for name in tracked
            },
        }
        writer.writerow(row)
    return out.getvalue()


def parse_track_args(values: list[str], *, include_defaults: bool) -> list[str]:
    tracked = list(DEFAULT_TRACK) if include_defaults else []
    for value in values:
        tracked.extend(part.strip() for part in value.split(",") if part.strip())
    return list(dict.fromkeys(tracked))


def resolve_repo(arg: Path | None) -> Path:
    if arg:
        return arg.expanduser().resolve()
    try:
        return Path(get_xahaud_root()).resolve()
    except Exception as exc:
        raise RuntimeError(
            "could not find a xahaud checkout; pass --repo /path/to/xahaud"
        ) from exc


def resolve_refs(refs: list[str], include_observed: bool) -> list[str]:
    resolved = list(refs) if refs else ["HEAD"]
    if include_observed:
        resolved.extend(OBSERVED_XAHAU_REFS)
    return list(dict.fromkeys(resolved))


def main() -> None:
    argp = argparse.ArgumentParser(
        description=(
            "Read amendment declarations from xahaud git refs/tags and report "
            "which binaries support which amendments."
        )
    )
    argp.add_argument("refs", nargs="*", help="git refs/tags to inspect")
    argp.add_argument("--repo", type=Path, help="xahaud checkout to inspect")
    argp.add_argument(
        "--observed-xahau",
        action="store_true",
        help="also inspect the Xahau release tags observed on the public network",
    )
    argp.add_argument(
        "--track",
        action="append",
        default=[],
        metavar="NAME[,NAME...]",
        help="extra amendment names to include as summary columns",
    )
    argp.add_argument(
        "--no-default-track",
        action="store_true",
        help="only show --track columns, not the default rollout-focused set",
    )
    argp.add_argument(
        "--all",
        action="store_true",
        help="append a full per-ref amendment table after the summary",
    )
    argp.add_argument(
        "--format",
        choices=["markdown", "csv", "json"],
        default="markdown",
        help="output format",
    )
    args = argp.parse_args()

    try:
        repo = resolve_repo(args.repo)
        refs = resolve_refs(args.refs, args.observed_xahau)
        tracked = parse_track_args(
            args.track, include_defaults=not args.no_default_track
        )
        parser = FeatureParser()
        snapshots = [load_ref_features(repo, ref, parser) for ref in refs]
    except Exception as exc:
        print(f"x-binary-features: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if args.format == "json":
        print(json.dumps([s.as_dict() for s in snapshots], indent=2))
    elif args.format == "csv":
        print(render_csv_summary(snapshots, tracked), end="")
    else:
        print(render_markdown_summary(snapshots, tracked))
        if args.all:
            print(render_markdown_details(snapshots))


if __name__ == "__main__":
    main()
