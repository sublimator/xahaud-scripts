"""Compare visible network versions with enabled amendment requirements."""

from __future__ import annotations

import subprocess
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from xahaud_scripts.binary_features import (
    FeatureDecl,
    FeatureParser,
    RefFeatures,
    load_ref_features,
)
from xahaud_scripts.inspect_net import amendments as amd

PUBLIC_DEFINITIONS_URL = "https://xrplwin.com/server-definitions"


@dataclass(frozen=True)
class EnabledAmendment:
    """One currently-enabled network amendment requirement."""

    name: str
    amendment_id: str


@dataclass(frozen=True)
class AmendmentEvidence:
    """Why one enabled amendment made a version incompatible."""

    name: str
    amendment_id: str
    issue: str
    source_line: int | None
    evidence_url: str | None
    sampled_rpc_url: str | None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "amendment_id": self.amendment_id,
            "issue": self.issue,
            "source_line": self.source_line,
            "evidence_url": self.evidence_url,
            "sampled_rpc_url": self.sampled_rpc_url,
            "public_definitions_url": PUBLIC_DEFINITIONS_URL,
        }


@dataclass(frozen=True)
class VersionCompatibility:
    """Compatibility result for one visible software version."""

    version: str
    nodes: int
    ref: str | None
    parsed: RefFeatures | None
    missing_enabled: tuple[str, ...]
    unsupported_enabled: tuple[str, ...]
    commit: str | None = None
    source_url: str | None = None
    evidence: tuple[AmendmentEvidence, ...] = ()
    error: str | None = None

    @property
    def checked(self) -> bool:
        return self.parsed is not None and self.error is None

    @property
    def incompatible(self) -> bool:
        return bool(self.missing_enabled or self.unsupported_enabled)

    @property
    def status(self) -> str:
        if self.error:
            return "unknown"
        if self.incompatible:
            return "incompatible"
        return "ok"

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "nodes": self.nodes,
            "ref": self.ref,
            "status": self.status,
            "commit": self.commit,
            "source_path": self.parsed.source_path if self.parsed else None,
            "source_url": self.source_url,
            "missing_enabled": list(self.missing_enabled),
            "unsupported_enabled": list(self.unsupported_enabled),
            "evidence": [e.as_dict() for e in self.evidence],
            "error": self.error,
        }


def enabled_amendments(
    amendments: amd.NetworkAmendments,
) -> tuple[EnabledAmendment, ...]:
    """Enabled requirements, including unstable sampled enabled=True signals.

    If sampled backends disagree on `enabled`, the report is provisional, but
    it must not drop the requirement and accidentally mark old binaries OK.
    """
    return tuple(
        sorted(
            (
                EnabledAmendment(name=a.name, amendment_id=a.hash)
                for a in amendments.amendments
                if a.enabled or a.name in amendments.enabled_unstable
            ),
            key=lambda a: a.name.lower(),
        )
    )


def version_ref(version: str, explicit: dict[str, str] | None = None) -> str | None:
    """Map a crawl short-version string to a likely git ref/tag.

    The crawler already strips the `xahaud-` / `rippled-` prefix, and Xahau
    release tags match that short form (e.g. `2026.6.21-release+3350`).
    Unknown/custom builds are intentionally left to the git lookup; if no tag
    exists the row becomes `unknown`, not `incompatible`.
    """
    if not version or version == "(unknown)":
        return None
    if explicit and version in explicit:
        return explicit[version]
    if version.startswith("rippled-"):
        return None
    return version


def visible_version_key(version: str | None) -> str:
    """Bucket key used for crawl versions and explicit ref overrides."""
    if not version:
        return "(unknown)"
    if version.startswith("xahaud-"):
        return version.removeprefix("xahaud-")
    return version


def visible_version_counts(versions: Iterable[str | None]) -> Counter[str]:
    """Version buckets for zombie inference.

    xahaud release tags use the visible version minus the `xahaud-` prefix.
    Non-xahaud build strings remain raw so `rippled-2.4.0` cannot silently map
    to an unrelated local `2.4.0` tag.
    """
    counts: Counter[str] = Counter()
    for version in versions:
        counts[visible_version_key(version)] += 1
    return counts


