"""Regression tests for optional network-monitor diagnostics."""

from __future__ import annotations

import pytest

from xahaud_scripts.testnet.config import NetworkConfig
from xahaud_scripts.testnet.monitor import NetworkMonitor, _get_rippled_cpu


def test_rippled_cpu_is_empty_when_ps_cannot_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_ps(*_args: object, **_kwargs: object) -> str:
        raise PermissionError("ps is unavailable")

    monkeypatch.setattr(
        "xahaud_scripts.testnet.monitor.subprocess.check_output", deny_ps
    )

    assert _get_rippled_cpu() == {}


def test_rippled_cpu_does_not_invoke_ps_in_ai_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_ps(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("ps must not run in AI sandbox mode")

    monkeypatch.setattr(
        "xahaud_scripts.testnet.monitor.subprocess.check_output", unexpected_ps
    )

    assert _get_rippled_cpu(ai_sandboxed=True) == {}


def test_network_monitor_defaults_ai_sandbox_mode_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_SANDBOXED", "yes")

    monitor = NetworkMonitor(
        rpc_client=object(),  # type: ignore[arg-type]
        network_config=NetworkConfig(node_count=1),
    )

    assert monitor._ai_sandboxed is True


def test_network_monitor_explicit_mode_overrides_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_SANDBOXED", "1")

    monitor = NetworkMonitor(
        rpc_client=object(),  # type: ignore[arg-type]
        network_config=NetworkConfig(node_count=1),
        ai_sandboxed=False,
    )

    assert monitor._ai_sandboxed is False
