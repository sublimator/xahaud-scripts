"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from xahaud_scripts.testnet import loopback


@pytest.fixture(autouse=True)
def hermetic_loopback_aliases(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pretend every 127.0.0.x alias is up, so tests never read the real host.

    The peer-addressing guard shells out to ``ifconfig lo0``. Left unstubbed,
    every test touching a dial path would pass or fail depending on which
    aliases the developer happened to create — green on a machine that ran
    ``x-testnet setup-aliases``, red on CI or a fresh laptop. Tests that assert
    the *missing* case re-stub ``_probe_loopback_addresses`` themselves.
    """
    loopback.reset_cache()
    monkeypatch.setattr(
        loopback,
        "_probe_loopback_addresses",
        lambda: {f"127.0.0.{i}" for i in range(1, 256)},
    )
    yield
    loopback.reset_cache()
