"""MP3 scrubbing — strip ID3v1, ID3v2, and APEv2 tags."""
from __future__ import annotations

import struct
from typing import Final

from .base import MediaScrubError, MediaScrubResult

_ID3V2_MAGIC: Final = b"ID3"
_ID3V1_MAGIC: Final = b"TAG"
_APE_MAGIC: Final = b"APETAGEX"
_APE_HEADER_FOOTER_LEN: Final = 32
# APEv2 flag bit indices per the official spec.
_APE_FLAG_IS_HEADER: Final = 1 << 31
_APE_FLAG_NO_FOOTER: Final = 1 << 30
_APE_FLAG_HAS_HEADER: Final = 1 << 29
_APE_ITEM_COUNT_MAX: Final = 0xFFFF
_APE_TAG_SIZE_MAX: Final = 32 * 1024 * 1024

_MP3_BITRATE_V1_L3: Final = (
    0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0,
)
_MP3_BITRATE_V2_L3: Final = (
    0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0,
)
_MP3_SAMPLE_RATE_V1: Final = (44100, 48000, 32000, 0)
_MP3_SAMPLE_RATE_V2: Final = (22050, 24000, 16000, 0)
_MP3_SAMPLE_RATE_V25: Final = (11025, 12000, 8000, 0)


def scrub_mp3(data: bytes) -> MediaScrubResult:
    if len(data) < 4:
        raise MediaScrubError("mp3 payload too small")

    start = 0
    if data.startswith(_ID3V2_MAGIC):
        if len(data) < 10:
            raise MediaScrubError("mp3 id3v2 header truncated")
        b1, b2, b3, b4 = data[6:10]
        if b1 & 0x80 or b2 & 0x80 or b3 & 0x80 or b4 & 0x80:
            raise MediaScrubError("mp3 id3v2 size not synchsafe")
        tag_size = (b1 << 21) | (b2 << 14) | (b3 << 7) | b4
        start = 10 + tag_size
        if start > len(data):
            raise MediaScrubError("mp3 id3v2 size larger than payload")
    end = len(data)
    start = _mp3_strip_ape_header(data, start, end)
    if end - start >= 128 and data[end - 128:end - 125] == _ID3V1_MAGIC:
        end -= 128
    end = _mp3_strip_ape_footer(data, start, end)
    if end - start < 4:
        raise MediaScrubError("mp3 has no audio frames after tag strip")

    _mp3_require_frames(data, start, end, minimum=3)
    return MediaScrubResult(
        data=bytes(data[start:end]),
        mime="audio/mpeg",
        duration_ms=None,
        width=None,
        height=None,
    )


def _mp3_read_ape_meta(data: bytes, preamble_at: int) -> tuple[int, int, int]:
    if preamble_at < 0 or preamble_at + _APE_HEADER_FOOTER_LEN > len(data):
        raise MediaScrubError("mp3 APEv2 preamble out of bounds")
    tag_size, item_count, flags = struct.unpack(
        "<III", data[preamble_at + 12:preamble_at + 24],
    )
    if tag_size < _APE_HEADER_FOOTER_LEN:
        raise MediaScrubError(
            f"mp3 APEv2 tag_size {tag_size} smaller than minimum 32"
        )
    if tag_size > _APE_TAG_SIZE_MAX:
        raise MediaScrubError(
            f"mp3 APEv2 tag_size {tag_size} exceeds implausible ceiling"
        )
    if item_count > _APE_ITEM_COUNT_MAX:
        raise MediaScrubError(
            f"mp3 APEv2 item count {item_count} exceeds implausible ceiling"
        )
    return tag_size, item_count, flags


def _mp3_strip_ape_header(data: bytes, start: int, end: int) -> int:
    if end - start < _APE_HEADER_FOOTER_LEN:
        return start
    if data[start:start + 8] != _APE_MAGIC:
        return start
    tag_size, _item_count, flags = _mp3_read_ape_meta(data, start)
    if flags & _APE_FLAG_IS_HEADER == 0:
        raise MediaScrubError("mp3 APEv2 marker at start is not a header")
    advance = _APE_HEADER_FOOTER_LEN + tag_size
    if advance > end - start:
        raise MediaScrubError("mp3 APEv2 header size larger than payload")
    return start + advance


def _mp3_strip_ape_footer(data: bytes, start: int, end: int) -> int:
    if end - start < _APE_HEADER_FOOTER_LEN:
        return end
    footer_start = end - _APE_HEADER_FOOTER_LEN
    if data[footer_start:footer_start + 8] != _APE_MAGIC:
        return end
    tag_size, _item_count, flags = _mp3_read_ape_meta(data, footer_start)
    if flags & _APE_FLAG_IS_HEADER:
        raise MediaScrubError("mp3 APEv2 marker at end is a header")
    trim = tag_size
    if flags & _APE_FLAG_HAS_HEADER:
        trim += _APE_HEADER_FOOTER_LEN
    if trim > end - start:
        raise MediaScrubError("mp3 APEv2 footer size larger than payload")
    return end - trim


def _mp3_require_frames(data: bytes, start: int, end: int, *, minimum: int) -> None:
    offset = start
    for _ in range(minimum):
        frame_len = _mp3_frame_length(data, offset, end)
        if frame_len is None:
            raise MediaScrubError(
                f"mp3 frame stream broken at offset {offset - start}"
            )
        offset += frame_len
        if offset > end:
            raise MediaScrubError("mp3 frame stream truncated")


def _mp3_frame_length(data: bytes, offset: int, end: int) -> int | None:
    if offset + 4 > end:
        return None
    if data[offset] != 0xFF or (data[offset + 1] & 0xE0) != 0xE0:
        return None
    version_bits = (data[offset + 1] >> 3) & 0x03
    layer_bits = (data[offset + 1] >> 1) & 0x03
    if version_bits == 1 or layer_bits == 0:
        return None
    bitrate_bits = (data[offset + 2] >> 4) & 0x0F
    sample_rate_bits = (data[offset + 2] >> 2) & 0x03
    padding = (data[offset + 2] >> 1) & 0x01
    if sample_rate_bits == 3 or bitrate_bits in (0, 15):
        return None
    if version_bits == 3:
        bitrate = _MP3_BITRATE_V1_L3[bitrate_bits] * 1000
        sample_rate = _MP3_SAMPLE_RATE_V1[sample_rate_bits]
        samples_per_frame = 1152
    elif version_bits == 2:
        bitrate = _MP3_BITRATE_V2_L3[bitrate_bits] * 1000
        sample_rate = _MP3_SAMPLE_RATE_V2[sample_rate_bits]
        samples_per_frame = 576
    else:
        bitrate = _MP3_BITRATE_V2_L3[bitrate_bits] * 1000
        sample_rate = _MP3_SAMPLE_RATE_V25[sample_rate_bits]
        samples_per_frame = 576
    if bitrate == 0 or sample_rate == 0:
        return None
    frame_length = (samples_per_frame // 8 * bitrate) // sample_rate + padding
    if frame_length < 4:
        return None
    return frame_length
