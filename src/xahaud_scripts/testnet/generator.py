"""Configuration and key generation for testnet nodes.

This module provides functions for:
- Generating validator keys using the validator-keys tool
- Generating xahaud.cfg configuration files
- Creating validators.txt files
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from xahaud_scripts.testnet.config import NodeInfo
from xahaud_scripts.utils.logging import make_logger

if TYPE_CHECKING:
    from xahaud_scripts.testnet.config import NetworkConfig

logger = make_logger(__name__)


class ValidatorKeysGenerator:
    """Generate validator keys using the validator-keys CLI tool."""

    def generate(self, node_id: int, output_dir: Path) -> dict[str, str]:
        """Generate validator keys for a node.

        Args:
            node_id: The node ID (0, 1, 2, etc.)
            output_dir: Directory to write key files

        Returns:
            Dict with 'public_key', 'token', and 'keyfile' keys

        Raises:
            RuntimeError: If key generation fails
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        keyfile = output_dir / "validator-keys.json"

        logger.debug(f"Generating validator keys for node {node_id}")

        # Generate keys
        try:
            subprocess.run(
                ["validator-keys", "create_keys", "--keyfile", str(keyfile)],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"Failed to create validator keys: {e.stderr.decode()}"
            ) from e
        except FileNotFoundError as e:
            raise RuntimeError(
                "validator-keys tool not found. "
                "Please install it and ensure it's in your PATH."
            ) from e

        # Generate token
        try:
            result = subprocess.run(
                ["validator-keys", "create_token", "--keyfile", str(keyfile)],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to create validator token: {e.stderr}") from e

        # Parse token from output
        token = self._parse_token(result.stdout)
        if not token:
            raise RuntimeError(
                f"Could not extract token from validator-keys output:\n{result.stdout}"
            )

        # Read public key from keyfile
        with open(keyfile) as f:
            keys = json.load(f)

        public_key = keys.get("public_key", "")
        if not public_key:
            raise RuntimeError("No public_key found in validator-keys.json")

        logger.debug(f"  Public key: {public_key}")

        return {
            "public_key": public_key,
            "token": token,
            "keyfile": str(keyfile),
        }

    def _parse_token(self, output: str) -> str | None:
        """Parse validator token from create_token output.

        The output format is:
            [validator_token]
            <base64 encoded token lines>

        Args:
            output: Raw output from validator-keys create_token

        Returns:
            Multi-line token string, or None if not found
        """
        lines = output.strip().split("\n")
        token_lines = []
        in_token = False

        for line in lines:
            if "[validator_token]" in line:
                in_token = True
                continue
            if in_token:
                # Stop at empty line or next section
                if not line.strip() or line.startswith("["):
                    break
                token_lines.append(line)

        return "\n".join(token_lines) if token_lines else None


def generate_validators_file(
    node_dir: Path,
    all_validators: list[str],
) -> Path:
    """Generate validators.txt file for a node.

    Args:
        node_dir: Node directory
        all_validators: List of all validator public keys

    Returns:
        Path to the generated validators.txt file
    """
    validators_file = node_dir / "validators.txt"

    with open(validators_file, "w") as f:
        f.write("# Test Network Validators\n\n")
        f.write("[validators]\n")
        for val_key in all_validators:
            f.write(f"{val_key}\n")

    logger.debug(f"Generated validators.txt with {len(all_validators)} validators")
    return validators_file


def generate_node_config(
    node_id: int,
    node_dir: Path,
    validator_token: str,
    validators_file: Path,
    network_config: NetworkConfig,
    is_injector: bool = False,
) -> Path:
    """Generate xahaud.cfg for a node.

    Args:
        node_id: The node ID (0, 1, 2, etc.)
        node_dir: Node directory
        validator_token: Validator token string
        validators_file: Path to validators.txt
        network_config: Network configuration
        is_injector: True if this is the exploit injector node

    Returns:
        Path to the generated config file
    """
    config_file = node_dir / "xahaud.cfg"

    port_peer = network_config.port_peer(node_id)
    port_rpc = network_config.port_rpc(node_id)
    port_ws = network_config.port_ws(node_id)

    # Build peer connection list (all other nodes)
    ips_entries = []
    for i in range(network_config.node_count):
        if i != node_id:
            peer_port = network_config.port_peer(i)
            ips_entries.append(f"127.0.0.1 {peer_port}")

    config = f"""# Node {node_id} Configuration
# Role: {"EXPLOIT INJECTOR" if is_injector else "Clean Validator"}

# peers_max must be > minOutCount (10) to have inbound slots available.
# See: src/ripple/peerfinder/impl/Tuning.h:59 (minOutCount = 10)
# See: src/ripple/peerfinder/impl/PeerfinderConfig.cpp:91-110
#
# Slot calculation (PeerfinderConfig.cpp:91-110):
#   outPeers = max((peers_max * 15% rounded), minOutCount)
#   inPeers  = peers_max - outPeers
#
# With peers_max=10: outPeers=10, inPeers=0 -> "slots full" on inbound!
# With peers_max=21: outPeers=10, inPeers=11 -> works correctly
#
# Fixed peers ([ips_fixed]) bypass slot check on OUTBOUND side only.
# INBOUND connections still need available slots.
# See: src/ripple/peerfinder/impl/Counts.h:70-83 (can_activate)
# See: src/ripple/overlay/impl/OverlayImpl.cpp:272-284 (slots full error)
[peers_max]
100

[network_id]
{network_config.network_id}

[server]
port_rpc_admin_local
port_peer
port_ws_admin_local

[port_rpc_admin_local]
port = {port_rpc}
ip = 127.0.0.1
admin = 127.0.0.1
protocol = http

[port_peer]
port = {port_peer}
ip = 0.0.0.0
protocol = peer

[port_ws_admin_local]
port = {port_ws}
ip = 127.0.0.1
admin = 127.0.0.1
protocol = ws

[node_size]
tiny

[ledger_history]
256

[node_db]
type = nudb
path = {node_dir}/db/nudb
online_delete = 256
advisory_delete = 0

[database_path]
{node_dir}/db

[debug_logfile]
{node_dir}/debug.log

[sntp_servers]
time.windows.com
time.apple.com

[validators_file]
{validators_file}

[validator_token]
{validator_token}

[ssl_verify]
0

[peer_private]
0

# Fixed persistent connections
[ips_fixed]
{chr(10).join(ips_entries)}

[rpc_startup]
{{ "command": "log_level", "severity": "warn" }}
{{ "command": "log_level", "severity": "trace", "partition": "Validations" }}
{{ "command": "log_level", "severity": "trace", "partition": "LedgerConsensus" }}
{{ "command": "log_level", "severity": "debug", "partition": "Overlay" }}
{{ "command": "log_level", "severity": "debug", "partition": "Peer" }}
{{ "command": "log_level", "severity": "debug", "partition": "PeerFinder" }}
{{ "command": "log_level", "severity": "trace", "partition": "PeerTMProposeSet" }}
{{ "command": "log_level", "severity": "trace", "partition": "ValidatorListDebug" }}
{{ "command": "log_level", "severity": "trace", "partition": "ManifestDebug" }}
"""

    with open(config_file, "w") as f:
        f.write(config)

    logger.debug(f"Generated xahaud.cfg for node {node_id}")
    return config_file


def generate_all_configs(
    base_dir: Path,
    network_config: NetworkConfig,
    key_generator: ValidatorKeysGenerator | None = None,
) -> list[NodeInfo]:
    """Generate configurations for all nodes.

    Args:
        base_dir: Base directory for node configs
        network_config: Network configuration
        key_generator: Optional key generator (defaults to ValidatorKeysGenerator)

    Returns:
        List of NodeInfo for all configured nodes
    """
    if key_generator is None:
        key_generator = ValidatorKeysGenerator()

    logger.info(f"Generating configs for {network_config.node_count} nodes")

    # Phase 1: Generate validator keys for all nodes
    validator_infos: list[dict[str, str]] = []
    for node_id in range(network_config.node_count):
        logger.info(f"  Generating keys for node {node_id}")
        node_dir = base_dir / f"n{node_id}"
        info = key_generator.generate(node_id, node_dir)
        validator_infos.append(info)

    # Get all public keys
    all_validators = [info["public_key"] for info in validator_infos]

    # Phase 2: Generate configs for all nodes
    nodes: list[NodeInfo] = []
    for node_id, validator_info in enumerate(validator_infos):
        logger.info(f"  Generating config for node {node_id}")
        node_dir = base_dir / f"n{node_id}"
        is_injector = node_id == 0

        # Generate validators.txt
        validators_file = generate_validators_file(node_dir, all_validators)

        # Generate xahaud.cfg
        config_path = generate_node_config(
            node_id=node_id,
            node_dir=node_dir,
            validator_token=validator_info["token"],
            validators_file=validators_file,
            network_config=network_config,
            is_injector=is_injector,
        )

        # Create NodeInfo
        node_info = NodeInfo(
            id=node_id,
            public_key=validator_info["public_key"],
            token=validator_info["token"],
            config_path=config_path,
            port_peer=network_config.port_peer(node_id),
            port_rpc=network_config.port_rpc(node_id),
            port_ws=network_config.port_ws(node_id),
            is_injector=is_injector,
        )
        nodes.append(node_info)

    logger.info(f"Generated configs for {len(nodes)} nodes")
    return nodes
