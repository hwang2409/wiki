from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit


BACKEND_DISCOVERY_ENV = "WIKI_BACKEND_DISCOVERY_FILE"


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
