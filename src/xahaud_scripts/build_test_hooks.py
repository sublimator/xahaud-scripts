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
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import click

from xahaud_scripts.utils.logging import make_logger, setup_logging
from xahaud_scripts.utils.paths import get_xahaud_root

logger = make_logger(__name__)


class BinaryChecker:
    """Check for required binaries and provide installation instructions."""

    REQUIRED_BINARIES = {
        "wasmcc": "curl https://raw.githubusercontent.com/aspect-build/aspect-cli/main/docs/aspect/wasmcc/install.sh | sh",
        "hook-cleaner": "git clone https://github.com/RichardAH/hook-cleaner-c.git && cd hook-cleaner-c && make && cp hook-cleaner ~/.local/bin/",
        "wat2wasm": "brew install wabt",
        "clang-format": "brew install clang-format",
    }

    def check_binary(self, name: str) -> str | None:
        """Check if binary exists and return its path."""
        result = subprocess.run(["which", name], capture_output=True, text=True)
        if result.returncode == 0:
            path = result.stdout.strip()
            logger.info(f"✓ {name}: {path}")
            return path
        return None

    def check_all(self) -> bool:
        """Check all required binaries. Returns True if all found."""
        logger.info("Checking required tools...")
        all_found = True

        for binary, install_msg in self.REQUIRED_BINARIES.items():
            path = self.check_binary(binary)
            if not path:
                logger.error(f"✗ {binary}: NOT FOUND")
                logger.error(f"  Install: {install_msg}")
                all_found = False

        if all_found:
            logger.info("All required tools found!")

        return all_found


class CompilationCache:
    """Cache compiled WASM bytecode based on source and binary versions."""

    def __init__(self) -> None:
        self.cache_dir = Path.home() / ".cache" / "build_test_hooks"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.binary_versions = self._get_binary_versions()
        logger.debug(f"Cache directory: {self.cache_dir}")

    def _get_binary_version(self, binary: str) -> str:
        """Get version hash of a binary."""
        try:
            which_result = subprocess.run(
                ["which", binary], capture_output=True, text=True, check=True
            )
            binary_path = which_result.stdout.strip()

            hasher = hashlib.sha256()
            with open(binary_path, "rb") as f:
                hasher.update(f.read())
            return hasher.hexdigest()[:16]
        except Exception as e:
            logger.warning(f"Could not hash {binary}: {e}")
            return "unknown"

    def _get_binary_versions(self) -> dict[str, str]:
        """Get version hashes of all compilation binaries."""
        binaries = ["wasmcc", "hook-cleaner", "wat2wasm"]
        versions = {}

        for binary in binaries:
            versions[binary] = self._get_binary_version(binary)
            logger.debug(f"{binary} version hash: {versions[binary]}")

        return versions

    def _compute_cache_key(self, source: str, is_wat: bool) -> str:
        """Compute cache key from source and binary versions."""
        hasher = hashlib.sha256()
        hasher.update(source.encode("utf-8"))
        hasher.update(b"wat" if is_wat else b"c")

        if is_wat:
            hasher.update(self.binary_versions["wat2wasm"].encode("utf-8"))
        else:
            hasher.update(self.binary_versions["wasmcc"].encode("utf-8"))
            hasher.update(self.binary_versions["hook-cleaner"].encode("utf-8"))

        return hasher.hexdigest()

    def get(self, source: str, is_wat: bool) -> bytes | None:
        """Get cached bytecode if available."""
        cache_key = self._compute_cache_key(source, is_wat)
        cache_file = self.cache_dir / f"{cache_key}.wasm"

        if cache_file.exists():
            logger.debug(f"Cache hit: {cache_key[:16]}...")
            return cache_file.read_bytes()

        logger.debug(f"Cache miss: {cache_key[:16]}...")
        return None

    def put(self, source: str, is_wat: bool, bytecode: bytes) -> None:
        """Store bytecode in cache."""
        cache_key = self._compute_cache_key(source, is_wat)
        cache_file = self.cache_dir / f"{cache_key}.wasm"

        cache_file.write_bytes(bytecode)
        logger.debug(f"Cached: {cache_key[:16]}... ({len(bytecode)} bytes)")


