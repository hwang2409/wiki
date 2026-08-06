"""Enumerate installed OS font families for the settings picker.

macOS-only for now: parses ``system_profiler SPFontsDataType -json``, which
takes ~7s cold. Result is cached in-process for the daemon lifetime; the first
request from the frontend triggers enumeration on a background thread. Later
requests return the cached list immediately.
"""

from __future__ import annotations

import json
import subprocess
import threading
from typing import Any

_LOCK = threading.Lock()
_CACHE: list[str] | None = None


def _extract_families(payload: Any) -> list[str]:
    families: set[str] = set()
    entries = payload.get("SPFontsDataType", []) if isinstance(payload, dict) else []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("enabled") != "yes":
            continue
        for typeface in entry.get("typefaces", []) or []:
            if not isinstance(typeface, dict) or typeface.get("enabled") != "yes":
                continue
            family = typeface.get("family")
            if isinstance(family, str):
                trimmed = family.strip()
                if trimmed:
                    families.add(trimmed)
    return sorted(families, key=str.casefold)


def _enumerate() -> list[str]:
    try:
        completed = subprocess.run(
            ["system_profiler", "SPFontsDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0 or not completed.stdout:
        return []
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    return _extract_families(payload)


def installed_families() -> list[str]:
    global _CACHE
    with _LOCK:
        if _CACHE is not None:
            return _CACHE
    computed = _enumerate()
    with _LOCK:
        if _CACHE is None:
            _CACHE = computed
        return _CACHE


def reset_cache_for_tests() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = None
