"""Tests for TestNetwork binary-swapping restart (rolling-upgrade support)."""

from __future__ import annotations

import asyncio
import builtins
import json
import logging
import shlex
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from xahaud_scripts.testnet.config import LaunchConfig, NetworkConfig, NodeInfo
from xahaud_scripts.testnet.generator import ValidatorKeysGenerator
from xahaud_scripts.testnet.launcher.tmux import TmuxLauncher
from xahaud_scripts.testnet.network import TestNetwork
from xahaud_scripts.testnet.process import UnixProcessManager
from xahaud_scripts.testnet.rpc import RequestsRPCClient
from xahaud_scripts.testnet.scenario import (
    AssertionError as ScenarioAssertionError,
)
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
    assert net.nodes[0].token == "rotated-token"
    network_info = json.loads((tmp_path / "network.json").read_text())
    assert network_info["nodes"][0]["token"] == "rotated-token"


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
        recovery_file = path.parent / "validator-key-revocation.cfg"
        recovery_file.write_text("[validator_key_revocation]\nrevocation-base64\n")
        return {
            "public_key": master.public_key,
            "revocation": "revocation-base64",
            "recovery_file": str(recovery_file),
        }

    monkeypatch.setattr(ValidatorKeysGenerator, "revoke", revoke)

    result = net.revoke_validator(0, 1)

    assert result == {
        "master_node_id": 0,
        "via_node_id": 1,
        "public_key": "pk0",
        "recovery_file": str(master.node_dir / "validator-key-revocation.cfg"),
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


def test_rotate_validator_manifest_rolls_back_metadata_when_persistence_fails(
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
    network_file = tmp_path / "network.json"
    network_file.write_text("original-network-info")

    def rotate(_self: ValidatorKeysGenerator, path: Path):
        path.write_text("rotated-keyfile")
        return {
            "public_key": node.public_key,
            "sequence": 2,
            "token": "rotated-token",
        }

    def fail_save() -> None:
        network_file.write_text("partial-network-info")
        raise OSError("disk full")

    monkeypatch.setattr(ValidatorKeysGenerator, "rotate", rotate)
    monkeypatch.setattr(net, "_save_network_info", fail_save)

    with pytest.raises(OSError, match="disk full"):
        net.rotate_validator_manifest(0)

    assert keyfile.read_text() == "old-keyfile"
    assert node.config_path.read_text() == "[validator_token]\nold-token\n"
    assert net.nodes[0].token == node.token
    assert network_file.read_text() == "original-network-info"


def test_revoke_validator_preserves_revoked_key_and_recovery_artifact_on_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    net = _network(tmp_path, _NoBuilderLauncher())
    net._config = NetworkConfig(node_count=2, validators=1)
    master = _node(tmp_path, 0)
    via = _node(tmp_path, 1)
    net._nodes = [master, via]
    master.node_dir.mkdir(parents=True)
    via.node_dir.mkdir(parents=True)
    keyfile = master.node_dir / "validator-keys.json"
    keyfile.write_text(json.dumps({"public_key": master.public_key, "revoked": False}))
    via.config_path.write_text("[server]\npeer\n")
    recovery_file = master.node_dir / "validator-key-revocation.cfg"

    def revoke(_self: ValidatorKeysGenerator, path: Path):
        path.write_text(json.dumps({"public_key": master.public_key, "revoked": True}))
        recovery_file.write_text("[validator_key_revocation]\nrevocation-base64\n")
        return {
            "public_key": master.public_key,
            "revocation": "revocation-base64",
            "recovery_file": str(recovery_file),
        }

    def fail_config(*_args: object, **_kwargs: object) -> None:
        via.config_path.write_text("partially-written")
        raise OSError("relay config is read-only")

    monkeypatch.setattr(ValidatorKeysGenerator, "revoke", revoke)
    monkeypatch.setattr(
        "xahaud_scripts.testnet.network.update_config_section", fail_config
    )

    with pytest.raises(RuntimeError, match="remains permanently revoked") as exc_info:
        net.revoke_validator(0, 1)

    assert str(recovery_file) in str(exc_info.value)
    assert json.loads(keyfile.read_text())["revoked"] is True
    assert recovery_file.read_text().endswith("revocation-base64\n")
    assert via.config_path.read_text() == "[server]\npeer\n"


class _RestartRPC:
    def __init__(self, network: _RestartScenarioNetwork) -> None:
        self._network = network

    def server_info(self, node_id: int) -> dict[str, Any] | None:
        self._network.events.append(f"rpc:{node_id}")
        return {"info": {}} if self._network.running else None


class _RestartScenarioNetwork:
    def __init__(
        self, *, stop_success: bool = True, start_success: bool = True
    ) -> None:
        self.events: list[str] = []
        self.running = True
        self.exit_status: int | None = None
        self.stop_success = stop_success
        self.start_success = start_success
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
        if not self.start_success:
            return {node_id: False}
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


def test_scenario_restart_node_zero_replaces_running_websocket_task():
    async def exercise() -> None:
        network = _RestartScenarioNetwork()
        ctx = ScenarioContext(cast(Any, network))
        starts = 0

        async def fake_ws_loop() -> None:
            nonlocal starts
            starts += 1
            await asyncio.Event().wait()

        ctx._ws_loop = fake_ws_loop  # type: ignore[method-assign]
        await ctx._start_ws()
        await asyncio.sleep(0)
        original_task = ctx._ws_task

        result = await ctx.restart_node(0)
        await asyncio.sleep(0)

        assert result == {0: True}
        assert original_task is not None and original_task.cancelled()
        assert ctx._ws_task is not original_task
        assert starts == 2
        await ctx._stop_ws()

    asyncio.run(exercise())


def test_scenario_assertion_error_is_a_builtin_assertion_error():
    assert issubclass(ScenarioAssertionError, builtins.AssertionError)


def test_tmux_launcher_preserves_text_stderr_on_launch_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    launcher = TmuxLauncher()
    node = _node(tmp_path, 0)
    config = LaunchConfig(
        xahaud_root=tmp_path,
        rippled_path=tmp_path / "build" / "rippled",
        genesis_file=tmp_path / "genesis.json",
    )

    def fail_create_session(_node: NodeInfo, _cmd: str) -> str:
        raise subprocess.CalledProcessError(
            1,
            ["tmux", "new-session"],
            output="",
            stderr="fork blocked",
        )

    monkeypatch.setattr(launcher, "_create_session", fail_create_session)
    with caplog.at_level(logging.ERROR, logger="xahaud_scripts.testnet.launcher.tmux"):
        assert launcher.launch(node, config) is False

    assert "tmux stderr: fork blocked" in caplog.text


def test_tmux_exit_status_must_match_current_pid_generation(tmp_path: Path):
    launcher = TmuxLauncher()
    launcher.load_launch_state({"base_dir": str(tmp_path)})
    node_dir = tmp_path / "n0"
    node_dir.mkdir()
    (node_dir / ".pid").write_text("current-generation 123\n")
    (node_dir / ".exit_status").write_text("stale-generation 123 0\n")

    assert launcher.get_exit_status(0) is None

    (node_dir / ".exit_status").write_text("current-generation 123 17\n")
    assert launcher.get_exit_status(0) == 17

    # Legacy untagged markers are deliberately not trusted.
    (node_dir / ".exit_status").write_text("0\n")
    assert launcher.get_exit_status(0) is None


def test_tmux_stop_refuses_unverified_marker_pid_and_uses_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    launcher = TmuxLauncher()
    launcher.load_launch_state({"base_dir": str(tmp_path), "pane_ids": {"0": "%0"}})
    node_dir = tmp_path / "n0"
    node_dir.mkdir()
    (node_dir / ".pid").write_text("generation 4242\n")

    monkeypatch.setattr(launcher, "_pid_belongs_to_node", lambda _pid, _nid: False)
    monkeypatch.setattr(launcher, "_validate_pane", lambda _nid: "%0")
    monkeypatch.setattr(
        "xahaud_scripts.testnet.launcher.tmux.os.kill",
        lambda *_args: pytest.fail("an unverified PID must not be signaled"),
    )
    sent: list[list[str]] = []

    def record_run(args: list[str], **_kwargs: Any):
        sent.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(
        "xahaud_scripts.testnet.launcher.tmux.subprocess.run", record_run
    )

    assert launcher.stop_node(0) is True
    assert sent == [["tmux", "send-keys", "-t", "%0", "C-c", ""]]


def test_tmux_pid_ownership_requires_exact_config_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    launcher = TmuxLauncher()
    launcher.load_launch_state({"base_dir": str(tmp_path)})
    config_path = tmp_path / "n0" / "xahaud.cfg"
    output = {"value": f"/opt/xahaud --conf {config_path}.backup --ledgerfile genesis"}

    def fake_run(args: list[str], **_kwargs: Any):
        return subprocess.CompletedProcess(args, 0, stdout=output["value"], stderr="")

    monkeypatch.setattr("xahaud_scripts.testnet.launcher.tmux.subprocess.run", fake_run)

    assert launcher._pid_belongs_to_node(4242, 0) is False

    output["value"] = f"/opt/xahaud --conf {config_path} --ledgerfile genesis"
    assert launcher._pid_belongs_to_node(4242, 0) is True


def test_tmux_refuses_reused_pane_from_another_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    launcher = TmuxLauncher()
    launcher.load_launch_state(
        {
            "base_dir": str(tmp_path),
            "pane_ids": {"0": "%0"},
            "pane_owner_tokens": {"0": "owner"},
        }
    )
    monkeypatch.setattr(launcher, "_list_live_pane_ids", lambda: {"%0"})
    monkeypatch.setattr(launcher, "_pane_owner", lambda _pane: "owner")
    monkeypatch.setattr(
        launcher, "_pane_current_path", lambda _pane: tmp_path / "other" / "n0"
    )
    monkeypatch.setattr(
        "xahaud_scripts.testnet.launcher.tmux.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail(
            "a pane owned by another network must not receive Ctrl+C"
        ),
    )

    assert launcher.stop_node(0) is False


def test_tmux_refuses_legacy_untagged_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    launcher = TmuxLauncher()
    launcher.load_launch_state({"base_dir": str(tmp_path), "pane_ids": {"0": "%0"}})
    monkeypatch.setattr(launcher, "_list_live_pane_ids", lambda: {"%0"})
    monkeypatch.setattr(
        "xahaud_scripts.testnet.launcher.tmux.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail(
            "an untagged legacy pane must not receive Ctrl+C"
        ),
    )

    assert launcher.stop_node(0) is False


