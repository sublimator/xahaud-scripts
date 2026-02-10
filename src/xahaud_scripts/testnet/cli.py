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
    MAX_NODE_COUNT,
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
    base_dir = ctx.obj.get("testnet_dir") or (xahaud_root / "testnet")

    # Use provided node_count, or from context, or default to 5
    if node_count is None:
        node_count = ctx.obj.get("node_count", 5)

    network_config = NetworkConfig(node_count=node_count)

    return TestNetwork(
        base_dir=base_dir,
        network_config=network_config,
        launcher=get_launcher(launcher_type),
        rpc_client=RequestsRPCClient(network_config.base_port_rpc),
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
    "--testnet-dir",
    type=click.Path(path_type=Path),
    default=None,
    envvar="X_TESTNET_DIR",
    help="Directory for generated configs (env: X_TESTNET_DIR, default: $xahaud-root/testnet)",
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
    testnet_dir: Path | None,
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
    ctx.obj["testnet_dir"] = testnet_dir


@testnet.command()
@click.option(
    "--node-count",
    "-n",
    type=click.IntRange(1, MAX_NODE_COUNT),
    default=5,
    help=f"Number of nodes (1-{MAX_NODE_COUNT})",
)
@click.option(
    "--log-level-suite",
    "log_level_suite",
    type=click.Choice(["consensus", "network", "verbose"]),
    default=None,
    help="Predefined log level suite to apply.",
)
@click.option(
    "--log-level",
    "log_levels",
    multiple=True,
    help="Log level override (Partition=severity). Applied on top of suite.",
)
@click.option(
    "--find-ports/--no-find-ports",
    "find_ports",
    default=False,
    help="Auto-find free ports if defaults are in use (default: error if ports in use).",
)
@click.pass_context
def generate(
    ctx: click.Context,
    node_count: int,
    log_level_suite: str | None,
    log_levels: tuple[str, ...],
    find_ports: bool,
) -> None:
    """Generate configs for all nodes.

    Creates validator keys and configuration files for each node
    in the test network.

    Examples:
        testnet generate
        testnet generate --node-count 3
        testnet generate --log-level-suite consensus
        testnet generate --log-level-suite consensus --log-level Shuffle=debug
        testnet generate --find-ports  # Auto-find free ports if defaults in use
    """
    from xahaud_scripts.testnet.generator import LOG_LEVEL_SUITES

    # Build log levels: start with suite, then apply overrides
    log_level_dict: dict[str, str] | None = None

    if log_level_suite:
        log_level_dict = LOG_LEVEL_SUITES[log_level_suite].copy()
        logger.info(f"Using log level suite: {log_level_suite}")

    if log_levels:
        if log_level_dict is None:
            log_level_dict = {}
        for spec in log_levels:
            if "=" not in spec:
                raise click.BadParameter(
                    f"Invalid log-level format: {spec}. Use Partition=severity"
                )
            partition, severity = spec.split("=", 1)
            log_level_dict[partition] = severity
            if severity:
                logger.info(f"Log level override: {partition}={severity}")
            else:
                logger.info(f"Log level disabled: {partition}")

    from xahaud_scripts.testnet.generator import PortConflictError

    network = _create_network(ctx, node_count=node_count)
    try:
        network.generate(log_levels=log_level_dict, find_ports=find_ports)
    except PortConflictError as e:
        raise click.ClickException(str(e)) from e

    click.echo(f"\nGenerated configs for {node_count} nodes")
    click.echo(f"  Base directory: {network.base_dir}")
    click.echo("\nValidator public keys:")
    for node in network.nodes:
        click.echo(f"  Node {node.id} [{node.role}]: {node.public_key}")


@testnet.command()
@click.option(
    "--node-count",
    "-n",
    type=click.IntRange(1, MAX_NODE_COUNT),
    default=5,
    help=f"Number of nodes (1-{MAX_NODE_COUNT})",
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
    type=float,
    default=1.0,
    help="Delay between node launches (seconds)",
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
    help="Environment variable. Use NAME=VALUE for all nodes, or n0:NAME=VALUE for node-specific.",
)
@click.option(
    "--launcher",
    type=click.Choice(["tmux", "iterm-panes", "iterm"]),
    default=None,
    help="Launcher type (default: tmux)",
)
@click.option(
    "--desktop",
    type=click.IntRange(1, 9),
    default=None,
    help="macOS desktop number to place window on (1-9)",
)
@click.option(
    "--reconnect",
    is_flag=True,
    help="Reconnect to existing network (skip launching, just monitor)",
)
@click.option(
    "--test-script",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to test script to run instead of monitoring",
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
    slave_delay: float,
    slave_net: bool,
    no_check_local: bool,
    no_check_pseudo_valid: bool,
    genesis_file: Path | None,
    features: tuple[str, ...],
    env_vars: tuple[str, ...],
    launcher: str | None,
    desktop: int | None,
    reconnect: bool,
    test_script: Path | None,
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

        # Reconnect to existing network
        testnet run --reconnect
    """
    network = _create_network(ctx, node_count=node_count, launcher_type=launcher)

    if reconnect:
        # Just reconnect to existing network and start monitoring
        logger.info("Reconnecting to existing network...")
        network.monitor(tracked_amendment=amendment_id)
        return

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

    # Parse environment variables (global and node-specific)
    # Syntax: VAR=value (global) or n0:VAR=value (node-specific)
    extra_env: dict[str, str] = {}
    node_env: dict[int, dict[str, str]] = {}
    for env_spec in env_vars:
        # Check for node-specific prefix (n0:, n1:, etc.)
        if env_spec and env_spec[0] == "n" and ":" in env_spec:
            prefix, rest = env_spec.split(":", 1)
            if prefix[1:].isdigit():
                node_id = int(prefix[1:])
                if "=" in rest:
                    key, value = rest.split("=", 1)
                else:
                    key, value = rest, "1"
                if node_id not in node_env:
                    node_env[node_id] = {}
                node_env[node_id][key] = value
                continue
        # Global env var
        if "=" in env_spec:
            key, value = env_spec.split("=", 1)
            extra_env[key] = value
        else:
            extra_env[env_spec] = "1"

    if extra_env:
        logger.info(f"Global environment variables: {len(extra_env)}")
        for key, value in extra_env.items():
            logger.info(f"  {key}={value}")
    if node_env:
        logger.info("Node-specific environment variables:")
        for node_id, env_dict in sorted(node_env.items()):
            for key, value in env_dict.items():
                logger.info(f"  n{node_id}: {key}={value}")

    # If --slave-delay was explicitly provided, enable delays
    from click.core import ParameterSource

    if ctx.get_parameter_source("slave_delay") == ParameterSource.COMMANDLINE:
        no_delays = False

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
        node_env=node_env,
        desktop=desktop,
    )

    network.run(launch_config)

    if test_script:
        # Run test script with monitor in background
        import asyncio

        from xahaud_scripts.testnet.testing import run_test_with_monitor

        ws_url = f"ws://localhost:{network._config.base_port_ws}"
        try:
            asyncio.run(
                run_test_with_monitor(
                    script_path=test_script,
                    ws_url=ws_url,
                    network_config=network._config,
                    rpc_client=network._rpc,
                    tracked_amendment=amendment_id,
                )
            )
            logger.info("Test script finished.")
        except KeyboardInterrupt:
            logger.info("Test script interrupted")
        except Exception as e:
            logger.error(f"Test script failed: {e}")

        # After test script (success, interrupted, or failed), continue monitoring
        logger.info("Continuing to monitor (Ctrl-C to stop)...")
        network.monitor(tracked_amendment=amendment_id)
    else:
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


@testnet.command("server-definitions")
@click.option(
    "--node",
    default="n0",
    help="Node to query (default: n0)",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Output file (default: stdout)",
)
@click.pass_context
def server_definitions(ctx: click.Context, node: str, output: Path | None) -> None:
    """Fetch server definitions and save to file.

    Queries a node for its server_definitions and writes the result
    to a file (or stdout). The output is the unwrapped definitions object.

    Examples:
        x-testnet server-definitions -o definitions.json
        x-testnet server-definitions --node n1 -o /tmp/defs.json
    """
    node_id = _parse_node_spec(node)
    network = _create_network(ctx)

    result = network.rpc_client.server_definitions(node_id)
    if not result:
        raise click.ClickException(f"Failed to get server_definitions from {node}")

    # Remove status field, keep the definitions
    result.pop("status", None)

    formatted = json.dumps(result, indent=2)

    if output:
        output.write_text(formatted)
        click.echo(f"Saved server definitions to {output}")
    else:
        click.echo(formatted)


@testnet.command()
@click.argument("ledger_index", default="validated")
@click.option(
    "--node",
    default="n0",
    help="Node to query (default: n0)",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Output file (default: stdout)",
)
@click.pass_context
def ledger(
    ctx: click.Context,
    ledger_index: str,
    node: str,
    output: Path | None,
) -> None:
    """Fetch a ledger with expanded transactions.

    LEDGER_INDEX can be "validated", "current", or a ledger sequence number.

    Examples:
        x-testnet ledger                    # Latest validated ledger
        x-testnet ledger 100                # Ledger 100
        x-testnet ledger validated -o l.json
    """
    node_id = _parse_node_spec(node)
    network = _create_network(ctx)

    # Parse ledger_index - could be "validated", "current", or a number
    try:
        ledger_idx: str | int = int(ledger_index)
    except ValueError:
        ledger_idx = ledger_index

    result = network.rpc_client.ledger(
        node_id,
        ledger_index=ledger_idx,
        expand=True,
        transactions=True,
    )
    if not result:
        raise click.ClickException(f"Failed to get ledger from {node}")

    formatted = json.dumps(result, indent=2)

    if output:
        output.write_text(formatted)
        click.echo(f"Saved ledger to {output}")
    else:
        click.echo(formatted)


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


@testnet.command("check-ports")
@click.option(
    "--node-count",
    "-n",
    type=click.IntRange(1, MAX_NODE_COUNT),
    default=5,
    help=f"Number of nodes (1-{MAX_NODE_COUNT})",
)
@click.pass_context
def check_ports(ctx: click.Context, node_count: int) -> None:
    """Check if required ports are free (all states including TIME_WAIT).

    This shows any ports that may block network startup, including
    connections in TIME_WAIT, CLOSE_WAIT, etc.

    Examples:
        x-testnet check-ports
        x-testnet check-ports -n 7
    """
    network = _create_network(ctx, node_count=node_count)
    ports_in_use = network.check_ports()

    if not ports_in_use:
        click.echo(f"All {node_count * 3} ports are free")
        return

    click.echo(f"Ports in use ({len(ports_in_use)} ports):\n")
    for port, connections in sorted(ports_in_use.items()):
        for conn in connections:
            state = conn["state"]
            # Color code by state
            if state == "LISTEN":
                state_str = click.style(state, fg="red", bold=True)
            elif state in ("TIME_WAIT", "CLOSE_WAIT"):
                state_str = click.style(state, fg="yellow")
            else:
                state_str = state
            click.echo(f"  {port}: {conn['process']} (PID {conn['pid']}, {state_str})")


@testnet.command("peer-addrs")
@click.option(
    "--node-count",
    "-n",
    type=click.IntRange(1, MAX_NODE_COUNT),
    default=None,
    help="Number of nodes (default: from network.json or 5)",
)
@click.option("--host", default="127.0.0.1", help="Host address (default: 127.0.0.1)")
@click.pass_context
def peer_addrs(ctx: click.Context, node_count: int | None, host: str) -> None:
    """Output peer addresses in ip:port format.

    Useful for copy/pasting into ADDITIONAL_PEERS or similar.

    Examples:
        x-testnet peer-addrs
        x-testnet peer-addrs -n 3
        x-testnet peer-addrs --host 79.110.60.121
    """
    network = _create_network(ctx, node_count=node_count)

    # Try to load from network.json, fall back to generated ports
    try:
        network._load_network_info()
        for node in network.nodes:
            click.echo(f"{host}:{node.port_peer}")
    except FileNotFoundError:
        # No network.json, use default ports
        count = node_count or 5
        base_port = network._config.base_port_peer
        for i in range(count):
            click.echo(f"{host}:{base_port + i}")


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


@testnet.command("create-config")
@click.option(
    "--network",
    type=click.Choice(["mainnet", "testnet"], case_sensitive=False),
    required=True,
    help="Target network",
)
@click.option(
    "--db-type",
    type=click.Choice(["NuDB", "RWDB"], case_sensitive=False),
    default="NuDB",
    help="Database backend (default: NuDB)",
)
@click.option(
    "--data-dir",
    default=None,
    help="Data directory for db and logs (default: <output-dir>)",
)
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=".",
    help="Where to write generated files (default: current dir)",
)
@click.option(
    "--node-size",
    type=click.Choice(
        ["tiny", "small", "medium", "large", "huge"], case_sensitive=False
    ),
    default="medium",
    help="Node size tuning (default: medium)",
)
@click.option(
    "--log-level",
    "cfg_log_level",
    type=click.Choice(
        ["trace", "debug", "info", "warning", "error", "fatal"], case_sensitive=False
    ),
    default="warning",
    help="Default log severity (default: warning)",
)
@click.option(
    "--online-delete",
    type=int,
    default=512,
    help="Ledgers to keep before deleting (default: 512)",
)
@click.option(
    "--ledger-history",
    default="256",
    help="Ledger history depth (default: 256, use 'full' for full history)",
)
@click.option(
    "--peer-port",
    type=int,
    default=None,
    help="Override peer port (default: network-specific)",
)
@click.option(
    "--rpc-port",
    type=int,
    default=5009,
    help="RPC port (default: 5009)",
)
@click.option(
    "--ws-port",
    type=int,
    default=6009,
    help="WebSocket port (default: 6009)",
)
@click.option(
    "--peers-max",
    type=int,
    default=21,
    help="Max peer connections (default: 21)",
)
@click.option(
    "--hooks-server",
    is_flag=True,
    default=False,
    help="Auto-subscribe to hooks-server (http://localhost:8080) on startup",
)
def create_config(
    network: str,
    db_type: str,
    data_dir: str | None,
    output_dir: Path,
    node_size: str,
    cfg_log_level: str,
    online_delete: int,
    ledger_history: str,
    peer_port: int | None,
    rpc_port: int,
    ws_port: int,
    peers_max: int,
    hooks_server: bool,
) -> None:
    """Generate production-ready xahaud.cfg and validators-xahau.txt.

    Creates configuration files for deploying a node to mainnet or testnet
    with sensible defaults.

    Examples:

        x-testnet create-config --network mainnet

        x-testnet create-config --network testnet --db-type RWDB

        x-testnet create-config --network mainnet --output-dir /tmp/cfg

        x-testnet create-config --network testnet --hooks-server
    """
    from xahaud_scripts.testnet.cli_handlers import create_config_handler

    effective_data_dir = data_dir or str(output_dir.resolve())

    create_config_handler(
        network=network,
        output_dir=output_dir,
        db_type=db_type,
        data_dir=effective_data_dir,
        node_size=node_size,
        log_level=cfg_log_level,
        online_delete=online_delete,
        ledger_history=ledger_history,
        peer_port=peer_port,
        rpc_port=rpc_port,
        ws_port=ws_port,
        peers_max=peers_max,
        subscribe_url="http://localhost:8080" if hooks_server else None,
    )


@testnet.command("hooks-server")
@click.option("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
@click.option("--port", type=int, default=8080, help="Listen port (default: 8080)")
@click.option(
    "--error",
    "errors",
    multiple=True,
    help="Error responses as STATUS:WEIGHT (e.g. 500:0.25). Can be repeated.",
)
def hooks_server(host: str, port: int, errors: tuple[str, ...]) -> None:
    """Run a mock webhook receiver for xahaud subscription events.

    Starts an HTTP server that receives POST requests from xahaud's
    outbound webhook system and logs them with Rich formatting.

    Examples:

        x-testnet hooks-server

        x-testnet hooks-server --port 9090

        x-testnet hooks-server --error 500:0.25 --error 400:0.1
    """
    from xahaud_scripts.testnet.cli_handlers import hooks_server_handler

    hooks_server_handler(host=host, port=port, errors=errors)


@testnet.command("logs-search")
@click.argument("pattern", default=".")
@click.option(
    "--tail",
    "-t",
    type=int,
    default=None,
    help="Only search last N lines of each log file",
)
@click.option(
    "--no-sort",
    is_flag=True,
    help="Don't sort by timestamp (faster for large outputs)",
)
@click.option(
    "--limit",
    "-l",
    type=int,
    default=None,
    help="Maximum number of results to display",
)
@click.option(
    "--time-start",
    "-s",
    default=None,
    help="Only show entries at or after this time (HH:MM:SS, -5m, -30s, -1h)",
)
@click.option(
    "--time-end",
    "-e",
    default=None,
    help="Only show entries at or before this time (HH:MM:SS, -5m, -30s, -1h)",
)
@click.option(
    "--nodes",
    "-n",
    default=None,
    help="Which nodes to search (e.g., '0-2', '1,3,5', '0-2,5,7-9')",
)
@click.pass_context
def logs_search(
    ctx: click.Context,
    pattern: str,
    tail: int | None,
    no_sort: bool,
    limit: int | None,
    time_start: str | None,
    time_end: str | None,
    nodes: str | None,
) -> None:
    """Search all node logs for a regex pattern and merge by timestamp.

    PATTERN is optional - if omitted, matches all lines.

    Uses a heap-based streaming merge to efficiently handle large log files
    without loading everything into memory.

    Examples:
        x-testnet logs-search -s -5m                  # all logs, last 5 minutes
        x-testnet logs-search -s -30s --limit 100     # last 30 seconds, max 100 lines
        x-testnet logs-search Shuffle
        x-testnet logs-search "LedgerConsensus.*accepted"
        x-testnet logs-search Shuffle --tail 1000
        x-testnet logs-search Shuffle --time-start 10:30:00 --time-end 10:31:00
        x-testnet logs-search -n 0-2                  # only n0, n1, n2
        x-testnet logs-search -n 1,3,5                # only n1, n3, n5
    """
    import re
    from datetime import datetime, timedelta

    from xahaud_scripts.testnet.cli_handlers import logs_search_handler

    # Parse time arguments (absolute or relative)
    def parse_time(s: str | None) -> datetime | None:
        if s is None:
            return None

        # Check for relative time: -5m, -30s, -1h, -2h30m, etc.
        rel_match = re.match(r"^-(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", s)
        if rel_match:
            hours = int(rel_match.group(1) or 0)
            minutes = int(rel_match.group(2) or 0)
            seconds = int(rel_match.group(3) or 0)
            if hours == 0 and minutes == 0 and seconds == 0:
                raise click.BadParameter(f"Invalid relative time: {s}")
            delta = timedelta(hours=hours, minutes=minutes, seconds=seconds)
            # Log timestamps are time-only (year=1900), so compute relative to now's time
            now = datetime.now()
            target = now - delta
            # Return with year=1900 to match log timestamp format
            return target.replace(year=1900, month=1, day=1)

        # Absolute time formats
        for fmt in ["%H:%M:%S.%f", "%H:%M:%S"]:
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        raise click.BadParameter(
            f"Invalid time format: {s} (use HH:MM:SS, HH:MM:SS.ffffff, or relative like -5m, -30s, -1h)"
        )

    xahaud_root = ctx.obj.get("xahaud_root") or _get_xahaud_root()
    base_dir = ctx.obj.get("testnet_dir") or (xahaud_root / "testnet")

    logs_search_handler(
        base_dir=base_dir,
        pattern=pattern,
        tail=tail,
        no_sort=no_sort,
        limit=limit,
        time_start=parse_time(time_start),
        time_end=parse_time(time_end),
        nodes=nodes,
    )


TEST_SCRIPT_GUIDE = '''
# x-testnet Test Script Guide

## Overview

Test scripts let you run automated tests against a local xahaud testnet.
The framework handles network setup, account funding, and provides an
xrpl-py client for interacting with the network.

## Running a Test Script

    x-testnet run --test-script my_test.py

This will:
1. Launch a local testnet (5 nodes by default)
2. Wait for the first ledger to close
3. Create wallets for declared accounts (deterministic from name)
4. Fund each account from genesis
5. Call your `async def run(ctx)` function
6. Tear down the network when done

## Test Script Format

A test script is a Python file with:
- `accounts` dict (optional): Maps account names to initial XAH balances
- `async def run(ctx)`: The test function that receives a TestContext

### Minimal Example

```python
async def run(ctx):
    print("Hello from test script!")
```

### With Accounts

```python
accounts = {
    "alice": 1000,  # 1000 XAH
    "bob": 500,     # 500 XAH
}

async def run(ctx):
    alice = ctx.get_account("alice")
    bob = ctx.get_account("bob")
    print(f"Alice: {alice.address}")
    print(f"Bob: {bob.address}")
```

## TestContext

The `ctx` object passed to your `run()` function provides:

### ctx.client

A wrapped xrpl-py client connected to node 0. The wrapper automatically
sets api_version=1 on all requests (required by xahaud).

```python
from xrpl.models import ServerInfo, AccountInfo

# Query server info
response = await ctx.client.request(ServerInfo())

# Check account balance
response = await ctx.client.request(AccountInfo(account=alice.address))
balance = response.result["account_data"]["Balance"]
```

### ctx.get_account(name) -> AccountInfo

Get account info by name. The account must be declared in the `accounts` dict.

Returns an `AccountInfo` dataclass with:
- `name`: The account name (e.g., "alice")
- `address`: The classic address (e.g., "rXXX...")
- `public_key`: The public key (e.g., "ED..." or "02/03...")
- `seed`: The seed/secret (e.g., "sXXX...")
- `wallet`: An xrpl-py `Wallet` object for signing transactions

### ctx.compile_hook(source, label="hook") -> bytes

Compile C or WAT source code to WASM bytecode. Uses caching - same source
returns cached result instantly. Raises on compilation failure.

```python
# Compile a simple C hook
wasm = ctx.compile_hook("""
    #include <stdint.h>
    int64_t hook(uint32_t r) {
        return 0;  // accept
    }
    int64_t cbak(uint32_t r) {
        return 0;
    }
""", label="my-hook")

print(f"Compiled to {len(wasm)} bytes")

# Use with SetHook transaction...
```

The compiler auto-detects C vs WAT format (WAT contains "(module").

Requires: wasmcc, hook-cleaner, wat2wasm installed.

### await ctx.submit_tx(tx_dict, wallet) -> dict

Sign and submit a raw transaction dict. This is async - use `await`.
Use this for Xahau-specific transactions like SetHook that aren't in xrpl-py.

Autofills Fee, Sequence, LastLedgerSequence, NetworkID, and Account if not provided.
Raises `ValueError` if the server request fails (e.g. connection error).

```python
# SetHook example
wasm = ctx.compile_hook(hook_source, label="my-hook")

result = await ctx.submit_tx({
    "TransactionType": "SetHook",
    "Hooks": [{
        "Hook": {
            "CreateCode": wasm.hex().upper(),
            "HookOn": "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFBFFFFF",
            "HookNamespace": "0" * 64,
            "HookApiVersion": 0,
        }
    }]
}, alice.wallet)

print(f"SetHook result: {result.get('engine_result')}")
```

Other Xahau-specific transactions: Invoke, UNLModify, Import, etc.

## Account Derivation

Accounts are derived deterministically from their name using SHA-512:
- Same name always produces the same address
- Useful for reproducible tests

```
"alice" -> sha512("alice")[:16] -> seed -> wallet
```

## Genesis Account

The genesis account (rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh) is used to
fund your test accounts. It starts with 100 billion XAH.

## Full Example: Payment Test

```python
"""Test that payments work correctly."""
import asyncio
from xrpl.models import AccountInfo

accounts = {
    "alice": 1000,
    "bob": 100,
}

async def run(ctx):
    alice = ctx.get_account("alice")
    bob = ctx.get_account("bob")

    # Check initial balances
    resp = await ctx.client.request(AccountInfo(account=bob.address))
    initial_balance = int(resp.result["account_data"]["Balance"])
    print(f"Bob initial balance: {initial_balance} drops")

    # Send 50 XAH from alice to bob using ctx.submit_tx
    result = await ctx.submit_tx({
        "TransactionType": "Payment",
        "Destination": bob.address,
        "Amount": "50000000",  # 50 XAH in drops
    }, alice.wallet)

    print(f"Payment result: {result.get('engine_result')}")

    # Wait for ledger close
    await asyncio.sleep(4)

    # Verify new balance
    resp = await ctx.client.request(AccountInfo(account=bob.address))
    new_balance = int(resp.result["account_data"]["Balance"])
    print(f"Bob new balance: {new_balance} drops")

    expected = initial_balance + 50_000_000
    assert new_balance == expected, f"Expected {expected}, got {new_balance}"

    print("Payment test PASSED!")
```

## Tips

- Use `drops` for amounts (1 XAH = 1,000,000 drops)
- The `accounts` dict values are in XAH, not drops
- Use `ctx.submit_tx()` for all transactions (handles Xahau api_version)
- The network has 5 validators, so consensus is fast (~4 second ledgers)
- No need to specify api_version=1 - the client wrapper handles it

## xrpl-py Documentation

For more on xrpl-py: https://xrpl-py.readthedocs.io/

Common imports:
```python
from xrpl.models import AccountInfo, ServerInfo, Tx, Ledger
from xrpl.utils import xrp_to_drops, drops_to_xrp
```

Note: Use `ctx.submit_tx()` instead of xrpl-py's `submit_and_wait()` to
avoid api_version incompatibilities with xahaud.
'''


@testnet.command("test-script-guide")
def test_script_guide() -> None:
    """Show guide for writing test scripts."""
    click.echo(TEST_SCRIPT_GUIDE)


def main() -> None:
    """Entry point for the testnet CLI."""
    testnet()


if __name__ == "__main__":
    main()
