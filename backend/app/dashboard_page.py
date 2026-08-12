"""Browser-served fleet + task dashboard (WIKI-276).

Mounts three routes on the existing FastAPI app so the sidecar exposes a
read-only, live-updating page at ``/dashboard``:

* ``GET /dashboard`` — the HTML shell (a static, hand-tuned single file).
* ``GET /dashboard/data`` — one aggregated JSON payload with everything
  the page renders: fleet workers, orchestrator rollups, tasks joined
  with PR state, and today's archived sessions.
* No new SSE endpoint — the page subscribes to the existing
  ``/api/events`` stream and re-fetches ``/dashboard/data`` whenever a
  ``type: agents`` message arrives.

The router is imported by ``main.py`` and included **before** the
frontend static mount so ``/dashboard`` is served here instead of falling
through to the SPA bundle.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import dashboard

router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "dashboard_static"
_HTML_PATH = _STATIC_DIR / "index.html"
logger = logging.getLogger(__name__)


def _load_html() -> str:
    try:
        return _HTML_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        logger.error("dashboard asset missing from bundle: %s", _HTML_PATH)
        raise HTTPException(
            status_code=500,
            detail="dashboard asset missing from bundle: index.html",
        ) from exc


PayloadBuilder = Callable[[], dict[str, object]]
_payload_builder: PayloadBuilder | None = None


def register_payload_builder(builder: PayloadBuilder) -> None:
    """main.py hands us a closure that reads registry/status/archive.

    Keeping the source of truth in main.py means we don't duplicate its
    registry/status file location logic and stay compatible with test
    fixtures that swap those paths.
    """
    global _payload_builder
    _payload_builder = builder


@router.get("/dashboard", include_in_schema=False)
def dashboard_page() -> HTMLResponse:
    return HTMLResponse(_load_html(), headers={"Cache-Control": "no-cache"})


@router.get("/dashboard/static/{filename}", include_in_schema=False)
def dashboard_static(filename: str) -> Response:
    # Only serves whitelisted ES modules that ship next to index.html —
    # keeps the path traversal surface at zero.
    if filename not in {"ticket-row.mjs"}:
        raise HTTPException(status_code=404)
    return Response(
        (_STATIC_DIR / filename).read_text(encoding="utf-8"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/dashboard/data")
def dashboard_data() -> JSONResponse:
    if _payload_builder is None:
        return JSONResponse(
            dashboard.build_page_payload({}, {}, []),
            headers={"Cache-Control": "no-store"},
        )
    payload = _payload_builder()
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


__all__ = ["router", "register_payload_builder"]
