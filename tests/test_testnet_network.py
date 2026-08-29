"""Tests for TestNetwork binary-swapping restart (rolling-upgrade support)."""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import Any, cast

import pytest

from xahaud_scripts.testnet.config import LaunchConfig, NetworkConfig, NodeInfo
from xahaud_scripts.testnet.generator import ValidatorKeysGenerator
from xahaud_scripts.testnet.launcher.tmux import TmuxLauncher
from xahaud_scripts.testnet.network import TestNetwork
from xahaud_scripts.testnet.process import UnixProcessManager
from xahaud_scripts.testnet.rpc import RequestsRPCClient
from xahaud_scripts.testnet.scenario import ScenarioContext


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


def test_wipe_wallet_db_removes_only_wallet_sqlite_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    net = _network(tmp_path, _NoBuilderLauncher())
    node = _node(tmp_path, 0)
    net._nodes = [node]
    db_dir = node.node_dir / "db"
    db_dir.mkdir(parents=True)
    wallet_files = [
        db_dir / "wallet.db",
        db_dir / "wallet.db-wal",
        db_dir / "wallet.db-shm",
        db_dir / "wallet.db-journal",
    ]
    for path in wallet_files:
        path.write_text("wallet state")
    ledger_file = db_dir / "nudb" / "nudb.dat"
    ledger_file.parent.mkdir()
    ledger_file.write_text("ledger state")
    monkeypatch.setattr(net, "get_exit_status", lambda _nid: 0)

    removed = net.wipe_wallet_db(0)

    assert removed == wallet_files
    assert all(not path.exists() for path in wallet_files)
    assert ledger_file.read_text() == "ledger state"


def test_wipe_wallet_db_refuses_while_process_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    net = _network(tmp_path, _NoBuilderLauncher())
    node = _node(tmp_path, 0)
    net._nodes = [node]
    wallet_db = node.node_dir / "db" / "wallet.db"
    wallet_db.parent.mkdir(parents=True)
    wallet_db.write_text("live wallet state")
    monkeypatch.setattr(net, "get_exit_status", lambda _nid: None)

    with pytest.raises(RuntimeError, match="before its process has exited"):
        net.wipe_wallet_db(0)

    assert wallet_db.read_text() == "live wallet state"


def test_rotate_validator_manifest_updates_generated_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    net = _network(tmp_path, _NoBuilderLauncher())
    net._config = NetworkConfig(node_count=2, validators=1)
    node = _node(tmp_path, 0)
    net._nodes = [node, _node(tmp_path, 1)]
    node.node_dir.mkdir(parents=True)
    node.config_path.write_text("[validator_token]\nold-token\n\n[server]\npeer\n")
    keyfile = node.node_dir / "validator-keys.json"
    keyfile.write_text("old-keyfile")

    def rotate(_self: ValidatorKeysGenerator, path: Path):
        assert path == keyfile
        path.write_text("rotated-keyfile")
        return {
            "public_key": node.public_key,
            "sequence": 2,
            "token": "rotated-token",
        }

    monkeypatch.setattr(ValidatorKeysGenerator, "rotate", rotate)

    result = net.rotate_validator_manifest(0)

    assert result == {"node_id": 0, "public_key": "pk0", "sequence": 2}
    assert keyfile.read_text() == "rotated-keyfile"
    assert "[validator_token]\nrotated-token\n" in node.config_path.read_text()
    assert "old-token" not in node.config_path.read_text()


def test_revoke_validator_installs_revocation_on_via_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    net = _network(tmp_path, _NoBuilderLauncher())
    net._config = NetworkConfig(node_count=2, validators=1)
    master = _node(tmp_path, 0)
    via = _node(tmp_path, 1)
    net._nodes = [master, via]
    master.node_dir.mkdir(parents=True)
    via.node_dir.mkdir(parents=True)
    master.config_path.write_text("[validator_token]\nmaster-token\n")
    via.config_path.write_text("[server]\npeer\n")
    keyfile = master.node_dir / "validator-keys.json"
    keyfile.write_text("old-keyfile")

    def revoke(_self: ValidatorKeysGenerator, path: Path):
        assert path == keyfile
        path.write_text("revoked-keyfile")
        return {
            "public_key": master.public_key,
            "revocation": "revocation-base64",
        }

    monkeypatch.setattr(ValidatorKeysGenerator, "revoke", revoke)

    result = net.revoke_validator(0, 1)

    assert result == {
        "master_node_id": 0,
        "via_node_id": 1,
        "public_key": "pk0",
    }
    assert keyfile.read_text() == "revoked-keyfile"
    assert via.config_path.read_text().endswith(
        "[validator_key_revocation]\nrevocation-base64\n\n"
    )


