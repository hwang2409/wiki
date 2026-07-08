from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles


def _configured_dist() -> Path | None:
    raw = os.environ.get("WIKI_FRONTEND_DIST")
    if not raw:
        return None

    dist = Path(raw).expanduser().resolve()
    if not dist.is_dir() or not (dist / "index.html").is_file():
        raise RuntimeError(
            f"WIKI_FRONTEND_DIST must point to a built frontend/dist directory: {dist}"
        )
    return dist


def mount_frontend_static(app: FastAPI) -> None:
    dist = _configured_dist()
    if dist is None:
        return
    app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
