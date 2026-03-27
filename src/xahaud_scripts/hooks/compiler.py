"""WASM hook compiler library.

Provides tools for compiling C and WAT source code to WASM bytecode,
with intelligent caching based on source content and binary versions.
"""

import hashlib
import re
import subprocess
from pathlib import Path

import tree_sitter_c as tsc
from tree_sitter import Language, Node, Parser

from xahaud_scripts.utils.logging import make_logger

logger = make_logger(__name__)


class BinaryChecker:
    """Check for required binaries and provide installation instructions."""

    REQUIRED_BINARIES = {
        "wasmcc": "curl https://raw.githubusercontent.com/aspect-build/aspect-cli/main/docs/aspect/wasmcc/install.sh | sh",
        "hook-cleaner": "git clone https://github.com/RichardAH/hook-cleaner-c.git && cd hook-cleaner-c && make && cp hook-cleaner ~/.local/bin/",
        "wat2wasm": "brew install wabt",
        "clang-format": "mise install clang-format",
    }

    def check_binary(self, name: str) -> str | None:
        """Check if binary exists and return its path."""
        result = subprocess.run(["which", name], capture_output=True, text=True)
        if result.returncode == 0:
            path = result.stdout.strip()
            logger.info(f"✓ {name}: {path}")
            return path
        return None

    def check_all(self, binaries: list[str] | None = None) -> bool:
        """Check required binaries. Returns True if all found.

        Args:
            binaries: List of binaries to check. If None, checks all required binaries.
        """
        logger.info("Checking required tools...")
        all_found = True

        to_check = binaries if binaries else list(self.REQUIRED_BINARIES.keys())

        for binary in to_check:
            path = self.check_binary(binary)
            if not path:
                logger.error(f"✗ {binary}: NOT FOUND")
                install_msg = self.REQUIRED_BINARIES.get(binary)
                if install_msg:
                    logger.error(f"  Install: {install_msg}")
                all_found = False

        if all_found:
            logger.info("All required tools found!")

        return all_found


class CompilationCache:
    """Cache compiled WASM bytecode based on source and binary versions.

    The cache key is computed from:
    - Source code content
    - Source type (C or WAT)
    - Binary versions (hashes of wasmcc, hook-cleaner, wat2wasm)

    This ensures recompilation when any of these change.
    """

    DEFAULT_CACHE_DIR = Path.home() / ".cache" / "xahaud-hooks"

    def __init__(self, cache_dir: Path | None = None) -> None:
        """Initialize the cache.

        Args:
            cache_dir: Directory to store cached WASM files.
                      Defaults to ~/.cache/xahaud-hooks
        """
        self.cache_dir = cache_dir or self.DEFAULT_CACHE_DIR
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

    def _compute_cache_key(
        self, source: str, is_wat: bool, coverage: bool = False
    ) -> str:
        """Compute cache key from source and binary versions."""
        hasher = hashlib.sha256()
        hasher.update(source.encode("utf-8"))
        hasher.update(b"wat" if is_wat else b"c")
        if coverage:
            hasher.update(b"coverage")

        if is_wat:
            hasher.update(self.binary_versions["wat2wasm"].encode("utf-8"))
        else:
            hasher.update(self.binary_versions["wasmcc"].encode("utf-8"))
            hasher.update(self.binary_versions["hook-cleaner"].encode("utf-8"))

        return hasher.hexdigest()

    def get(self, source: str, is_wat: bool, coverage: bool = False) -> bytes | None:
        """Get cached bytecode if available.

        Args:
            source: The source code that was compiled
            is_wat: True if source is WAT format, False for C
            coverage: True if compiled with coverage instrumentation

        Returns:
            The cached WASM bytecode, or None if not cached
        """
        cache_key = self._compute_cache_key(source, is_wat, coverage=coverage)
        cache_file = self.cache_dir / f"{cache_key}.wasm"

        if cache_file.exists():
            logger.debug(f"Cache hit: {cache_key[:16]}...")
            return cache_file.read_bytes()

        logger.debug(f"Cache miss: {cache_key[:16]}...")
        return None

    def put(
        self, source: str, is_wat: bool, bytecode: bytes, coverage: bool = False
    ) -> None:
        """Store bytecode in cache.

        Args:
            source: The source code that was compiled
            is_wat: True if source is WAT format, False for C
            bytecode: The compiled WASM bytecode
            coverage: True if compiled with coverage instrumentation
        """
        cache_key = self._compute_cache_key(source, is_wat, coverage=coverage)
        cache_file = self.cache_dir / f"{cache_key}.wasm"

        cache_file.write_bytes(bytecode)
        logger.debug(f"Cached: {cache_key[:16]}... ({len(bytecode)} bytes)")


