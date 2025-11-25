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

from typing import TYPE_CHECKING

from xahaud_scripts.testnet.launcher.iterm import ITermLauncher
from xahaud_scripts.utils.logging import make_logger

if TYPE_CHECKING:
    from xahaud_scripts.testnet.protocols import Launcher

logger = make_logger(__name__)

__all__ = [
    "ITermLauncher",
    "get_launcher",
]


def get_launcher() -> Launcher:
    """Get the best available launcher for the current platform.

    Returns:
        A launcher instance appropriate for this system

    Raises:
        RuntimeError: If no suitable launcher is available
    """
    # Try launchers in order of preference
    launchers: list[Launcher] = [
        ITermLauncher(),
        # Future: TerminalLauncher(), TmuxLauncher(), etc.
    ]

    for launcher in launchers:
        if launcher.is_available():
            logger.debug(f"Using launcher: {launcher.__class__.__name__}")
            return launcher

    raise RuntimeError(
        "No suitable launcher found. "
        "iTerm2 is required on macOS. "
        "Run with --help for more options."
    )
