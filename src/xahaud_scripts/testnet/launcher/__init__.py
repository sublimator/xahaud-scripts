"""Launcher module for testnet nodes.

This module provides different launcher implementations for starting
xahaud nodes in terminal windows or other environments.

Available launchers:
    - ITermLauncher: Launch in iTerm2 on macOS

Usage:
    >>> from xahaud_scripts.testnet.launcher import get_launcher
    >>> launcher = get_launcher()
    >>> launcher.launch(node, config)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from xahaud_scripts.testnet.launcher.iterm import ITermLauncher
from xahaud_scripts.testnet.launcher.tmux import TmuxLauncher
from xahaud_scripts.utils.logging import make_logger

if TYPE_CHECKING:
    from xahaud_scripts.testnet.protocols import Launcher

logger = make_logger(__name__)

__all__ = [
    "ITermLauncher",
    "TmuxLauncher",
    "get_launcher",
]


LAUNCHER_TYPES: dict[str, Callable[[], Launcher]] = {
    "tmux": TmuxLauncher,
    "iterm": ITermLauncher,
}


def get_launcher(launcher_type: str | None = None) -> Launcher:
    """Get a launcher for the current platform.

    Args:
        launcher_type: Optional launcher type ("tmux", "iterm").
                      If None, tries tmux first, then iterm.

    Returns:
        A launcher instance appropriate for this system

    Raises:
        RuntimeError: If no suitable launcher is available
    """
    if launcher_type:
        if launcher_type not in LAUNCHER_TYPES:
            raise RuntimeError(
                f"Unknown launcher type: {launcher_type}. "
                f"Available: {', '.join(LAUNCHER_TYPES.keys())}"
            )
        launcher = LAUNCHER_TYPES[launcher_type]()
        if not launcher.is_available():
            raise RuntimeError(f"Launcher '{launcher_type}' is not available")
        logger.debug(f"Using launcher: {launcher.__class__.__name__}")
        return launcher

    # Try launchers in order of preference (tmux first for single-window experience)
    launchers: list[Launcher] = [
        TmuxLauncher(),
        ITermLauncher(),
    ]

    for launcher in launchers:
        if launcher.is_available():
            logger.debug(f"Using launcher: {launcher.__class__.__name__}")
            return launcher

    raise RuntimeError(
        "No suitable launcher found. "
        "tmux or iTerm2 is required on macOS. "
        "Run with --help for more options."
    )