_C_LANGUAGE = Language(tsc.language())
_C_PARSER = Parser(_C_LANGUAGE)


class SourceValidator:
    """Validate C source code for undeclared functions using tree-sitter."""

    def extract_declarations(self, source: str) -> tuple[list[str], list[str]]:
        """Extract declared and used function names via AST parsing.

        Returns:
            Tuple of (declared_functions, used_functions)
        """
        tree = _C_PARSER.parse(source.encode("utf-8"))

        declared: set[str] = set()
        used: set[str] = set()

        self._walk(tree.root_node, declared, used)

        return sorted(declared), sorted(used)

    def _walk(self, node: Node, declared: set[str], used: set[str]) -> None:
        """Recursively walk AST nodes collecting declared and used functions."""
        # Skip comments and string literals entirely
        if node.type in ("comment", "string_literal", "char_literal"):
            return

        if node.type == "declaration":
            self._handle_declaration(node, declared)
        elif node.type == "call_expression":
            self._handle_call(node, used)

        for child in node.children:
            self._walk(child, declared, used)

    def _handle_declaration(self, node: Node, declared: set[str]) -> None:
        """Extract function name from an extern declaration."""
        # Check for extern storage class
        has_extern = any(
            child.type == "storage_class_specifier" and child.text == b"extern"
            for child in node.children
        )  # child.text is bytes | None; == b"extern" is False when None
        if not has_extern:
            return

        # Find function declarator — may be direct or pointer-wrapped
        for child in node.children:
            name = self._extract_declarator_name(child)
            if name and name != "sizeof":
                declared.add(name)

    def _extract_declarator_name(self, node: Node) -> str | None:
        """Extract function name from a declarator node."""
        if node.type == "function_declarator":
            for child in node.children:
                if child.type == "identifier":
                    return child.text.decode("utf-8") if child.text else None
                if child.type in (
                    "parenthesized_declarator",
                    "pointer_declarator",
                    "function_declarator",
                ):
                    result = self._extract_declarator_name(child)
                    if result:
                        return result
        elif node.type in ("pointer_declarator", "parenthesized_declarator"):
            for child in node.children:
                result = self._extract_declarator_name(child)
                if result:
                    return result
        return None

    def _handle_call(self, node: Node, used: set[str]) -> None:
        """Extract function name from a call_expression node."""
        if not node.children:
            return
        func_node = node.children[0]

        # Direct call: name(...)
        if func_node.type == "identifier" and func_node.text:
            name = func_node.text.decode("utf-8")
            if name != "sizeof" and not name.startswith(("hook", "cbak")):
                used.add(name)

    def validate(self, source: str, label: str = "source") -> None:
        """Validate that all used functions are declared.

        Args:
            source: C source code to validate
            label: Label for error messages (e.g., block number)

        Raises:
            ValueError: If undeclared functions are found
        """
        declared, used = self.extract_declarations(source)
        undeclared = set(used) - set(declared)

        if undeclared:
            logger.error(
                f"Undeclared functions in {label}: {', '.join(sorted(undeclared))}"
            )
            logger.debug(f"  Declared: {', '.join(declared)}")
            logger.debug(f"  Used: {', '.join(used)}")
            raise ValueError(f"Undeclared functions: {', '.join(sorted(undeclared))}")