def test_tmux_refuses_reused_pane_with_same_path_and_wrong_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    launcher = TmuxLauncher()
    launcher.load_launch_state(
        {
            "base_dir": str(tmp_path),
            "pane_ids": {"0": "%0"},
            "pane_owner_tokens": {"0": "original-owner"},
        }
    )
    monkeypatch.setattr(launcher, "_list_live_pane_ids", lambda: {"%0"})
    monkeypatch.setattr(launcher, "_pane_owner", lambda _pane: "replacement-owner")
    monkeypatch.setattr(launcher, "_pane_current_path", lambda _pane: tmp_path / "n0")
    monkeypatch.setattr(
        "xahaud_scripts.testnet.launcher.tmux.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail(
            "a reused pane with the same path must not receive Ctrl+C"
        ),
    )

    assert launcher.stop_node(0) is False


def test_tmux_pane_owner_is_tagged_and_persisted(
    monkeypatch: pytest.MonkeyPatch,
):
    launcher = TmuxLauncher()
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "xahaud_scripts.testnet.launcher.tmux.secrets.token_hex",
        lambda _size: "owner-token",
    )
    monkeypatch.setattr(
        "xahaud_scripts.testnet.launcher.tmux.subprocess.run",
        lambda args, **_kwargs: calls.append(args)
        or subprocess.CompletedProcess(args, 0),
    )

    launcher._set_pane_owner(0, "%7")

    assert calls == [
        [
            "tmux",
            "set-option",
            "-p",
            "-t",
            "%7",
            "@xahaud_owner",
            "owner-token",
        ]
    ]
    assert launcher.launch_state["pane_owner_tokens"] == {"0": "owner-token"}