def test_rotate_validator_manifest_rejects_non_validator(tmp_path: Path):
    net = _network(tmp_path, _NoBuilderLauncher())
    net._config = NetworkConfig(node_count=2, validators=1)
    net._nodes = [_node(tmp_path, 0), _node(tmp_path, 1)]

    with pytest.raises(ValueError, match="is not a validator"):
        net.rotate_validator_manifest(1)


def test_rotate_validator_manifest_rolls_back_keyfile_and_config_on_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    net = _network(tmp_path, _NoBuilderLauncher())
    net._config = NetworkConfig(node_count=1, validators=1)
    node = _node(tmp_path, 0)
    net._nodes = [node]
    node.node_dir.mkdir(parents=True)
    node.config_path.write_text("[validator_token]\nold-token\n")
    keyfile = node.node_dir / "validator-keys.json"
    keyfile.write_text("old-keyfile")

    def bad_rotate(_self: ValidatorKeysGenerator, path: Path):
        path.write_text("mutated-keyfile")
        return {
            "public_key": "wrong-master",
            "sequence": 2,
            "token": "rotated-token",
        }

    monkeypatch.setattr(ValidatorKeysGenerator, "rotate", bad_rotate)

    with pytest.raises(RuntimeError, match="keyfile master does not match"):
        net.rotate_validator_manifest(0)

    assert keyfile.read_text() == "old-keyfile"
    assert node.config_path.read_text() == "[validator_token]\nold-token\n"


class _RestartRPC:
    def __init__(self, network: _RestartScenarioNetwork) -> None:
        self._network = network

    def server_info(self, node_id: int) -> dict[str, Any] | None:
        self._network.events.append(f"rpc:{node_id}")
        return {"info": {}} if self._network.running else None


class _RestartScenarioNetwork:
    def __init__(self, *, stop_success: bool = True) -> None:
        self.events: list[str] = []
        self.running = True
        self.exit_status: int | None = None
        self.stop_success = stop_success
        self.rpc_client = _RestartRPC(self)

    def stop_nodes(self, node_ids: list[int]) -> dict[int, bool]:
        node_id = node_ids[0]
        self.events.append(f"stop:{node_id}")
        if not self.stop_success:
            return {node_id: False}
        self.running = False
        self.exit_status = 0
        return {node_id: True}

    def start_nodes(self, node_ids: list[int]) -> dict[int, bool]:
        node_id = node_ids[0]
        self.events.append(f"start:{node_id}")
        self.exit_status = None
        self.running = True
        return {node_id: True}

    def get_exit_status(self, node_id: int) -> int | None:
        self.events.append(f"exit-status:{node_id}")
        return self.exit_status

    def wipe_wallet_db(self, node_id: int) -> list[Path]:
        assert self.exit_status is not None
        self.events.append(f"wipe:{node_id}")
        return []

    def rotate_validator_manifest(self, node_id: int) -> dict[str, Any]:
        self.events.append(f"rotate:{node_id}")
        return {"node_id": node_id, "public_key": "pk", "sequence": 2}

    def revoke_validator(self, master_node_id: int, via_node_id: int) -> dict[str, Any]:
        self.events.append(f"revoke:{master_node_id}:via:{via_node_id}")
        return {
            "master_node_id": master_node_id,
            "via_node_id": via_node_id,
            "public_key": "pk",
        }


def test_scenario_restart_node_waits_for_exit_then_wipes_and_starts():
    network = _RestartScenarioNetwork()
    ctx = ScenarioContext(cast(Any, network))

    result = asyncio.run(ctx.restart_node(2, wipe_wallet_db=True))

    assert result == {2: True}
    assert network.events == [
        "stop:2",
        "exit-status:2",
        "wipe:2",
        "start:2",
        "rpc:2",
    ]


def test_scenario_rotate_validator_manifest_restarts_rotated_node():
    network = _RestartScenarioNetwork()
    ctx = ScenarioContext(cast(Any, network))

    result = asyncio.run(ctx.rotate_validator_manifest(1))

    assert result["sequence"] == 2
    assert result["restart"] == {1: True}
    assert network.events == [
        "rotate:1",
        "stop:1",
        "exit-status:1",
        "start:1",
        "rpc:1",
    ]


def test_scenario_revoke_validator_restarts_via_node():
    network = _RestartScenarioNetwork()
    ctx = ScenarioContext(cast(Any, network))

    result = asyncio.run(ctx.revoke_validator(0, 2))

    assert result["restart"] == {2: True}
    assert network.events == [
        "revoke:0:via:2",
        "stop:2",
        "exit-status:2",
        "start:2",
        "rpc:2",
    ]


def test_scenario_manifest_mutation_stays_committed_when_stop_fails():
    network = _RestartScenarioNetwork(stop_success=False)
    ctx = ScenarioContext(cast(Any, network))

    result = asyncio.run(ctx.rotate_validator_manifest(1))

    assert result["sequence"] == 2
    assert result["restart"] == {1: False}
    assert network.events == ["rotate:1", "stop:1"]


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
