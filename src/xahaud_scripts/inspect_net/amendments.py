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

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

# Ripple epoch (2000-01-01 UTC) in unix seconds; amendment majority close times
# are expressed in it. An amendment activates ~2 weeks after reaching majority.
RIPPLE_EPOCH = 946684800
AMENDMENT_HOLD = timedelta(weeks=2)

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

    @property
    def vote_fraction(self) -> str | None:
        """'count/validations' (yes-votes / validators) if the node reports it."""
        if self.count is None or self.validations is None:
            return None
        frac = f"{self.count}/{self.validations}"
        return f"{frac} (need {self.threshold})" if self.threshold else frac

    def activation_eta(self) -> datetime | None:
        """When a majority amendment activates: majority close-time + 2wk hold.

        Only meaningful while not yet enabled; returns None if no majority
        timestamp is present (or it isn't a numeric close time).
        """
        if not isinstance(self.majority, int):
            return None
        reached = datetime.fromtimestamp(RIPPLE_EPOCH + self.majority, tz=UTC)
        return reached + AMENDMENT_HOLD

    def vote_detail(self) -> str:
        """Human-readable vote/state annotation (without the status word)."""
        bits: list[str] = []
        if self.vote_fraction:
            bits.append(f"votes {self.vote_fraction}")
        if self.majority is not None:
            eta = self.activation_eta()
            bits.append(
                f"majority → enables ~{eta:%Y-%m-%d}"
                if eta
                else "majority reached (2wk hold)"
            )
        if not self.supported:
            bits.append("unsupported-by-node")
        return "  ".join(bits)


@dataclass
class NetworkAmendments:
    """Amendment state for one network, aggregated across one or more samples.

    Only ``enabled`` is network truth (a ledger property — every synced node
    agrees). ``vetoed``/``count``/``majority`` are the queried node's view, so
    on a load-balanced endpoint they can vary between samples. The two
    ``*_varied`` / ``*_unstable`` sets capture that, surfaced from --samples.
    """

    amendments: list[Amendment]
    ledger_seq: int | None
    # Distinct backend nodes / builds seen across samples (load-balancing).
    nodes: list[str] = field(default_factory=list)
    builds: list[str] = field(default_factory=list)
    samples: int = 1
    # Amendment names whose `enabled` disagreed across samples — a real problem
    # (an out-of-sync / amendment-blocked backend), not just node-local opinion.
    enabled_unstable: set[str] = field(default_factory=set)
    # Names whose veto/vote fields varied across samples — node-local noise.
    nodeview_varied: set[str] = field(default_factory=set)

    def by_name(self) -> dict[str, Amendment]:
        return {a.name: a for a in self.amendments}

    def enabled_of(self, name: str) -> bool | None:
        a = self.by_name().get(name)
        return a.enabled if a else None


@dataclass
class _Sample:
    """One (server_definitions + server_info) reading from a backend node."""

    amendments: list[Amendment]
    node: str | None
    build: str | None
    ledger_seq: int | None


def _rpc(url: str, method: str, timeout: float) -> dict[str, Any]:
    """Make an anonymous JSON-RPC call and return its ``result`` object."""
    resp = requests.post(url, json={"method": method, "params": [{}]}, timeout=timeout)
    resp.raise_for_status()
    body = resp.json() or {}
    result = body.get("result")
    if not isinstance(result, dict):
        raise ValueError(f"{method}: malformed JSON-RPC result")
    return result


def _server_definition_features(result: dict[str, Any]) -> dict[str, Any]:
    """Return the amendment feature map, failing closed on bad RPC shape."""
    features = result.get("features")
    if not isinstance(features, dict) or not features:
        raise ValueError("server_definitions: missing or empty features map")
    for amendment_id, entry in features.items():
        if not isinstance(amendment_id, str) or not isinstance(entry, dict):
            raise ValueError("server_definitions: malformed features map")
        if not isinstance(entry.get("enabled"), bool):
            raise ValueError(
                f"server_definitions: feature {amendment_id} missing boolean enabled"
            )
    return features


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


def _node_identity(
    url: str, timeout: float
) -> tuple[str | None, str | None, int | None]:
    """Return (pubkey_node, build_version, validated_seq) for the queried node."""
    try:
        info = _rpc(url, "server_info", timeout).get("info") or {}
    except (requests.RequestException, ValueError):
        return None, None, None
    raw_seq = (info.get("validated_ledger") or {}).get("seq")
    seq = int(raw_seq) if isinstance(raw_seq, int) else None
    return info.get("pubkey_node"), info.get("build_version"), seq


def _aggregate(samples: list[_Sample]) -> NetworkAmendments:
    """Fold N samples into one view, recording enabled/node-local variance.

    The representative ``enabled`` per amendment is the most common across
    samples; an amendment whose ``enabled`` was not unanimous is flagged in
    ``enabled_unstable`` (an out-of-sync backend), and one whose veto/vote
    fields varied is flagged in ``nodeview_varied`` (node-local noise).
    """
    enabled_vals: dict[str, list[bool]] = defaultdict(list)
    nodeview_seen: dict[str, set[tuple[Any, ...]]] = defaultdict(set)
    rep: dict[str, Amendment] = {}
    for sample in samples:
        for a in sample.amendments:
            rep.setdefault(a.name, a)
            enabled_vals[a.name].append(a.enabled)
            nodeview_seen[a.name].add(
                (a.vetoed, a.count, a.validations, a.threshold, a.majority)
            )

    for name, a in rep.items():
        a.enabled = Counter(enabled_vals[name]).most_common(1)[0][0]

    return NetworkAmendments(
        amendments=sorted(rep.values(), key=lambda a: a.name.lower()),
        ledger_seq=samples[-1].ledger_seq,
        nodes=list(dict.fromkeys(s.node for s in samples if s.node)),
        builds=list(dict.fromkeys(s.build for s in samples if s.build)),
        samples=len(samples),
        enabled_unstable={n for n, v in enabled_vals.items() if len(set(v)) > 1},
        nodeview_varied={n for n, v in nodeview_seen.items() if len(v) > 1},
    )


def fetch_sampled(
    url: str, timeout: float, samples: int = 1, *, want_seq: bool = True
) -> NetworkAmendments:
    """Read amendments ``samples`` times, cross-referencing across backends.

    With samples > 1 a load-balanced endpoint will route to different nodes;
    aggregation then reveals which fields are network-truth vs node-local.
    Node identity is collected when ``want_seq`` is set or samples > 1.
    """
    samples = max(1, samples)
    readings: list[_Sample] = []
    for _ in range(samples):
        features = _server_definition_features(_rpc(url, "server_definitions", timeout))
        if want_seq or samples > 1:
            node, build, seq = _node_identity(url, timeout)
        else:
            node, build, seq = None, None, None
        readings.append(_Sample(normalize(features), node, build, seq))
    return _aggregate(readings)


def fetch(url: str, timeout: float, *, want_seq: bool = True) -> NetworkAmendments:
    """Single-sample fetch (back-compat shim over fetch_sampled)."""
    return fetch_sampled(url, timeout, samples=1, want_seq=want_seq)
