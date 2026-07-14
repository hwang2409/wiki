from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request as UrlRequest
from urllib.request import urlopen


BACKEND_DISCOVERY_ENV = "WIKI_BACKEND_DISCOVERY_FILE"


class BackendRequestError(RuntimeError):
    pass


def normalize_loopback_url(value: str) -> str:
    raw = value.strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("backend URL must be an http loopback URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("backend URL has an invalid port") from exc
    if port is None:
        raise ValueError("backend URL must include a port")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("backend URL must not include credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("backend URL must not include a path")
    return raw


def discovery_path(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    configured = values.get(BACKEND_DISCOVERY_ENV)
    if configured:
        return Path(configured).expanduser()
    home = Path(values.get("HOME") or Path.home()).expanduser()
    return home / ".wiki" / "backend-url"


def publish_backend_url(value: str, env: Mapping[str, str] | None = None) -> Path:
    normalized = normalize_loopback_url(value)
    target = discovery_path(env)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    with tempfile.NamedTemporaryFile(
        "w",
        dir=target.parent,
        prefix=f".{target.name}.",
        delete=False,
        encoding="utf-8",
    ) as handle:
        handle.write(normalized + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        temp = Path(handle.name)
    temp.chmod(0o600)
    temp.replace(target)
    return target


def read_backend_url(env: Mapping[str, str] | None = None) -> str | None:
    try:
        value = discovery_path(env).read_text(encoding="utf-8")
        return normalize_loopback_url(value)
    except (OSError, ValueError):
        return None


def request_json(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 10,
) -> dict[str, Any]:
    base = normalize_loopback_url(base_url)
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = UrlRequest(
        f"{base}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback validated
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            error = json.loads(exc.read().decode("utf-8"))
            detail = error.get("detail") if isinstance(error, dict) else None
        except (OSError, ValueError):
            detail = None
        raise BackendRequestError(str(detail or exc)) from exc
    except (URLError, OSError, ValueError) as exc:
        raise BackendRequestError(str(exc)) from exc
    if not isinstance(result, dict):
        raise BackendRequestError("backend returned non-object JSON")
    return result