class WasmCompiler:
    """Compile WASM from C or WAT source.

    Supports both C (via wasmcc + hook-cleaner) and WAT (via wat2wasm) sources.
    Uses a CompilationCache to avoid redundant compilation.

    Example:
        cache = CompilationCache()
        compiler = WasmCompiler(cache=cache)

        # Compile C source
        bytecode = compiler.compile(c_source)

        # Compile WAT source
        bytecode = compiler.compile(wat_source)
    """

    def __init__(
        self,
        cache: CompilationCache | None = None,
        validate_c: bool = True,
    ) -> None:
        """Initialize the compiler.

        Args:
            cache: Optional cache for compiled bytecode. If None, creates a new one.
            validate_c: Whether to validate C source for undeclared functions.
        """
        self.cache = cache or CompilationCache()
        self.validate_c = validate_c
        self._validator = SourceValidator()

    @staticmethod
    def is_wat_format(source: str) -> bool:
        """Check if source is WAT format (contains module declaration)."""
        return "(module" in source

    def compile_c(
        self,
        source: str,
        label: str = "source",
        validate: bool = True,
        include_dirs: list[Path] | None = None,
        coverage: bool = False,
    ) -> bytes:
        """Compile C source to WASM.

        Args:
            source: C source code
            label: Label for error messages
            validate: Whether to validate declarations (default True)
            include_dirs: Extra -I paths for wasmcc
            coverage: Compile with SanitizerCoverage instrumentation

        Returns:
            Compiled WASM bytecode
        """
        logger.debug(f"Compiling C for {label}")

        if validate and self.validate_c:
            self._validator.validate(source, label)

        cmd = [
            "wasmcc",
            "-x",
            "c",
            "/dev/stdin",
            "-o",
            "/dev/stdout",
        ]
        if coverage:
            cmd.extend(["-fsanitize-coverage=trace-pc-guard", "-g", "-O0"])
        else:
            cmd.append("-O2")
        cmd.append("-Wl,--allow-undefined")

        for d in include_dirs or []:
            cmd.extend(["-I", str(d)])

        wasmcc_result = subprocess.run(
            cmd,
            input=source.encode("utf-8"),
            capture_output=True,
            check=True,
        )

        cleaner_cmd = ["hook-cleaner", "-", "-"]
        if coverage:
            cleaner_cmd.append("--keep-coverage")

        cleaner_result = subprocess.run(
            cleaner_cmd,
            input=wasmcc_result.stdout,
            capture_output=True,
            check=True,
        )

        return cleaner_result.stdout

    def compile_wat(self, source: str) -> bytes:
        """Compile WAT source to WASM.

        Args:
            source: WAT source code

        Returns:
            Compiled WASM bytecode
        """
        logger.debug("Compiling WAT")
        source = re.sub(r"/\*end\*/$", "", source)

        result = subprocess.run(
            ["wat2wasm", "-", "-o", "/dev/stdout"],
            input=source.encode("utf-8"),
            capture_output=True,
            check=True,
        )

        return result.stdout

    def compile(
        self,
        source: str,
        label: str = "source",
        validate: bool = True,
        include_dirs: list[Path] | None = None,
        coverage: bool = False,
    ) -> bytes:
        """Compile source to WASM, using cache if available.

        Automatically detects whether source is C or WAT format.

        Args:
            source: C or WAT source code
            label: Label for logging and error messages
            validate: Whether to validate C declarations (default True)
            include_dirs: Extra -I paths for wasmcc
            coverage: Compile with SanitizerCoverage instrumentation

        Returns:
            Compiled WASM bytecode

        Raises:
            subprocess.CalledProcessError: If compilation fails
            ValueError: If C source has undeclared functions (when validate_c=True)
        """
        is_wat = self.is_wat_format(source)

        cached = self.cache.get(source, is_wat, coverage=coverage)
        if cached is not None:
            logger.info(f"{label}: using cached bytecode")
            return cached

        cov_tag = " (coverage)" if coverage else ""
        logger.info(f"{label}: compiling {'WAT' if is_wat else 'C'}{cov_tag}")

        try:
            if is_wat:
                if coverage:
                    logger.warning(
                        f"{label}: coverage not supported for WAT, compiling without"
                    )
                bytecode = self.compile_wat(source)
            else:
                bytecode = self.compile_c(
                    source,
                    label,
                    validate=validate,
                    include_dirs=include_dirs,
                    coverage=coverage,
                )

            self.cache.put(source, is_wat, bytecode, coverage=coverage)
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