def compare_ref_to_enabled(
    ref_features: RefFeatures, enabled: tuple[EnabledAmendment, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    by_id: dict[str, FeatureDecl] = {
        d.amendment_id.upper(): d for d in ref_features.declarations
    }
    missing: list[str] = []
    unsupported: list[str] = []
    for amendment in enabled:
        decl = by_id.get(amendment.amendment_id.upper())
        if decl is None:
            missing.append(amendment.name)
        elif not decl.supported:
            unsupported.append(amendment.name)
    return tuple(missing), tuple(unsupported)


def amendment_evidence(
    ref_features: RefFeatures,
    enabled: tuple[EnabledAmendment, ...],
    source_url: str | None,
    sampled_rpc_url: str | None = None,
) -> tuple[AmendmentEvidence, ...]:
    """Build auditable evidence for missing/unsupported enabled amendments."""
    by_id: dict[str, FeatureDecl] = {
        d.amendment_id.upper(): d for d in ref_features.declarations
    }
    evidence: list[AmendmentEvidence] = []
    for amendment in enabled:
        decl = by_id.get(amendment.amendment_id.upper())
        if decl is None:
            evidence.append(
                AmendmentEvidence(
                    name=amendment.name,
                    amendment_id=amendment.amendment_id,
                    issue="missing",
                    source_line=None,
                    evidence_url=source_url,
                    sampled_rpc_url=sampled_rpc_url,
                )
            )
        elif not decl.supported:
            evidence.append(
                AmendmentEvidence(
                    name=amendment.name,
                    amendment_id=amendment.amendment_id,
                    issue="unsupported",
                    source_line=decl.line,
                    evidence_url=line_url(source_url, decl.line),
                    sampled_rpc_url=sampled_rpc_url,
                )
            )
    return tuple(evidence)


def git_commit(repo: Path, ref: str) -> str | None:
    return _git(repo, "rev-parse", f"{ref}^{{commit}}")


def github_base(repo: Path) -> str | None:
    remote = _git(repo, "remote", "get-url", "origin")
    return github_base_from_remote(remote) if remote else None


def github_base_from_remote(remote: str) -> str | None:
    value = remote.strip()
    if value.endswith(".git"):
        value = value[:-4]
    if value.startswith("git@github.com:"):
        return f"https://github.com/{value.removeprefix('git@github.com:')}"
    if value.startswith("ssh://git@github.com/"):
        return f"https://github.com/{value.removeprefix('ssh://git@github.com/')}"
    if value.startswith("https://github.com/"):
        return value
    return None


def github_blob_url(
    base_url: str | None, commit: str | None, path: str, line: int | None = None
) -> str | None:
    if not base_url or not commit:
        return None
    url = f"{base_url}/blob/{commit}/{path}"
    return line_url(url, line)


def line_url(url: str | None, line: int | None) -> str | None:
    if not url or line is None:
        return url
    return f"{url}#L{line}"


def _git(repo: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def analyze_versions(
    *,
    repo: Path,
    version_counts: Counter[str],
    enabled: tuple[EnabledAmendment, ...],
    explicit_refs: dict[str, str] | None = None,
    sampled_rpc_url: str | None = None,
) -> list[VersionCompatibility]:
    parser = FeatureParser()
    base_url = github_base(repo)
    out: list[VersionCompatibility] = []
    for version, count in version_counts.most_common():
        ref = version_ref(version, explicit_refs)
        if ref is None:
            out.append(
                VersionCompatibility(
                    version=version,
                    nodes=count,
                    ref=None,
                    parsed=None,
                    missing_enabled=(),
                    unsupported_enabled=(),
                    error="no version/ref",
                )
            )
            continue
        try:
            parsed = load_ref_features(repo, ref, parser)
            missing, unsupported = compare_ref_to_enabled(parsed, enabled)
            commit = git_commit(repo, ref)
            source_url = github_blob_url(base_url, commit, parsed.source_path)
            out.append(
                VersionCompatibility(
                    version=version,
                    nodes=count,
                    ref=ref,
                    parsed=parsed,
                    missing_enabled=missing,
                    unsupported_enabled=unsupported,
                    commit=commit,
                    source_url=source_url,
                    evidence=amendment_evidence(
                        parsed,
                        enabled,
                        source_url,
                        sampled_rpc_url=sampled_rpc_url,
                    ),
                )
            )
        except Exception as exc:
            out.append(
                VersionCompatibility(
                    version=version,
                    nodes=count,
                    ref=ref,
                    parsed=None,
                    missing_enabled=(),
                    unsupported_enabled=(),
                    error=str(exc),
                )
            )
    return out