def test_tmux_failed_owner_tag_discards_new_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    launcher = TmuxLauncher()
    node = _node(tmp_path, 1)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any):
        calls.append(args)
        if args[1] == "split-window":
            return subprocess.CompletedProcess(args, 0, stdout="%7\n", stderr="")
        if args[1] == "set-option":
            raise subprocess.CalledProcessError(1, args, stderr="unsupported")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("xahaud_scripts.testnet.launcher.tmux.subprocess.run", fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        launcher._create_pane(node, " command")

    assert calls[-1] == ["tmux", "kill-pane", "-t", "%7"]
    assert launcher.launch_state["pane_owner_tokens"] == {}


def test_tmux_cleanup_failure_does_not_mask_owner_tag_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    launcher = TmuxLauncher()
    node = _node(tmp_path, 1)

    def fake_run(args: list[str], **_kwargs: Any):
        if args[1] == "split-window":
            return subprocess.CompletedProcess(args, 0, stdout="%7\n", stderr="")
        if args[1] == "set-option":
            raise subprocess.CalledProcessError(17, args, stderr="tag failed")
        if args[1] == "kill-pane":
            raise OSError("tmux disappeared")
        raise AssertionError(f"unexpected tmux command: {args}")

    monkeypatch.setattr("xahaud_scripts.testnet.launcher.tmux.subprocess.run", fake_run)

    with pytest.raises(subprocess.CalledProcessError) as raised:
        launcher._create_pane(node, " command")

    assert raised.value.returncode == 17
    assert raised.value.stderr == "tag failed"
    assert launcher.launch_state["pane_owner_tokens"] == {}


def test_tmux_failed_launch_restores_exact_prior_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    launcher = TmuxLauncher()
    launcher._session_created = True
    launcher._pane_count = 1
    launcher._base_dir = tmp_path / "existing"
    launcher._desktop = 2
    launcher._pane_ids = {0: "%3"}
    launcher._pane_owner_tokens = {0: "existing-owner"}
    launcher._launch_commands = {0: " existing-command"}
    before = launcher.launch_state
    node = _node(tmp_path, 1)
    config = LaunchConfig(
        xahaud_root=tmp_path,
        rippled_path=tmp_path / "build" / "rippled",
        genesis_file=tmp_path / "genesis.json",
        desktop=4,
    )

    def fail_after_mutating_owner(_node: NodeInfo, _command: str) -> str:
        launcher._session_created = False
        launcher._pane_count = 99
        launcher._pane_ids[1] = "%9"
        launcher._pane_owner_tokens[1] = "transient-owner"
        launcher._base_dir = tmp_path / "transient"
        raise subprocess.CalledProcessError(19, ["tmux", "set-option"])

    monkeypatch.setattr(launcher, "_create_pane", fail_after_mutating_owner)

    assert launcher.launch(node, config) is False
    assert launcher.launch_state == before
    assert launcher._session_created is True
    assert launcher._pane_count == 1
    assert launcher._desktop == 2


def test_tmux_post_setup_failure_discards_pane_and_restores_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    launcher = TmuxLauncher()
    node = _node(tmp_path, 0)
    config = LaunchConfig(
        xahaud_root=tmp_path,
        rippled_path=tmp_path / "build" / "rippled",
        genesis_file=tmp_path / "genesis.json",
    )
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any):
        calls.append(args)
        stdout = "%7\n" if args[1] == "new-session" else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("xahaud_scripts.testnet.launcher.tmux.subprocess.run", fake_run)
    monkeypatch.setattr(
        "xahaud_scripts.testnet.launcher.tmux.logger.info",
        lambda _message: (_ for _ in ()).throw(OSError("logger failed")),
    )

    with pytest.raises(OSError, match="logger failed"):
        launcher.launch(node, config)

    assert calls[-1] == ["tmux", "kill-pane", "-t", "%7"]
    assert launcher.launch_state == {
        "launcher": "tmux",
        "pane_ids": {},
        "pane_owner_tokens": {},
        "launch_commands": {},
    }
    assert launcher._session_created is False
    assert launcher._pane_count == 0


