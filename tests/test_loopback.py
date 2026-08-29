"""Tests for the loopback-alias precondition helper."""

from __future__ import annotations

import pytest

from xahaud_scripts.testnet import loopback
from xahaud_scripts.testnet.loopback import (
    LoopbackAliasError,
    alias_for,
    format_alias_fix,
    missing_loopback_aliases,
    require_loopback_hosts,
)


@pytest.fixture
def on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loopback, "aliases_required", lambda: True)


def _probe(*addresses: str):
    return lambda: set(addresses)


def test_alias_for_maps_node_id_to_distinct_address():
    assert alias_for(0) == "127.0.0.1"
    assert alias_for(1) == "127.0.0.2"
    assert alias_for(6) == "127.0.0.7"


def test_base_loopback_never_needs_an_alias(on_macos, monkeypatch):
    monkeypatch.setattr(loopback, "_probe_loopback_addresses", _probe("127.0.0.1"))
    loopback.reset_cache()
    assert missing_loopback_aliases(["127.0.0.1"]) == []


def test_missing_aliases_are_reported_sorted_and_deduped(on_macos, monkeypatch):
    monkeypatch.setattr(
        loopback, "_probe_loopback_addresses", _probe("127.0.0.1", "127.0.0.3")
    )
    loopback.reset_cache()
    missing = missing_loopback_aliases(
        ["127.0.0.4", "127.0.0.2", "127.0.0.3", "127.0.0.2"]
    )
    assert missing == ["127.0.0.2", "127.0.0.4"]


def test_no_enforcement_off_macos(monkeypatch: pytest.MonkeyPatch):
    """Linux routes all of 127/8; elsewhere we have no reliable probe."""
    monkeypatch.setattr(loopback, "aliases_required", lambda: False)
    monkeypatch.setattr(loopback, "_probe_loopback_addresses", _probe("127.0.0.1"))
    loopback.reset_cache()
    assert missing_loopback_aliases(["127.0.0.2"]) == []
    require_loopback_hosts(["127.0.0.2"], context="anything")


def test_unusable_probe_does_not_block(on_macos, monkeypatch):
    """If ifconfig can't be read we warn elsewhere, but never hard-block."""
    monkeypatch.setattr(loopback, "_probe_loopback_addresses", lambda: None)
    loopback.reset_cache()
    assert missing_loopback_aliases(["127.0.0.2"]) == []


def test_cache_is_refreshed_before_declaring_a_miss(on_macos, monkeypatch):
    """An alias created after the first probe must not read as missing forever.

    A long suite run probes once and caches; if the developer creates the alias
    mid-run, a stale cache would fail every later dial for no reason.
    """
    calls: list[int] = []

    def probe() -> set[str]:
        calls.append(1)
        # Absent on the first probe, present on the refresh.
        return {"127.0.0.1"} if len(calls) == 1 else {"127.0.0.1", "127.0.0.2"}

    monkeypatch.setattr(loopback, "_probe_loopback_addresses", probe)
    loopback.reset_cache()

    assert missing_loopback_aliases(["127.0.0.2"]) == []
    assert len(calls) == 2


def test_require_loopback_hosts_error_names_context_and_both_remedies(
    on_macos, monkeypatch
):
    monkeypatch.setattr(loopback, "_probe_loopback_addresses", _probe("127.0.0.1"))
    loopback.reset_cache()

    with pytest.raises(LoopbackAliasError) as exc:
        require_loopback_hosts(
            ["127.0.0.2", "127.0.0.3"], context="n0->n1 connect", node_count=3
        )

    message = str(exc.value)
    assert "n0->n1 connect" in message
    assert "x-testnet setup-aliases -n 3" in message
    assert "sudo ifconfig lo0 alias 127.0.0.2 up" in message
    assert "sudo ifconfig lo0 alias 127.0.0.3 up" in message


def test_format_alias_fix_without_node_count_is_still_actionable():
    text = format_alias_fix(["127.0.0.2"])
    assert "x-testnet setup-aliases -n <node-count>" in text
    assert "sudo ifconfig lo0 alias 127.0.0.2 up" in text


def test_positive_cache_expires_so_a_removed_alias_is_noticed(on_macos, monkeypatch):
    """A cached hit must not certify an alias after it is torn down.

    Caching a positive result for the process lifetime kept reporting success
    after an alias was removed mid-run — the same silent failure this module
    exists to prevent, just relocated.
    """
    present = {"127.0.0.1", "127.0.0.2"}
    monkeypatch.setattr(loopback, "_probe_loopback_addresses", lambda: set(present))
    loopback.reset_cache()

    assert missing_loopback_aliases(["127.0.0.2"]) == []

    present.discard("127.0.0.2")
    # Still inside the TTL: the cached answer is deliberately reused.
    assert missing_loopback_aliases(["127.0.0.2"]) == []

    # Past the TTL, the removal is noticed.
    clock = [0.0]
    monkeypatch.setattr(loopback.time, "monotonic", lambda: clock[0])
    loopback.reset_cache()
    assert missing_loopback_aliases(["127.0.0.2"]) == ["127.0.0.2"]
    present.add("127.0.0.2")
    clock[0] += loopback.CACHE_TTL_SECONDS + 1
    assert missing_loopback_aliases(["127.0.0.2"]) == []


def test_failed_probe_is_cached_so_it_is_not_reshelled_per_dial(on_macos, monkeypatch):
    """An unusable ifconfig must not be shelled out once per dial."""
    calls: list[int] = []

    def probe():
        calls.append(1)
        return None

    monkeypatch.setattr(loopback, "_probe_loopback_addresses", probe)
    loopback.reset_cache()

    for _ in range(5):
        assert missing_loopback_aliases(["127.0.0.2"]) == []

    assert len(calls) == 1
