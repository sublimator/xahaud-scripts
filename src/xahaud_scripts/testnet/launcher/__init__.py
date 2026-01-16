"""Launcher module for testnet nodes.

This module provides different launcher implementations for starting
xahaud nodes in terminal windows or other environments.

Available launchers:
    - TmuxLauncher: Launch in tmux session (default, best experience)
    - ITermPanesLauncher: Launch in iTerm2 panes (single window)
    - ITermLauncher: Launch in separate iTerm2 windows

Usage:
    >>> from xahaud_scripts.testnet.launcher import get_launcher
    >>> launcher = get_launcher()
    >>> launcher.launch(node, config)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from xahaud_scripts.testnet.launcher.iterm import ITermLauncher
from xahaud_scripts.testnet.launcher.iterm_panes import ITermPanesLauncher
from xahaud_scripts.testnet.launcher.tmux import TmuxLauncher
from xahaud_scripts.utils.logging import make_logger

if TYPE_CHECKING:
    from xahaud_scripts.testnet.protocols import Launcher

logger = make_logger(__name__)

__all__ = [
    "ITermLauncher",
    "ITermPanesLauncher",
    "TmuxLauncher",
    "get_launcher",
]


LAUNCHER_TYPES: dict[str, Callable[[], Launcher]] = {
    "iterm-panes": ITermPanesLauncher,
    "iterm": ITermLauncher,
    "tmux": TmuxLauncher,
}


def get_launcher(launcher_type: str | None = None) -> Launcher:
    """Get a launcher for the current platform.

    Args:
        launcher_type: Optional launcher type ("iterm-panes", "iterm", "tmux").
                      If None, uses iterm-panes (single window with panes).

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

    # Try launchers in order of preference (tmux first - best experience)
    launchers: list[Launcher] = [
        TmuxLauncher(),
        ITermPanesLauncher(),
        ITermLauncher(),
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
