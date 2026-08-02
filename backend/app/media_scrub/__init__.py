"""Media artifact scrubbers — public entry points + test-visible internals.

For every supported container, the stored bytes are REBUILT from parsed
and validated structure. Copying input bytes wholesale is not permitted
at any nesting level; a container whose children cannot all be rebuilt
rejects the whole file. See each format module's docstring for the
per-container acceptance rules.
"""
from __future__ import annotations

from .base import (
    AUDIO_MIMES,
    MediaScrubError,
    MediaScrubResult,
    VIDEO_MIMES,
    WAVEFORM_MAX_PEAKS,
)
from .gif import scrub_gif
from .mp3 import (
    _APE_FLAG_HAS_HEADER,
    _APE_FLAG_IS_HEADER,
    _APE_FLAG_NO_FOOTER,
    _APE_HEADER_FOOTER_LEN,
    _APE_MAGIC,
    scrub_mp3,
)
from .mp4 import scrub_mp4
from .wav import (
    _WAV_KSDATAFORMAT_IEEE_FLOAT,
    _WAV_KSDATAFORMAT_PCM,
    _wav_stream_peaks,
    scrub_wav,
)
from .webm import scrub_webm

__all__ = [
    "AUDIO_MIMES",
    "MediaScrubError",
    "MediaScrubResult",
    "VIDEO_MIMES",
    "WAVEFORM_MAX_PEAKS",
    "scrub_audio",
    "scrub_video",
]


def scrub_video(data: bytes, mime: str) -> MediaScrubResult:
    if mime == "video/mp4":
        return scrub_mp4(data)
    if mime == "image/gif":
        return scrub_gif(data)
    if mime == "video/webm":
        return scrub_webm(data)
    raise MediaScrubError(f"unsupported video mime: {mime}")


def scrub_audio(data: bytes, mime: str) -> MediaScrubResult:
    if mime == "audio/wav":
        return scrub_wav(data)
    if mime == "audio/mpeg":
        return scrub_mp3(data)
    raise MediaScrubError(f"unsupported audio mime: {mime}")
