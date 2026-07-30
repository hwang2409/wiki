"""Bounded, single-flight diff cache with attested keys and values."""

from __future__ import annotations

import threading
from concurrent.futures import Future
from typing import Callable

from .blast_radius_types import Attested, ChangedFiles, MAX_CACHE_ENTRIES


class DiffCache:
    """Cache entries are valid only for candidate, branch, and main heads."""

    def __init__(self, max_entries: int = MAX_CACHE_ENTRIES) -> None:
        self.max_entries = max(1, max_entries)
        self._values: dict[tuple[str, str, str], Attested[ChangedFiles]] = {}
        self._inflight: dict[tuple[str, str, str], Future[Attested[ChangedFiles]]] = {}
        self._lock = threading.Lock()

    def get_or_compute(
        self,
        branch: str,
        head_sha: str,
        compute: Callable[[], tuple[str, ...] | ChangedFiles | Attested[ChangedFiles]],
        *,
        candidate_head_sha: str = "",
        main_head_sha: str = "",
    ) -> Attested[ChangedFiles]:
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
            if isinstance(computed, ChangedFiles):
                value = computed
            elif isinstance(computed, Attested) and isinstance(computed.value, ChangedFiles):
                value = computed.value
            else:
                value = ChangedFiles(computed)
            cached_value = Attested(
                value,
                "cache-entry",
                value.valid,
                value.shape_valid,
                value.fresh,
                value.reason,
            )
            if not cached_value.valid:
                raise RuntimeError(cached_value.reason or "cache entry is unattested")
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
                self._values[key] = cached_value
                cached = cached_value
            self._inflight.pop(key, None)
            future.set_result(cached)
        return cached


DIFF_CACHE = DiffCache()
