"""Fetch and normalize amendment status from a network's public RPC.

xahaud's ``doServerDefinitions`` calls the amendment table with ``isAdmin =
true`` hardcoded (xahaud:src/xrpld/rpc/handlers/ServerDefinitions.cpp:542), so
an anonymous ``server_definitions`` call returns the full table:

    result.features = {
      "<AMENDMENT_HASH>": {
        "name": "NamedHooks", "supported": true, "enabled": false,
        "vetoed": true,            # bool, or "Obsolete"
        "count": 0, "validations": 4, "threshold": 3,   # vote tallies (opt)
        "majority": <closeTime>    # reached majority, in the hold (opt)
      }, ...
    }

``enabled == true`` means the amendment is live on that network's ledger.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

# Ripple epoch (2000-01-01 UTC) in unix seconds; amendment majority close times
# are expressed in it.
RIPPLE_EPOCH = 946684800

# How long an amendment must hold a majority before validators enable it.
# Xahau's compiled default is five days, not the two weeks XRPL uses:
#   xahaud:include/xrpl/protocol/SystemParameters.h:81
#     defaultAmendmentMajorityTime = std::chrono::days{5};
# and it is applied as `(*majorityTime + majorityTime_) <= closeTime`
# (xahaud:src/xrpld/app/misc/detail/AmendmentTable.cpp:916).
#
# A node may override it with `amendment_majority_time` in its config, so an
# ETA computed here is the default-configuration answer rather than a promise.
AMENDMENT_HOLD = timedelta(days=5)

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
        """Coarse status bucket for tables/coloring.

        Majority outranks veto deliberately. A majority is a ledger fact — the
        amendment activates on a known date whether or not the node you asked
        votes for it — while ``vetoed`` is that node's own configuration.
        Reporting "vetoed" for an amendment that is days from activating
        hides the only part a reader can act on.
        """
        if self.enabled:
            return STATUS_ENABLED
        if self.is_obsolete:
            return STATUS_OBSOLETE
        if self.majority is not None:
            return STATUS_MAJORITY
        if self.is_vetoed:
            return STATUS_VETOED
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
        """When a majority amendment activates: majority close-time + the hold.

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
                else "majority reached (5d hold)"
            )
        if not self.supported:
            bits.append("unsupported-by-node")
        return "  ".join(bits)


@dataclass
class NetworkAmendments:
    """Amendment state for one network, aggregated across one or more samples.

    ``enabled`` and ``majority`` are both ledger properties — every synced node
    agrees on them — so a disagreement across samples means a backend is out of
    sync, and each is tracked separately because they mean different things.
    ``vetoed``/``count``/``validations``/``threshold`` are the queried node's
    own view and vary legitimately on a load-balanced endpoint; that is
    ``nodeview_varied``. All three are surfaced from --samples.
    """

    amendments: list[Amendment]
    ledger_seq: int | None
    # Distinct backend nodes / builds seen across samples (load-balancing).
    nodes: list[str] = field(default_factory=list)
    builds: list[str] = field(default_factory=list)
    samples: int = 1
    network_id: int | None = None
    # When the reading was taken. A manifest generated from this is only as
    # current as its timestamp, and a consumer cannot tell staleness without it.
    queried_at: str | None = None
    # Amendment names whose `enabled` disagreed across samples — a real problem
    # (an out-of-sync / amendment-blocked backend), not just node-local opinion.
    enabled_unstable: set[str] = field(default_factory=set)
    # Names whose `majority` disagreed. Also a ledger property, so also a
    # backend being out of step — but it says nothing about whether the
    # amendment is live, and a consumer that conflates the two turns an
    # amendment days away from activating into a requirement today.
    majority_unstable: set[str] = field(default_factory=set)
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
    network_id: int | None = None


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
    # name.lower() alone ties for names differing only in case; the hash
    # breaks it, so the order is total and a manifest is reproducible.
    out.sort(key=lambda a: (a.name.lower(), a.hash))
    return out


def _node_identity(
    url: str, timeout: float
) -> tuple[str | None, str | None, int | None, int | None]:
    """Return (pubkey_node, build_version, validated_seq, network_id)."""
    try:
        info = _rpc(url, "server_info", timeout).get("info") or {}
    except (requests.RequestException, ValueError):
        return None, None, None, None
    raw_seq = (info.get("validated_ledger") or {}).get("seq")
    seq = int(raw_seq) if isinstance(raw_seq, int) else None
    raw_net = info.get("network_id")
    net_id = int(raw_net) if isinstance(raw_net, int) else None
    return info.get("pubkey_node"), info.get("build_version"), seq, net_id