class SourceValidator:
    """Validate C source code for undeclared functions."""

    def extract_declarations(self, source: str) -> tuple[list[str], list[str]]:
        """Extract declared and used function names."""
        normalized = re.sub(r"\s+", " ", source)

        declared = set()
        used = set()

        decl_pattern = r"(?:extern|define)\s+[a-z0-9_]+\s+([a-z_-]+)\s*\("
        for match in re.finditer(decl_pattern, normalized):
            func_name = match.group(1)
            if func_name != "sizeof":
                declared.add(func_name)

        call_pattern = r"([a-z_-]+)\("
        for match in re.finditer(call_pattern, normalized):
            func_name = match.group(1)
            if func_name != "sizeof" and not func_name.startswith(("hook", "cbak")):
                used.add(func_name)

        return sorted(declared), sorted(used)

    def validate(self, source: str, counter: int) -> None:
        """Validate that all used functions are declared."""
        declared, used = self.extract_declarations(source)
        undeclared = set(used) - set(declared)

        if undeclared:
            logger.error(
                f"Undeclared functions in block {counter}: {', '.join(sorted(undeclared))}"
            )
            logger.debug(f"  Declared: {', '.join(declared)}")
            logger.debug(f"  Used: {', '.join(used)}")
            raise ValueError(f"Undeclared functions: {', '.join(sorted(undeclared))}")


class WasmCompiler:
    """Compile WASM from C or WAT source."""

    def __init__(self, wasm_dir: Path, cache: CompilationCache) -> None:
        self.wasm_dir = wasm_dir
        self.cache = cache
        self.validator = SourceValidator()

    def is_wat_format(self, source: str) -> bool:
        """Check if source is WAT format."""
        return "(module" in source

    def compile_c(self, source: str, counter: int) -> bytes:
        """Compile C source to WASM."""
        logger.debug(f"Compiling C for block {counter}")
        self.validator.validate(source, counter)

        source_file = self.wasm_dir / f"test-{counter}-gen.c"
        source_file.write_text(f'#include "api.h"\n{source}')

        wasmcc_result = subprocess.run(
            [
                "wasmcc",
                "-x",
                "c",
                "/dev/stdin",
                "-o",
                "/dev/stdout",
                "-O2",
                "-Wl,--allow-undefined",
            ],
            input=source.encode("utf-8"),
            capture_output=True,
            check=True,
        )

        cleaner_result = subprocess.run(
            ["hook-cleaner", "-", "-"],
            input=wasmcc_result.stdout,
            capture_output=True,
            check=True,
        )

        return cleaner_result.stdout

    def compile_wat(self, source: str) -> bytes:
        """Compile WAT source to WASM."""
        logger.debug("Compiling WAT")
        source = re.sub(r"/\*end\*/$", "", source)

        result = subprocess.run(
            ["wat2wasm", "-", "-o", "/dev/stdout"],
            input=source.encode("utf-8"),
            capture_output=True,
            check=True,
        )

        return result.stdout

    def compile(self, source: str, counter: int) -> bytes:
        """Compile source, using cache if available."""
        is_wat = self.is_wat_format(source)

        cached = self.cache.get(source, is_wat)
        if cached is not None:
            logger.info(f"Block {counter}: using cached bytecode")
            return cached

        logger.info(f"Block {counter}: compiling {'WAT' if is_wat else 'C'}")

        try:
            if is_wat:
                bytecode = self.compile_wat(source)
            else:
                bytecode = self.compile_c(source, counter)

            self.cache.put(source, is_wat, bytecode)
            return bytecode

        except subprocess.CalledProcessError as e:
            error_msg = str(e)
            if e.stderr:
                try:
                    error_msg = e.stderr.decode("utf-8")
                except Exception:
                    error_msg = f"Binary error output ({len(e.stderr)} bytes)"
            logger.error(f"Compilation failed: {error_msg}")
            raise


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


