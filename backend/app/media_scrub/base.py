"""Shared types and constants for the media scrubbers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final


VIDEO_MIMES: Final = {
    "video/mp4": "mp4",
    "image/gif": "gif",
}
AUDIO_MIMES: Final = {
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
}

# Streaming waveform cap. Peaks are stored as unsigned 8-bit values.
WAVEFORM_MAX_PEAKS: Final = 512


class MediaScrubError(ValueError):
    """Raised when the payload cannot be validated or safely rebuilt."""


@dataclass(frozen=True)
class MediaScrubResult:
    data: bytes
    mime: str
    duration_ms: int | None
    width: int | None
    height: int | None
    peaks: list[int] | None = None
