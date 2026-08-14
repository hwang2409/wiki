"""Startup feature flag and public kind metadata for the wk lanes.

This module has no provider or harness imports.  It is safe for the existing
backend to import while the opt-in feature remains disabled.
"""

from __future__ import annotations

import os
from typing import Literal


WkKind = Literal["wk-claude", "wk-codex"]
WK_KINDS: tuple[WkKind, WkKind] = ("wk-claude", "wk-codex")
WK_ENV_VAR = "WIKI_ENABLE_WK"

# Resolve this once.  A running backend must not change execution policy when
# its environment changes.
_WK_ENABLED = os.environ.get(WK_ENV_VAR) == "1"


def wk_enabled() -> bool:
    """Return the process-start wk feature state."""

    return _WK_ENABLED


def is_wk_kind(kind: str) -> bool:
    """Return whether *kind* names one of the distinct wk lanes."""

    return kind in WK_KINDS


def wk_lane_for_kind(kind: str) -> Literal["claude", "codex"] | None:
    """Map a public wk kind to its provider lane."""

    if kind == "wk-claude":
        return "claude"
    if kind == "wk-codex":
        return "codex"
    return None

