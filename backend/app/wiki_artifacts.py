from __future__ import annotations

import base64
import binascii
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID, uuid4

from . import knowledge


TEXT_LIMIT = 100_000
IMAGE_LIMIT = 5 * 1024 * 1024
SENTINEL_START = "<<wiki-artifact:v1>>"
SENTINEL_END = "<<end>>"
ARTIFACT_KINDS = {"mermaid", "svg", "image", "table", "plot", "code"}
IMAGE_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
TABLE_COLUMN_TYPES = {"string", "number", "date", "link"}


class ArtifactValidationError(ValueError):
    pass


TOOL_DESCRIPTION = (
    "Render a typed artifact inline in the Wiki.app session view. Prefer this over "
    "dumping /tmp file paths: the artifact is inspectable, downloadable, and lives "
    "with the transcript. Use table artifacts only for 20+ rows or data the user will "
    "want to sort, export, or inspect. For prose comparisons with at most 6 rows and "
    "3 columns, use a plain markdown table instead."
)

TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "payload"],
    "properties": {
        "kind": {"enum": sorted(ARTIFACT_KINDS)},
        "title": {"type": "string", "maxLength": 200},
        "caption": {"type": "string", "maxLength": 500},
        "payload": {"type": "object"},
    },
}

SEARCH_TOOL_DESCRIPTION = (
    "Search durable Wiki knowledge across vault notes and Wiki-managed fleet run "
    "history. Returns ranked snippets with note-path or run-id/event-sequence citations."
)
SEARCH_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["query"],
    "properties": {
        "query": {"type": "string", "minLength": 1},
        "ticket": {"type": "string"},
        "kind": {"enum": ["note", "run"]},
        "type": {"type": "string"},
        "since": {"type": "string", "format": "date"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
}


def _require_keys(
    value: dict[str, Any],
    *,
    required: set[str],
    optional: set[str] = frozenset(),
) -> None:
    missing = required - value.keys()
    if missing:
        raise ArtifactValidationError(f"missing required field: {sorted(missing)[0]}")
    extra = value.keys() - required - optional
    if extra:
        raise ArtifactValidationError(f"unknown field: {sorted(extra)[0]}")


def _require_string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ArtifactValidationError(f"{field} must be a non-empty string")
    return value


def _text_size(payload: dict[str, Any]) -> int:
    return len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _validate_text_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    if kind in {"mermaid", "svg"}:
        _require_keys(payload, required={"source"})
        source = _require_string(payload["source"], "payload.source")
        if kind == "svg" and not re.search(r"<svg(?:\s|>)", source, re.IGNORECASE):
            raise ArtifactValidationError("payload.source must contain an <svg> root")
    elif kind == "table":
        _require_keys(payload, required={"columns", "rows"})
        columns = payload["columns"]
        rows = payload["rows"]
        if not isinstance(columns, list) or not columns:
            raise ArtifactValidationError("payload.columns must be a non-empty array")
        if not isinstance(rows, list):
            raise ArtifactValidationError("payload.rows must be an array")
        keys: set[str] = set()
        for index, column in enumerate(columns):
            if not isinstance(column, dict):
                raise ArtifactValidationError(f"payload.columns[{index}] must be an object")
            _require_keys(column, required={"key", "label", "type"})
            key = _require_string(column["key"], f"payload.columns[{index}].key")
            _require_string(column["label"], f"payload.columns[{index}].label")
            if column["type"] not in TABLE_COLUMN_TYPES:
                raise ArtifactValidationError(
                    f"payload.columns[{index}].type must be string, number, date, or link"
                )
            if key in keys:
                raise ArtifactValidationError(f"duplicate table column key: {key}")
            keys.add(key)
        for index, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != len(columns):
                raise ArtifactValidationError(
                    f"payload.rows[{index}] must contain {len(columns)} cells"
                )
            if any(isinstance(cell, (dict, list)) for cell in row):
                raise ArtifactValidationError(
                    f"payload.rows[{index}] cells must be scalar values"
                )
    elif kind == "plot":
        _require_keys(payload, required={"spec_vega_lite"})
        if not isinstance(payload["spec_vega_lite"], dict):
            raise ArtifactValidationError("payload.spec_vega_lite must be an object")
    elif kind == "code":
        _require_keys(
            payload,
            required={"language", "source"},
            optional={"filename", "diff_from"},
        )
        _require_string(payload["language"], "payload.language")
        _require_string(payload["source"], "payload.source", allow_empty=True)
        for field in ("filename", "diff_from"):
            if field in payload and not isinstance(payload[field], str):
                raise ArtifactValidationError(f"payload.{field} must be a string")
    if _text_size(payload) > TEXT_LIMIT:
        raise ArtifactValidationError(
            f"{kind} payload exceeds the {TEXT_LIMIT // 1000}KB text limit"
        )
    return dict(payload)


def _validated_run_id(raw: str) -> str:
    try:
        parsed = UUID(raw)
    except (ValueError, AttributeError) as exc:
        raise ArtifactValidationError("WIKI_RUN_ID must be a canonical UUID") from exc
    if str(parsed) != raw:
        raise ArtifactValidationError("WIKI_RUN_ID must be a canonical UUID")
    return raw


def _write_image(payload: dict[str, Any], artifact_id: str) -> dict[str, Any]:
    _require_keys(payload, required={"data_base64", "mime"})
    encoded = _require_string(payload["data_base64"], "payload.data_base64")
    mime = payload["mime"]
    if mime not in IMAGE_TYPES:
        raise ArtifactValidationError("payload.mime must be image/png, image/jpeg, or image/webp")
    if len(encoded) > ((IMAGE_LIMIT + 2) // 3) * 4 + 4:
        raise ArtifactValidationError("image payload exceeds the 5MB image limit")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ArtifactValidationError("payload.data_base64 is not valid base64") from exc
    if len(data) > IMAGE_LIMIT:
        raise ArtifactValidationError("image payload exceeds the 5MB image limit")

    runtime_value = os.environ.get("WIKI_AGENT_RUNTIME_DIR")
    if not runtime_value:
        raise ArtifactValidationError("WIKI_AGENT_RUNTIME_DIR is required")
    runtime_dir = Path(runtime_value).expanduser()
    run_id = _validated_run_id(os.environ.get("WIKI_RUN_ID") or "")
    artifact_dir = runtime_dir / "runs" / run_id / "artifacts"
    artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if artifact_dir.is_symlink():
        raise ArtifactValidationError("refusing symlink artifact directory")
    artifact_dir.chmod(0o700)
    target = artifact_dir / f"{artifact_id}.{IMAGE_TYPES[mime]}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(target, flags, 0o600)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {
        "ref": f"artifact://{artifact_id}",
        "mime": mime,
        "byte_size": len(data),
    }


def render_artifact(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ArtifactValidationError("tool input must be an object")
    _require_keys(
        arguments,
        required={"kind", "payload"},
        optional={"title", "caption"},
    )
    kind = arguments["kind"]
    if kind not in ARTIFACT_KINDS:
        raise ArtifactValidationError(f"unsupported artifact kind: {kind!r}")
    for field, limit in (("title", 200), ("caption", 500)):
        if field in arguments:
            value = _require_string(arguments[field], field, allow_empty=True)
            if len(value) > limit:
                raise ArtifactValidationError(f"{field} exceeds {limit} characters")
    payload = arguments["payload"]
    if not isinstance(payload, dict):
        raise ArtifactValidationError("payload must be an object")

    artifact_id = str(uuid4())
    artifact = {"kind": kind}
    if kind == "image":
        artifact.update(_write_image(payload, artifact_id))
    else:
        artifact.update(_validate_text_payload(kind, payload))
    event: dict[str, Any] = {
        "kind": "artifact",
        "id": artifact_id,
        "artifact": artifact,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    for field in ("title", "caption"):
        if field in arguments:
            event[field] = arguments[field]
    return event


def sentinel_text(event: dict[str, Any]) -> str:
    return (
        SENTINEL_START
        + json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        + SENTINEL_END
    )


def artifact_from_text(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    start = value.find(SENTINEL_START)
    if start < 0:
        return None
    start += len(SENTINEL_START)
    end = value.find(SENTINEL_END, start)
    if end < 0:
        return None
    try:
        event = json.loads(value[start:end])
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("kind") != "artifact":
        return None
    artifact = event.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("kind") not in ARTIFACT_KINDS:
        return None
    try:
        _validated_run_id(str(event.get("id") or ""))
    except ArtifactValidationError:
        return None
    return event


def artifact_server_command() -> tuple[str, ...]:
    if getattr(sys, "frozen", False):
        return (sys.executable, "--wiki-artifacts-mcp")
    return (sys.executable, "-m", "backend.app.wiki_artifacts")


def artifact_server_environment(
    child_env: Mapping[str, str],
    run_id: str,
) -> dict[str, str]:
    server_env = {
        "WIKI_AGENT_RUNTIME_DIR": child_env["WIKI_AGENT_RUNTIME_DIR"],
        "WIKI_RUN_ID": run_id,
    }
    for key in (
        "WIKI_KNOWLEDGE_DB_PATH",
        "WIKI_VAULT_DIR",
        "WIKI_AGENT_ARCHIVE_DIR",
    ):
        if child_env.get(key):
            server_env[key] = child_env[key]
    return server_env


def _tool_result(request_id: Any, arguments: Any) -> dict[str, Any]:
    try:
        event = render_artifact(arguments)
    except (ArtifactValidationError, OSError) as exc:
        result = {
            "content": [{"type": "text", "text": f"artifact rejected: {exc}"}],
            "isError": True,
        }
    else:
        result = {"content": [{"type": "text", "text": sentinel_text(event)}]}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def search_knowledge(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise knowledge.KnowledgeQueryError("tool input must be an object")
    extra = set(arguments) - {"query", "ticket", "kind", "type", "since", "limit"}
    if extra:
        raise knowledge.KnowledgeQueryError(f"unknown field: {sorted(extra)[0]}")
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise knowledge.KnowledgeQueryError("query must be a non-empty string")
    limit = arguments.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise knowledge.KnowledgeQueryError("limit must be an integer")
    for field in ("ticket", "kind", "type", "since"):
        if field in arguments and not isinstance(arguments[field], str):
            raise knowledge.KnowledgeQueryError(f"{field} must be a string")
    return knowledge.KnowledgeIndex.from_env().search(
        query,
        ticket=arguments.get("ticket"),
        kind=arguments.get("kind"),
        event_type=arguments.get("type"),
        since=arguments.get("since"),
        limit=limit,
    )


def _knowledge_tool_result(request_id: Any, arguments: Any) -> dict[str, Any]:
    try:
        payload = search_knowledge(arguments)
    except (knowledge.KnowledgeError, OSError) as exc:
        result = {
            "content": [{"type": "text", "text": f"knowledge search failed: {exc}"}],
            "isError": True,
        }
    else:
        result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                }
            ],
            "structuredContent": payload,
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _response(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": requested or "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "wiki-artifacts", "version": "1.0.0"},
            },
        }
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [
                    {
                        "name": "render_artifact",
                        "description": TOOL_DESCRIPTION,
                        "inputSchema": TOOL_SCHEMA,
                    },
                    {
                        "name": "search_knowledge",
                        "description": SEARCH_TOOL_DESCRIPTION,
                        "inputSchema": SEARCH_TOOL_SCHEMA,
                    },
                ]
            },
        }
    if method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") == "render_artifact":
            return _tool_result(request_id, params.get("arguments"))
        if params.get("name") == "search_knowledge":
            return _knowledge_tool_result(request_id, params.get("arguments"))
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "unknown tool"},
        }
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": "method not found"},
    }


def main() -> None:
    for raw_line in sys.stdin.buffer:
        try:
            message = json.loads(raw_line)
            if not isinstance(message, dict):
                raise ValueError("request must be an object")
            response = _response(message)
        except (ValueError, TypeError) as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"parse error: {exc}"},
            }
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
