"""Tests for TestNetwork binary-swapping restart (rolling-upgrade support)."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, cast

import pytest

from xahaud_scripts.testnet.config import LaunchConfig, NetworkConfig, NodeInfo
from xahaud_scripts.testnet.launcher.tmux import TmuxLauncher
from xahaud_scripts.testnet.network import TestNetwork
from xahaud_scripts.testnet.process import UnixProcessManager
from xahaud_scripts.testnet.rpc import RequestsRPCClient


def _node(tmp_path: Path, node_id: int) -> NodeInfo:
    return NodeInfo(
        id=node_id,
        public_key=f"pk{node_id}",
        token=f"tok{node_id}",
        config_path=tmp_path / f"n{node_id}" / "xahaud.cfg",
        port_peer=21235 + node_id,
        port_rpc=5005 + node_id,
        port_ws=6005 + node_id,
    )


def _network(tmp_path: Path, launcher: object) -> TestNetwork:
    return TestNetwork(
        base_dir=tmp_path,
        network_config=NetworkConfig(node_count=2),
        launcher=launcher,  # type: ignore[arg-type]
        rpc_client=RequestsRPCClient(5005),
        process_manager=UnixProcessManager(),
    )


class _NoBuilderLauncher:
    """Launcher stub lacking build_launch_command → forces the token fallback."""


def test_rebuild_launch_command_swaps_binary_via_builder(tmp_path: Path):
    launcher = TmuxLauncher()
    net = _network(tmp_path, launcher)
    node = _node(tmp_path, 0)
    net._nodes = [node, _node(tmp_path, 1)]

    old_path = tmp_path / "build" / "rippled"
    net._launch_config = LaunchConfig(
        xahaud_root=tmp_path,
        rippled_path=old_path,
        genesis_file=tmp_path / "genesis.json",
    )
    old_cmd = launcher.build_launch_command(node, net._launch_config)
    net._launch_state = {
        "launcher": "tmux",
        "pane_ids": {"0": "%0"},
        "launch_commands": {"0": old_cmd},
    }

    new_path = tmp_path / "saved" / "new-rippled"
    new_cmd = net.rebuild_launch_command(0, new_path)

    old_tok = shlex.quote(str(old_path))
    new_tok = shlex.quote(str(new_path))
    assert new_tok in new_cmd
    assert old_tok not in new_cmd
    # Only the binary token changed — env vars and startup flags are identical.
    assert new_cmd == old_cmd.replace(old_tok, new_tok)
    # Persisted back and config updated for later restarts.
    assert net._launch_state["launch_commands"]["0"] == new_cmd
    assert net._launch_config.node_rippled_paths[0] == new_path


def test_rebuild_launch_command_fallback_token_replace(tmp_path: Path):
    net = _network(tmp_path, _NoBuilderLauncher())
    net._nodes = [_node(tmp_path, 0), _node(tmp_path, 1)]

    old_path = tmp_path / "build" / "rippled"
    net._launch_config = LaunchConfig(
        xahaud_root=tmp_path,
        rippled_path=old_path,
        genesis_file=tmp_path / "genesis.json",
    )
    old_tok = shlex.quote(str(old_path))
    old_cmd = (
        f" export NO_COLOR=1 && _xrun {old_tok} "
        f"--conf {shlex.quote(str(tmp_path / 'n0' / 'xahaud.cfg'))} "
        f"--ledgerfile {shlex.quote(str(tmp_path / 'genesis.json'))}"
    )
    net._launch_state = {"launch_commands": {"0": old_cmd}}

    new_path = tmp_path / "saved" / "new-rippled"
    new_cmd = net.rebuild_launch_command(0, new_path)

    new_tok = shlex.quote(str(new_path))
    assert new_tok in new_cmd
    assert old_tok not in new_cmd
    assert new_cmd == old_cmd.replace(old_tok, new_tok)
    assert net._launch_state["launch_commands"]["0"] == new_cmd
    assert net._launch_config.node_rippled_paths[0] == new_path


def test_rebuild_launch_command_fallback_ambiguous_token_raises(tmp_path: Path):
    net = _network(tmp_path, _NoBuilderLauncher())
    net._nodes = [_node(tmp_path, 0)]

    old_path = tmp_path / "rippled"
    net._launch_config = LaunchConfig(
        xahaud_root=tmp_path,
        rippled_path=old_path,
        genesis_file=tmp_path / "genesis.json",
    )
    old_tok = shlex.quote(str(old_path))
    # Token appears twice → ambiguous → must fail loud rather than guess.
    net._launch_state = {"launch_commands": {"0": f" _xrun {old_tok} --x {old_tok}"}}

    with pytest.raises(RuntimeError, match="exactly one"):
        net.rebuild_launch_command(0, tmp_path / "new-rippled")


def test_rebuild_launch_command_unknown_node_raises(tmp_path: Path):
    net = _network(tmp_path, _NoBuilderLauncher())
    net._nodes = [_node(tmp_path, 0)]
    net._launch_config = LaunchConfig(
        xahaud_root=tmp_path,
        rippled_path=tmp_path / "rippled",
        genesis_file=tmp_path / "genesis.json",
    )

    with pytest.raises(ValueError, match="Unknown node"):
        net.rebuild_launch_command(9, tmp_path / "new-rippled")


class _RecordingLauncher:
    """Launcher that records whether it was ever asked to start a node."""

    def __init__(self) -> None:
        self.launched: list[int] = []

    def launch(self, node, config) -> bool:  # noqa: ANN001
        self.launched.append(node.id)
        return True

    def finalize(self) -> None:
        pass

    def shutdown(self, base_dir, process_manager) -> int:  # noqa: ANN001
        return 0


class _FreePortsProcessManager:
    def check_ports_free(self, _ports):  # noqa: ANN001, ANN201
        return {}

    def get_port_state(self, _port):  # noqa: ANN001, ANN201
        return []

    def kill(self, _pid: int) -> None:
        pass


def _preflight_network(tmp_path: Path, launcher, *, fixed_peers: bool):  # noqa: ANN001, ANN201
    from xahaud_scripts.testnet.config import NetworkConfig, NodeInfo
    from xahaud_scripts.testnet.network import TestNetwork
    from xahaud_scripts.testnet.rpc import RequestsRPCClient

    network = TestNetwork(
        base_dir=tmp_path / "testnet",
        network_config=NetworkConfig(node_count=2, fixed_peers=fixed_peers),
        launcher=cast(Any, launcher),
        rpc_client=RequestsRPCClient(5005),
        process_manager=cast(Any, _FreePortsProcessManager()),
    )
    network._nodes = [
        NodeInfo(
            id=i,
            public_key=f"pk{i}",
            token=f"tok{i}",
            config_path=tmp_path / "testnet" / f"n{i}" / "xahaud.cfg",
            port_peer=21235 + i,
            port_rpc=5005 + i,
            port_ws=6005 + i,
        )
        for i in range(2)
    ]
    return network


def _preflight_launch_config(tmp_path: Path):  # noqa: ANN201
    from xahaud_scripts.testnet.config import LaunchConfig

    return LaunchConfig(
        xahaud_root=tmp_path,
        rippled_path=tmp_path / "rippled",
        genesis_file=tmp_path / "genesis.json",
    )


def test_run_aborts_before_launching_when_alias_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The launch preflight must actually be wired into run().

    Without this, deleting the _verify_loopback_aliases() call from run() left
    the whole suite green — the guard was untested at the integration level.
    """
    from xahaud_scripts.testnet import loopback
    from xahaud_scripts.testnet.loopback import LoopbackAliasError

    monkeypatch.setattr(loopback, "aliases_required", lambda: True)
    monkeypatch.setattr(loopback, "_probe_loopback_addresses", lambda: {"127.0.0.1"})
    loopback.reset_cache()

    launcher = _RecordingLauncher()
    network = _preflight_network(tmp_path, launcher, fixed_peers=True)

    with pytest.raises(LoopbackAliasError, match="127.0.0.2"):
        network.run(_preflight_launch_config(tmp_path))

    assert launcher.launched == [], "must fail before any node is launched"


def test_run_allows_missing_alias_when_not_fixed_peers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A no-fixed-peers network does no dialing at launch, so it must not block.

    Runtime dials are guarded individually by connect_managed_peer instead.
    """
    from xahaud_scripts.testnet import loopback

    monkeypatch.setattr(loopback, "aliases_required", lambda: True)
    monkeypatch.setattr(loopback, "_probe_loopback_addresses", lambda: {"127.0.0.1"})
    loopback.reset_cache()

    launcher = _RecordingLauncher()
    network = _preflight_network(tmp_path, launcher, fixed_peers=False)

    network.run(_preflight_launch_config(tmp_path))

    assert launcher.launched == [0, 1]
