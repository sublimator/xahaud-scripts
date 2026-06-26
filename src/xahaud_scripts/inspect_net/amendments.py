"""Fetch and normalize amendment status from a network's public RPC.

xahaud's ``doServerDefinitions`` calls the amendment table with ``isAdmin =
true`` hardcoded, so an anonymous ``server_definitions`` call returns the full
table:

    result.features = {
      "<AMENDMENT_HASH>": {
        "name": "NamedHooks", "supported": true, "enabled": false,
        "vetoed": true,            # bool, or "Obsolete"
        "count": 0, "validations": 4, "threshold": 3,   # vote tallies (opt)
        "majority": <closeTime>    # reached majority, in 2wk hold (opt)
      }, ...
    }

``enabled == true`` means the amendment is live on that network's ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

# Status buckets, ordered worst-to-best for stable rendering choices.
STATUS_ENABLED = "enabled"
STATUS_MAJORITY = "majority"
STATUS_PENDING = "pending"
STATUS_VETOED = "vetoed"
STATUS_OBSOLETE = "obsolete"
STATUS_UNSUPPORTED = "unsupported"
STATUS_ABSENT = "absent"


@dataclass
class Amendment:
    """One amendment's state on a single network."""

    hash: str
    name: str
    supported: bool
    enabled: bool
    vetoed: bool | str | None
    count: int | None
    validations: int | None
    threshold: int | None
    majority: int | str | None

    @property
    def is_obsolete(self) -> bool:
        return self.vetoed == "Obsolete"

    @property
    def is_vetoed(self) -> bool:
        return bool(self.vetoed) and not self.is_obsolete

    def status(self) -> str:
        """Coarse status bucket for tables/coloring."""
        if self.enabled:
            return STATUS_ENABLED
        if self.is_obsolete:
            return STATUS_OBSOLETE
        if self.is_vetoed:
            return STATUS_VETOED
        if self.majority is not None:
            return STATUS_MAJORITY
        if not self.supported:
            return STATUS_UNSUPPORTED
        return STATUS_PENDING

    def vote_detail(self) -> str:
        """Human-readable vote/state annotation (without the status word)."""
        bits: list[str] = []
        if self.count is not None and self.validations is not None:
            tally = f"votes {self.count}/{self.validations}"
            if self.threshold:
                tally += f" need {self.threshold}"
            bits.append(tally)
        if self.majority is not None:
            bits.append("majority reached (2wk hold)")
        if not self.supported:
            bits.append("unsupported-by-node")
        return "  ".join(bits)


@dataclass
class NetworkAmendments:
    """All amendments for one network, plus the ledger they were read at."""

    amendments: list[Amendment]
    ledger_seq: int | None

    def by_name(self) -> dict[str, Amendment]:
        return {a.name: a for a in self.amendments}


def _rpc(url: str, method: str, timeout: float) -> dict[str, Any]:
    """Make an anonymous JSON-RPC call and return its ``result`` object."""
    resp = requests.post(url, json={"method": method, "params": [{}]}, timeout=timeout)
    resp.raise_for_status()
    body = resp.json() or {}
    result = body.get("result")
    return result if isinstance(result, dict) else {}


def normalize(features: dict[str, Any]) -> list[Amendment]:
    """Flatten the hash-keyed features map into a name-sorted list."""
    out: list[Amendment] = []
    for h, v in features.items():
        out.append(
            Amendment(
                hash=h,
                name=v.get("name") or f"(unknown {h[:8]})",
                supported=bool(v.get("supported")),
                enabled=bool(v.get("enabled")),
                vetoed=v.get("vetoed"),
                count=v.get("count"),
                validations=v.get("validations"),
                threshold=v.get("threshold"),
                majority=v.get("majority"),
            )
        )
    out.sort(key=lambda a: a.name.lower())
    return out


def fetch(url: str, timeout: float, *, want_seq: bool = True) -> NetworkAmendments:
    """Fetch amendments (and optionally the validated ledger seq) from ``url``."""
    features = _rpc(url, "server_definitions", timeout).get("features") or {}
    seq: int | None = None
    if want_seq:
        try:
            info = _rpc(url, "server_info", timeout).get("info") or {}
            validated = info.get("validated_ledger") or {}
            raw_seq = validated.get("seq")
            seq = int(raw_seq) if isinstance(raw_seq, int) else None
        except (requests.RequestException, ValueError):
            seq = None
    return NetworkAmendments(amendments=normalize(features), ledger_seq=seq)