def _aggregate(samples: list[_Sample]) -> NetworkAmendments:
    """Fold N samples into one view, recording enabled/node-local variance.

    The representative ``enabled`` per amendment is the most common across
    samples. Three kinds of disagreement are recorded separately, because a
    consumer acts on them differently:

    * ``enabled_unstable`` — the samples disagreed about whether it is live.
    * ``majority_unstable`` — they disagreed about its majority close time.
      Also a ledger property, so also a backend out of step, but it is not
      evidence the amendment is live.
    * ``nodeview_varied`` — veto/vote fields differed, which is ordinary
      node-local opinion on a load-balanced endpoint.
    """
    # Keyed by hash, which is what identifies an amendment. The name is a label
    # the binary attaches to it, and an amendment the queried build has never
    # heard of has no name at all — keying by name would fold two of those into
    # one row and drop whichever was read second.
    enabled_vals: dict[str, list[bool]] = defaultdict(list)
    majority_vals: dict[str, list[Any]] = defaultdict(list)
    nodeview_seen: dict[str, set[tuple[Any, ...]]] = defaultdict(set)
    rep: dict[str, Amendment] = {}
    for sample in samples:
        for a in sample.amendments:
            rep.setdefault(a.hash, a)
            enabled_vals[a.hash].append(a.enabled)
            # `majority` is a ledger property like `enabled`, so a
            # disagreement about it means a backend is out of sync — not the
            # node-local opinion that nodeview_varied is for.
            nodeview_seen[a.hash].add(
                (a.vetoed, a.count, a.validations, a.threshold)
            )
            majority_vals[a.hash].append(a.majority)

    # replace() rather than assignment: `rep` holds references to the first
    # sample's objects, and mutating them makes that sample silently disagree
    # with what it actually read.
    merged = {
        h: replace(a, enabled=Counter(enabled_vals[h]).most_common(1)[0][0])
        for h, a in rep.items()
    }

    def _names(varied: dict[str, Any]) -> set[str]:
        """Report the disagreements by name — that is what consumers match on."""
        return {rep[h].name for h, v in varied.items()
                if len(v if isinstance(v, set) else set(v)) > 1}

    return NetworkAmendments(
        amendments=sorted(merged.values(), key=lambda a: (a.name.lower(), a.hash)),
        ledger_seq=samples[-1].ledger_seq,
        network_id=next((s.network_id for s in samples if s.network_id), None),
        queried_at=datetime.now(UTC).isoformat(timespec="seconds"),
        nodes=list(dict.fromkeys(s.node for s in samples if s.node)),
        builds=list(dict.fromkeys(s.build for s in samples if s.build)),
        samples=len(samples),
        enabled_unstable=_names(enabled_vals),
        majority_unstable=_names(majority_vals),
        nodeview_varied=_names(nodeview_seen),
    )


def as_manifest(url: str, data: NetworkAmendments) -> dict[str, Any]:
    """One network's entry in a ``--json`` manifest.

    A manifest, not just a dump: a consumer pinning its behaviour to this needs
    to know which network it describes, when it was true, and which ledger it
    was read at — otherwise it cannot tell a current answer from a year-old
    one. ``enabled`` is broken out because it is the only field that is network
    truth, and the only one worth depending on.

    Names are exactly what the network reports. A consumer with its own symbol
    convention normalises on import; this file does not know or care what
    anyone calls these downstream.
    """
    return {
        "url": url,
        "network_id": data.network_id,
        "queried_at": data.queried_at,
        "ledger_seq": data.ledger_seq,
        "samples": data.samples,
        "backend_nodes": sorted(data.nodes),
        "builds": sorted(data.builds),
        "enabled_unstable": sorted(data.enabled_unstable),
        "majority_unstable": sorted(data.majority_unstable),
        "nodeview_varied": sorted(data.nodeview_varied),
        "enabled": sorted(a.name for a in data.amendments if a.enabled),
        "amendments": [vars(a) for a in data.amendments],
    }


def manifest_bytes(raw: dict[str, Any]) -> str:
    """Canonical text for a manifest.

    Sorted keys throughout and a trailing newline, so a regenerated manifest
    diffs only where the network actually changed. ``queried_at`` and
    ``ledger_seq`` necessarily move every run — they are the provenance, and a
    manifest that hid them to keep the bytes still would be worse.
    """
    import json

    return json.dumps(raw, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def fetch_sampled(
    url: str, timeout: float, samples: int = 1, *, want_seq: bool = True
) -> NetworkAmendments:
    """Read amendments ``samples`` times, cross-referencing across backends.

    With samples > 1 a load-balanced endpoint will route to different nodes;
    aggregation then reveals which fields are network-truth vs node-local.
    Node identity is collected when ``want_seq`` is set or samples > 1.

    Caveat worth knowing before trusting the attribution: identity comes from a
    *second* call (``server_info``), and a load balancer is free to route it to
    a different backend than answered ``server_definitions``. So the node/build
    recorded against a sample is best-effort, and the set of nodes seen is more
    reliable than any particular pairing. JSON-RPC offers no way to pin a
    backend across calls, so this is a limit of the transport rather than
    something to fix here.
    """
    samples = max(1, samples)
    readings: list[_Sample] = []
    for _ in range(samples):
        features = _server_definition_features(_rpc(url, "server_definitions", timeout))
        if want_seq or samples > 1:
            node, build, seq, net_id = _node_identity(url, timeout)
        else:
            node, build, seq, net_id = None, None, None, None
        readings.append(_Sample(normalize(features), node, build, seq, net_id))
    return _aggregate(readings)


def fetch(url: str, timeout: float, *, want_seq: bool = True) -> NetworkAmendments:
    """Single-sample fetch (back-compat shim over fetch_sampled)."""
    return fetch_sampled(url, timeout, samples=1, want_seq=want_seq)
