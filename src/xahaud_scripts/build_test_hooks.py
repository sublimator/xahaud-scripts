#!/usr/bin/env python3
"""
Generate SetHook_wasm.h from SetHook_test.cpp

Extracts WASM test code blocks from the test file, compiles them using wasmcc or wat2wasm,
and generates a C++ header file with the compiled bytecode.

Features intelligent caching based on source content and binary versions.

Originally from: https://github.com/Xahau/xahaud/blob/dev/src/test/app/build_test_hooks.py
"""

import hashlib
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import click

from xahaud_scripts.hooks import BinaryChecker, CompilationCache, WasmCompiler
from xahaud_scripts.utils.logging import make_logger, setup_logging
from xahaud_scripts.utils.paths import get_xahaud_root
from xahaud_scripts.utils.shell_utils import get_mise_tool_cmd

logger = make_logger(__name__)


class OutputFormatter:
    """Format compiled bytecode as C++ arrays."""

    @staticmethod
    def bytes_to_cpp_array(data: bytes) -> str:
        """Convert binary data to C++ array format."""
        lines = []
        for i in range(0, len(data), 10):
            chunk = data[i : i + 10]
            hex_values = ",".join(f"0x{b:02X}U" for b in chunk)
            lines.append(f"    {hex_values},")
        return "\n".join(lines)


@dataclass
class HookBlock:
    """A hook block to compile."""

    map_key: str  # C++ map key: inline source or "file:xxx.c"
    source: str  # Compilable source code
    line_number: int  # Line number in test file
    is_file_ref: bool  # True if from external file


class SourceExtractor:
    """Extract WASM test blocks from source file."""

    def __init__(
        self,
        input_file: Path,
        hooks_c_dirs: dict[str, Path] | None = None,
    ) -> None:
        self.input_file = input_file
        self.hooks_c_dirs = hooks_c_dirs or {}

    def _resolve_file_ref(self, ref: str, line_number: int) -> tuple[str, Path]:
        """Resolve a file:domain/path reference.

        Returns (domain, resolved_path).
        """
        if "/" not in ref:
            raise click.ClickException(
                f'"file:{ref}" at line {line_number} is missing a domain. '
                f'Use "file:<domain>/<path>" (e.g. "file:tipbot/tip.c")'
            )

        domain, path = ref.split("/", 1)

        if not self.hooks_c_dirs:
            raise click.ClickException(
                f'Found file reference "file:{ref}" at line {line_number} '
                f"but no --hooks-c-dir was specified"
            )

        if domain not in self.hooks_c_dirs:
            available = ", ".join(sorted(self.hooks_c_dirs))
            raise click.ClickException(
                f'Unknown domain "{domain}" in "file:{ref}" at line {line_number}. '
                f"Available: {available}"
            )

        file_path = self.hooks_c_dirs[domain] / path
        if not file_path.exists():
            raise click.ClickException(
                f"Hook file not found: {file_path} "
                f'(referenced as "file:{ref}" at line {line_number})'
            )

        return domain, file_path

    def extract(self) -> list[HookBlock]:
        """Extract all WASM test blocks and file references."""
        logger.info(f"Reading {self.input_file}")
        content = self.input_file.read_text()

        blocks: list[HookBlock] = []

        # Inline blocks: R"[test.hook](...)[test.hook]"
        pattern = r'R"\[test\.hook\]\((.*?)\)\[test\.hook\]"'
        for match in re.finditer(pattern, content, re.DOTALL):
            source = match.group(1)
            line_number = content[: match.start()].count("\n") + 1
            blocks.append(
                HookBlock(
                    map_key=source,
                    source=source,
                    line_number=line_number,
                    is_file_ref=False,
                )
            )

        # File references: "file:domain/path.c"
        file_pattern = r'"file:([^"]+)"'
        seen_refs: set[str] = set()
        for match in re.finditer(file_pattern, content):
            ref = match.group(1)
            if ref in seen_refs:
                continue
            seen_refs.add(ref)

            line_number = content[: match.start()].count("\n") + 1
            _domain, file_path = self._resolve_file_ref(ref, line_number)

            source = file_path.read_text()
            blocks.append(
                HookBlock(
                    map_key=f"file:{ref}",
                    source=source,
                    line_number=line_number,
                    is_file_ref=True,
                )
            )

        inline_count = sum(1 for b in blocks if not b.is_file_ref)
        file_count = sum(1 for b in blocks if b.is_file_ref)
        logger.info(
            f"Found {len(blocks)} hook blocks"
            f" ({inline_count} inline, {file_count} file refs)"
        )
        return blocks


