"""CLI commands for testnet management.

This module provides Click commands for managing local xahaud test networks.

Usage:
    testnet generate [--node-count N]
    testnet run [--amendment-id ID] [--quorum N] [--no-delays]
    testnet check [--amendment-id ID]
    testnet server-info n0
    testnet ping n0
    testnet inject n0,n1,n2 --amendment-id ID --ledger-seq N
    testnet logs PARTITION SEVERITY [NODE]
    testnet topology
    testnet ports
    testnet dump-conf
    testnet teardown
    testnet clean
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import click

from xahaud_scripts.testnet.config import (
    LaunchConfig,
    NetworkConfig,
    feature_name_to_hash,
    get_bundled_genesis_file,
    prepare_genesis_file,
)
from xahaud_scripts.testnet.launcher import get_launcher
from xahaud_scripts.testnet.monitor import (
    display_amendment_status,
    display_port_status,
    display_topology,
    dump_configs,
)
from xahaud_scripts.testnet.network import TestNetwork
from xahaud_scripts.testnet.process import UnixProcessManager
from xahaud_scripts.testnet.rpc import RequestsRPCClient
from xahaud_scripts.testnet.websocket import WebSocketClient
from xahaud_scripts.utils.logging import make_logger, setup_logging

logger = make_logger(__name__)


def _get_xahaud_root() -> Path:
    """Get xahaud root via git rev-parse --show-toplevel."""
    try:
        result = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return Path(result.strip())
    except subprocess.CalledProcessError as e:
        raise click.ClickException(
            "Could not determine xahaud root. "
            "Please specify --xahaud-root or run from within the repository."
        ) from e


def _parse_node_spec(spec: str) -> int:
    """Parse 'n0', 'n1', etc. to node ID."""
    if spec and spec.startswith("n") and spec[1:].isdigit():
        return int(spec[1:])
    raise click.BadParameter(f"Invalid node spec: {spec}. Use n0, n1, etc.")


def _parse_node_list(specs: str) -> list[int]:
    """Parse 'n0,n1,n2' to list of node IDs."""
    return [_parse_node_spec(s.strip()) for s in specs.split(",")]


def _create_network(
    ctx: click.Context,
    node_count: int | None = None,
    launcher_type: str | None = None,
) -> TestNetwork:
    """Create a TestNetwork instance from context."""
    xahaud_root = ctx.obj.get("xahaud_root") or _get_xahaud_root()
    base_dir = ctx.obj.get("base_dir") or (xahaud_root / "testnet")

    # Use provided node_count, or from context, or default to 5
    if node_count is None:
        node_count = ctx.obj.get("node_count", 5)

    network_config = NetworkConfig(node_count=node_count)

    return TestNetwork(
        base_dir=base_dir,
        network_config=network_config,
        launcher=get_launcher(launcher_type),
        rpc_client=RequestsRPCClient(network_config.base_port_rpc),
        ws_client=WebSocketClient(network_config.base_port_ws),
        process_manager=UnixProcessManager(),
    )


@click.group()
@click.option(
    "--xahaud-root",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to xahaud repository (default: inferred via git)",
)
@click.option(
    "--rippled-path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to rippled binary (default: $xahaud-root/build/rippled)",
)
@click.option(
    "--base-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory for generated configs (default: $xahaud-root/testnet)",
)
@click.option(
    "--log-level",
    type=click.Choice(["debug", "info", "warning", "error"], case_sensitive=False),
    default="info",
    help="Log level",
)
@click.pass_context
def testnet(
    ctx: click.Context,
    xahaud_root: Path | None,
    rippled_path: Path | None,
    base_dir: Path | None,
    log_level: str,
) -> None:
    """Manage a local xahaud test network.

    This tool helps you create and manage a local test network of xahaud
    validators for development and testing purposes.

    Examples:

        # Generate configs for a 5-node network
        testnet generate

        # Generate configs for a 3-node network
        testnet generate --node-count 3

        # Launch the network and monitor
        testnet run

        # Launch with custom amendment ID
        testnet run --amendment-id ABC123...

        # Check amendment status
        testnet check --amendment-id ABC123...

        # Kill all running nodes
        testnet teardown
    """
    setup_logging(log_level.upper(), logger)

    ctx.ensure_object(dict)
    ctx.obj["xahaud_root"] = xahaud_root
    ctx.obj["rippled_path"] = rippled_path
    ctx.obj["base_dir"] = base_dir


@testnet.command()
@click.option(
    "--node-count",
    "-n",
    type=click.IntRange(1, 10),
    default=5,
    help="Number of nodes (1-10)",
)
@click.pass_context
def generate(ctx: click.Context, node_count: int) -> None:
    """Generate configs for all nodes.

    Creates validator keys and configuration files for each node
    in the test network.

    Examples:
        testnet generate
        testnet generate --node-count 3
        testnet generate -n 3
    """
    network = _create_network(ctx, node_count=node_count)
    network.generate()

    click.echo(f"\nGenerated configs for {node_count} nodes")
    click.echo(f"  Base directory: {network.base_dir}")
    click.echo("\nValidator public keys:")
    for node in network.nodes:
        click.echo(f"  Node {node.id} [{node.role}]: {node.public_key}")


@testnet.command()
@click.option(
    "--node-count",
    "-n",
    type=click.IntRange(1, 10),
    default=5,
    help="Number of nodes (1-10)",
)
@click.option("--amendment-id", help="Amendment ID for injection")
@click.option("--quorum", type=int, help="Quorum value for consensus")
@click.option("--flood", type=int, help="Inject every N ledgers (0 for once only)")
@click.option("--n-txns", type=int, help="Number of transactions per injection")
@click.option(
    "--inject-type",
    type=click.Choice(["rcl", "txq"]),
    default="rcl",
    help="Injection type",
)
@click.option(
    "--no-delays/--delays",
    default=True,
    help="Skip startup delays (default: no delays)",
)
@click.option(
    "--slave-delay",
    type=int,
    default=2,
    help="Delay between slave nodes (seconds)",
)
@click.option("--slave-net/--no-slave-net", default=False, help="Add --net to slaves")
@click.option(
    "--no-check-local/--check-local",
    default=False,
    help="Disable CHECK_LOCAL_PSEUDO",
)
@click.option(
    "--no-check-pseudo-valid/--check-pseudo-valid",
    default=False,
    help="Disable CHECK_PSEUDO_VALIDITY",
)
@click.option(
    "--genesis-file",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to genesis ledger file (default: bundled genesis.json)",
)
@click.option(
    "--feature",
    "features",
    multiple=True,
    help="Amendment hash to enable. Prefix with '-' to disable. Can be repeated.",
)
@click.option(
    "--env",
    "env_vars",
    multiple=True,
    help="Environment variable (NAME or NAME=VALUE). No value means =1.",
)
@click.option(
    "--launcher",
    type=click.Choice(["iterm-panes", "iterm", "tmux"]),
    default=None,
    help="Launcher type (default: iterm-panes)",
)
@click.argument("extra_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def run(
    ctx: click.Context,
    node_count: int,
    amendment_id: str | None,
    quorum: int | None,
    flood: int | None,
    n_txns: int | None,
    inject_type: str,
    no_delays: bool,
    slave_delay: int,
    slave_net: bool,
    no_check_local: bool,
    no_check_pseudo_valid: bool,
    genesis_file: Path | None,
    features: tuple[str, ...],
    env_vars: tuple[str, ...],
    launcher: str | None,
    extra_args: tuple[str, ...],
) -> None:
    """Launch nodes in terminal windows and start monitoring.

    This command launches each node in a separate iTerm window and then
    starts a monitoring loop that displays network status.

    Examples:

        # Basic launch
        testnet run

        # Launch with custom amendment ID
        testnet run --amendment-id 5B8E5D8F3D8687D3CE567FB5BDAED152...

        # Launch with custom quorum
        testnet run --quorum 4

        # Launch without startup delays (faster but may be unstable)
        testnet run --no-delays

        # Launch with specific launcher
        testnet run --launcher tmux
    """
    network = _create_network(ctx, node_count=node_count, launcher_type=launcher)

    xahaud_root = ctx.obj.get("xahaud_root") or _get_xahaud_root()
    rippled_path = ctx.obj.get("rippled_path") or (xahaud_root / "build" / "rippled")

    # Prepare genesis file with feature modifications
    base_genesis = genesis_file or get_bundled_genesis_file()
    effective_genesis = prepare_genesis_file(base_genesis, list(features))

    # Log if modifications were made
    if features:
        logger.info(f"Created modified genesis with {len(features)} feature change(s)")
        for f in features:
            action = "disabled" if f.startswith("-") else "enabled"
            spec = f.lstrip("-")
            if spec.startswith("@"):
                name = spec[1:]
                hash_value = feature_name_to_hash(name)
                logger.info(f"  {action}: @{name} -> {hash_value[:16]}...")
            else:
                logger.info(f"  {action}: {spec[:16]}...")

    # Parse environment variables
    extra_env: dict[str, str] = {}
    for env_spec in env_vars:
        if "=" in env_spec:
            key, value = env_spec.split("=", 1)
            extra_env[key] = value
        else:
            extra_env[env_spec] = "1"

    if extra_env:
        logger.info(f"Extra environment variables: {len(extra_env)}")
        for key, value in extra_env.items():
            logger.info(f"  {key}={value}")

    launch_config = LaunchConfig(
        xahaud_root=xahaud_root,
        rippled_path=rippled_path,
        genesis_file=effective_genesis,
        amendment_id=amendment_id,
        quorum=quorum,
        flood=flood,
        n_txns=n_txns,
        inject_type=inject_type,
        no_delays=no_delays,
        slave_delay=slave_delay,
        slave_net=slave_net,
        no_check_local=no_check_local,
        no_check_pseudo_valid=no_check_pseudo_valid,
        extra_args=list(extra_args),
        extra_env=extra_env,
    )

    network.run(launch_config)
    network.monitor(tracked_amendment=amendment_id)


@testnet.command()
@click.option("--amendment-id", help="Amendment ID to check")
@click.pass_context
def check(ctx: click.Context, amendment_id: str | None) -> None:
    """Check amendment status on all nodes.

    Queries each node for its amendment status and displays
    the results in a table.
    """
    network = _create_network(ctx)

    if not amendment_id:
        amendment_id = (
            "56B241D7A43D40354D02A9DC4C8DF5C7A1F930D92A9035C4E12291B3CA3E1C2B"
        )

    try:
        network._load_network_info()
    except FileNotFoundError as e:
        raise click.ClickException(str(e)) from e

    display_amendment_status(network.rpc_client, network.nodes, amendment_id)


@testnet.command("server-info")
@click.argument("node")
@click.pass_context
def server_info(ctx: click.Context, node: str) -> None:
    """Query server_info for a specific node.

    NODE should be specified as n0, n1, n2, etc.

    Example:
        testnet server-info n0
    """
    node_id = _parse_node_spec(node)
    network = _create_network(ctx)

    result = network.server_info(node_id)
    if result:
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(f"Failed to get server_info from node {node_id}")
        sys.exit(1)


@testnet.command()
@click.argument("node")
@click.pass_context
def ping(ctx: click.Context, node: str) -> None:
    """Trigger injection on a specific node.

    Sends a ping with inject=true to trigger manual injection.

    NODE should be specified as n0, n1, n2, etc.

    Example:
        testnet ping n0
    """
    node_id = _parse_node_spec(node)
    network = _create_network(ctx)

    click.echo(f"Triggering injection on node {node_id}...")
    result = network.ping(node_id, inject=True)
    if result:
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(f"Failed to ping node {node_id}")
        sys.exit(1)


@testnet.command()
@click.argument("nodes")
@click.option("--amendment-id", required=True, help="Amendment ID to inject")
@click.option("--ledger-seq", required=True, type=int, help="Ledger sequence")
@click.pass_context
def inject(
    ctx: click.Context,
    nodes: str,
    amendment_id: str,
    ledger_seq: int,
) -> None:
    """Inject EnableAmendment pseudo-tx via RPC.

    NODES should be a comma-separated list like n0,n1,n2

    Example:
        testnet inject n0,n1,n2 --amendment-id ABC123... --ledger-seq 100
    """
    node_ids = _parse_node_list(nodes)
    network = _create_network(ctx)

    for node_id in node_ids:
        click.echo(f"\nInjecting on node {node_id}...")
        result = network.inject_amendment(node_id, amendment_id, ledger_seq)
        click.echo(json.dumps(result, indent=2))


@testnet.command()
@click.argument("partition")
@click.argument("severity")
@click.argument("node", required=False)
@click.pass_context
def logs(
    ctx: click.Context,
    partition: str,
    severity: str,
    node: str | None,
) -> None:
    """Set log level for a partition on all or specific node.

    Examples:
        testnet logs Validations trace
        testnet logs PeerTMProposeSet debug n0
    """
    node_id = _parse_node_spec(node) if node else None
    network = _create_network(ctx)
    network.set_log_level(partition, severity, node_id)


@testnet.command()
@click.pass_context
def topology(ctx: click.Context) -> None:
    """Show peer connection topology."""
    network = _create_network(ctx)

    try:
        network._load_network_info()
    except FileNotFoundError as e:
        raise click.ClickException(str(e)) from e

    display_topology(network.rpc_client, network.nodes)


@testnet.command()
@click.pass_context
def ports(ctx: click.Context) -> None:
    """Check which peer and RPC ports are listening."""
    network = _create_network(ctx)

    try:
        network._load_network_info()
    except FileNotFoundError as e:
        raise click.ClickException(str(e)) from e

    display_port_status(network._process_mgr, network.nodes)


@testnet.command("dump-conf")
@click.pass_context
def dump_conf(ctx: click.Context) -> None:
    """Dump all node configurations."""
    network = _create_network(ctx)

    try:
        network._load_network_info()
    except FileNotFoundError as e:
        raise click.ClickException(str(e)) from e

    dump_configs(network.nodes)


@testnet.command()
@click.pass_context
def teardown(ctx: click.Context) -> None:
    """Kill all running test network processes."""
    network = _create_network(ctx)
    count = network.teardown()
    click.echo(f"Killed {count} processes")


@testnet.command()
@click.pass_context
def clean(ctx: click.Context) -> None:
    """Remove all generated files."""
    network = _create_network(ctx)
    network.clean()
    click.echo("Cleaned up generated files")


def main() -> None:
    """Entry point for the testnet CLI."""
    testnet()


if __name__ == "__main__":
    main()
