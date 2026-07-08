from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


UI_STATE_PATH = Path(
    os.environ.get("WIKI_UI_STATE_PATH", Path.home() / ".wiki" / "ui-state.json")
).expanduser()

MAX_KEY_BYTES = 256
MAX_VALUE_BYTES = 200_000
MAX_TOTAL_BYTES = 2_000_000
KEY_PREFIX = "wiki-"


router = APIRouter()


class UiStatePut(BaseModel):
    entries: dict[str, str] = Field(default_factory=dict)


def _load() -> dict[str, str]:
    try:
        raw = UI_STATE_PATH.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in data.items()
        if isinstance(k, str) and isinstance(v, str) and k.startswith(KEY_PREFIX)
    }


def _atomic_write(state: dict[str, str]) -> None:
    UI_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = UI_STATE_PATH.with_suffix(UI_STATE_PATH.suffix + ".tmp")
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=1)
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, UI_STATE_PATH)


@router.get("/api/ui-state")
def get_ui_state() -> dict[str, Any]:
    return {"entries": _load()}


@router.put("/api/ui-state")
def put_ui_state(body: UiStatePut) -> dict[str, Any]:
    merged = _load()
    for key, value in body.entries.items():
        if not isinstance(key, str) or not key.startswith(KEY_PREFIX):
            continue
        if len(key.encode("utf-8")) > MAX_KEY_BYTES:
            continue
        if not isinstance(value, str):
            continue
        if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
            raise HTTPException(status_code=413, detail=f"Value for {key} too large")
        merged[key] = value

    if len(json.dumps(merged).encode("utf-8")) > MAX_TOTAL_BYTES:
        raise HTTPException(status_code=413, detail="UI state exceeds size limit")

    try:
        _atomic_write(merged)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not write ui-state: {exc}") from exc

    return {"entries": merged}
