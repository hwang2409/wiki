"""Shared types and positive attestations for branch collision analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


MAX_ACTIVE_BRANCHES = 100
MAX_CHANGED_FILES = 5_000
MAX_CACHE_ENTRIES = 256
GIT_TIMEOUT_SECONDS = 2.0
ANALYSIS_TIMEOUT_SECONDS = 5.0
OPEN_PR_REFRESH_SECONDS = 30.0
OPEN_PR_MAX_AGE_SECONDS = 90.0


@dataclass(frozen=True)
class SourceAttestation:
    source: str
    ok: bool
    shape_valid: bool
    fresh: bool
    reason: str | None = None

    @property
    def valid(self) -> bool:
        return self.ok and self.shape_valid and self.fresh

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "ok": self.ok,
            "shape_valid": self.shape_valid,
            "fresh": self.fresh,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BranchRef:
    name: str
    ref: str
    head_sha: str


@dataclass(frozen=True)
class ActiveBranch:
    name: str
    ref: str
    head_sha: str
    source: str
    ticket: str | None = None


@dataclass(frozen=True)
class BranchFiles:
    branch: str
    head_sha: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class OpenPRBranch:
    name: str
    head_sha: str | None = None
    ticket: str | None = None
    attestation: SourceAttestation = field(
        default_factory=lambda: SourceAttestation("open-pr-row", False, False, False, "row is unattested")
    )


@dataclass(frozen=True)
class OpenPRSnapshotState:
    branches: tuple[OpenPRBranch, ...]
    complete: bool
    refreshed_at: float | None = None
    error: str | None = None
    attestation: SourceAttestation = field(
        default_factory=lambda: SourceAttestation("open-pr-snapshot", False, False, False, "snapshot is unattested")
    )

    def __post_init__(self) -> None:
        if isinstance(self.refreshed_at, str) and self.error is None:
            object.__setattr__(self, "error", self.refreshed_at)
            object.__setattr__(self, "refreshed_at", None)


@dataclass(frozen=True)
class DiscoveryResult:
    branches: tuple[ActiveBranch, ...]
    failed_branches: tuple[dict[str, str], ...]
    open_pr_snapshot_complete: bool
    attestations: tuple[SourceAttestation, ...] = ()


class ChangedFiles(tuple[str, ...]):
    """Immutable changed files with diff and cache attestations."""

    truncated: bool
    dropped_count: int
    attestation: SourceAttestation
    cache_attestation: SourceAttestation

    def __new__(
        cls,
        files: Iterable[str] = (),
        *,
        truncated: bool = False,
        dropped_count: int = 0,
        attestation: SourceAttestation | None = None,
        cache_attestation: SourceAttestation | None = None,
    ) -> "ChangedFiles":
        value = super().__new__(cls, files)
        value.truncated = truncated
        value.dropped_count = dropped_count
        value.attestation = attestation or SourceAttestation(
            "git-diff", not truncated, True, True, "file list truncated" if truncated else None
        )
        value.cache_attestation = cache_attestation or SourceAttestation(
            "cache-entry", True, True, True
        )
        return value


class GitAnalysisError(RuntimeError):
    pass