def test_tmux_shutdown_clears_pane_ownership_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    launcher = TmuxLauncher()
    launcher._session_created = True
    launcher._pane_count = 1
    launcher._base_dir = tmp_path
    launcher._desktop = 2
    launcher._pane_ids = {0: "%7"}
    launcher._pane_owner_tokens = {0: "owner-token"}
    launcher._launch_commands = {0: " command"}
    monkeypatch.setattr(
        "xahaud_scripts.testnet.launcher.tmux.subprocess.run",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, 0),
    )

    launcher.shutdown(tmp_path, cast(Any, object()))

    assert launcher.launch_state == {
        "launcher": "tmux",
        "pane_ids": {},
        "pane_owner_tokens": {},
        "launch_commands": {},
    }
    assert launcher._session_created is False
    assert launcher._pane_count == 0
    assert launcher._base_dir is None
    assert launcher._desktop is None


def test_tmux_shutdown_preserves_state_when_tmux_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    launcher = TmuxLauncher()
    launcher._session_created = True
    launcher._base_dir = tmp_path
    launcher._pane_ids = {0: "%7"}
    launcher._pane_owner_tokens = {0: "owner-token"}
    launcher._launch_commands = {0: " command"}

    before = launcher.launch_state

    def fail_tmux(*_args: Any, **_kwargs: Any):
        raise OSError("tmux disappeared")

    monkeypatch.setattr(
        "xahaud_scripts.testnet.launcher.tmux.subprocess.run", fail_tmux
    )

    with pytest.raises(OSError, match="tmux disappeared"):
        launcher.shutdown(tmp_path, cast(Any, object()))

    assert launcher.launch_state == before
    assert launcher._session_created is True
    assert launcher._base_dir == tmp_path


