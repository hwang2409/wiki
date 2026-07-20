from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope


class FrontendStaticFiles(StaticFiles):
    def file_response(
        self,
        full_path: os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        if scope["path"].startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


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
    app.mount("/", FrontendStaticFiles(directory=dist, html=True), name="frontend")
