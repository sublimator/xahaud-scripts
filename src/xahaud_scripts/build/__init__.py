"""Build utilities for xahaud."""

from xahaud_scripts.build.ccache import (
    CCACHE_CONFIG_PATH,
    ccache_show_config,
    ccache_show_stats,
    ccache_zero_stats,
    get_ccache_debug_logfile,
    get_ccache_env,
    get_ccache_launcher,
    is_ccache_available,
    setup_ccache_config,
)
from xahaud_scripts.build.cmake import (
    CMakeOptions,
    cmake_build,
    cmake_configure,
)
from xahaud_scripts.build.conan import conan_install
from xahaud_scripts.build.config import (
    BuildConfig,
    check_config_mismatch,
    detect_previous_build_config,
    generate_coverage_prefix,
)

__all__ = [
    "detect_previous_build_config",
    "generate_coverage_prefix",
    "check_config_mismatch",
    "BuildConfig",
    "conan_install",
    "cmake_configure",
    "cmake_build",
    "CMakeOptions",
    "ccache_zero_stats",
    "ccache_show_stats",
    "ccache_show_config",
    "setup_ccache_config",
    "get_ccache_launcher",
    "get_ccache_debug_logfile",
    "get_ccache_env",
    "is_ccache_available",
    "CCACHE_CONFIG_PATH",
]