class SourceExtractor:
    """Extract WASM test blocks from source file."""

    def __init__(self, input_file: Path) -> None:
        self.input_file = input_file

    def extract(self) -> list[tuple[str, int]]:
        """Extract all WASM test blocks with line numbers."""
        logger.info(f"Reading {self.input_file}")
        content = self.input_file.read_text()

        pattern = r'R"\[test\.hook\]\((.*?)\)\[test\.hook\]"'
        blocks_with_lines = []

        for match in re.finditer(pattern, content, re.DOTALL):
            source = match.group(1)
            line_number = content[: match.start()].count("\n") + 1
            blocks_with_lines.append((source, line_number))

        logger.info(f"Found {len(blocks_with_lines)} WASM test blocks")
        return blocks_with_lines


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
std::map<std::string, std::vector<uint8_t>> {self.symbol_name} = {{
"""

    def _get_footer(self) -> str:
        return """}};
}
}
#endif
"""

    def _get_clang_format_cache_file(self, content_hash: str) -> Path:
        """Get cache file path for formatted output."""
        return self.cache_dir / f"formatted_{content_hash}.h"

    def _format_content(self, unformatted_content: str) -> str:
        """Format content using clang-format via temp file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".h", delete=False) as tmp:
            tmp.write(unformatted_content)
            tmp_path = tmp.name

        try:
            subprocess.run(["clang-format", "-i", tmp_path], check=True)
            with open(tmp_path) as f:
                return f.read()
        finally:
            os.unlink(tmp_path)

    def write(
        self, compiled_blocks: dict[int, tuple[str, bytes]], force_write: bool = False
    ) -> None:
        """Write all compiled blocks to output file, only if changed."""
        unformatted = []
        unformatted.append(self._get_header())
        for counter in sorted(compiled_blocks.keys()):
            source, bytecode = compiled_blocks[counter]
            unformatted.append(f"/* ==== WASM: {counter} ==== */\n")
            unformatted.append('{ R"[test.hook](')
            unformatted.append(source)
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
            logger.info("Formatting with clang-format")
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
        self, jobs: int, force_write: bool, input_file: Path | None = None
    ) -> None:
        self.jobs = jobs
        self.force_write = force_write

        xahaud_root = Path(get_xahaud_root())
        test_app_dir = xahaud_root / "src" / "test" / "app"

        self.wasm_dir = test_app_dir / "generated" / "hook" / "c"

        # Use provided input file or default to SetHook_test.cpp
        if input_file is not None:
            self.input_file = input_file
            # Generate output name: Foo_test.cpp -> Foo_test_hooks.h
            stem = input_file.stem  # e.g., "Export_test"
            self.output_file = input_file.parent / f"{stem}_hooks.h"
            # Generate symbol name: Export_test -> export_test_wasm
            self.symbol_name = f"{stem.lower()}_wasm"
        else:
            self.input_file = test_app_dir / "SetHook_test.cpp"
            self.output_file = test_app_dir / "SetHook_wasm.h"
            self.symbol_name = "wasm"  # Keep backward compatibility

        self.checker = BinaryChecker()
        self.cache = CompilationCache()
        self.compiler = WasmCompiler(self.wasm_dir, self.cache)
        self.extractor = SourceExtractor(self.input_file)
        self.writer = OutputWriter(
            self.output_file, self.cache.cache_dir, self.symbol_name
        )

    def _get_worker_count(self) -> int:
        """Get number of parallel workers to use."""
        if self.jobs > 0:
            return self.jobs
        return os.cpu_count() or 1

    def compile_block(
        self, counter: int, source: str, line_number: int
    ) -> tuple[int, str, bytes]:
        """Compile a single block."""
        bytecode = self.compiler.compile(source, counter)
        return (counter, source, bytecode)

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
        self, blocks: list[tuple[str, int]]
    ) -> dict[int, tuple[str, bytes]]:
        """Compile all blocks in parallel."""
        workers = self._get_worker_count()
        logger.info(f"Compiling {len(blocks)} blocks using {workers} workers")

        compiled: dict[int, tuple[str, bytes]] = {}
        failed_blocks = []

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self.compile_block, i, block, line_num): (i, line_num)
                for i, (block, line_num) in enumerate(blocks)
            }

            for future in as_completed(futures):
                counter, line_num = futures[future]
                try:
                    result_counter, source, bytecode = future.result()
                    compiled[result_counter] = (source, bytecode)
                except Exception as e:
                    logger.error(
                        f"Block {counter} (line {line_num} in {self.input_file.name}) failed: {e}"
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
        logger.info(f"  Input: {self.input_file}")
        logger.info(f"  Output: {self.output_file}")
        logger.info(f"  Cache: {self.cache.cache_dir}")

        if not self.checker.check_all():
            logger.error("Missing required binaries")
            sys.exit(1)

        self.wasm_dir.mkdir(parents=True, exist_ok=True)

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
def main(
    input_file: Path | None, log_level: str, jobs: int, force_write: bool
) -> None:
    """Generate _hooks.h from a test file containing WASM blocks.

    Extracts WASM test code blocks, compiles them using wasmcc or wat2wasm,
    and generates a C++ header with the compiled bytecode.

    If INPUT_FILE is provided, output is named <stem>_hooks.h (e.g.,
    Export_test.cpp -> Export_test_hooks.h).

    If no INPUT_FILE, defaults to SetHook_test.cpp -> SetHook_wasm.h.

    Examples:

        x-build-test-hooks                         # Default: SetHook_test.cpp

        x-build-test-hooks Export_test.cpp         # -> Export_test_hooks.h

        x-build-test-hooks -j 4 Foo_test.cpp       # 4 workers

        x-build-test-hooks --force-write           # Always write output
    """
    setup_logging(log_level, logger)

    try:
        builder = TestHookBuilder(
            jobs=jobs, force_write=force_write, input_file=input_file
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
