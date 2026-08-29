"""Regression tests for optional network-monitor diagnostics."""

from __future__ import annotations

import pytest

from xahaud_scripts.testnet.monitor import _get_rippled_cpu


def test_rippled_cpu_is_empty_when_ps_cannot_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_ps(*_args: object, **_kwargs: object) -> str:
        raise PermissionError("ps is unavailable")

    monkeypatch.setattr(
        "xahaud_scripts.testnet.monitor.subprocess.check_output", deny_ps
    )

    assert _get_rippled_cpu() == {}
