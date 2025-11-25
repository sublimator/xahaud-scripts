"""CMake configuration and build utilities."""

import os
from dataclasses import dataclass

from xahaud_scripts.build.ccache import (
    CCACHE_CONFIG_PATH,
    get_ccache_debug_logfile,
    get_ccache_env,
    get_ccache_launcher,
    setup_ccache_config,
)
from xahaud_scripts.utils.logging import make_logger
from xahaud_scripts.utils.shell_utils import (
    change_directory,
    check_tool_exists,
    get_logical_cpu_count,
    run_command,
)

logger = make_logger(__name__)


def format_command(cmd: list[str], indent: str = "  ") -> str:
    """Format a command list for nice display, one arg per line."""
    if not cmd:
        return ""
    lines = [cmd[0] + " \\"]
    for arg in cmd[1:-1]:
        lines.append(f"{indent}{arg} \\")
    if len(cmd) > 1:
        lines.append(f"{indent}{cmd[-1]}")
    return "\n".join(lines)


@dataclass
class CMakeOptions:
    """Options for CMake configuration."""

    build_type: str = "Debug"
    coverage: bool = False
    verbose: bool = False
    ccache: bool = False
    ccache_basedir: str | None = None  # Absolute path for cache sharing
    ccache_sloppy: bool = False  # Ignore locale, __DATE__, __TIME__
    ccache_debug: bool = False  # Enable ccache debug logging
    log_line_numbers: bool = True
    use_conan: bool = False
    conan_v2: bool = False
    unity: bool = False  # OFF for faster incremental builds during development

    @property
    def toolchain_file(self) -> str | None:
        """Get the toolchain file path if using conan."""
        if self.use_conan:
            # Both Conan 1.x and 2.x place the toolchain in generators subfolder
            return "generators/conan_toolchain.cmake"
        return None


def cmake_configure(
    build_dir: str,
    options: CMakeOptions,
    dry_run: bool = False,
) -> bool:
    """Configure the CMake build.

    Args:
        build_dir: Path to the build directory
        options: CMake configuration options
        dry_run: If True, print the command without executing

    Returns:
        True if successful, False otherwise
    """
    logger.info("Configuring CMake build...")

    with change_directory(build_dir):
        # Get environment variables
        llvm_dir = os.environ.get("LLVM_DIR", "")
        llvm_library_dir = os.environ.get("LLVM_LIBRARY_DIR", "")

        # Build cmake command
        cmake_cmd = ["cmake"]

        # Add generator if ninja is available
        if check_tool_exists("ninja"):
            cmake_cmd.extend(["-G", "Ninja"])

        # Set the build type
        cmake_cmd.append(f"-DCMAKE_BUILD_TYPE={options.build_type}")

        # Common flags
        if options.verbose:
            cmake_cmd.append("-DCMAKE_VERBOSE_MAKEFILE=ON")

        cmake_cmd.append("-Dassert=TRUE")

        if options.log_line_numbers:
            cmake_cmd.append("-DBEAST_ENHANCED_LOGGING=ON")

        # Enable compile_commands.json generation
        cmake_cmd.append("-DCMAKE_EXPORT_COMPILE_COMMANDS=ON")

        # Handle ccache - need special handling when combined with conan
        use_ccache = False
        ccache_debug_logfile = None
        if options.ccache:
            if check_tool_exists("ccache"):
                use_ccache = True
                setup_ccache_config(dry_run=dry_run)
                logger.info(f"Using ccache with config from {CCACHE_CONFIG_PATH}")
                if options.ccache_debug:
                    ccache_debug_logfile = get_ccache_debug_logfile()
                    logger.info(f"ccache debug logging to: {ccache_debug_logfile}")
            else:
                logger.warning(
                    "ccache requested but not found in PATH, continuing without it"
                )

        # Add coverage settings if requested
        if options.coverage:
            logger.info("Configuring build with coverage instrumentation")
            cmake_cmd.extend(
                [
                    "-Dcoverage=ON",
                    "-Dcoverage_core_only=ON",
                    "-DCMAKE_CXX_FLAGS=-O0 -fcoverage-mapping -fprofile-instr-generate",
                    "-DCMAKE_C_FLAGS=-O0 -fcoverage-mapping -fprofile-instr-generate",
                ]
            )
        else:
            logger.info(f"Configuring standard {options.build_type} build")

        # Add conan toolchain if using conan
        if options.use_conan:
            conan_toolchain = options.toolchain_file
            assert conan_toolchain is not None  # Always set when use_conan is True

            if use_ccache:
                # Create a wrapper toolchain that includes Conan's toolchain
                # then overlays ccache. This is needed because Conan's toolchain
                # can override CMAKE_*_COMPILER_LAUNCHER settings.
                # We use `env` to bake ccache config inline so it works across worktrees.
                wrapper_path = os.path.join(build_dir, "ccache_wrapper_toolchain.cmake")
                ccache_launcher = get_ccache_launcher(
                    basedir=options.ccache_basedir,
                    sloppy=options.ccache_sloppy,
                    debug_logfile=ccache_debug_logfile,
                )
                wrapper_content = f"""# Wrapper toolchain: includes Conan toolchain then adds ccache
# Auto-generated by run-tests

# Include Conan's generated toolchain first (sets compiler, flags, etc.)
include(${{CMAKE_CURRENT_LIST_DIR}}/{conan_toolchain})

# Overlay ccache configuration (FORCE to override any Conan settings)
# Uses env to bake in ccache config for cache sharing between worktrees
set(CMAKE_C_COMPILER_LAUNCHER {ccache_launcher} CACHE STRING "C compiler launcher" FORCE)
set(CMAKE_CXX_COMPILER_LAUNCHER {ccache_launcher} CACHE STRING "C++ compiler launcher" FORCE)
"""
                if not dry_run:
                    with open(wrapper_path, "w") as f:
                        f.write(wrapper_content)
                    logger.debug(f"Created ccache wrapper toolchain at {wrapper_path}")
                else:
                    print(f"\n[DRY RUN] Would create {wrapper_path} with content:")
                    print(wrapper_content)

                toolchain_path = "ccache_wrapper_toolchain.cmake"
            else:
                toolchain_path = conan_toolchain

            logger.debug(f"Using toolchain at {toolchain_path}")
            cmake_cmd.append(f"-DCMAKE_TOOLCHAIN_FILE={toolchain_path}")
        elif use_ccache:
            # No conan - can use ccache directly with env wrapper for config
            ccache_launcher = get_ccache_launcher(
                basedir=options.ccache_basedir,
                sloppy=options.ccache_sloppy,
                debug_logfile=ccache_debug_logfile,
            )
            cmake_cmd.extend(
                [
                    f"-DCMAKE_C_COMPILER_LAUNCHER={ccache_launcher}",
                    f"-DCMAKE_CXX_COMPILER_LAUNCHER={ccache_launcher}",
                ]
            )

        # Unity build setting (default OFF for faster incremental builds)
        cmake_cmd.append(f"-Dunity={'ON' if options.unity else 'OFF'}")

        # Always add xrpld flag to make rippled target available
        logger.debug("Setting -Dxrpld=ON to enable rippled target")
        cmake_cmd.append("-Dxrpld=ON")

        # Always add tests flag
        logger.debug("Setting -Dtests=ON to enable tests")
        cmake_cmd.append("-Dtests=ON")

        # Add LLVM settings if provided
        if llvm_dir:
            logger.debug(f"Using LLVM directory: {llvm_dir}")
            cmake_cmd.append(f"-DLLVM_DIR={llvm_dir}")

        if llvm_library_dir:
            logger.debug(f"Using LLVM library directory: {llvm_library_dir}")
            cmake_cmd.append(f"-DLLVM_LIBRARY_DIR={llvm_library_dir}")

        # Add source directory (we're in build dir, source is parent)
        cmake_cmd.append("..")

        if dry_run:
            print("\n[DRY RUN] CMake configure command:")
            print(f"  Working directory: {build_dir}")
            print(format_command(cmake_cmd, indent="    "))
            print()
            return True

        try:
            run_command(cmake_cmd)
            logger.info("CMake configuration completed successfully")
            return True
        except Exception as e:
            logger.error(f"CMake configuration failed: {e}")
            return False


