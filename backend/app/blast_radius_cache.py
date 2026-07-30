"""Bounded, single-flight diff cache with attested keys and values."""

from __future__ import annotations

import threading
from concurrent.futures import Future
from typing import Callable

from .blast_radius_types import ChangedFiles, MAX_CACHE_ENTRIES, SourceAttestation


class DiffCache:
    """Cache entries are valid only for candidate, branch, and main heads."""

    def __init__(self, max_entries: int = MAX_CACHE_ENTRIES) -> None:
        self.max_entries = max(1, max_entries)
        self._values: dict[tuple[str, str, str], ChangedFiles] = {}
        self._inflight: dict[tuple[str, str, str], Future[ChangedFiles]] = {}
        self._lock = threading.Lock()

    def get_or_compute(
        self,
        branch: str,
        head_sha: str,
        compute: Callable[[], tuple[str, ...]],
        *,
        candidate_head_sha: str = "",
        main_head_sha: str = "",
    ) -> ChangedFiles:
        del branch
        key = (candidate_head_sha, head_sha, main_head_sha)
        with self._lock:
            cached = self._values.get(key)
            if cached is not None:
                return cached
            future = self._inflight.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._inflight[key] = future

        if not owner:
            return future.result()

        try:
            computed = compute()
            value = computed if isinstance(computed, ChangedFiles) else ChangedFiles(computed)
            if not value.cache_attestation.valid:
                raise RuntimeError(value.cache_attestation.reason or "cache entry is unattested")
        except BaseException as exc:
            with self._lock:
                self._inflight.pop(key, None)
                future.set_exception(exc)
            raise

        with self._lock:
            cached = self._values.get(key)
            if cached is None:
                while len(self._values) >= self.max_entries:
                    self._values.pop(next(iter(self._values)))
                value = ChangedFiles(
                    value,
                    truncated=value.truncated,
                    dropped_count=value.dropped_count,
                    attestation=value.attestation,
                    cache_attestation=SourceAttestation("cache-entry", True, True, True),
                )
                self._values[key] = value
                cached = value
            self._inflight.pop(key, None)
            future.set_result(cached)
        return cached


DIFF_CACHE = DiffCache()
