"""Conan package manager integration."""

import json
import os
import subprocess
from pathlib import Path

from xahaud_scripts.utils.logging import make_logger
from xahaud_scripts.utils.shell_utils import (
    change_directory,
    check_tool_exists,
    run_command,
)

logger = make_logger(__name__)


def check_conan_available() -> bool:
    """Check if conan is available in PATH.

    Returns:
        True if conan is available, False otherwise
    """
    if not check_tool_exists("conan"):
        logger.error("Conan is required but not found in PATH")
        return False
    return True


def find_conan_toolchain(build_dir: str) -> Path | None:
    """Locate ``conan_toolchain.cmake`` anywhere under ``build_dir``.

    Conan's exact write location depends on the ``cmake_layout`` settings
    in conanfile.py and the ``--output-folder`` we pass — common layouts:

      * ``<build_dir>/generators/conan_toolchain.cmake`` (legacy: no
        ``--output-folder``, build_dir matched conan's default ``build/``)
      * ``<build_dir>/build/generators/conan_toolchain.cmake``
        (``--output-folder .`` from build_dir, when the project sets
        ``self.folders.generators = 'build/generators'``)
      * ``<build_dir>/build/<Config>/generators/conan_toolchain.cmake``
        (multi-config layouts)

    Rather than hard-code one path, find whichever the current install
    actually produced. Prefers the shallowest match.
    """
    bp = Path(build_dir)
    if not bp.is_dir():
        return None
    matches = sorted(bp.rglob("conan_toolchain.cmake"), key=lambda p: len(p.parts))
    return matches[0] if matches else None


def conan_toolchain_present(build_dir: str) -> bool:
    """Return True if a conan-generated toolchain exists somewhere under build_dir."""
    return find_conan_toolchain(build_dir) is not None


def _pick_date_tz_option(graph: dict) -> list[str]:
    """Return the ``-o`` override that forces the ``date`` dep onto the OS tz db.

    conan-center renamed date's timezone-source option across recipe revisions
    (``use_system_tz_db`` bool -> ``tz_db`` system/download) and flipped the
    default to ``download``. A download-mode build aborts at runtime with
    ``Unable to get Timezone database version from ~/Downloads/tzdata/`` unless
    that directory is provisioned. We pin the OS tz db, picking whichever option
    name the resolved recipe actually exposes so this works on old and new tags
    alike. Returns ``[]`` if no ``date`` node / recognised option is found (the
    build then proceeds at the recipe default, unchanged).
    """
    nodes = (graph.get("graph") or {}).get("nodes") or {}
    for node in nodes.values():
        ref = str(node.get("ref") or "")
        if node.get("name") == "date" or ref.startswith("date/"):
            options = node.get("options") or {}
            if "tz_db" in options:
                return ["-o", "date/*:tz_db=system"]
            if "use_system_tz_db" in options:
                return ["-o", "date/*:use_system_tz_db=True"]
            break
    return []


def _date_os_tzdb_options(xahaud_root: str, build_type: str) -> list[str]:
    """Detect the ``date`` recipe's tz option and return the OS-tz-db ``-o`` override.

    Runs ``conan graph info`` (no build) to resolve the recipe and read which tz
    option it exposes. Detection must never break the build: any failure logs a
    warning and returns ``[]`` (recipe default preserved).
    """
    try:
        proc = subprocess.run(
            [
                "conan",
                "graph",
                "info",
                xahaud_root,
                "-s",
                f"build_type={build_type}",
                "--format=json",
            ],
            cwd=xahaud_root,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            logger.warning(
                "date OS-tzdb detection: `conan graph info` failed; "
                "leaving date options at the recipe default"
            )
            return []
        return _pick_date_tz_option(json.loads(proc.stdout))
    except Exception as e:  # detection is best-effort; never fail the build over it
        logger.warning(f"date OS-tzdb detection skipped: {e}")
        return []


def conan_install(
    xahaud_root: str,
    build_type: str = "Debug",
    build_dir: str | None = None,
    dry_run: bool = False,
) -> bool:
    """Install dependencies using Conan into the given build dir.

    When ``build_dir`` is provided, runs from that dir with
    ``--output-folder=.`` so the generators land at
    ``<build_dir>/generators/`` regardless of the dir name (build-debug,
    build-debug-llvm, build-debug-cov, etc.). When ``build_dir`` is None
    we fall back to the legacy behaviour (conan layout default under
    ``<xahaud_root>/build/``).

    Args:
        xahaud_root: Path to the xahaud source root.
        build_type: CMake build type (Debug or Release).
        build_dir: Build directory to scope the conan output to.
        dry_run: If True, print the command without executing.

    Returns:
        True if successful, False otherwise.
    """
    if not check_conan_available():
        return False

    logger.info("Installing dependencies with Conan...")
    logger.info(f"Using build type {build_type}")

    if build_dir is not None:
        # Scope conan's output to this exact build dir so generators land
        # under <build_dir>/generators/ — what cmake_configure expects.
        cmd = [
            "conan",
            "install",
            "--output-folder",
            ".",
            "--build=missing",
            "-s",
            f"build_type={build_type}",
            xahaud_root,
        ]
        cwd = build_dir
    else:
        cmd = [
            "conan",
            "install",
            ".",
            "--build=missing",
            "-s",
            f"build_type={build_type}",
        ]
        cwd = xahaud_root

    if dry_run:
        print("\n[DRY RUN] Conan install command:")
        print(f"  Working directory: {cwd}")
        print(f"  {' '.join(cmd)}")
        print("  (on a real run a `-o date/*:...=system` override is auto-detected")
        print("   and inserted; skipped here to keep --dry-run offline)")
        print()
        return True

    # Pin the `date` dependency onto the OS tz database so built binaries don't
    # abort at runtime looking for a downloaded ~/Downloads/tzdata (see
    # _pick_date_tz_option). This runs `conan graph info` (network), so it is kept
    # out of the dry-run path above. Inserted after "install" so it precedes any
    # positional.
    date_opts = _date_os_tzdb_options(xahaud_root, build_type)
    if date_opts:
        logger.info(f"Pinning date OS tz db: {' '.join(date_opts)}")
        cmd[2:2] = date_opts

    if build_dir is not None:
        os.makedirs(build_dir, exist_ok=True)

    with change_directory(cwd):
        try:
            run_command(cmd)
            logger.info("Conan dependencies installed successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to install dependencies with Conan: {e}")
            return False
