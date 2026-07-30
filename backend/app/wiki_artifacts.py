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
from . import wiki_agent_tools
from .image_scrub import ImageScrubError, scrub_image
from .media_scrub import (
    AUDIO_MIMES,
    MediaScrubError,
    VIDEO_MIMES,
    scrub_audio,
    scrub_video,
)
from .pathwalk import open_relative_file


TEXT_LIMIT = 100_000
IMAGE_LIMIT = 5 * 1024 * 1024
PDF_LIMIT = 25 * 1024 * 1024
VIDEO_LIMIT = 40 * 1024 * 1024
AUDIO_LIMIT = 20 * 1024 * 1024
PDF_MAGIC = b"%PDF-"
# Transport-level cap on a single MCP request line. Sized to fit the largest
# base64-encoded video payload (4/3 inflation) plus JSON envelope headroom, so
# json.loads never sees an unbounded buffer even when a caller sends garbage.
MEDIA_TRANSPORT_MAX = max(PDF_LIMIT, VIDEO_LIMIT, AUDIO_LIMIT, IMAGE_LIMIT)
MAX_REQUEST_BYTES = ((MEDIA_TRANSPORT_MAX + 2) // 3) * 4 + 64 * 1024
SENTINEL_START = "<<wiki-artifact:v1>>"
SENTINEL_END = "<<end>>"
ARTIFACT_KINDS = {
    "mermaid",
    "svg",
    "image",
    "table",
    "plot",
    "code",
    "diff",
    "file-list",
    "json",
    "pdf",
    "video",
    "audio",
}
IMAGE_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
PDF_MIME = "application/pdf"
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
            if (
                not isinstance(column["type"], str)
                or column["type"] not in TABLE_COLUMN_TYPES
            ):
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
    elif kind == "diff":
        _require_keys(payload, required={"source"})
        _require_string(payload["source"], "payload.source", allow_empty=True)
    elif kind == "file-list":
        _require_keys(payload, required={"files"})
        files = payload["files"]
        if not isinstance(files, list):
            raise ArtifactValidationError("payload.files must be an array")
        for index, entry in enumerate(files):
            if not isinstance(entry, dict):
                raise ArtifactValidationError(f"payload.files[{index}] must be an object")
            _require_keys(
                entry,
                required={"path"},
                optional={"label", "size", "status"},
            )
            _require_string(entry["path"], f"payload.files[{index}].path")
            if "label" in entry and not isinstance(entry["label"], str):
                raise ArtifactValidationError(f"payload.files[{index}].label must be a string")
            if "status" in entry and not isinstance(entry["status"], str):
                raise ArtifactValidationError(f"payload.files[{index}].status must be a string")
            if "size" in entry and not isinstance(entry["size"], (int, float)):
                raise ArtifactValidationError(f"payload.files[{index}].size must be a number")
    elif kind == "json":
        _require_keys(payload, required={"json_data"})
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


def _artifact_run_dir() -> Path:
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
    return artifact_dir


def _write_binary(artifact_dir: Path, artifact_id: str, extension: str, data: bytes) -> Path:
    target = artifact_dir / f"{artifact_id}.{extension}"
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
    return target


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        return path.is_relative_to(root)
    except AttributeError:  # pragma: no cover - Python <3.9 fallback
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False


def _pdf_path_allowed_roots() -> list[Path]:
    roots: list[Path] = []
    for var in ("WIKI_VAULT_DIR", "WIKI_AGENT_RUNTIME_DIR", "WIKI_AGENT_ARCHIVE_DIR"):
        value = os.environ.get(var)
        if not value:
            continue
        try:
            roots.append(Path(value).expanduser().resolve(strict=False))
        except (OSError, RuntimeError):
            continue
    return roots


def _read_fd_bounded(fd: int, limit: int, kind: str, limit_label: str) -> bytes:
    """Read up to `limit` bytes from `fd`. Reject if the source has more."""
    chunks: list[bytes] = []
    remaining = limit + 1  # +1 lets us detect overflow without buffering it
    while remaining > 0:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > limit:
        raise ArtifactValidationError(f"{kind} payload exceeds the {limit_label}")
    return data


def _open_root_fd(root: Path) -> int:
    """Open ``root`` as an O_NOFOLLOW directory fd, or raise ArtifactValidationError."""
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return os.open(root, flags)


def _read_media_path(raw: str, *, kind: str, byte_limit: int, limit_label: str) -> bytes:
    """Bounded, dir-fd-walking read of a payload path inside an allowed root."""
    if not raw or not isinstance(raw, str):
        raise ArtifactValidationError("payload.path must be a non-empty string")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ArtifactValidationError("payload.path must be an absolute filesystem path")
    if candidate.is_symlink():
        raise ArtifactValidationError(f"refusing symlink {kind} source")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ArtifactValidationError(f"payload.path could not be resolved: {exc}") from exc
    roots = _pdf_path_allowed_roots()
    if not roots:
        raise ArtifactValidationError(
            "payload.path rejected: no allowed roots configured (set WIKI_VAULT_DIR or WIKI_AGENT_RUNTIME_DIR)"
        )
    # Find the containing root AND the relative path segments — the walker
    # opens each component with O_NOFOLLOW so a symlink at ANY level
    # (intermediate directory or leaf) fails. Plain O_NOFOLLOW on a
    # single os.open() only protects the leaf.
    containing_root: Path | None = None
    relative_parts: tuple[str, ...] = ()
    for root in roots:
        if _is_relative_to(resolved, root):
            containing_root = root
            relative_parts = resolved.relative_to(root).parts
            break
    if containing_root is None:
        raise ArtifactValidationError(
            "payload.path is outside the allowed roots (vault, runtime, or archive)"
        )
    if not relative_parts:
        raise ArtifactValidationError("payload.path must reference a file inside the root")
    try:
        root_fd = _open_root_fd(containing_root)
    except OSError as exc:
        raise ArtifactValidationError(f"allowed root could not be opened: {exc}") from exc
    # NONBLOCK on the leaf keeps FIFOs/devices swapped in at the last step
    # from blocking the open — fstat below still rejects them.
    final_flags = getattr(os, "O_NONBLOCK", 0)
    try:
        try:
            fd = open_relative_file(
                root_fd, relative_parts, extra_final_flags=final_flags
            )
        except OSError as exc:
            raise ArtifactValidationError(
                f"payload.path could not be opened: {exc}"
            ) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ArtifactValidationError(
                    "payload.path must reference a regular file"
                )
            if info.st_size > byte_limit:
                raise ArtifactValidationError(f"{kind} payload exceeds the {limit_label}")
            return _read_fd_bounded(fd, byte_limit, kind, limit_label)
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)


