#!/usr/bin/env python3

import argparse
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from xahaud_scripts.utils.logging import make_logger, setup_logging
from xahaud_scripts.utils.paths import get_xahaud_root

logger = make_logger(__name__)

SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".m", ".mm"}
HEADER_SUFFIXES = {".h", ".hh", ".hpp", ".hxx", ".ipp", ".inc", ".macro"}
FLAGS_WITH_ARG_TO_DROP = {"-MF", "-MT", "-MQ", "-o", "--serialize-diagnostics"}
FLAGS_WITH_JOINED_ARG_TO_DROP = {"-MF", "-MT", "-MQ", "--serialize-diagnostics"}
FLAGS_TO_DROP = {"-MD", "-MMD", "-MP", "-c"}


@dataclass(frozen=True)
class CompileEntry:
    file: Path
    directory: Path
    args: list[str]


def run_git(root: Path, args: list[str]) -> list[Path]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [root / line for line in result.stdout.splitlines() if line.strip()]


def worktree_dirty_files(root: Path) -> set[Path]:
    paths: set[Path] = set()
    paths.update(
        run_git(root, ["diff", "--cached", "--name-only", "--diff-filter=ACMRTUXB"])
    )
    paths.update(run_git(root, ["diff", "--name-only", "--diff-filter=ACMRTUXB"]))
    paths.update(run_git(root, ["ls-files", "--others", "--exclude-standard"]))
    return {path for path in paths if path.exists()}


def dirty_files(root: Path, since: str | None) -> list[Path]:
    paths = worktree_dirty_files(root)
    if since:
        paths.update(
            run_git(
                root,
                [
                    "diff",
                    "--name-only",
                    "--diff-filter=ACMRTUXB",
                    f"{since}...HEAD",
                ],
            )
        )
    return sorted(path for path in paths if path.exists())


def default_compile_commands(root: Path, build_dir: Path | None) -> Path:
    candidates = []
    if build_dir:
        candidates.append(build_dir / "compile_commands.json")
    candidates.extend(
        [
            root / "build" / "compile_commands.json",
            root / "compile_commands.json",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def load_compile_commands(path: Path) -> dict[Path, CompileEntry]:
    if not path.exists():
        raise FileNotFoundError(f"compile database not found: {path}")

    raw = json.loads(path.read_text())
    entries: dict[Path, CompileEntry] = {}
    for item in raw:
        directory = Path(item.get("directory", ".")).resolve()
        file_path = Path(item["file"])
        if not file_path.is_absolute():
            file_path = directory / file_path
        file_path = file_path.resolve()

        if "arguments" in item:
            args = [str(arg) for arg in item["arguments"]]
        else:
            args = shlex.split(item["command"])

        entries[file_path] = CompileEntry(
            file=file_path, directory=directory, args=args
        )
    return entries


def sibling_translation_units(
    path: Path, compile_db: dict[Path, CompileEntry]
) -> list[Path]:
    units = []
    for suffix in SOURCE_SUFFIXES:
        sibling = path.with_suffix(suffix).resolve()
        if sibling in compile_db:
            units.append(sibling)
    return units


def select_translation_units(
    root: Path,
    changed: list[Path],
    compile_db: dict[Path, CompileEntry],
    extra_tus: list[Path],
) -> tuple[list[Path], list[Path]]:
    selected: set[Path] = set()
    unmapped_headers: list[Path] = []

    for path in changed:
        resolved = path.resolve()
        suffix = path.suffix
        if suffix in SOURCE_SUFFIXES:
            if resolved in compile_db:
                selected.add(resolved)
            else:
                logger.warning(
                    f"dirty source has no compile_db entry: {path.relative_to(root)}"
                )
        elif suffix in HEADER_SUFFIXES:
            siblings = sibling_translation_units(resolved, compile_db)
            if siblings:
                selected.update(siblings)
            else:
                unmapped_headers.append(path)

    for tu in extra_tus:
        resolved = (root / tu).resolve() if not tu.is_absolute() else tu.resolve()
        if resolved in compile_db:
            selected.add(resolved)
        else:
            logger.warning(f"--tu has no compile_db entry: {tu}")

    return sorted(selected), unmapped_headers


def syntax_only_args(args: list[str]) -> list[str]:
    cleaned: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in FLAGS_WITH_ARG_TO_DROP:
            i += 2
            continue
        if any(
            arg.startswith(flag) and arg != flag
            for flag in FLAGS_WITH_JOINED_ARG_TO_DROP
        ):
            i += 1
            continue
        if arg in FLAGS_TO_DROP:
            i += 1
            continue
        cleaned.append(arg)
        i += 1

    cleaned.append("-fsyntax-only")
    return cleaned


def run_one(entry: CompileEntry, root: Path, verbose: bool) -> bool:
    rel: Path | str
    if entry.file.is_relative_to(root):
        rel = entry.file.relative_to(root)
    else:
        rel = str(entry.file)

    cmd = syntax_only_args(entry.args)
    logger.info(f"checking {rel}")
    if verbose:
        logger.info("  " + shlex.join(cmd))

    result = subprocess.run(
        cmd,
        cwd=entry.directory,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode == 0:
        return True

    logger.error(f"failed {rel}")
    sys.stderr.write(result.stdout)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a fast compiler syntax check for dirty xahaud C/C++ files."
    )
    parser.add_argument("--repo", type=Path, help="xahaud repository root")
    parser.add_argument(
        "--build-dir", type=Path, help="build directory with compile_commands.json"
    )
    parser.add_argument(
        "--compile-commands", type=Path, help="explicit compile_commands.json path"
    )
    parser.add_argument(
        "--since", metavar="REF", help="check files changed since REF...HEAD"
    )
    parser.add_argument(
        "--tu",
        action="append",
        type=Path,
        default=[],
        help="extra translation unit to check; useful for header-only edits",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print selected TUs only"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="print compiler commands"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Set the logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )
    args = parser.parse_args()

    setup_logging(args.log_level, logger)

    root = args.repo.resolve() if args.repo else Path(get_xahaud_root()).resolve()
    build_dir = args.build_dir.resolve() if args.build_dir else None
    compile_commands = (
        args.compile_commands.resolve()
        if args.compile_commands
        else default_compile_commands(root, build_dir)
    )

    changed = dirty_files(root, args.since)
    if not changed and not args.tu:
        logger.info("no dirty files to quick-check")
        return

    compile_db = load_compile_commands(compile_commands)
    selected, unmapped_headers = select_translation_units(
        root, changed, compile_db, args.tu
    )

    for header in unmapped_headers:
        rel: Path | str
        rel = header.relative_to(root) if header.is_relative_to(root) else str(header)
        logger.warning(f"dirty header has no sibling TU; add --tu if needed: {rel}")

    if not selected:
        logger.info("no translation units selected")
        return

    logger.info(f"compile database: {compile_commands}")
    logger.info(f"selected {len(selected)} translation unit(s)")

    if args.dry_run:
        for path in selected:
            if path.is_relative_to(root):
                print(path.relative_to(root))
            else:
                print(path)
        return

    ok = True
    for path in selected:
        ok = run_one(compile_db[path], root, args.verbose) and ok

    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
