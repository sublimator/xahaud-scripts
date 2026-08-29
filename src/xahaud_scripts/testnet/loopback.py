"""Loopback alias checks for the localhost peer mesh.

Every node is dialed at a distinct ``127.0.0.<id+1>`` (see ``NodeInfo.peer_host``)
because rippled's peerfinder dedups peers by IP address, ignoring port — reuse
``127.0.0.1`` and the mesh collapses to ~1 peer. Distinct addresses get that
behavior from a stock binary, at the cost of one-time setup: macOS needs the
``127.0.0.2+`` aliases created, while Linux already routes all of ``127/8``.

A missing alias fails *silently and late*: the ``connect`` RPC still reports
success (it only schedules an attempt), the TCP connect then fails, and the only
symptom is a peer edge that never forms. So check up front and raise something
that names the exact fix.

Contract, stated explicitly because both halves matter:

* **Fail closed on a definite miss.** If the probe works and an address is
  absent, dialing raises.
* **Fail OPEN on an unusable probe.** If ``ifconfig`` cannot be run or parsed we
  warn and allow the operation. That deliberately forfeits the guarantee in
  such an environment rather than blocking work on a check we cannot perform —
  the pre-existing silent-failure mode is what you get back.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Iterable

from xahaud_scripts.utils.logging import make_logger

logger = make_logger(__name__)

# 127.0.0.1 always exists; it is the base address, not an alias.
BASE_LOOPBACK = "127.0.0.1"

# Aliases can be added *or removed* while a long suite runs, so a positive
# result is only trusted briefly. The window just has to be short enough that a
# stale answer cannot outlive a test, while still collapsing the burst of checks
# a single topology apply produces into one probe.
CACHE_TTL_SECONDS = 5.0

# (probed_at_monotonic, addresses_or_None)
_cached: tuple[float, set[str] | None] | None = None
_probe_warned = False


class LoopbackAliasError(RuntimeError):
    """Raised when a peer address needs a loopback alias that is not up."""


def reset_cache() -> None:
    """Forget the probed alias set (tests, and after creating aliases)."""
    global _cached, _probe_warned
    _cached = None
    _probe_warned = False


def alias_for(node_id: int) -> str:
    """The loopback address node ``node_id`` is dialed at."""
    return f"127.0.0.{node_id + 1}"


def _probe_loopback_addresses() -> set[str] | None:
    """Parse ``ifconfig lo0`` for configured inet addresses.

    Returns None when the probe itself failed, which is deliberately distinct
    from "probed, found nothing" — we never hard-block on an unusable probe.
    """
    try:
        out = subprocess.run(
            ["ifconfig", "lo0"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as e:
        global _probe_warned
        if not _probe_warned:
            logger.warning(
                "Could not verify loopback aliases via ifconfig (%s); if the peer "
                "mesh fails to form, run: x-testnet setup-aliases",
                e,
            )
            _probe_warned = True
        return None

    return {
        line.split()[1] for line in out.splitlines() if line.strip().startswith("inet ")
    }


def present_loopback_addresses(*, refresh: bool = False) -> set[str] | None:
    """Configured loopback addresses, cached for ``CACHE_TTL_SECONDS``.

    The TTL applies to a *successful* probe too. Caching a positive result for
    the life of the process would keep certifying an alias after it was torn
    down mid-run, which is the same silent failure this module exists to stop.
    A failed probe is cached for the same window so an unusable ``ifconfig``
    does not get shelled out once per dial.
    """
    global _cached
    now = time.monotonic()
    if refresh or _cached is None or (now - _cached[0]) >= CACHE_TTL_SECONDS:
        _cached = (now, _probe_loopback_addresses())
    return _cached[1]


def _needs_alias(host: str) -> bool:
    """Whether ``host`` is a loopback address that requires an alias."""
    return host != BASE_LOOPBACK and host.startswith("127.")


def aliases_required() -> bool:
    """Whether this platform needs loopback aliases created explicitly.

    Linux routes all of 127/8 to loopback already; on anything that is not
    macOS we have no reliable probe, so we do not pretend to know.
    """
    return sys.platform == "darwin"


def missing_loopback_aliases(hosts: Iterable[str]) -> list[str]:
    """Which of ``hosts`` need an alias that is not currently up."""
    if not aliases_required():
        return []

    wanted = sorted({h for h in hosts if _needs_alias(h)})
    if not wanted:
        return []

    present = present_loopback_addresses()
    if present is None:
        return []

    missing = [h for h in wanted if h not in present]
    if not missing:
        return []

    # The set is cached for the process, so an alias created since the first
    # probe would otherwise read as missing forever. Re-probe before failing.
    present = present_loopback_addresses(refresh=True)
    if present is None:
        return []
    return [h for h in wanted if h not in present]


def format_alias_fix(missing: Iterable[str], *, node_count: int | None = None) -> str:
    """Actionable remedy text naming the exact aliases and commands."""
    missing = sorted(set(missing))
    lines = [
        "Missing macOS loopback alias(es) for peer addressing: " + ", ".join(missing),
        "",
        "  Fix (one command):",
    ]
    if node_count is not None:
        lines.append(f"    x-testnet setup-aliases -n {node_count}")
    else:
        lines.append("    x-testnet setup-aliases -n <node-count>")
    lines += ["", "  Or create them individually:"]
    lines += [f"    sudo ifconfig lo0 alias {host} up" for host in missing]
    return "\n".join(lines)


def require_loopback_hosts(
    hosts: Iterable[str],
    *,
    context: str,
    node_count: int | None = None,
) -> None:
    """Raise if any of ``hosts`` needs a loopback alias that is not up.

    Args:
        hosts: Peer addresses about to be dialed or written into a config.
        context: What needed them, e.g. ``"n0->n2 connect"``.
        node_count: Network size, so the remedy can name the exact command.

    Raises:
        LoopbackAliasError: with the missing aliases and how to create them.
    """
    missing = missing_loopback_aliases(hosts)
    if not missing:
        return
    raise LoopbackAliasError(
        f"{context} needs loopback address(es) that are not configured.\n\n"
        + format_alias_fix(missing, node_count=node_count)
    )
