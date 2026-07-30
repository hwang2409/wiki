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
    # Front-of-file APEv2 (header form). This one variant only appears at
    # the very front so it advances `start`; every other trailing tag is
    # handled by the fixpoint loop below.
    start = _mp3_strip_ape_header(data, start, end)
    # Fixpoint trailing-tag strip: round-6 review flagged that an ID3v1
    # placed BEFORE an APEv2 footer survived because the ID3v1 branch
    # only ran once and only if it was the tail. Loop across ID3v1,
    # APEv2 footer, Lyrics3v1, and Lyrics3v2 until nothing changes, so
    # any ordering (ID3v1→APE, APE→ID3v1, Lyrics3→ID3v1→APE, …) is
    # peeled cleanly off the tail.
    end = _mp3_strip_trailing_tags_to_fixpoint(data, start, end)
    if end - start < 4:
        raise MediaScrubError("mp3 has no audio frames after tag strip")

    # Round-7 review: the previous validator only walked the first three
    # frames. An attacker could pad the tail with unstructured bytes and
    # they would survive because the walk never reached them. Full-stream
    # validation now walks EVERY frame and requires the final walk step
    # to land exactly on `end` — no untracked bytes may exist in the
    # frame stream at all.
    _mp3_validate_full_frame_stream(data, start, end)
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


_LYRICS3_BEGIN: Final = b"LYRICSBEGIN"
_LYRICS3V1_END: Final = b"LYRICSEND"
_LYRICS3V2_MAGIC: Final = b"LYRICS200"
_LYRICS3V2_SIZE_LEN: Final = 6


def _mp3_strip_trailing_tags_to_fixpoint(data: bytes, start: int, end: int) -> int:
    """Peel ID3v1, APEv2, Lyrics3v1, and Lyrics3v2 off the tail until the
    end position stops moving. Order-independent — the loop keeps stripping
    whichever tag currently sits at the tail.
    """
    while True:
        prev_end = end
        # ID3v1: fixed 128-byte block ending with "TAG" at offset end-128.
        if end - start >= 128 and data[end - 128:end - 125] == _ID3V1_MAGIC:
            end -= 128
            continue
        # APEv2 footer sits at end-32 with an APETAGEX preamble. The
        # helper returns a smaller `end` if it stripped, and raises on
        # malformed sizes. Nothing to do here if no preamble.
        stripped = _mp3_strip_ape_footer(data, start, end)
        if stripped != end:
            end = stripped
            continue
        # Lyrics3v2: final 15 bytes are "LYRICS200" + 6 ASCII digits
        # giving the tag size (of items only — the 15-byte footer itself
        # AND the preceding LYRICSBEGIN sit outside it). The parser
        # accepts only self-consistent sizes; anything malformed rejects.
        if end - start >= 15 and data[end - 15:end - 6] == _LYRICS3V2_MAGIC:
            size_field = data[end - 6:end]
            try:
                items_size = int(size_field.decode("ascii"))
            except ValueError as exc:
                raise MediaScrubError("mp3 Lyrics3v2 size not ASCII digits") from exc
            if items_size < 0:
                raise MediaScrubError("mp3 Lyrics3v2 size negative")
            tag_start = end - 15 - items_size
            begin_at = tag_start - len(_LYRICS3_BEGIN)
            if begin_at < start:
                raise MediaScrubError("mp3 Lyrics3v2 tag size larger than payload")
            if data[begin_at:begin_at + len(_LYRICS3_BEGIN)] != _LYRICS3_BEGIN:
                raise MediaScrubError(
                    "mp3 Lyrics3v2 LYRICSBEGIN marker missing before tag body"
                )
            end = begin_at
            continue
        # Lyrics3v1: ends with "LYRICSEND" (no size field). Walk back to
        # the matching LYRICSBEGIN marker. Refuse if we can't find one.
        if end - start >= len(_LYRICS3V1_END) and data[end - len(_LYRICS3V1_END):end] == _LYRICS3V1_END:
            search_end = end - len(_LYRICS3V1_END)
            begin_at = data.rfind(_LYRICS3_BEGIN, start, search_end)
            if begin_at < 0:
                raise MediaScrubError(
                    "mp3 Lyrics3v1 LYRICSEND without matching LYRICSBEGIN"
                )
            end = begin_at
            continue
        if end == prev_end:
            return end


def _mp3_validate_full_frame_stream(data: bytes, start: int, end: int) -> None:
    """Walk every MPEG frame between `start` and `end`. The walk must land
    exactly on `end` — any unaccounted-for byte in the payload rejects the
    file. This is the round-7 fix for the pre-R7 validator that only
    checked the first three frames.
    """
    offset = start
    frames_seen = 0
    while offset < end:
        frame_len = _mp3_frame_length(data, offset, end)
        if frame_len is None:
            raise MediaScrubError(
                f"mp3 frame stream broken at offset {offset - start} "
                f"({frames_seen} frames validated)"
            )
        offset += frame_len
        frames_seen += 1
    if offset != end:
        raise MediaScrubError(
            f"mp3 frame stream ends {offset - end} bytes past declared end"
        )
    if frames_seen < 1:
        raise MediaScrubError("mp3 frame stream contains zero frames")


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
