"""WASM hook compiler library.

Provides tools for compiling C and WAT source code to WASM bytecode,
with intelligent caching based on source content and binary versions.
"""

import hashlib
import re
import subprocess
from pathlib import Path

from xahaud_scripts.utils.logging import make_logger

logger = make_logger(__name__)


def find_wasi_sdk() -> Path | None:
    """Find wasi-sdk installation via mise."""
    try:
        result = subprocess.run(
            ["mise", "where", "wasi-sdk"],
            capture_output=True,
            text=True,
            check=True,
        )
        base = Path(result.stdout.strip())
        # mise installs wasi-sdk with a nested wasi-sdk/ directory
        sdk = base / "wasi-sdk"
        if not sdk.exists():
            sdk = base
        clang = sdk / "bin" / "clang"
        if clang.exists():
            return sdk
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return None


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
        self,
        source: str,
        is_wat: bool,
        coverage: bool = False,
        hooks_compiler: str = "wasmcc",
    ) -> str:
        """Compute cache key from source and binary versions."""
        hasher = hashlib.sha256()
        hasher.update(source.encode("utf-8"))
        hasher.update(b"wat" if is_wat else b"c")
        hasher.update(hooks_compiler.encode("utf-8"))
        if coverage:
            hasher.update(b"coverage")

        if is_wat:
            hasher.update(self.binary_versions["wat2wasm"].encode("utf-8"))
        else:
            compiler_key = (
                "wasi-sdk-clang" if hooks_compiler == "wasi-sdk" else "wasmcc"
            )
            if compiler_key in self.binary_versions:
                hasher.update(self.binary_versions[compiler_key].encode("utf-8"))
            hasher.update(self.binary_versions.get("hook-cleaner", "").encode("utf-8"))

        return hasher.hexdigest()

    def get(
        self,
        source: str,
        is_wat: bool,
        coverage: bool = False,
        hooks_compiler: str = "wasmcc",
    ) -> bytes | None:
        """Get cached bytecode if available."""
        cache_key = self._compute_cache_key(
            source, is_wat, coverage=coverage, hooks_compiler=hooks_compiler
        )
        cache_file = self.cache_dir / f"{cache_key}.wasm"

        if cache_file.exists():
            logger.debug(f"Cache hit: {cache_key[:16]}...")
            return cache_file.read_bytes()

        logger.debug(f"Cache miss: {cache_key[:16]}...")
        return None

    def put(
        self,
        source: str,
        is_wat: bool,
        bytecode: bytes,
        coverage: bool = False,
        hooks_compiler: str = "wasmcc",
    ) -> None:
        """Store bytecode in cache."""
        cache_key = self._compute_cache_key(
            source, is_wat, coverage=coverage, hooks_compiler=hooks_compiler
        )
        cache_file = self.cache_dir / f"{cache_key}.wasm"

        cache_file.write_bytes(bytecode)
        logger.debug(f"Cached: {cache_key[:16]}... ({len(bytecode)} bytes)")