def _read_pdf_path(raw: str) -> bytes:
    return _read_media_path(
        raw,
        kind="pdf",
        byte_limit=PDF_LIMIT,
        limit_label=f"{PDF_LIMIT // (1024 * 1024)}MB pdf limit",
    )


def _write_pdf(payload: dict[str, Any], artifact_id: str) -> dict[str, Any]:
    extra = payload.keys() - {"data_base64", "path"}
    if extra:
        raise ArtifactValidationError(f"unknown field: {sorted(extra)[0]}")
    has_base64 = "data_base64" in payload
    has_path = "path" in payload
    if has_base64 == has_path:
        raise ArtifactValidationError(
            "pdf payload must include exactly one of data_base64 or path"
        )
    if has_base64:
        encoded = _require_string(payload["data_base64"], "payload.data_base64")
        if len(encoded) > ((PDF_LIMIT + 2) // 3) * 4 + 4:
            raise ArtifactValidationError(
                f"pdf payload exceeds the {PDF_LIMIT // (1024 * 1024)}MB pdf limit"
            )
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ArtifactValidationError("payload.data_base64 is not valid base64") from exc
    else:
        data = _read_pdf_path(str(payload["path"]))
    if len(data) > PDF_LIMIT:
        raise ArtifactValidationError(
            f"pdf payload exceeds the {PDF_LIMIT // (1024 * 1024)}MB pdf limit"
        )
    if not data.startswith(PDF_MAGIC):
        raise ArtifactValidationError("pdf payload is not a valid PDF (missing %PDF- header)")
    artifact_dir = _artifact_run_dir()
    _write_binary(artifact_dir, artifact_id, "pdf", data)
    return {
        "ref": f"artifact://{artifact_id}",
        "mime": PDF_MIME,
        "byte_size": len(data),
    }


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

    try:
        result = scrub_image(data, mime)
    except ImageScrubError as exc:
        raise ArtifactValidationError(f"image payload rejected: {exc}") from exc
    if len(result.data) > IMAGE_LIMIT:
        raise ArtifactValidationError("image payload exceeds the 5MB image limit")

    artifact_dir = _artifact_run_dir()
    _write_binary(artifact_dir, artifact_id, IMAGE_TYPES[mime], result.data)
    normalized: dict[str, Any] = {
        "ref": f"artifact://{artifact_id}",
        "mime": mime,
        "byte_size": len(result.data),
        "width": result.width,
        "height": result.height,
    }
    if result.preview_base64:
        normalized["preview_base64"] = result.preview_base64
    return normalized


def _decode_media_payload(
    payload: dict[str, Any],
    *,
    kind: str,
    allowed_mimes: dict[str, str],
    byte_limit: int,
    limit_label: str,
    optional_keys: set[str] = frozenset(),
) -> tuple[bytes, str]:
    required = {"mime"}
    optional = optional_keys | {"data_base64", "path"}
    _require_keys(payload, required=required, optional=optional)
    mime = payload["mime"]
    if mime not in allowed_mimes:
        raise ArtifactValidationError(
            f"payload.mime must be one of {sorted(allowed_mimes)!s} for {kind}"
        )
    has_base64 = "data_base64" in payload
    has_path = "path" in payload
    if has_base64 == has_path:
        raise ArtifactValidationError(
            f"{kind} payload must include exactly one of data_base64 or path"
        )
    if has_base64:
        encoded = _require_string(payload["data_base64"], "payload.data_base64")
        if len(encoded) > ((byte_limit + 2) // 3) * 4 + 4:
            raise ArtifactValidationError(f"{kind} payload exceeds the {limit_label}")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ArtifactValidationError("payload.data_base64 is not valid base64") from exc
    else:
        data = _read_media_path(
            str(payload["path"]), kind=kind, byte_limit=byte_limit, limit_label=limit_label,
        )
    if len(data) > byte_limit:
        raise ArtifactValidationError(f"{kind} payload exceeds the {limit_label}")
    return data, mime


def _scrub_optional_poster(payload: dict[str, Any]) -> tuple[str, int, int] | None:
    """Route a caller-provided poster through image_scrub. Return preview_base64 + dims."""
    poster = payload.get("poster_base64")
    if poster is None:
        return None
    if not isinstance(poster, str) or not poster:
        raise ArtifactValidationError("payload.poster_base64 must be a non-empty string")
    if len(poster) > ((IMAGE_LIMIT + 2) // 3) * 4 + 4:
        raise ArtifactValidationError("payload.poster_base64 exceeds the 5MB image limit")
    try:
        poster_bytes = base64.b64decode(poster, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ArtifactValidationError("payload.poster_base64 is not valid base64") from exc
    poster_mime = payload.get("poster_mime", "image/png")
    if poster_mime not in IMAGE_TYPES:
        raise ArtifactValidationError(
            "payload.poster_mime must be image/png, image/jpeg, or image/webp"
        )
    try:
        result = scrub_image(poster_bytes, poster_mime)
    except ImageScrubError as exc:
        raise ArtifactValidationError(f"video poster rejected: {exc}") from exc
    if not result.preview_base64:
        # scrub_image always emits a preview for the bounded-side downsample.
        return None
    return result.preview_base64, result.width, result.height


def _write_video(payload: dict[str, Any], artifact_id: str) -> dict[str, Any]:
    data, mime = _decode_media_payload(
        payload,
        kind="video",
        allowed_mimes=VIDEO_MIMES,
        byte_limit=VIDEO_LIMIT,
        limit_label=f"{VIDEO_LIMIT // (1024 * 1024)}MB video limit",
        optional_keys={"poster_base64", "poster_mime"},
    )
    try:
        result = scrub_video(data, mime)
    except MediaScrubError as exc:
        raise ArtifactValidationError(f"video payload rejected: {exc}") from exc
    if len(result.data) > VIDEO_LIMIT:
        raise ArtifactValidationError(
            f"video payload exceeds the {VIDEO_LIMIT // (1024 * 1024)}MB video limit"
        )
    poster = _scrub_optional_poster(payload)

    artifact_dir = _artifact_run_dir()
    _write_binary(artifact_dir, artifact_id, VIDEO_MIMES[mime], result.data)
    normalized: dict[str, Any] = {
        "ref": f"artifact://{artifact_id}",
        "mime": result.mime,
        "byte_size": len(result.data),
    }
    if result.duration_ms is not None:
        normalized["duration_ms"] = result.duration_ms
    if result.width is not None:
        normalized["width"] = result.width
    if result.height is not None:
        normalized["height"] = result.height
    if poster is not None:
        preview_b64, poster_w, poster_h = poster
        normalized["poster_base64"] = preview_b64
        # Poster dims can be a stable fallback when the container omits its own dims.
        if result.width is None and poster_w:
            normalized["width"] = poster_w
        if result.height is None and poster_h:
            normalized["height"] = poster_h
    return normalized


def _validate_audio_transcript(payload: dict[str, Any]) -> str | None:
    transcript = payload.get("transcript")
    if transcript is None:
        return None
    if not isinstance(transcript, str):
        raise ArtifactValidationError("payload.transcript must be a string")
    if len(transcript) > TEXT_LIMIT:
        raise ArtifactValidationError(
            f"audio transcript exceeds the {TEXT_LIMIT // 1000}KB text limit"
        )
    return transcript


def _write_audio(payload: dict[str, Any], artifact_id: str) -> dict[str, Any]:
    data, mime = _decode_media_payload(
        payload,
        kind="audio",
        allowed_mimes=AUDIO_MIMES,
        byte_limit=AUDIO_LIMIT,
        limit_label=f"{AUDIO_LIMIT // (1024 * 1024)}MB audio limit",
        optional_keys={"transcript"},
    )
    # Validate EVERY field before touching the filesystem. A rejected
    # transcript (or any other optional field) that fires after
    # _write_binary would leave an orphaned .wav/.mp3 in the artifact
    # directory — the write is atomic, so the caller can retry with a
    # different id but the earlier bytes stay resident until the run's
    # cleanup path runs.
    try:
        result = scrub_audio(data, mime)
    except MediaScrubError as exc:
        raise ArtifactValidationError(f"audio payload rejected: {exc}") from exc
    if len(result.data) > AUDIO_LIMIT:
        raise ArtifactValidationError(
            f"audio payload exceeds the {AUDIO_LIMIT // (1024 * 1024)}MB audio limit"
        )
    transcript = _validate_audio_transcript(payload)

    artifact_dir = _artifact_run_dir()
    _write_binary(artifact_dir, artifact_id, AUDIO_MIMES[mime], result.data)
    normalized: dict[str, Any] = {
        "ref": f"artifact://{artifact_id}",
        "mime": result.mime,
        "byte_size": len(result.data),
    }
    if result.duration_ms is not None:
        normalized["duration_ms"] = result.duration_ms
    if result.peaks is not None:
        normalized["peaks"] = result.peaks
    if transcript is not None:
        normalized["transcript"] = transcript
    return normalized


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
    elif kind == "pdf":
        artifact.update(_write_pdf(payload, artifact_id))
    elif kind == "video":
        artifact.update(_write_video(payload, artifact_id))
    elif kind == "audio":
        artifact.update(_write_audio(payload, artifact_id))
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
    for field, limit in (("title", 200), ("caption", 500)):
        if field in event and (
            not isinstance(event[field], str) or len(event[field]) > limit
        ):
            return None
    kind = artifact["kind"]
    if kind not in {"image", "pdf", "video", "audio"}:
        payload = {key: item for key, item in artifact.items() if key != "kind"}
        try:
            _validate_text_payload(kind, payload)
        except ArtifactValidationError:
            return None
    try:
        _validated_run_id(str(event.get("id") or ""))
    except ArtifactValidationError:
        return None
    return event


def artifact_from_codex_mcp_tool_result(item: Any) -> dict[str, Any] | None:
    """Return a validated artifact event from a completed Codex MCP tool item."""
    if not isinstance(item, dict):
        return None
    if (
        item.get("type") != "mcpToolCall"
        or item.get("server") != "wiki_artifacts"
        or item.get("tool") != "render_artifact"
        or item.get("status") != "completed"
        or item.get("error") is not None
    ):
        return None
    result = item.get("result")
    if not isinstance(result, dict) or result.get("isError") is True:
        return None
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            event = artifact_from_text(block.get("text"))
            if event is not None:
                return event
    return None


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
        "WIKI_AGENT_ID",
        "WIKI_AGENT_ROLE",
        "WIKI_BACKEND_URL",
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
        tools = [
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
        if os.environ.get("WIKI_AGENT_ROLE") == "orchestrator":
            tools.extend(wiki_agent_tools.TOOL_DEFINITIONS)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": tools},
        }
    if method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") == "render_artifact":
            return _tool_result(request_id, params.get("arguments"))
        if params.get("name") == "search_knowledge":
            return _knowledge_tool_result(request_id, params.get("arguments"))
        if params.get("name") in wiki_agent_tools.TOOL_HANDLERS:
            return wiki_agent_tools.tool_result(
                request_id,
                params["name"],
                params.get("arguments"),
            )
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


def _drain_oversized_line(stream) -> None:
    """Discard the rest of an oversized line in bounded chunks."""
    while True:
        chunk = stream.readline(65536)
        if not chunk or chunk.endswith(b"\n"):
            return


def _emit(response: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> None:
    stream = sys.stdin.buffer
    while True:
        # readline(size) reads up to `size` bytes OR until a newline — this
        # caps the buffered request before json.loads sees it, so an oversized
        # base64 payload cannot exhaust memory before the pre-parse check.
        raw_line = stream.readline(MAX_REQUEST_BYTES)
        if not raw_line:
            return
        if not raw_line.endswith(b"\n"):
            _drain_oversized_line(stream)
            _emit({
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32700,
                    "message": (
                        f"request exceeds {MAX_REQUEST_BYTES}-byte transport limit"
                    ),
                },
            })
            continue
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
            _emit(response)


if __name__ == "__main__":
    main()
