"""Tests for testnet runtime-config helpers."""

from __future__ import annotations

import json
from pathlib import Path

from xahaud_scripts.testnet.cli_handlers.rc import (
    build_runtime_config_envs,
    parse_rc_spec,
)
from xahaud_scripts.testnet.config import NodeInfo
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
    spec = parse_rc_spec("n0@n1:delay=700,msg=candidate_set_fetch")

    assert spec.node_id == 0
    assert spec.peer_id == 1
    assert spec.delay == 700
    assert spec.msg == ["candidate_set_fetch"]


def test_build_runtime_config_envs_resolves_directed_peer() -> None:
    specs = [parse_rc_spec("n0@n1:delay=700,msg=candidate_set_fetch")]

    envs = build_runtime_config_envs(specs, _nodes(Path("/tmp/xahaud-test")))

    assert set(envs) == {0}
    assert json.loads(envs[0]) == {
        "127.0.0.1:5006": {
            "send_delay_ms": 700,
            "message_types": ["candidate_set_fetch"],
        }
    }


def test_suite_launch_config_applies_rc_specs(tmp_path: Path) -> None:
    launch = _build_launch_config(
        tmp_path,
        {
            "rc": ["n0@n1:delay=700,msg=proposal"],
            "node_env": {"1": {"EXISTING": "1"}},
        },
        nodes=_nodes(tmp_path),
    )

    assert "XAHAU_RUNTIME_CONFIG" in launch.node_env[0]
    assert json.loads(launch.node_env[0]["XAHAU_RUNTIME_CONFIG"]) == {
        "127.0.0.1:5006": {
            "send_delay_ms": 700,
            "message_types": ["proposal"],
        }
    }
    assert launch.node_env[1]["EXISTING"] == "1"