class WasmCompiler:
    """Compile WASM from C or WAT source.

    Supports both C (via wasmcc + hook-cleaner) and WAT (via wat2wasm) sources.
    Uses a CompilationCache to avoid redundant compilation.

    The ``hooks_compiler`` parameter selects the C compiler backend:
    - ``"wasmcc"`` (default): wasienv clang-9 wrapper
    - ``"wasi-sdk"``: modern clang from wasi-sdk (install: ``mise install wasi-sdk``)

    Example:
        compiler = WasmCompiler(hooks_compiler="wasi-sdk")
        bytecode = compiler.compile(c_source)
    """

    def __init__(
        self,
        cache: CompilationCache | None = None,
        hooks_compiler: str = "wasmcc",
    ) -> None:
        """Initialize the compiler.

        Args:
            cache: Optional cache for compiled bytecode. If None, creates a new one.
            hooks_compiler: C compiler backend — "wasmcc" or "wasi-sdk".
        """
        self.cache = cache or CompilationCache()
        self.hooks_compiler = hooks_compiler
        self._wasi_sdk_path: Path | None = None

        if hooks_compiler == "wasi-sdk":
            self._wasi_sdk_path = find_wasi_sdk()
            if not self._wasi_sdk_path:
                raise RuntimeError(
                    "wasi-sdk not found. Install with: mise install wasi-sdk"
                )

    @staticmethod
    def is_wat_format(source: str) -> bool:
        """Check if source is WAT format (contains module declaration)."""
        return "(module" in source

    def compile_c(
        self,
        source: str,
        label: str = "source",
        include_dirs: list[Path] | None = None,
        coverage: bool = False,
    ) -> bytes:
        """Compile C source to WASM.

        Args:
            source: C source code
            label: Label for error messages
            include_dirs: Extra -I paths for the compiler
            coverage: Compile with SanitizerCoverage instrumentation

        Returns:
            Compiled WASM bytecode
        """
        logger.debug(f"Compiling C for {label} (compiler={self.hooks_compiler})")

        if self.hooks_compiler == "wasi-sdk":
            return self._compile_c_wasi_sdk(source, label, include_dirs, coverage)
        return self._compile_c_wasmcc(source, label, include_dirs, coverage)

    def _compile_c_wasmcc(
        self,
        source: str,
        label: str,
        include_dirs: list[Path] | None,
        coverage: bool,
    ) -> bytes:
        """Compile C via wasmcc (wasienv clang-9) + hook-cleaner."""
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

    def _compile_c_wasi_sdk(
        self,
        source: str,
        label: str,
        include_dirs: list[Path] | None,
        coverage: bool,
    ) -> bytes:
        """Compile C via wasi-sdk (modern clang) + hook-cleaner."""
        assert self._wasi_sdk_path is not None
        clang = str(self._wasi_sdk_path / "bin" / "clang")
        sysroot = str(self._wasi_sdk_path / "share" / "wasi-sysroot")

        cmd = [
            clang,
            "--target=wasm32-wasip1",
            f"--sysroot={sysroot}",
            "-nostdlib",
            "-x",
            "c",
            "/dev/stdin",
            "-o",
            "/dev/stdout",
            # Hook code relies on implicit pointer-to-int casts (WASM32 addrs)
            "-Wno-incompatible-pointer-types",
            "-Wno-int-conversion",
            "-Wno-macro-redefined",
            "-Wl,--allow-undefined",
            "-Wl,--no-entry",
            "-Wl,--export=hook",
            "-Wl,--export=cbak",
        ]
        if coverage:
            cmd.extend(["-fsanitize-coverage=trace-pc-guard", "-g", "-O0"])
        else:
            cmd.append("-O2")

        for d in include_dirs or []:
            cmd.extend(["-I", str(d)])

        clang_result = subprocess.run(
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
            input=clang_result.stdout,
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
        include_dirs: list[Path] | None = None,
        coverage: bool = False,
    ) -> bytes:
        """Compile source to WASM, using cache if available.

        Automatically detects whether source is C or WAT format.

        Args:
            source: C or WAT source code
            label: Label for logging and error messages
            include_dirs: Extra -I paths for wasmcc
            coverage: Compile with SanitizerCoverage instrumentation

        Returns:
            Compiled WASM bytecode

        Raises:
            subprocess.CalledProcessError: If compilation fails
        """
        is_wat = self.is_wat_format(source)

        cached = self.cache.get(
            source, is_wat, coverage=coverage, hooks_compiler=self.hooks_compiler
        )
        if cached is not None:
            logger.info(f"{label}: using cached bytecode")
            return cached

        compiler_tag = (
            f" [{self.hooks_compiler}]" if self.hooks_compiler != "wasmcc" else ""
        )
        cov_tag = " (coverage)" if coverage else ""
        logger.info(
            f"{label}: compiling {'WAT' if is_wat else 'C'}{compiler_tag}{cov_tag}"
        )

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
                    include_dirs=include_dirs,
                    coverage=coverage,
                )

            self.cache.put(
                source,
                is_wat,
                bytecode,
                coverage=coverage,
                hooks_compiler=self.hooks_compiler,
            )
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