def test_tmux_shutdown_preserves_state_when_kill_command_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    launcher = TmuxLauncher()
    launcher._session_created = True
    launcher._base_dir = tmp_path
    launcher._pane_ids = {0: "%7"}
    launcher._pane_owner_tokens = {0: "owner-token"}
    launcher._launch_commands = {0: " command"}
    before = launcher.launch_state
    monkeypatch.setattr(
        "xahaud_scripts.testnet.launcher.tmux.subprocess.run",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args, 1, stdout=b"", stderr=b"server error"
        ),
    )

    with pytest.raises(subprocess.CalledProcessError) as raised:
        launcher.shutdown(tmp_path, cast(Any, object()))

    assert raised.value.returncode == 1
    assert launcher.launch_state == before
    assert launcher._session_created is True


def test_tmux_shutdown_resets_state_when_session_is_already_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    launcher = TmuxLauncher()
    launcher._session_created = True
    launcher._base_dir = tmp_path
    launcher._pane_ids = {0: "%7"}
    launcher._pane_owner_tokens = {0: "owner-token"}
    launcher._launch_commands = {0: " command"}
    monkeypatch.setattr(
        "xahaud_scripts.testnet.launcher.tmux.subprocess.run",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args,
            1,
            stdout=b"",
            stderr=b"can't find session: xahaud-testnet",
        ),
    )

    assert launcher.shutdown(tmp_path, cast(Any, object())) == 0
    assert launcher.launch_state == {
        "launcher": "tmux",
        "pane_ids": {},
        "pane_owner_tokens": {},
        "launch_commands": {},
    }
    assert launcher._session_created is False


