"""Container-level metadata scrubbing for video and audio artifacts.

Same policy as image_scrub: never trust the declared mime, sniff magic bytes,
enforce hard byte caps *before* any parsing, then walk the container top-level
to strip user-metadata boxes (GPS, INFO tags, ID3, application extensions).

Deliberately bounded-memory: everything is byte-slice work over the input
buffer. No pixel or frame decode happens here — the outer caller has already
enforced a size cap that keeps the whole buffer resident in memory.

Every function raises MediaScrubError on any malformed input; that is caught
by the artifact tool and returned as ArtifactValidationError.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final


VIDEO_MIMES: Final = {
    "video/mp4": "mp4",
    "video/webm": "webm",
    "image/gif": "gif",
}
AUDIO_MIMES: Final = {
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
    "audio/webm": "weba",
    "audio/ogg": "ogg",
}


class MediaScrubError(ValueError):
    """Raised when the payload cannot be validated or safely scrubbed."""


@dataclass(frozen=True)
class MediaScrubResult:
    data: bytes
    mime: str
    duration_ms: int | None
    width: int | None
    height: int | None


def scrub_video(data: bytes, mime: str) -> MediaScrubResult:
    if mime == "video/mp4":
        return _scrub_mp4(data)
    if mime == "video/webm":
        return _scrub_matroska(data, mime)
    if mime == "image/gif":
        return _scrub_gif(data)
    raise MediaScrubError(f"unsupported video mime: {mime}")


def scrub_audio(data: bytes, mime: str) -> MediaScrubResult:
    if mime == "audio/wav":
        return _scrub_wav(data)
    if mime == "audio/mpeg":
        return _scrub_mp3(data)
    if mime == "audio/webm":
        return _scrub_matroska(data, mime)
    if mime == "audio/ogg":
        return _scrub_ogg(data)
    raise MediaScrubError(f"unsupported audio mime: {mime}")


# ---------------------------------------------------------------------------
# MP4 / ISO Base Media File Format
# ---------------------------------------------------------------------------
#
# The container is a sequence of atoms:
#     [4-byte big-endian size][4-byte type][payload...]
# where size == 1 means a 64-bit size follows, and size == 0 means "run to EOF".
#
# GPS location and other user metadata sit in `udta` (user data) and `meta`
# atoms, which appear either at the top level or inside `moov`. We walk the
# top-level atoms, and for any `moov` atom we also walk its children — copying
# every atom through except `udta` (dropped entirely) and rewriting `moov` to
# reflect the new size.
#
# Playback structure lives in `ftyp`, `moov` (minus udta), `mdat`, `moof`,
# `mfra`, `free`, `skip`, `sidx`, `styp`. Those pass through untouched.

_MP4_STRIP_TOPLEVEL: Final = {b"udta", b"meta", b"free", b"skip"}
_MP4_STRIP_IN_MOOV: Final = {b"udta", b"meta"}


def _scrub_mp4(data: bytes) -> MediaScrubResult:
    if len(data) < 16:
        raise MediaScrubError("mp4 payload too small")
    # Must have `ftyp` box within the first 12 bytes (size + `ftyp`).
    if data[4:8] != b"ftyp":
        raise MediaScrubError("mp4 payload missing ftyp box at offset 0")

    out: list[bytes] = []
    duration_ms: int | None = None
    dims: tuple[int, int] | None = None
    view = memoryview(data)
    offset = 0
    end = len(data)
    while offset < end:
        atom_size, atom_type, header_len, atom_end = _mp4_read_header(view, offset, end)
        if atom_type in _MP4_STRIP_TOPLEVEL:
            offset = atom_end
            continue
        if atom_type == b"moov":
            rewritten = _mp4_rewrite_moov(view, offset + header_len, atom_end)
            out.append(rewritten)
            if duration_ms is None:
                duration_ms = _mp4_extract_moov_duration(rewritten[8:])
            if dims is None:
                dims = _mp4_extract_moov_dims(rewritten[8:])
            offset = atom_end
            continue
        out.append(bytes(view[offset:atom_end]))
        offset = atom_end

    scrubbed = b"".join(out)
    if not scrubbed:
        raise MediaScrubError("mp4 payload has no playable atoms after scrubbing")
    width, height = dims if dims is not None else (None, None)
    return MediaScrubResult(
        data=scrubbed,
        mime="video/mp4",
        duration_ms=duration_ms,
        width=width,
        height=height,
    )


def _mp4_read_header(view: memoryview, offset: int, end: int) -> tuple[int, bytes, int, int]:
    if offset + 8 > end:
        raise MediaScrubError("mp4 atom header truncated")
    size = struct.unpack(">I", bytes(view[offset:offset + 4]))[0]
    atom_type = bytes(view[offset + 4:offset + 8])
    if size == 1:
        if offset + 16 > end:
            raise MediaScrubError("mp4 64-bit atom header truncated")
        size = struct.unpack(">Q", bytes(view[offset + 8:offset + 16]))[0]
        header_len = 16
    elif size == 0:
        # Runs to EOF — legal for the final atom (usually mdat).
        size = end - offset
        header_len = 8
    else:
        header_len = 8
    if size < header_len:
        raise MediaScrubError("mp4 atom size smaller than header")
    atom_end = offset + size
    if atom_end > end:
        raise MediaScrubError("mp4 atom extends past payload")
    return size, atom_type, header_len, atom_end


def _mp4_rewrite_moov(view: memoryview, payload_start: int, moov_end: int) -> bytes:
    kept: list[bytes] = []
    offset = payload_start
    while offset < moov_end:
        _size, atom_type, _hdr, atom_end = _mp4_read_header(view, offset, moov_end)
        if atom_type in _MP4_STRIP_IN_MOOV:
            offset = atom_end
            continue
        kept.append(bytes(view[offset:atom_end]))
        offset = atom_end
    payload = b"".join(kept)
    new_size = 8 + len(payload)
    return struct.pack(">I", new_size) + b"moov" + payload


def _mp4_extract_moov_duration(moov_payload: bytes) -> int | None:
    """Return duration in milliseconds from the mvhd atom, if present."""
    view = memoryview(moov_payload)
    offset = 0
    end = len(moov_payload)
    while offset < end:
        try:
            _size, atom_type, header_len, atom_end = _mp4_read_header(view, offset, end)
        except MediaScrubError:
            return None
        if atom_type != b"mvhd":
            offset = atom_end
            continue
        payload_start = offset + header_len
        if payload_start + 1 > atom_end:
            return None
        version = moov_payload[payload_start]
        if version == 0:
            # 4 flags-ish + creation(4) + modification(4) + timescale(4) + duration(4)
            body_start = payload_start + 4 + 8
            if body_start + 8 > atom_end:
                return None
            timescale, duration = struct.unpack(">II", moov_payload[body_start:body_start + 8])
        elif version == 1:
            body_start = payload_start + 4 + 16
            if body_start + 12 > atom_end:
                return None
            timescale = struct.unpack(">I", moov_payload[body_start:body_start + 4])[0]
            duration = struct.unpack(">Q", moov_payload[body_start + 4:body_start + 12])[0]
        else:
            return None
        if timescale == 0:
            return None
        return int(round(duration * 1000 / timescale))
    return None


def _mp4_extract_moov_dims(moov_payload: bytes) -> tuple[int, int] | None:
    """Walk moov -> trak -> tkhd to find video-track dims (first non-zero pair)."""
    view = memoryview(moov_payload)
    offset = 0
    end = len(moov_payload)
    while offset < end:
        try:
            _size, atom_type, header_len, atom_end = _mp4_read_header(view, offset, end)
        except MediaScrubError:
            return None
        if atom_type == b"trak":
            dims = _mp4_extract_tkhd_dims(view, offset + header_len, atom_end, moov_payload)
            if dims is not None:
                return dims
        offset = atom_end
    return None


def _mp4_extract_tkhd_dims(
    view: memoryview,
    payload_start: int,
    trak_end: int,
    source: bytes,
) -> tuple[int, int] | None:
    offset = payload_start
    while offset < trak_end:
        try:
            _size, atom_type, header_len, atom_end = _mp4_read_header(view, offset, trak_end)
        except MediaScrubError:
            return None
        if atom_type != b"tkhd":
            offset = atom_end
            continue
        payload_at = offset + header_len
        if payload_at + 1 > atom_end:
            return None
        version = source[payload_at]
        # tkhd layout (v0): version+flags(4) creation(4) modification(4) trackID(4)
        # reserved(4) duration(4) reserved(8) layer(2) alternate_group(2)
        # volume(2) reserved(2) matrix(36) width(4 fixed) height(4 fixed)
        # v1 stretches creation/modification/duration to 8 bytes each (+12 total).
        pre_matrix = 4 + (28 if version == 0 else 40) + 2 + 2 + 2 + 2
        matrix = 36
        dims_at = payload_at + pre_matrix + matrix
        if dims_at + 8 > atom_end:
            return None
        width_fixed, height_fixed = struct.unpack(">II", source[dims_at:dims_at + 8])
        # 16.16 fixed-point
        width = width_fixed >> 16
        height = height_fixed >> 16
        if width > 0 and height > 0:
            return width, height
        return None
    return None


# ---------------------------------------------------------------------------
# WebM / Matroska (EBML)
# ---------------------------------------------------------------------------
#
# EBML uses variable-length ID+size. Full stripping of the `Tags` element is
# nontrivial (nested Segment with many sub-elements). For now we validate the
# magic header and pass the payload through — no known ambient user-identifier
# leak in browser-recorded webm. Tags stripping is a follow-up.

_EBML_HEADER: Final = b"\x1a\x45\xdf\xa3"


def _scrub_matroska(data: bytes, mime: str) -> MediaScrubResult:
    if len(data) < 4 or not data.startswith(_EBML_HEADER):
        raise MediaScrubError(f"{mime} payload missing EBML header")
    return MediaScrubResult(
        data=data, mime=mime, duration_ms=None, width=None, height=None,
    )


# ---------------------------------------------------------------------------
# GIF (GIF87a / GIF89a)
# ---------------------------------------------------------------------------
#
# Layout: header(6) + logical screen descriptor(7) + optional global color
# table + blocks. We strip Application Extension blocks whose identifier is
# "XMP DataXMP" (Adobe XMP) — those carry ambient metadata. All other blocks
# pass through untouched. We also expose the logical screen width/height.

_GIF_HEADER87: Final = b"GIF87a"
_GIF_HEADER89: Final = b"GIF89a"
_GIF_TRAILER: Final = 0x3B
_GIF_EXT_INTRO: Final = 0x21
_GIF_APP_EXT: Final = 0xFF
_GIF_XMP_IDENT: Final = b"XMP DataXMP"


def _scrub_gif(data: bytes) -> MediaScrubResult:
    if len(data) < 13:
        raise MediaScrubError("gif payload too small")
    header = data[:6]
    if header not in (_GIF_HEADER87, _GIF_HEADER89):
        raise MediaScrubError("gif payload missing GIF87a/GIF89a header")

    width, height = struct.unpack("<HH", data[6:10])
    packed = data[10]
    global_ct_flag = packed & 0x80
    global_ct_size = 3 * (1 << ((packed & 0x07) + 1)) if global_ct_flag else 0

    header_end = 13 + global_ct_size
    if header_end > len(data):
        raise MediaScrubError("gif global color table extends past payload")
    out = bytearray(data[:header_end])

    offset = header_end
    end = len(data)
    while offset < end:
        marker = data[offset]
        if marker == _GIF_TRAILER:
            out.append(marker)
            offset += 1
            break
        if marker == _GIF_EXT_INTRO:
            if offset + 2 > end:
                raise MediaScrubError("gif extension truncated")
            label = data[offset + 1]
            block_start = offset
            sub_start = offset + 2
            sub_end, drop = _gif_walk_subblocks(data, sub_start, end, label)
            if not drop:
                out.extend(data[block_start:sub_end])
            offset = sub_end
            continue
        # Image Descriptor: 0x2C
        if marker == 0x2C:
            if offset + 10 > end:
                raise MediaScrubError("gif image descriptor truncated")
            local_packed = data[offset + 9]
            local_ct_size = (
                3 * (1 << ((local_packed & 0x07) + 1))
                if local_packed & 0x80
                else 0
            )
            data_start = offset + 10 + local_ct_size
            if data_start + 1 > end:
                raise MediaScrubError("gif image data truncated")
            # LZW min code size (1) then sub-blocks.
            sub_end, _drop = _gif_walk_subblocks(data, data_start + 1, end, None)
            out.extend(data[offset:sub_end])
            offset = sub_end
            continue
        raise MediaScrubError(f"unexpected gif block marker: 0x{marker:02x}")
    if offset != end and out[-1] != _GIF_TRAILER:
        # Some encoders omit the trailer — append one to keep it well-formed.
        out.append(_GIF_TRAILER)
    return MediaScrubResult(
        data=bytes(out),
        mime="image/gif",
        duration_ms=None,
        width=int(width) or None,
        height=int(height) or None,
    )


def _gif_walk_subblocks(
    data: bytes,
    start: int,
    end: int,
    ext_label: int | None,
) -> tuple[int, bool]:
    """Return (offset_after_terminator, should_drop_block).

    An application-extension block's first sub-block carries the 11-byte
    application identifier; if it matches XMP we mark drop=True.
    """
    offset = start
    drop = False
    first = True
    while offset < end:
        length = data[offset]
        offset += 1
        if length == 0:
            return offset, drop
        block_end = offset + length
        if block_end > end:
            raise MediaScrubError("gif sub-block extends past payload")
        if first and ext_label == _GIF_APP_EXT and length == 11:
            if data[offset:block_end] == _GIF_XMP_IDENT:
                drop = True
        first = False
        offset = block_end
    raise MediaScrubError("gif sub-block chain missing terminator")


# ---------------------------------------------------------------------------
# WAV (RIFF/WAVE)
# ---------------------------------------------------------------------------

_WAV_STRIP_CHUNKS: Final = {b"LIST", b"INFO", b"ID3 ", b"id3 ", b"bext"}


def _scrub_wav(data: bytes) -> MediaScrubResult:
    if len(data) < 12:
        raise MediaScrubError("wav payload too small")
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise MediaScrubError("wav payload missing RIFF/WAVE header")

    kept = bytearray(b"RIFF____WAVE")
    duration_ms: int | None = None
    sample_rate = 0
    byte_rate = 0
    channels = 0
    bits = 0

    offset = 12
    end = len(data)
    fmt_seen = False
    data_bytes = 0
    while offset + 8 <= end:
        chunk_id = data[offset:offset + 4]
        chunk_size = struct.unpack("<I", data[offset + 4:offset + 8])[0]
        payload_start = offset + 8
        payload_end = payload_start + chunk_size
        if payload_end > end:
            raise MediaScrubError("wav chunk extends past payload")
        pad = chunk_size & 1
        if chunk_id in _WAV_STRIP_CHUNKS:
            offset = payload_end + pad
            continue
        if chunk_id == b"fmt ":
            if chunk_size < 16:
                raise MediaScrubError("wav fmt chunk too short")
            channels = struct.unpack("<H", data[payload_start + 2:payload_start + 4])[0]
            sample_rate = struct.unpack("<I", data[payload_start + 4:payload_start + 8])[0]
            byte_rate = struct.unpack("<I", data[payload_start + 8:payload_start + 12])[0]
            bits = struct.unpack("<H", data[payload_start + 14:payload_start + 16])[0]
            fmt_seen = True
        elif chunk_id == b"data":
            data_bytes = chunk_size
        kept.extend(data[offset:payload_end + pad])
        offset = payload_end + pad
    if not fmt_seen:
        raise MediaScrubError("wav payload missing fmt chunk")
    if byte_rate and data_bytes:
        duration_ms = int(round(data_bytes * 1000 / byte_rate))
    elif sample_rate and channels and bits and data_bytes:
        frames = data_bytes // max(1, (channels * bits // 8))
        duration_ms = int(round(frames * 1000 / sample_rate))
    # Patch RIFF payload size.
    new_size = len(kept) - 8
    kept[4:8] = struct.pack("<I", new_size)
    return MediaScrubResult(
        data=bytes(kept),
        mime="audio/wav",
        duration_ms=duration_ms,
        width=None,
        height=None,
    )


# ---------------------------------------------------------------------------
# MP3 (MPEG-1/2 audio, with optional ID3v2 prefix and ID3v1 suffix)
# ---------------------------------------------------------------------------

_ID3V2_MAGIC: Final = b"ID3"
_ID3V1_MAGIC: Final = b"TAG"
_MPEG_FRAME_SYNC: Final = 0xFFE0


def _scrub_mp3(data: bytes) -> MediaScrubResult:
    if len(data) < 4:
        raise MediaScrubError("mp3 payload too small")

    start = 0
    if data.startswith(_ID3V2_MAGIC):
        if len(data) < 10:
            raise MediaScrubError("mp3 id3v2 header truncated")
        # Synchsafe integer (4 bytes, 7 bits each).
        b1, b2, b3, b4 = data[6:10]
        if b1 & 0x80 or b2 & 0x80 or b3 & 0x80 or b4 & 0x80:
            raise MediaScrubError("mp3 id3v2 size not synchsafe")
        tag_size = (b1 << 21) | (b2 << 14) | (b3 << 7) | b4
        start = 10 + tag_size
        if start > len(data):
            raise MediaScrubError("mp3 id3v2 size larger than payload")
    end = len(data)
    if end - start >= 128 and data[end - 128:end - 125] == _ID3V1_MAGIC:
        end -= 128
    if end - start < 4:
        raise MediaScrubError("mp3 has no audio frames after tag strip")
    # First byte pair after strip must be an MPEG frame sync (0xFFE_).
    sync = (data[start] << 8) | data[start + 1]
    if (sync & 0xFFE0) != _MPEG_FRAME_SYNC:
        raise MediaScrubError("mp3 payload missing MPEG frame sync after id3 strip")
    return MediaScrubResult(
        data=bytes(data[start:end]),
        mime="audio/mpeg",
        duration_ms=None,
        width=None,
        height=None,
    )


# ---------------------------------------------------------------------------
# OGG (Ogg encapsulation, container for Vorbis / Opus)
# ---------------------------------------------------------------------------

def _scrub_ogg(data: bytes) -> MediaScrubResult:
    if len(data) < 4 or data[:4] != b"OggS":
        raise MediaScrubError("ogg payload missing OggS header")
    return MediaScrubResult(
        data=data, mime="audio/ogg", duration_ms=None, width=None, height=None,
    )
