"""iTerm2 launcher for xahaud nodes on macOS.

This module provides a launcher that opens each node in a new
iTerm2 window with appropriate environment variables and commands.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

from xahaud_scripts.utils.logging import make_logger

if TYPE_CHECKING:
    from xahaud_scripts.testnet.config import LaunchConfig, NodeInfo

logger = make_logger(__name__)


class ITermLauncher:
    """Launch xahaud nodes in iTerm2 windows on macOS.

    Uses AppleScript to control iTerm2, creating a new window for each
    node with the appropriate environment variables and startup command.
    """

    def is_available(self) -> bool:
        """Check if iTerm launcher is available on this system.

        Returns:
            True if running on macOS with osascript available
        """
        return sys.platform == "darwin" and shutil.which("osascript") is not None

    def launch(self, node: NodeInfo, config: LaunchConfig) -> bool:
        """Launch a node in a new iTerm window.

        Args:
            node: Node configuration and identity information
            config: Launch configuration including paths and flags

        Returns:
            True if launch succeeded, False otherwise
        """
        env_vars = self._build_env_vars(node, config)
        startup_flags = self._build_startup_flags(node, config)

        role = "[EXPLOIT]" if node.is_injector else "[CLEAN]"
        window_title = f"XahaudTest_Node{node.id}"

        # Build the command to run
        cmd = f"{config.rippled_path} --conf {node.config_path} {startup_flags}"

        applescript = f'''
tell application "iTerm"
    create window with default profile
    tell current session of current window
        set name to "{window_title} {role}"
        write text "cd {node.node_dir}"
        write text "# {window_title} - PID will be saved for teardown"
        write text "{env_vars} && {cmd}"
    end tell
end tell
'''

        logger.info(f"Launching node {node.id} {role} in iTerm")
        logger.debug(f"  Working dir: {node.node_dir}")
        logger.debug(f"  Config: {node.config_path}")
        logger.debug(f"  Env vars: {env_vars}")
        logger.debug(f"  Startup flags: {startup_flags}")

        try:
            subprocess.run(
                ["osascript", "-e", applescript],
                check=True,
                capture_output=True,
            )
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to launch node {node.id}: {e}")
            if e.stderr:
                logger.error(f"  AppleScript error: {e.stderr.decode()}")
            return False

    def finalize(self) -> None:
        """No-op for iTerm launcher - each node already has its own window."""
        pass

    def _build_env_vars(self, node: NodeInfo, config: LaunchConfig) -> str:
        """Build environment variable exports for the node.

        Args:
            node: Node configuration
            config: Launch configuration

        Returns:
            Shell command string setting environment variables
        """
        parts = []

        # Log date format for easier identification
        parts.append(f"export LOG_DATE_FORMAT='N{node.id} %T %Z'")
        parts.append("export LOG_DATE_LOCAL=1")
        parts.append("export NO_COLOR=1")

        # Amendment ID for injection
        amendment_id = (
            config.amendment_id
            or "56B241D7A43D40354D02A9DC4C8DF5C7A1F930D92A9035C4E12291B3CA3E1C2B"
        )
        parts.append(f"export AMENDMENT_ID={amendment_id}")

        # Injection type
        parts.append(f"export INJECT_TYPE={config.inject_type}")

        # Optional flood setting
        if config.flood is not None:
            parts.append(f"export FLOOD={config.flood}")

        # Optional n_txns setting
        if config.n_txns is not None:
            parts.append(f"export N_TXNS={config.n_txns}")

        # Disable local pseudo-transaction checking if requested
        if config.no_check_local:
            parts.append("export CHECK_LOCAL_PSEUDO=0")

        # Disable pseudo-transaction validity checking if requested
        if config.no_check_pseudo_valid:
            parts.append("export CHECK_PSEUDO_VALIDITY=0")

        # Extra environment variables from CLI (global)
        for key, value in config.extra_env.items():
            parts.append(f"export {key}={value}")

        # Node-specific environment variables (override global)
        if node.id in config.node_env:
            for key, value in config.node_env[node.id].items():
                parts.append(f"export {key}={value}")

        return " && ".join(parts)

    def _build_startup_flags(self, node: NodeInfo, config: LaunchConfig) -> str:
        """Build startup flags for the rippled command.

        Args:
            node: Node configuration
            config: Launch configuration

        Returns:
            Space-separated string of startup flags
        """
        parts = []

        # Genesis ledger file
        parts.append(f"--ledgerfile {config.genesis_file}")

        # Quorum setting
        if config.quorum is not None:
            parts.append(f"--quorum {config.quorum}")

        # Slave-net mode: add --net flag to non-master nodes
        if config.slave_net and not node.is_injector:
            parts.append("--net")

        # Extra arguments
        if config.extra_args:
            parts.extend(config.extra_args)

        return " ".join(parts)
