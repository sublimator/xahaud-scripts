"""Tests for testnet runtime-config helpers."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import click
import pytest

from xahaud_scripts.testnet.cli_handlers.rc import (
    build_runtime_config_envs,
    parse_rc_spec,
)
from xahaud_scripts.testnet.config import LaunchConfig, NetworkConfig, NodeInfo
from xahaud_scripts.testnet.launcher.iterm import ITermLauncher
from xahaud_scripts.testnet.launcher.iterm_panes import ITermPanesLauncher
from xahaud_scripts.testnet.launcher.tmux import TmuxLauncher
from xahaud_scripts.testnet.suite import _build_launch_config


def _nodes(tmp_path: Path) -> list[NodeInfo]:
    return [
        NodeInfo(
            id=0,
            public_key="pk0",
            token="token0",
            config_path=tmp_path / "n0" / "xahaud.cfg",
            port_peer=5005,
            port_rpc=6005,
            port_ws=7005,
        ),
        NodeInfo(
            id=1,
            public_key="pk1",
            token="token1",
            config_path=tmp_path / "n1" / "xahaud.cfg",
            port_peer=5006,
            port_rpc=6006,
            port_ws=7006,
        ),
    ]


def test_rc_parser_accepts_candidate_set_alias() -> None:
    spec = parse_rc_spec("n0->n1:delay=700,msg=candidate_set_fetch")

    assert spec.node_id == 0
    assert spec.peer_id == 1
    assert spec.delay == 700
    assert spec.msg == ["candidate_set_fetch"]


def test_rc_parser_rejects_old_at_directed_peer_syntax() -> None:
    with pytest.raises(click.BadParameter, match="Use n0, n0->n2"):
        parse_rc_spec("n0@n1:delay=700,msg=candidate_set_fetch")


def test_build_runtime_config_envs_resolves_directed_peer() -> None:
    specs = [parse_rc_spec("n0->n1:delay=700,msg=candidate_set_fetch")]

    envs = build_runtime_config_envs(specs, _nodes(Path("/tmp/xahaud-test")))

    assert set(envs) == {0}
    assert json.loads(envs[0]) == {
        "set": {
            "peer:127.0.0.1:5006": {
                "send_delay_ms": 700,
                "message_types": ["candidate_set_fetch"],
            }
        }
    }


def test_build_runtime_config_envs_splits_peer_and_global_scopes() -> None:
    specs = [parse_rc_spec("delay=700,rngdrop=25,rngrevealdrop=50,msg=proposal")]

    envs = build_runtime_config_envs(specs, _nodes(Path("/tmp/xahaud-test")))

    assert set(envs) == {0, 1}
    assert json.loads(envs[0]) == {
        "set": {
            "global": {
                "rng_claim_drop_pct": 25.0,
                "rng_reveal_drop_pct": 50.0,
            },
            "peer_defaults": {
                "send_delay_ms": 700,
                "message_types": ["proposal"],
            },
        }
    }


def test_rc_parser_rejects_peer_scoped_rng_reveal_drop() -> None:
    with pytest.raises(click.BadParameter, match="rngrevealdrop is node-scoped"):
        parse_rc_spec("n0->n1:rngrevealdrop=100")


def test_build_runtime_config_envs_accepts_export_global_knobs() -> None:
    specs = [
        parse_rc_spec(
            "n1:rng_poll_ms=333,bootstrap_fast_start=true,"
            "no_export_sig=true,no_export_sig_hash=false"
        )
    ]

    envs = build_runtime_config_envs(specs, _nodes(Path("/tmp/xahaud-test")))

    assert set(envs) == {1}
    assert json.loads(envs[1]) == {
        "set": {
            "global": {
                "rng_poll_ms": 333,
                "bootstrap_fast_start": True,
                "no_export_sig": True,
                "no_export_sig_hash": False,
            }
        }
    }


def test_suite_launch_config_uses_network_config_for_unl_report_seed(
    tmp_path: Path,
) -> None:
    nodes = [
        NodeInfo(
            id=i,
            public_key=f"02{i + 1:064X}",
            token=f"token{i}",
            config_path=tmp_path / f"n{i}" / "xahaud.cfg",
            port_peer=5005 + i,
            port_rpc=6005 + i,
            port_ws=7005 + i,
        )
        for i in range(4)
    ]

    launch = _build_launch_config(
        tmp_path,
        {"node_count": 4, "unl_report": True},
        nodes=nodes,
        network_config=NetworkConfig(node_count=4, validators=2),
    )

    genesis = json.loads(launch.genesis_file.read_text())
    reports = [
        e
        for e in genesis["ledger"]["accountState"]
        if e.get("LedgerEntryType") == "UNLReport"
    ]
    assert len(reports) == 1
    active_keys = [
        entry["ActiveValidator"]["PublicKey"]
        for entry in reports[0]["ActiveValidators"]
    ]
    assert active_keys == [nodes[0].public_key, nodes[1].public_key]


def test_suite_launch_config_rejects_unl_report_validator_count_mismatch(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="unl_report requires 3"):
        _build_launch_config(
            tmp_path,
            {"node_count": 3, "unl_report": True},
            nodes=_nodes(tmp_path),
            network_config=NetworkConfig(node_count=3, validators=3),
        )


def test_suite_launch_config_applies_rc_specs(tmp_path: Path) -> None:
    launch = _build_launch_config(
        tmp_path,
        {
            "rc": ["n0->n1:delay=700,msg=proposal"],
            "node_env": {"1": {"EXISTING": "1"}},
        },
        nodes=_nodes(tmp_path),
    )

    assert "XAHAUD_RUNTIME_TEST_CONFIG" in launch.node_env[0]
    assert json.loads(launch.node_env[0]["XAHAUD_RUNTIME_TEST_CONFIG"]) == {
        "set": {
            "peer:127.0.0.1:5006": {
                "send_delay_ms": 700,
                "message_types": ["proposal"],
            }
        }
    }
    assert launch.node_env[1]["EXISTING"] == "1"


def test_suite_launch_config_merges_rc_with_existing_runtime_config(
    tmp_path: Path,
) -> None:
    launch = _build_launch_config(
        tmp_path,
        {
            "env": {
                "XAHAUD_RUNTIME_TEST_CONFIG": (
                    '{"set":{"global":{"bootstrap_fast_start":true}}}'
                )
            },
            "rc": ["n0->n1:delay=700,msg=proposal"],
        },
        nodes=_nodes(tmp_path),
    )

    assert json.loads(launch.node_env[0]["XAHAUD_RUNTIME_TEST_CONFIG"]) == {
        "set": {
            "global": {"bootstrap_fast_start": True},
            "peer:127.0.0.1:5006": {
                "send_delay_ms": 700,
                "message_types": ["proposal"],
            },
        }
    }


@pytest.mark.parametrize(
    "launcher_cls",
    [TmuxLauncher, ITermLauncher, ITermPanesLauncher],
)
def test_launchers_shell_quote_json_env_values(tmp_path: Path, launcher_cls) -> None:
    runtime_json = '{"set":{"global":{"rng_poll_ms":333}}}'
    node_json = '{"set":{"global":{"no_export_sig_hash":true}}}'
    node = _nodes(tmp_path)[0]
    launch = LaunchConfig(
        xahaud_root=tmp_path,
        rippled_path=tmp_path / "build" / "rippled",
        genesis_file=tmp_path / "genesis.json",
        extra_env={"XAHAUD_RUNTIME_TEST_CONFIG": runtime_json},
        node_env={0: {"NODE_JSON": node_json}},
    )

    env_vars = launcher_cls()._build_env_vars(node, launch)
    output = subprocess.check_output(
        [
            "sh",
            "-c",
            (
                f"{env_vars} && "
                'printf "%s\\n%s\\n" "$XAHAUD_RUNTIME_TEST_CONFIG" "$NODE_JSON"'
            ),
        ],
        text=True,
    ).splitlines()

    assert json.loads(output[0]) == {"set": {"global": {"rng_poll_ms": 333}}}
    assert json.loads(output[1]) == {"set": {"global": {"no_export_sig_hash": True}}}


def test_tmux_launcher_quotes_binary_config_and_genesis_paths(tmp_path: Path) -> None:
    node = NodeInfo(
        id=0,
        public_key="pk",
        token="token",
        config_path=tmp_path / "node dir; touch bad" / "xahaud.cfg",
        port_peer=5005,
        port_rpc=6005,
        port_ws=7005,
    )
    launch = LaunchConfig(
        xahaud_root=tmp_path,
        rippled_path=tmp_path / "bin dir; touch bad" / "rippled",
        genesis_file=tmp_path / "genesis dir; touch bad" / "genesis.json",
        extra_env={
            "XAHAUD_RUNTIME_TEST_CONFIG": '{"set":{"global":{"rng_poll_ms":333}}}'
        },
    )

    cmd = TmuxLauncher()._build_full_command(node, launch)
    output = subprocess.check_output(
        [
            "sh",
            "-c",
            "_xrun() { printf '%s\\n' \"$@\"; }; " + cmd,
        ],
        text=True,
    ).splitlines()

    assert output[:5] == [
        str(launch.rippled_path),
        "--conf",
        str(node.config_path),
        "--ledgerfile",
        str(launch.genesis_file),
    ]


@pytest.mark.parametrize(
    "launcher_cls,module_name",
    [
        (ITermLauncher, "xahaud_scripts.testnet.launcher.iterm"),
        (ITermPanesLauncher, "xahaud_scripts.testnet.launcher.iterm_panes"),
    ],
)
def test_iterm_launchers_quote_binary_config_and_genesis_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launcher_cls,
    module_name: str,
) -> None:
    node = NodeInfo(
        id=0,
        public_key="pk",
        token="token",
        config_path=tmp_path / "node dir; touch bad" / "xahaud.cfg",
        port_peer=5005,
        port_rpc=6005,
        port_ws=7005,
    )
    launch = LaunchConfig(
        xahaud_root=tmp_path,
        rippled_path=tmp_path / "bin dir; touch bad" / "rippled",
        genesis_file=tmp_path / "genesis dir; touch bad" / "genesis.json",
        extra_env={
            "XAHAUD_RUNTIME_TEST_CONFIG": '{"set":{"global":{"rng_poll_ms":333}}}'
        },
    )
    scripts: list[str] = []

    def fake_run(args, **_kwargs):
        scripts.append(args[2])
        return subprocess.CompletedProcess(args, 0, stdout="window-id")

    monkeypatch.setattr(f"{module_name}.subprocess.run", fake_run)

    assert launcher_cls().launch(node, launch) is True
    script = scripts[-1]

    assert f'write text "cd {shlex.quote(str(node.node_dir))}"' in script
    assert shlex.quote(str(launch.rippled_path)) in script
    assert f"--conf {shlex.quote(str(node.config_path))}" in script
    assert f"--ledgerfile {shlex.quote(str(launch.genesis_file))}" in script
    assert '{"set"' not in script
    assert '{\\"set\\"' in script


@pytest.mark.parametrize(
    "launcher_cls",
    [TmuxLauncher, ITermLauncher, ITermPanesLauncher],
)
def test_launchers_reject_invalid_env_names(tmp_path: Path, launcher_cls) -> None:
    node = _nodes(tmp_path)[0]
    launch = LaunchConfig(
        xahaud_root=tmp_path,
        rippled_path=tmp_path / "build" / "rippled",
        genesis_file=tmp_path / "genesis.json",
        extra_env={"BAD-NAME": "1"},
    )

    with pytest.raises(ValueError, match="shell identifiers"):
        launcher_cls()._build_env_vars(node, launch)