class OutputWriter:
    """Write compiled blocks to output file."""

    def __init__(self, output_file: Path, cache_dir: Path, symbol_name: str) -> None:
        self.output_file = output_file
        self.cache_dir = cache_dir
        self.symbol_name = symbol_name
        # Generate unique include guard from symbol name
        self.include_guard = f"{symbol_name.upper()}_INCLUDED"

    def _get_header(self) -> str:
        return f"""
//This file is generated by build_test_hooks.py
#ifndef {self.include_guard}
#define {self.include_guard}
#include <map>
#include <stdint.h>
#include <string>
#include <vector>
namespace ripple {{
namespace test {{
inline std::map<std::string, std::vector<uint8_t>> {self.symbol_name} = {{
"""

    def _get_footer(self) -> str:
        return """};
}
}
#endif
"""

    def _get_clang_format_cache_file(self, content_hash: str) -> Path:
        """Get cache file path for formatted output."""
        return self.cache_dir / f"formatted_{content_hash}.h"

    def _format_content(self, unformatted_content: str) -> str:
        """Format content using clang-format.

        Uses --assume-filename so clang-format finds the project's .clang-format
        file based on the output file's location.
        """
        cf_cmd = get_mise_tool_cmd("clang-format")
        result = subprocess.run(
            [*cf_cmd, f"--assume-filename={self.output_file}"],
            input=unformatted_content,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(f"clang-format stderr: {result.stderr.strip()}")
            result.check_returncode()
        return result.stdout

    def write(
        self,
        compiled_blocks: dict[int, tuple[HookBlock, bytes]],
        force_write: bool = False,
    ) -> None:
        """Write all compiled blocks to output file, only if changed.

        Caching strategy:
        1. Cache formatted output keyed by hash of unformatted content
        2. Only write to disk if content actually changed
        This avoids modifying file mtime when content is unchanged,
        which prevents unnecessary rebuilds in the build system.
        """
        unformatted = []
        unformatted.append(self._get_header())
        for counter in sorted(compiled_blocks.keys()):
            block, bytecode = compiled_blocks[counter]
            if block.is_file_ref:
                unformatted.append(f"/* ==== WASM: {block.map_key} ==== */\n")
                unformatted.append(f'{{ "{block.map_key}",\n{{\n')
            else:
                unformatted.append(f"/* ==== WASM: {counter} ==== */\n")
                unformatted.append('{ R"[test.hook](')
                unformatted.append(block.map_key)
                unformatted.append(')[test.hook]",\n{\n')
            unformatted.append(OutputFormatter.bytes_to_cpp_array(bytecode))
            unformatted.append("\n}},\n\n")
        unformatted.append(self._get_footer())
        unformatted_content = "".join(unformatted)

        content_hash = hashlib.sha256(unformatted_content.encode("utf-8")).hexdigest()
        cache_file = self._get_clang_format_cache_file(content_hash)

        if cache_file.exists():
            logger.info("Using cached clang-format output")
            formatted_content = cache_file.read_text()
        else:
            # Log which clang-format we're using
            cf_cmd = get_mise_tool_cmd("clang-format")
            cf_version = subprocess.run(
                [*cf_cmd, "--version"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            logger.info(f"Formatting with {' '.join(cf_cmd)} ({cf_version})")
            formatted_content = self._format_content(unformatted_content)
            cache_file.write_text(formatted_content)
            logger.debug(f"Cached formatted output: {content_hash[:16]}...")

        if not force_write and self.output_file.exists():
            existing_content = self.output_file.read_text()
            if existing_content == formatted_content:
                logger.info("Output unchanged, skipping write")
                return

        logger.info(f"Writing {self.output_file}")
        self.output_file.write_text(formatted_content)


class TestHookBuilder:
    """Main builder orchestrating the compilation process."""

    def __init__(
        self,
        jobs: int,
        force_write: bool,
        input_file: Path | None = None,
        hooks_c_dirs: dict[str, Path] | None = None,
        hook_coverage: bool = False,
        validate_hooks: bool = False,
    ) -> None:
        self.jobs = jobs
        self.force_write = force_write
        self.hook_coverage = hook_coverage

        # Resolve xahaud root if available (not required when input_file is given)
        try:
            xahaud_root: Path | None = Path(get_xahaud_root())
        except Exception:
            xahaud_root = None

        self.hook_include_dir: Path | None = (
            xahaud_root / "hook" if xahaud_root else None
        )

        # Use provided input file or default to SetHook_test.cpp (requires xahaud root)
        if input_file is not None:
            self.input_file = input_file
            # Generate output name: Foo_test.cpp -> Foo_test_hooks.h
            stem = input_file.stem  # e.g., "Export_test"
            self.output_file = input_file.parent / f"{stem}_hooks.h"
            # Generate symbol name: Export_test -> export_test_wasm
            self.symbol_name = f"{stem.lower()}_wasm"
        else:
            if xahaud_root is None:
                raise click.ClickException(
                    "No INPUT_FILE given and could not find a xahaud repo root "
                    "(no CMakeLists.txt + .git found). Either run from inside a "
                    "xahaud worktree, set XAHAUD_ROOT, or pass an explicit INPUT_FILE."
                )
            test_app_dir = xahaud_root / "src" / "test" / "app"
            self.input_file = test_app_dir / "SetHook_test.cpp"
            self.output_file = test_app_dir / "SetHook_wasm.h"
            self.symbol_name = "wasm"  # Keep backward compatibility

        self.checker = BinaryChecker()
        self.cache = CompilationCache()
        self.compiler = WasmCompiler(cache=self.cache, validate_c=validate_hooks)
        self.extractor = SourceExtractor(self.input_file, hooks_c_dirs=hooks_c_dirs)
        self.writer = OutputWriter(
            self.output_file, self.cache.cache_dir, self.symbol_name
        )

    def _get_worker_count(self) -> int:
        """Get number of parallel workers to use."""
        if self.jobs > 0:
            return self.jobs
        return os.cpu_count() or 1

    def compile_block(
        self, counter: int, block: HookBlock
    ) -> tuple[int, HookBlock, bytes]:
        """Compile a single block."""
        label = block.map_key if block.is_file_ref else f"Block {counter}"
        bytecode = self.compiler.compile(
            block.source,
            label,
            validate=not block.is_file_ref,
            include_dirs=(
                [self.hook_include_dir]
                if block.is_file_ref and self.hook_include_dir
                else None
            ),
            coverage=self.hook_coverage,
        )
        return (counter, block, bytecode)

    def _format_block_ranges(self, block_numbers: list[int]) -> str:
        """Format block numbers as compact ranges (e.g., '1-3,5,7-9')."""
        if not block_numbers:
            return ""

        sorted_blocks = sorted(block_numbers)
        ranges = []
        start = sorted_blocks[0]
        end = sorted_blocks[0]

        for num in sorted_blocks[1:]:
            if num == end + 1:
                end = num
            else:
                if start == end:
                    ranges.append(str(start))
                else:
                    ranges.append(f"{start}-{end}")
                start = end = num

        if start == end:
            ranges.append(str(start))
        else:
            ranges.append(f"{start}-{end}")

        return ",".join(ranges)

    def compile_all_blocks(
        self, blocks: list[HookBlock]
    ) -> dict[int, tuple[HookBlock, bytes]]:
        """Compile all blocks in parallel."""
        workers = self._get_worker_count()
        logger.info(f"Compiling {len(blocks)} blocks using {workers} workers")

        compiled: dict[int, tuple[HookBlock, bytes]] = {}
        failed_blocks = []

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self.compile_block, i, block): (i, block)
                for i, block in enumerate(blocks)
            }

            for future in as_completed(futures):
                counter, block = futures[future]
                try:
                    result_counter, result_block, bytecode = future.result()
                    compiled[result_counter] = (result_block, bytecode)
                except Exception as e:
                    label = block.map_key if block.is_file_ref else f"Block {counter}"
                    logger.error(
                        f"{label} (line {block.line_number} in {self.input_file.name}) failed: {e}"
                    )
                    failed_blocks.append(counter)

        if failed_blocks:
            block_range = self._format_block_ranges(failed_blocks)
            total = len(failed_blocks)
            plural = "s" if total > 1 else ""
            raise RuntimeError(f"Block{plural} {block_range} failed ({total} total)")

        return compiled

    def build(self) -> None:
        """Execute the full build process."""
        logger.info("Starting WASM test hook build")

        workers = self._get_worker_count()
        logger.info(f"  Workers: {workers}")
        logger.info(f"  Force write: {self.force_write}")
        logger.info(f"  Coverage: {self.hook_coverage}")
        logger.info(f"  Input: {self.input_file}")
        logger.info(f"  Output: {self.output_file}")
        logger.info(f"  Cache: {self.cache.cache_dir}")

        # Check WASM compiler binaries + clang-format for output formatting
        # hook-cleaner is skipped in coverage mode but still check it's available
        required = ["wasmcc", "hook-cleaner", "wat2wasm", "clang-format"]
        if not self.checker.check_all(required):
            logger.error("Missing required binaries")
            sys.exit(1)

        blocks = self.extractor.extract()
        compiled = self.compile_all_blocks(blocks)
        self.writer.write(compiled, force_write=self.force_write)

        logger.info(f"Successfully generated {self.output_file}")


@click.command()
@click.argument(
    "input_file",
    type=click.Path(exists=True, path_type=Path),
    required=False,
    default=None,
)
@click.option(
    "--log-level",
    type=click.Choice(["debug", "info", "warning", "error"], case_sensitive=False),
    default="info",
    help="Set logging level",
)
@click.option(
    "-j",
    "--jobs",
    type=int,
    default=0,
    help="Parallel workers (default: CPU count)",
)
@click.option(
    "--force-write",
    is_flag=True,
    help="Always write output file even if unchanged",
)
@click.option(
    "--hooks-c-dir",
    "hooks_c_dir_raw",
    multiple=True,
    help="Hook source dirs as domain=path (e.g. tipbot=/path/to/hooks). Repeatable.",
)
@click.option(
    "--hook-coverage/--no-hook-coverage",
    is_flag=True,
    default=False,
    help="Compile with SanitizerCoverage instrumentation (-fsanitize-coverage=trace-pc-guard).",
)
@click.option(
    "--validate-hooks/--no-validate-hooks",
    is_flag=True,
    default=False,
    help="Validate inline hook C source for undeclared functions (default: disabled).",
)
def main(
    input_file: Path | None,
    log_level: str,
    jobs: int,
    force_write: bool,
    hooks_c_dir_raw: tuple[str, ...],
    hook_coverage: bool,
    validate_hooks: bool,
) -> None:
    """Generate _hooks.h from a test file containing WASM blocks.

    Extracts WASM test code blocks, compiles them using wasmcc or wat2wasm,
    and generates a C++ header with the compiled bytecode.

    Test files can contain inline hooks via R"[test.hook](...)[test.hook]"
    and/or external file references via "file:domain/path.c" (requires
    --hooks-c-dir domain=path).

    If INPUT_FILE is provided, output is named <stem>_hooks.h (e.g.,
    Export_test.cpp -> Export_test_hooks.h).

    If no INPUT_FILE, defaults to SetHook_test.cpp -> SetHook_wasm.h.

    Examples:

        x-build-test-hooks                         # Default: SetHook_test.cpp

        x-build-test-hooks Export_test.cpp         # -> Export_test_hooks.h

        x-build-test-hooks -j 4 Foo_test.cpp       # 4 workers

        x-build-test-hooks --hooks-c-dir tipbot=/path/to/hooks Tip_test.cpp
    """
    setup_logging(log_level, logger)

    hooks_c_dirs: dict[str, Path] = {}
    for entry in hooks_c_dir_raw:
        if "=" not in entry:
            raise click.ClickException(
                f'Invalid --hooks-c-dir "{entry}". Expected domain=path '
                f"(e.g. tipbot=/path/to/hooks)"
            )
        domain, dir_path = entry.split("=", 1)
        resolved = Path(dir_path).expanduser().resolve()
        if not resolved.is_dir():
            raise click.ClickException(
                f'--hooks-c-dir "{domain}": directory not found: {resolved}'
            )
        hooks_c_dirs[domain] = resolved

    try:
        builder = TestHookBuilder(
            jobs=jobs,
            force_write=force_write,
            input_file=input_file,
            hooks_c_dirs=hooks_c_dirs or None,
            hook_coverage=hook_coverage,
            validate_hooks=validate_hooks,
        )
        builder.build()
    except RuntimeError as e:
        logger.error(f"Build failed: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Build failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
