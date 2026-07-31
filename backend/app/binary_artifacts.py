"""Bounded binary media ingestion and normalized artifact results.

The tool dispatcher supplies filesystem and sentinel validation callbacks. This
module owns payload decoding, media scrubbing, poster handling, and the single
normalized-result boundary used by video and audio artifacts.
"""
from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .image_scrub import ImageScrubError, scrub_image
from .media_scrub import AUDIO_MIMES, VIDEO_MIMES, MediaScrubError, scrub_audio, scrub_video


IMAGE_LIMIT = 5 * 1024 * 1024
AUDIO_LIMIT = 20 * 1024 * 1024
VIDEO_LIMIT = 40 * 1024 * 1024
TEXT_LIMIT = 100_000
IMAGE_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}


class ArtifactValidationError(ValueError):
    """Raised when a binary artifact payload is outside the supported contract."""


def _require_keys(
    value: dict[str, Any], *, required: set[str], optional: set[str] = frozenset(),
) -> None:
    missing = required - value.keys()
    if missing:
        raise ArtifactValidationError(f"missing required field: {sorted(missing)[0]}")
    extra = value.keys() - required - optional
    if extra:
        raise ArtifactValidationError(f"unknown field: {sorted(extra)[0]}")


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ArtifactValidationError(f"{field} must be a non-empty string")
    return value


def _decode_base64(value: Any, field: str, limit: int, label: str) -> bytes:
    encoded = _require_string(value, field)
    if len(encoded) > ((limit + 2) // 3) * 4 + 4:
        raise ArtifactValidationError(f"{field} exceeds the {label}")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ArtifactValidationError(f"{field} is not valid base64") from exc
    if len(decoded) > limit:
        raise ArtifactValidationError(f"{field} exceeds the {label}")
    return decoded


def _decode_media_payload(
    payload: dict[str, Any],
    *,
    kind: str,
    allowed_mimes: dict[str, str],
    byte_limit: int,
    limit_label: str,
    read_path: Callable[..., bytes],
    optional_keys: set[str] = frozenset(),
) -> tuple[bytes, str]:
    _require_keys(
        payload,
        required={"mime"},
        optional=optional_keys | {"data_base64", "path"},
    )
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
        data = _decode_base64(payload["data_base64"], "payload.data_base64", byte_limit, limit_label)
    else:
        data = read_path(
            str(payload["path"]),
            kind=kind,
            byte_limit=byte_limit,
            limit_label=limit_label,
        )
    return data, mime


def _scrub_optional_poster(
    payload: dict[str, Any], image_limit: int,
) -> tuple[str, int, int] | None:
    poster = payload.get("poster_base64")
    if poster is None:
        return None
    poster_bytes = _decode_base64(
        poster,
        "payload.poster_base64",
        image_limit,
        f"{image_limit // (1024 * 1024)}MB image limit",
    )
    poster_mime = payload.get("poster_mime", "image/png")
    if poster_mime not in IMAGE_TYPES:
        raise ArtifactValidationError(
            "payload.poster_mime must be image/png, image/jpeg, or image/webp"
        )
    try:
        result = scrub_image(poster_bytes, poster_mime)
    except ImageScrubError as exc:
        raise ArtifactValidationError(f"video poster rejected: {exc}") from exc
    if len(result.data) > image_limit:
        raise ArtifactValidationError(
            f"video poster exceeds the {image_limit // (1024 * 1024)}MB image limit"
        )
    full_data_url = (
        f"data:{result.mime};base64,{base64.b64encode(result.data).decode('ascii')}"
    )
    return full_data_url, result.width, result.height


def ingest_binary_artifact(
    kind: str,
    payload: dict[str, Any],
    artifact_id: str,
    *,
    read_path: Callable[..., bytes],
    artifact_dir: Callable[[], Path],
    write_binary: Callable[[Path, str, str, bytes], Path],
    validate_normalized: Callable[[str, str, dict[str, Any]], None],
    scrub_video_fn: Callable[[bytes, str], Any] = scrub_video,
    scrub_audio_fn: Callable[[bytes, str], Any] = scrub_audio,
) -> dict[str, Any]:
    """Scrub one media payload, write it, and return its normalized metadata."""
    if kind == "video":
        data, mime = _decode_media_payload(
            payload,
            kind="video",
            allowed_mimes=VIDEO_MIMES,
            byte_limit=VIDEO_LIMIT,
            limit_label="40MB video limit",
            read_path=read_path,
            optional_keys={"poster_base64", "poster_mime"},
        )
        try:
            result = scrub_video_fn(data, mime)
        except MediaScrubError as exc:
            raise ArtifactValidationError(f"video payload rejected: {exc}") from exc
        if len(result.data) > VIDEO_LIMIT:
            raise ArtifactValidationError("video payload exceeds the 40MB video limit")
        poster = _scrub_optional_poster(payload, IMAGE_LIMIT)
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
            poster_b64, poster_w, poster_h = poster
            normalized["poster_base64"] = poster_b64
            if result.width is None and poster_w:
                normalized["width"] = poster_w
            if result.height is None and poster_h:
                normalized["height"] = poster_h
        validate_normalized(kind, artifact_id, normalized)
        write_binary(artifact_dir(), artifact_id, VIDEO_MIMES[mime], result.data)
        return normalized

    if kind == "audio":
        _require_keys(
            payload,
            required={"mime"},
            optional={"data_base64", "path", "transcript"},
        )
        data, mime = _decode_media_payload(
            payload,
            kind="audio",
            allowed_mimes=AUDIO_MIMES,
            byte_limit=AUDIO_LIMIT,
            limit_label="20MB audio limit",
            read_path=read_path,
            optional_keys={"transcript"},
        )
        try:
            result = scrub_audio_fn(data, mime)
        except MediaScrubError as exc:
            raise ArtifactValidationError(f"audio payload rejected: {exc}") from exc
        if len(result.data) > AUDIO_LIMIT:
            raise ArtifactValidationError("audio payload exceeds the 20MB audio limit")
        transcript = payload.get("transcript")
        if transcript is not None:
            if not isinstance(transcript, str):
                raise ArtifactValidationError("payload.transcript must be a string")
            if len(transcript.encode("utf-8")) > TEXT_LIMIT:
                raise ArtifactValidationError("audio transcript exceeds the 100KB text limit")
        normalized = {
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
        validate_normalized(kind, artifact_id, normalized)
        write_binary(artifact_dir(), artifact_id, AUDIO_MIMES[mime], result.data)
        return normalized

    raise ArtifactValidationError(f"unsupported binary artifact kind: {kind}")
