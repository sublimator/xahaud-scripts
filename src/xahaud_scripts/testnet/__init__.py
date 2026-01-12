"""Testnet management for xahaud.

This package provides tools for creating and managing local xahaud test networks
for development and testing purposes.

Usage (CLI):
    testnet generate [--node-count N]
    testnet run [--amendment-id ID] [--quorum N]
    testnet check [--amendment-id ID]
    testnet teardown
    testnet clean

Usage (Python):
    >>> from xahaud_scripts.testnet import create_testnet, ConfigBuilder
    >>>
    >>> # Create with defaults
    >>> network = create_testnet()
    >>> network.generate()
    >>> network.run(launch_config)
    >>>
    >>> # Or use the builder
    >>> network_config, launch_config = (
    ...     ConfigBuilder()
    ...     .xahaud_root()  # Auto-detect via git
    ...     .node_count(3)
    ...     .build()
    ... )
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from xahaud_scripts.testnet.config import (
    DEFAULT_BASE_PORT_PEER,
    DEFAULT_BASE_PORT_RPC,
    DEFAULT_BASE_PORT_WS,
    DEFAULT_NETWORK_ID,
    DEFAULT_NODE_COUNT,
    ConfigBuilder,
    LaunchConfig,
    NetworkConfig,
    NodeInfo,
)
from xahaud_scripts.testnet.generator import (
    ValidatorKeysGenerator,
    generate_all_configs,
    generate_node_config,
    generate_validators_file,
)
from xahaud_scripts.testnet.launcher import (
    ITermLauncher,
    get_launcher,
)
from xahaud_scripts.testnet.network import TestNetwork
from xahaud_scripts.testnet.process import UnixProcessManager
from xahaud_scripts.testnet.protocols import (
    KeyGenerator,
    Launcher,
    ProcessManager,
    RPCClient,
)
from xahaud_scripts.testnet.rpc import RequestsRPCClient
from xahaud_scripts.testnet.websocket import WebSocketClient

if TYPE_CHECKING:
    pass

__all__ = [
    # Main classes
    "TestNetwork",
    "ConfigBuilder",
    # Config dataclasses
    "NetworkConfig",
    "LaunchConfig",
    "NodeInfo",
    # Constants
    "DEFAULT_BASE_PORT_PEER",
    "DEFAULT_BASE_PORT_RPC",
    "DEFAULT_BASE_PORT_WS",
    "DEFAULT_NETWORK_ID",
    "DEFAULT_NODE_COUNT",
    # Protocols
    "Launcher",
    "RPCClient",
    "ProcessManager",
    "KeyGenerator",
    # Implementations
    "ITermLauncher",
    "RequestsRPCClient",
    "WebSocketClient",
    "UnixProcessManager",
    "ValidatorKeysGenerator",
    # Generator functions
    "generate_all_configs",
    "generate_node_config",
    "generate_validators_file",
    # Factory functions
    "get_launcher",
    "create_testnet",
]


def create_testnet(
    base_dir: Path | None = None,
    xahaud_root: Path | None = None,
    launcher: Launcher | None = None,
    rpc_client: RPCClient | None = None,
    process_manager: ProcessManager | None = None,
    network_config: NetworkConfig | None = None,
) -> TestNetwork:
    """Factory function to create a TestNetwork with sensible defaults.

    All dependencies can be overridden for testing or custom configurations.

    Args:
        base_dir: Directory for generated configs (default: $xahaud_root/testnet)
        xahaud_root: Path to xahaud repository (default: auto-detect via git)
        launcher: Launcher implementation (default: auto-detect best available)
        rpc_client: RPC client (default: RequestsRPCClient)
        process_manager: Process manager (default: UnixProcessManager)
        network_config: Network configuration (default: NetworkConfig())

    Returns:
        Configured TestNetwork instance

    Example:
        >>> network = create_testnet()
        >>> network.generate()

        >>> # With custom config
        >>> network = create_testnet(
        ...     network_config=NetworkConfig(node_count=3),
        ... )
    """
    # Auto-detect xahaud_root via git if not provided
    if xahaud_root is None:
        try:
            xahaud_root = Path(
                subprocess.check_output(
                    ["git", "rev-parse", "--show-toplevel"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                "Could not determine xahaud root. "
                "Please specify xahaud_root or run from within the repository."
            ) from e

    # Default base_dir
    if base_dir is None:
        base_dir = xahaud_root / "testnet"

    # Default network config
    if network_config is None:
        network_config = NetworkConfig()

    # Default launcher
    if launcher is None:
        launcher = get_launcher()

    # Default RPC client
    if rpc_client is None:
        rpc_client = RequestsRPCClient(network_config.base_port_rpc)

    # Default process manager
    if process_manager is None:
        process_manager = UnixProcessManager()

    return TestNetwork(
        base_dir=base_dir,
        network_config=network_config,
        launcher=launcher,
        rpc_client=rpc_client,
        process_manager=process_manager,
    )