def cmake_build(
    build_dir: str,
    target: str = "rippled",
    verbose: bool = False,
    parallel: int | None = None,
    dry_run: bool = False,
    ccache: bool = False,
    ccache_basedir: str | None = None,
    ccache_sloppy: bool = False,
) -> bool:
    """Build the specified target.

    Args:
        build_dir: Path to the build directory
        target: Build target (e.g., rippled, xrpld)
        verbose: Enable verbose build output
        parallel: Number of parallel jobs (defaults to CPU count)
        dry_run: If True, print the command without executing
        ccache: If True, use ccache with custom config
        ccache_basedir: Base directory for ccache path normalization (enables cache sharing)
        ccache_sloppy: If True, ignore __FILE__, __DATE__, __TIME__ differences

    Returns:
        True if successful, False otherwise
    """
    logger.info(f"Building {target}...")

    if parallel is None:
        parallel = get_logical_cpu_count()

    with change_directory(build_dir):
        build_cmd = ["cmake", "--build", "."]

        # Add target
        build_cmd.extend(["--target", target])

        # Add parallel flag
        build_cmd.extend(["--parallel", str(parallel)])

        if verbose:
            logger.debug(
                "Build will use verbose output if configured with CMAKE_VERBOSE_MAKEFILE=ON"
            )

        # Set up environment for ccache if enabled
        env = None
        if ccache and check_tool_exists("ccache"):
            env = get_ccache_env(base_dir=ccache_basedir, sloppy=ccache_sloppy)
            logger.debug(f"Using CCACHE_CONFIGPATH={env['CCACHE_CONFIGPATH']}")
            if ccache_basedir:
                logger.debug(f"Using CCACHE_BASEDIR={env['CCACHE_BASEDIR']}")
            if ccache_sloppy:
                logger.debug(f"Using CCACHE_SLOPPINESS={env['CCACHE_SLOPPINESS']}")

        if dry_run:
            print("\n[DRY RUN] CMake build command:")
            print(f"  Working directory: {build_dir}")
            if env:
                print(f"  CCACHE_CONFIGPATH={env['CCACHE_CONFIGPATH']}")
                if "CCACHE_BASEDIR" in env:
                    print(f"  CCACHE_BASEDIR={env['CCACHE_BASEDIR']}")
                if "CCACHE_SLOPPINESS" in env:
                    print(f"  CCACHE_SLOPPINESS={env['CCACHE_SLOPPINESS']}")
            print(f"  {' '.join(build_cmd)}")
            print()
            return True

        try:
            run_command(build_cmd, env=env)
            logger.info("Build completed successfully")

            # Verify the build output exists
            rippled_path = os.path.join(build_dir, "rippled")
            if not os.path.exists(rippled_path):
                rippled_path = os.path.join(build_dir, "rippled.exe")
                if not os.path.exists(rippled_path):
                    logger.error("Could not find rippled executable after build")
                    return False

            logger.debug(f"Verified rippled executable exists at {rippled_path}")
            return True
        except Exception as e:
            logger.error(f"Build failed: {e}")
            return False
