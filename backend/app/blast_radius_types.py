"""Attested values and shared types for branch collision analysis."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Iterable, TypeVar


MAX_ACTIVE_BRANCHES = 100
MAX_CHANGED_FILES = 5_000
MAX_CACHE_ENTRIES = 256
GIT_TIMEOUT_SECONDS = 2.0
ANALYSIS_TIMEOUT_SECONDS = 5.0
OPEN_PR_REFRESH_SECONDS = 30.0
OPEN_PR_MAX_AGE_SECONDS = 90.0

T = TypeVar("T")


@dataclass(frozen=True, eq=False)
class Attested(Generic[T]):
    """A value that carries the only validity state analysis may trust."""

    value: T | None
    source: str
    ok: bool
    shape_valid: bool
    fresh: bool
    reason: str | None = None

    @property
    def valid(self) -> bool:
        return self.value is not None and self.ok and self.shape_valid and self.fresh

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "ok": self.ok,
            "shape_valid": self.shape_valid,
            "fresh": self.fresh,
            "reason": self.reason,
        }

    def __iter__(self):
        value = self.value
        if isinstance(value, ChangedFiles):
            return iter(value)
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
            return iter(value)
        raise TypeError(f"{type(self).__name__} value is not iterable")

    def __len__(self) -> int:
        value = self.value
        if isinstance(value, ChangedFiles):
            return len(value)
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
            return len(tuple(value))
        raise TypeError(f"{type(self).__name__} value has no length")

    def __getitem__(self, key: object):
        if isinstance(self.value, dict):
            return self.value[key]
        raise TypeError(f"{type(self).__name__} value is not a mapping")

    def get(self, key: object, default: object = None):
        if isinstance(self.value, dict):
            return self.value.get(key, default)
        return default

    def items(self):
        if isinstance(self.value, dict):
            return self.value.items()
        raise TypeError(f"{type(self).__name__} value is not a mapping")

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Attested):
            return self.value == other.value and self.source == other.source and self.valid == other.valid
        value = self.value
        if isinstance(value, ChangedFiles):
            return tuple(value) == other
        return value == other


class SourceAttestation(Attested[object]):
    """Compatibility constructor for diagnostic attestations.

    Internal code stores these as values in the attestation ledger.
    """

    def __init__(
        self,
        source: str,
        ok: bool,
        shape_valid: bool,
        fresh: bool,
        reason: str | None = None,
    ) -> None:
        super().__init__(True, source, ok, shape_valid, fresh, reason)


@dataclass(frozen=True)
class BranchRef:
    name: str
    ref: str
    head: Attested[str]

    def __init__(self, name: str, ref: str, head_sha: str | Attested[str]) -> None:
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "ref", ref)
        object.__setattr__(
            self,
            "head",
            head_sha
            if isinstance(head_sha, Attested)
            else Attested(head_sha, f"git-ref:{name}", True, True, True),
        )

    @property
    def head_sha(self) -> str:
        return self.head.value or ""


@dataclass(frozen=True)
class ActiveBranch:
    name: str
    ref: str
    head: Attested[str]
    source: str
    ticket: str | None = None

    def __init__(
        self,
        name: str,
        ref: str,
        head_sha: str | Attested[str],
        source: str,
        ticket: str | None = None,
    ) -> None:
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "ref", ref)
        object.__setattr__(
            self,
            "head",
            head_sha
            if isinstance(head_sha, Attested)
            else Attested(head_sha, f"branch-head:{name}", True, True, True),
        )
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "ticket", ticket)

    @property
    def head_sha(self) -> str:
        return self.head.value or ""


@dataclass(frozen=True)
class BranchFiles:
    branch: str
    head_sha: str
    files: tuple[str, ...] | Attested[tuple[str, ...]]

    def __post_init__(self) -> None:
        if isinstance(self.files, Attested):
            object.__setattr__(self, "files", self.files.value or ())


@dataclass(frozen=True, init=False)
class OpenPRBranch:
    name: str
    head: Attested[str]
    ticket: str | None

    def __init__(
        self,
        name: str,
        head_sha: str | None | Attested[str] = None,
        ticket: str | None = None,
        attestation: SourceAttestation | Attested[str] | None = None,
    ) -> None:
        if isinstance(head_sha, Attested):
            head = head_sha
        else:
            source = attestation.source if attestation is not None else f"open-pr-row:{name}"
            head = Attested(
                head_sha,
                source,
                attestation.ok if attestation is not None else True,
                attestation.shape_valid if attestation is not None else True,
                attestation.fresh if attestation is not None else True,
                attestation.reason if attestation is not None else None,
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "head", head)
        object.__setattr__(self, "ticket", ticket)

    @property
    def head_sha(self) -> str | None:
        return self.head.value

    @property
    def attestation(self) -> Attested[str]:
        return self.head


@dataclass(frozen=True)
class SnapshotValue:
    branches: tuple[OpenPRBranch, ...]
    refreshed_at: float | None
    error: str | None


@dataclass(frozen=True, init=False)
class OpenPRSnapshotState:
    snapshot: Attested[SnapshotValue]

    def __init__(
        self,
        branches: tuple[OpenPRBranch, ...] | Attested[SnapshotValue],
        complete: bool = True,
        refreshed_at: float | None = None,
        error: str | None = None,
        attestation: SourceAttestation | Attested[SnapshotValue] | None = None,
    ) -> None:
        if isinstance(branches, Attested):
            snapshot = branches
        else:
            if isinstance(refreshed_at, str) and error is None:
                error = refreshed_at
                refreshed_at = None
            source = attestation.source if attestation is not None else "open-pr-snapshot"
            snapshot = Attested(
                SnapshotValue(tuple(branches), refreshed_at, error),
                source,
                complete and (attestation.ok if attestation is not None else True),
                attestation.shape_valid if attestation is not None else True,
                attestation.fresh if attestation is not None else True,
                attestation.reason if attestation is not None else error,
            )
        object.__setattr__(self, "snapshot", snapshot)

    @property
    def branches(self) -> tuple[OpenPRBranch, ...]:
        return self.snapshot.value.branches if self.snapshot.value is not None else ()

    @property
    def complete(self) -> bool:
        return self.snapshot.valid

    @property
    def refreshed_at(self) -> float | None:
        return self.snapshot.value.refreshed_at if self.snapshot.value is not None else None

    @property
    def error(self) -> str | None:
        return self.snapshot.value.error if self.snapshot.value is not None else self.snapshot.reason

    @property
    def attestation(self) -> Attested[SnapshotValue]:
        return self.snapshot


@dataclass(frozen=True)
class DiscoveryResult:
    branches: Attested[tuple[ActiveBranch, ...]]
    failed_branches: tuple[dict[str, str], ...]
    inputs: tuple[Attested[Any], ...]
    expected_input_count: int


@dataclass(frozen=True, eq=False, init=False)
class ChangedFiles(Attested[tuple[str, ...]]):
    truncated: bool
    dropped_count: int

    def __init__(
        self,
        files: Iterable[str] = (),
        *,
        truncated: bool = False,
        dropped_count: int = 0,
        attestation: Attested[tuple[str, ...]] | SourceAttestation | None = None,
        cache_attestation: SourceAttestation | None = None,
    ) -> None:
        values = tuple(files)
        source = attestation.source if attestation is not None else "git-diff"
        ok = not truncated and (attestation.ok if attestation is not None else True)
        shape_valid = attestation.shape_valid if attestation is not None else True
        fresh = attestation.fresh if attestation is not None else True
        reason = attestation.reason if attestation is not None else (f"dropped {dropped_count} files" if dropped_count else None)
        if cache_attestation is not None:
            ok = ok and cache_attestation.ok
            shape_valid = shape_valid and cache_attestation.shape_valid
            fresh = fresh and cache_attestation.fresh
            reason = cache_attestation.reason or reason
        Attested.__init__(self, values, source, ok, shape_valid, fresh, reason)
        object.__setattr__(self, "truncated", truncated)
        object.__setattr__(self, "dropped_count", dropped_count)


@dataclass(frozen=True)
class AttestationLedger:
    received: tuple[Attested[Any], ...]
    expected_count: int

    @property
    def complete(self) -> bool:
        sources = tuple(value.source for value in self.received)
        return (
            len(self.received) == self.expected_count
            and len(set(sources)) == self.expected_count
            and all(value.valid for value in self.received)
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "complete": self.complete,
            "expected_count": self.expected_count,
            "received_count": len(self.received),
            "sources": [value.as_dict() for value in self.received],
        }


class GitAnalysisError(RuntimeError):
    pass