def test_tmux_shutdown_resets_state_when_iterm_cleanup_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    launcher = TmuxLauncher()
    launcher._session_created = True
    launcher._base_dir = tmp_path
    launcher._pane_ids = {0: "%7"}
    launcher._pane_owner_tokens = {0: "owner-token"}
    launcher._launch_commands = {0: " command"}
    monkeypatch.setattr(
        "xahaud_scripts.testnet.launcher.tmux.subprocess.run",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, 0),
    )
    monkeypatch.setattr(
        launcher,
        "_close_iterm_window",
        lambda _base_dir: (_ for _ in ()).throw(OSError("iTerm failed")),
    )

    with pytest.raises(OSError, match="iTerm failed"):
        launcher.shutdown(tmp_path, cast(Any, object()))

    assert launcher.launch_state == {
        "launcher": "tmux",
        "pane_ids": {},
        "pane_owner_tokens": {},
        "launch_commands": {},
    }
    assert launcher._session_created is False
    assert launcher._base_dir is None


def test_load_network_info_restores_tmux_base_dir_for_exit_status(tmp_path: Path):
    launcher = TmuxLauncher()
    net = _network(tmp_path, launcher)
    node = _node(tmp_path, 0)
    node.node_dir.mkdir(parents=True)
    (node.node_dir / ".pid").write_text("generation 123\n")
    (node.node_dir / ".exit_status").write_text("generation 123 0\n")
    (tmp_path / "network.json").write_text(
        json.dumps(
            {
                "network_id": net.config.network_id,
                "node_count": 1,
                "validators": 1,
                "fixed_peers": True,
                "base_port_peer": 21235,
                "base_port_rpc": 5005,
                "base_port_ws": 6005,
                "nodes": [
                    {
                        "id": 0,
                        "public_key": node.public_key,
                        "token": node.token,
                        "config": str(node.config_path),
                        "port_peer": node.port_peer,
                        "port_rpc": node.port_rpc,
                        "port_ws": node.port_ws,
                    }
                ],
                "launch_state": {
                    "launcher": "tmux",
                    "pane_ids": {"0": "%0"},
                    "launch_commands": {"0": "command"},
                },
            }
        )
    )

    net._load_network_info()

    assert launcher.get_exit_status(0) == 0


def test_launch_env_labels_runtime_config_branch_support(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    network = _network(tmp_path, _NoBuilderLauncher())
    config = LaunchConfig(
        xahaud_root=tmp_path,
        rippled_path=tmp_path / "build" / "rippled",
        genesis_file=tmp_path / "genesis.json",
        extra_env={
            "XAHAUD_RUNTIME_TEST_CONFIG": (
                '{"set":{"global":{"bootstrap_fast_start":true}}}'
            )
        },
    )

    with caplog.at_level(logging.INFO, logger="xahaud_scripts.testnet.network"):
        network._dump_launch_env(config)

    assert "feature-export-rng branches only; inert elsewhere" in caplog.text


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

    with pytest.raises(RuntimeError, match="Failed to dispatch stop for n1"):
        asyncio.run(ctx.rotate_validator_manifest(1))

    assert network.events == ["rotate:1", "stop:1"]


def test_scenario_restart_start_dispatch_failure_is_fatal():
    network = _RestartScenarioNetwork(start_success=False)
    ctx = ScenarioContext(cast(Any, network))

    with pytest.raises(RuntimeError, match="Failed to dispatch start for n2"):
        asyncio.run(ctx.restart_node(2))

    assert network.events == ["stop:2", "exit-status:2", "start:2"]


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
