"""WebM (EBML/Matroska subset) scrubbing — strict-subset reconstruction.

Every stored byte in the output is either (a) an EBML VINT encoding a
validated identifier / size / integer field, (b) a UTF-8 DocType string
selected from an allowlist, or (c) an opaque codec frame body whose
container envelope (SimpleBlock header, BlockGroup Block header) was
parsed from validated integer fields. No element body is byte-copied
without a per-element rebuild that reads specific fields.

Top-level chain:
    EBML header              parsed field-by-field; DocType must be
                             ``webm``; version fields validated
    Segment                  children walked with an allowlist:
        SeekHead             DROP (segment offsets change on rebuild)
        Void / CRC-32        DROP (padding / integrity — regenerated
                             would need a paired writer)
        Info                 rebuild TimestampScale + Duration only;
                             DROP MuxingApp / WritingApp / Title /
                             DateUTC / SegmentUID / SegmentFilename
        Tracks               rebuild TrackEntry fields; codec allowlist
                             = V_VP8, V_VP9, A_OPUS, A_VORBIS
        Cluster              rebuild Timestamp + SimpleBlock /
                             BlockGroup; frame bodies pass through with
                             validated header envelopes
        Tags                 DROP (metadata)
        Attachments          DROP (embedded files)
        Chapters             DROP (chapter metadata)
        Cues                 DROP (index offsets are invalidated by
                             rebuild; players tolerate missing cues)

Bounded parse work per input byte:
    - EBML VINT width capped at 8 bytes for size, 4 bytes for ID.
    - Container walker capped at ``_WEBM_MAX_ELEMENTS`` children.
    - Recursion depth capped at ``_WEBM_MAX_DEPTH``.
    - No element allocates more than its declared VINT-size body.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Final

from .base import MediaScrubError, MediaScrubResult


# EBML top-level identifiers we recognise. Encoded as raw uint big-endian
# bytes; the VINT marker bit is part of the encoding, so we compare against
# the wire form to avoid re-deriving it.
_ID_EBML: Final = 0x1A45DFA3
_ID_SEGMENT: Final = 0x18538067
_ID_VOID: Final = 0xEC
_ID_CRC32: Final = 0xBF

# EBML header children
_ID_EBML_VERSION: Final = 0x4286
_ID_EBML_READ_VERSION: Final = 0x42F7
_ID_EBML_MAX_ID_LENGTH: Final = 0x42F2
_ID_EBML_MAX_SIZE_LENGTH: Final = 0x42F3
_ID_DOC_TYPE: Final = 0x4282
_ID_DOC_TYPE_VERSION: Final = 0x4287
_ID_DOC_TYPE_READ_VERSION: Final = 0x4285

# Segment children
_ID_SEEK_HEAD: Final = 0x114D9B74
_ID_INFO: Final = 0x1549A966
_ID_TRACKS: Final = 0x1654AE6B
_ID_CLUSTER: Final = 0x1F43B675
_ID_CUES: Final = 0x1C53BB6B
_ID_TAGS: Final = 0x1254C367
_ID_ATTACHMENTS: Final = 0x1941A469
_ID_CHAPTERS: Final = 0x1043A770

# Info children
_ID_TIMESTAMP_SCALE: Final = 0x2AD7B1
_ID_DURATION: Final = 0x4489
_ID_MUXING_APP: Final = 0x4D80
_ID_WRITING_APP: Final = 0x5741
_ID_TITLE: Final = 0x7BA9
_ID_DATE_UTC: Final = 0x4461
_ID_SEGMENT_UID: Final = 0x73A4
_ID_SEGMENT_FAMILY: Final = 0x4444
_ID_SEGMENT_FILENAME: Final = 0x7384
_ID_PREV_UID: Final = 0x3CB923
_ID_NEXT_UID: Final = 0x3EB923
_ID_PREV_FILENAME: Final = 0x3C83AB
_ID_NEXT_FILENAME: Final = 0x3E83BB
_ID_CHAPTER_TRANSLATE: Final = 0x6924

# TrackEntry children
_ID_TRACK_ENTRY: Final = 0xAE
_ID_TRACK_NUMBER: Final = 0xD7
_ID_TRACK_UID: Final = 0x73C5
_ID_TRACK_TYPE: Final = 0x83
_ID_FLAG_ENABLED: Final = 0xB9
_ID_FLAG_DEFAULT: Final = 0x88
_ID_FLAG_FORCED: Final = 0x55AA
_ID_FLAG_LACING: Final = 0x9C
_ID_DEFAULT_DURATION: Final = 0x23E383
_ID_LANGUAGE: Final = 0x22B59C
_ID_LANGUAGE_IETF: Final = 0x22B59D
_ID_CODEC_ID: Final = 0x86
_ID_CODEC_PRIVATE: Final = 0x63A2
_ID_CODEC_DELAY: Final = 0x56AA
_ID_SEEK_PRE_ROLL: Final = 0x56BB
_ID_NAME: Final = 0x536E
_ID_VIDEO: Final = 0xE0
_ID_AUDIO: Final = 0xE1
_ID_CONTENT_ENCODINGS: Final = 0x6D80
_ID_MAX_BLOCK_ADDITION_ID: Final = 0x55EE
_ID_MIN_CACHE: Final = 0x6DE7
_ID_MAX_CACHE: Final = 0x6DF8

# Video children
_ID_PIXEL_WIDTH: Final = 0xB0
_ID_PIXEL_HEIGHT: Final = 0xBA
_ID_DISPLAY_WIDTH: Final = 0x54B0
_ID_DISPLAY_HEIGHT: Final = 0x54BA
_ID_DISPLAY_UNIT: Final = 0x54B2
_ID_PIXEL_CROP_BOTTOM: Final = 0x54AA
_ID_PIXEL_CROP_TOP: Final = 0x54BB
_ID_PIXEL_CROP_LEFT: Final = 0x54CC
_ID_PIXEL_CROP_RIGHT: Final = 0x54DD
_ID_FLAG_INTERLACED: Final = 0x9A
_ID_STEREO_MODE: Final = 0x53B8
_ID_ALPHA_MODE: Final = 0x53C0
_ID_COLOUR: Final = 0x55B0

# Audio children
_ID_SAMPLING_FREQUENCY: Final = 0xB5
_ID_OUTPUT_SAMPLING_FREQUENCY: Final = 0x78B5
_ID_CHANNELS: Final = 0x9F
_ID_BIT_DEPTH: Final = 0x6264

# Cluster children
_ID_TIMESTAMP: Final = 0xE7
_ID_POSITION: Final = 0xA7
_ID_PREV_SIZE: Final = 0xAB
_ID_SIMPLE_BLOCK: Final = 0xA3
_ID_BLOCK_GROUP: Final = 0xA0
_ID_BLOCK: Final = 0xA1
_ID_BLOCK_DURATION: Final = 0x9B
_ID_REFERENCE_BLOCK: Final = 0xFB
_ID_BLOCK_ADDITIONS: Final = 0x75A1
_ID_DISCARD_PADDING: Final = 0x75A2


_WEBM_DOC_TYPE: Final = "webm"
# Bounded to keep hostile many-child fixtures from amplifying traced
# memory before rejection. WIKI-225 REVIEW1 flagged the prior 1M cap
# as reachable-but-tiny elements can materialise many MB before the
# scrubber refuses. 4096 is generous for real fixtures (a few dozen
# Segment children even for hour-long streams) and forces early
# rejection on hostile shapes.
_WEBM_MAX_ELEMENTS: Final = 4096
_WEBM_MAX_DEPTH: Final = 6
_WEBM_MAX_VINT_WIDTH: Final = 8
_WEBM_MAX_ID_WIDTH: Final = 4
_WEBM_MAX_INT_BYTES: Final = 8
_WEBM_MAX_STRING_BYTES: Final = 256
_WEBM_MAX_CODEC_PRIVATE: Final = 1 << 16
_WEBM_MAX_DURATION_MS: Final = 7 * 24 * 60 * 60 * 1000
_WEBM_MAX_DIMENSION: Final = 8192
_WEBM_MAX_CHANNELS: Final = 8
_WEBM_MAX_SAMPLE_RATE: Final = 192_000
_WEBM_MAX_BLOCKS: Final = 1 << 20
_WEBM_MAX_FRAME_SIZE: Final = 1 << 24

# Track types that appear in scrubber-supported WebM containers.
_TRACK_TYPE_VIDEO: Final = 1
_TRACK_TYPE_AUDIO: Final = 2

# Codec allowlist. Vorbis is intentionally OUT under WIKI-225 REVIEW1 —
# its CodecPrivate is a 3-header laced blob (identification / comment /
# setup) whose comment header carries arbitrary vendor + user comment
# strings. Scrubbing it well requires a Vorbis comment parser, and its
# in-band frames are opaque codec data. Until we add that parser, any
# A_VORBIS track rejects the file rather than smuggling metadata.
_WEBM_VIDEO_CODECS: Final = frozenset({b"V_VP8", b"V_VP9"})
_WEBM_AUDIO_CODECS: Final = frozenset({b"A_OPUS"})


@dataclass(frozen=True)
class _WebmElement:
    identifier: int
    id_width: int
    size: int
    size_width: int
    body_start: int
    body_end: int


@dataclass(frozen=True)
class _WebmTrack:
    number: int
    kind: int
    codec_id: bytes
    width: int | None
    height: int | None


def scrub_webm(data: bytes) -> MediaScrubResult:
    if len(data) < 8:
        raise MediaScrubError("webm payload too small")
    if data[:4] != b"\x1a\x45\xdf\xa3":
        raise MediaScrubError("webm payload missing EBML magic")

    view = memoryview(data)
    ebml_element = _read_element(view, 0, len(data))
    if ebml_element.identifier != _ID_EBML:
        raise MediaScrubError("webm first element must be the EBML header")
    ebml_bytes, doc_type = _rebuild_ebml_header(view, ebml_element)
    if doc_type != _WEBM_DOC_TYPE:
        raise MediaScrubError(
            f"webm EBML DocType must be 'webm', got {doc_type!r}"
        )

    segment = _read_element(view, ebml_element.body_end, len(data))
    if segment.identifier != _ID_SEGMENT:
        raise MediaScrubError("webm second element must be Segment")
    if segment.body_end != len(data):
        raise MediaScrubError("webm trailing bytes past Segment")

    segment_body, width, height, duration_ms = _rebuild_segment(view, segment)
    segment_bytes = _emit_element(_ID_SEGMENT, segment_body)

    return MediaScrubResult(
        data=ebml_bytes + segment_bytes,
        mime="video/webm",
        duration_ms=duration_ms,
        width=width,
        height=height,
    )


# ---------------------------------------------------------------------------
# EBML VINT primitives
# ---------------------------------------------------------------------------


def _read_vint(view: memoryview, offset: int, end: int, *, is_id: bool) -> tuple[int, int]:
    """Return (value, width) for one EBML VINT starting at offset."""
    if offset >= end:
        raise MediaScrubError("webm VINT header past payload end")
    first = view[offset]
    if first == 0:
        raise MediaScrubError("webm VINT length is zero")
    width = 1
    mask = 0x80
    while width <= _WEBM_MAX_VINT_WIDTH and not (first & mask):
        width += 1
        mask >>= 1
    if width > _WEBM_MAX_VINT_WIDTH:
        raise MediaScrubError("webm VINT is longer than 8 bytes")
    if is_id and width > _WEBM_MAX_ID_WIDTH:
        raise MediaScrubError("webm element ID exceeds 4-byte cap")
    if offset + width > end:
        raise MediaScrubError("webm VINT extends past payload")
    if is_id:
        value = 0
        for index in range(width):
            value = (value << 8) | view[offset + index]
        return value, width
    value = first & (mask - 1)
    for index in range(1, width):
        value = (value << 8) | view[offset + index]
    # An all-ones VINT is the "unknown size" marker; forbidden in a strict
    # scrubber because a downstream element could reach the end of Segment.
    unknown_mask = (1 << (7 * width)) - 1
    if value == unknown_mask:
        raise MediaScrubError("webm VINT unknown-size marker is not accepted")
    return value, width


def _read_element(view: memoryview, offset: int, end: int) -> _WebmElement:
    identifier, id_width = _read_vint(view, offset, end, is_id=True)
    size, size_width = _read_vint(view, offset + id_width, end, is_id=False)
    body_start = offset + id_width + size_width
    body_end = body_start + size
    if body_end > end:
        raise MediaScrubError(
            f"webm element 0x{identifier:x} extends past parent boundary"
        )
    return _WebmElement(identifier, id_width, size, size_width, body_start, body_end)


def _iter_children(view: memoryview, start: int, end: int) -> list[_WebmElement]:
    children: list[_WebmElement] = []
    offset = start
    while offset < end:
        if len(children) >= _WEBM_MAX_ELEMENTS:
            raise MediaScrubError(
                f"webm container exceeds {_WEBM_MAX_ELEMENTS} children"
            )
        element = _read_element(view, offset, end)
        children.append(element)
        offset = element.body_end
    if offset != end:
        raise MediaScrubError("webm element boundary is misaligned")
    return children


# ---------------------------------------------------------------------------
# VINT and integer emitters
# ---------------------------------------------------------------------------


def _emit_vint_id(identifier: int) -> bytes:
    for width in range(1, _WEBM_MAX_ID_WIDTH + 1):
        if identifier < (1 << (8 * width)):
            return identifier.to_bytes(width, "big")
    raise MediaScrubError("webm element ID does not fit in 4 bytes")


def _emit_vint_size(value: int) -> bytes:
    if value < 0:
        raise MediaScrubError("webm element size must be non-negative")
    for width in range(1, _WEBM_MAX_VINT_WIDTH + 1):
        capacity = (1 << (7 * width)) - 1
        if value < capacity:
            marker = 1 << (7 * width)
            return (marker | value).to_bytes(width, "big")
    raise MediaScrubError("webm element size exceeds VINT capacity")


def _emit_uint(identifier: int, value: int) -> bytes:
    if value < 0 or value >> (_WEBM_MAX_INT_BYTES * 8):
        raise MediaScrubError(
            f"webm uint field 0x{identifier:x} exceeds 8-byte capacity"
        )
    if value == 0:
        payload = b"\x00"
    else:
        width = (value.bit_length() + 7) // 8
        payload = value.to_bytes(width, "big")
    return _emit_element(identifier, payload)


def _emit_float(identifier: int, value: float) -> bytes:
    payload = struct.pack(">d", value)
    return _emit_element(identifier, payload)


def _emit_element(identifier: int, payload: bytes) -> bytes:
    return _emit_vint_id(identifier) + _emit_vint_size(len(payload)) + payload


def _parse_uint(view: memoryview, element: _WebmElement, label: str) -> int:
    size = element.size
    if size == 0:
        return 0
    if size > _WEBM_MAX_INT_BYTES:
        raise MediaScrubError(
            f"webm {label} uint payload exceeds 8-byte cap"
        )
    value = 0
    for index in range(size):
        value = (value << 8) | view[element.body_start + index]
    return value


def _parse_float(view: memoryview, element: _WebmElement, label: str) -> float:
    payload = bytes(view[element.body_start:element.body_end])
    if len(payload) == 4:
        value = struct.unpack(">f", payload)[0]
    elif len(payload) == 8:
        value = struct.unpack(">d", payload)[0]
    else:
        raise MediaScrubError(f"webm {label} float payload must be 4 or 8 bytes")
    # WIKI-225 REVIEW1 MAJOR: NaN / ±Inf slip past `< 0` and `> cap`
    # comparisons and eventually reach `int(round(...))`, which raises
    # a bare ValueError outside our MediaScrubError guarantee. Reject
    # non-finite floats at parse time so every downstream comparison
    # and emission is well-defined.
    if not math.isfinite(value):
        raise MediaScrubError(f"webm {label} float value is not finite")
    return value


def _parse_ascii(view: memoryview, element: _WebmElement, label: str) -> str:
    if element.size > _WEBM_MAX_STRING_BYTES:
        raise MediaScrubError(
            f"webm {label} string exceeds {_WEBM_MAX_STRING_BYTES} bytes"
        )
    payload = bytes(view[element.body_start:element.body_end])
    try:
        decoded = payload.rstrip(b"\x00").decode("ascii")
    except UnicodeDecodeError as exc:  # pragma: no cover — validated below
        raise MediaScrubError(f"webm {label} is not ASCII") from exc
    for char in decoded:
        if not (0x20 <= ord(char) < 0x7F):
            raise MediaScrubError(f"webm {label} contains non-printable character")
    return decoded


# ---------------------------------------------------------------------------
# EBML header
# ---------------------------------------------------------------------------


def _rebuild_ebml_header(view: memoryview, ebml: _WebmElement) -> tuple[bytes, str]:
    children = _iter_children(view, ebml.body_start, ebml.body_end)
    version = 1
    read_version = 1
    max_id_length = 4
    max_size_length = 8
    doc_type_version = 1
    doc_type_read_version = 1
    doc_type: str | None = None
    for child in children:
        cid = child.identifier
        if cid == _ID_EBML_VERSION:
            version = _parse_uint(view, child, "EBMLVersion")
        elif cid == _ID_EBML_READ_VERSION:
            read_version = _parse_uint(view, child, "EBMLReadVersion")
        elif cid == _ID_EBML_MAX_ID_LENGTH:
            max_id_length = _parse_uint(view, child, "EBMLMaxIDLength")
        elif cid == _ID_EBML_MAX_SIZE_LENGTH:
            max_size_length = _parse_uint(view, child, "EBMLMaxSizeLength")
        elif cid == _ID_DOC_TYPE:
            doc_type = _parse_ascii(view, child, "DocType")
        elif cid == _ID_DOC_TYPE_VERSION:
            doc_type_version = _parse_uint(view, child, "DocTypeVersion")
        elif cid == _ID_DOC_TYPE_READ_VERSION:
            doc_type_read_version = _parse_uint(view, child, "DocTypeReadVersion")
        elif cid == _ID_VOID:
            continue
        else:
            raise MediaScrubError(
                f"webm EBML header child 0x{cid:x} outside allowlist"
            )
    if doc_type is None:
        raise MediaScrubError("webm EBML header missing DocType")
    if version > 1 or read_version > 1:
        raise MediaScrubError("webm EBML version fields exceed 1")
    if max_id_length > _WEBM_MAX_ID_WIDTH:
        raise MediaScrubError("webm EBMLMaxIDLength exceeds 4 bytes")
    if max_size_length > _WEBM_MAX_VINT_WIDTH:
        raise MediaScrubError("webm EBMLMaxSizeLength exceeds 8 bytes")
    if doc_type_version < 1 or doc_type_read_version < 1:
        raise MediaScrubError("webm DocType version fields must be positive")
    header_body = (
        _emit_uint(_ID_EBML_VERSION, version)
        + _emit_uint(_ID_EBML_READ_VERSION, read_version)
        + _emit_uint(_ID_EBML_MAX_ID_LENGTH, max_id_length)
        + _emit_uint(_ID_EBML_MAX_SIZE_LENGTH, max_size_length)
        + _emit_element(_ID_DOC_TYPE, doc_type.encode("ascii"))
        + _emit_uint(_ID_DOC_TYPE_VERSION, doc_type_version)
        + _emit_uint(_ID_DOC_TYPE_READ_VERSION, doc_type_read_version)
    )
    return _emit_element(_ID_EBML, header_body), doc_type


# ---------------------------------------------------------------------------
# Segment + inner containers
# ---------------------------------------------------------------------------


def _rebuild_segment(
    view: memoryview, segment: _WebmElement,
) -> tuple[bytes, int | None, int | None, int | None]:
    children = _iter_children(view, segment.body_start, segment.body_end)
    info_seen = False
    tracks_seen = False
    cluster_seen = False
    timestamp_scale = 1_000_000
    duration_ticks: float | None = None
    tracks: list[_WebmTrack] = []
    info_bytes = b""
    tracks_bytes = b""
    clusters: list[bytes] = []
    for child in children:
        cid = child.identifier
        if cid in (_ID_SEEK_HEAD, _ID_TAGS, _ID_ATTACHMENTS, _ID_CHAPTERS, _ID_CUES, _ID_VOID, _ID_CRC32):
            continue
        if cid == _ID_INFO:
            if info_seen:
                raise MediaScrubError("webm Segment has duplicate Info")
            info_seen = True
            info_bytes, timestamp_scale, duration_ticks = _rebuild_info(view, child)
        elif cid == _ID_TRACKS:
            if tracks_seen:
                raise MediaScrubError("webm Segment has duplicate Tracks")
            tracks_seen = True
            tracks_bytes, tracks = _rebuild_tracks(view, child)
        elif cid == _ID_CLUSTER:
            cluster_seen = True
            clusters.append(_rebuild_cluster(view, child, tracks))
        else:
            raise MediaScrubError(
                f"webm Segment child 0x{cid:x} outside allowlist"
            )
    if not info_seen:
        raise MediaScrubError("webm Segment missing Info")
    if not tracks_seen or not tracks:
        raise MediaScrubError("webm Segment missing Tracks")
    if not cluster_seen:
        raise MediaScrubError("webm Segment missing at least one Cluster")

    duration_ms: int | None = None
    if duration_ticks is not None:
        duration_ms = int(round(duration_ticks * timestamp_scale / 1_000_000))
        if duration_ms < 0 or duration_ms > _WEBM_MAX_DURATION_MS:
            raise MediaScrubError("webm duration exceeds scrubber cap")

    width, height = _select_dimensions(tracks)
    body = info_bytes + tracks_bytes + b"".join(clusters)
    return body, width, height, duration_ms


def _select_dimensions(tracks: list[_WebmTrack]) -> tuple[int | None, int | None]:
    for track in tracks:
        if track.kind == _TRACK_TYPE_VIDEO and track.width and track.height:
            return track.width, track.height
    return None, None


def _rebuild_info(
    view: memoryview, info: _WebmElement,
) -> tuple[bytes, int, float | None]:
    children = _iter_children(view, info.body_start, info.body_end)
    timestamp_scale = 1_000_000
    duration: float | None = None
    for child in children:
        cid = child.identifier
        if cid == _ID_TIMESTAMP_SCALE:
            timestamp_scale = _parse_uint(view, child, "TimestampScale")
            if not 1 <= timestamp_scale <= 1_000_000_000:
                raise MediaScrubError(
                    "webm TimestampScale outside 1..1_000_000_000"
                )
        elif cid == _ID_DURATION:
            duration = _parse_float(view, child, "Duration")
            if duration < 0 or duration > 1e12:
                raise MediaScrubError("webm Duration outside scrubber bounds")
        elif cid in (
            _ID_MUXING_APP, _ID_WRITING_APP, _ID_TITLE, _ID_DATE_UTC,
            _ID_SEGMENT_UID, _ID_SEGMENT_FAMILY, _ID_SEGMENT_FILENAME,
            _ID_PREV_UID, _ID_NEXT_UID, _ID_PREV_FILENAME, _ID_NEXT_FILENAME,
            _ID_CHAPTER_TRANSLATE, _ID_VOID, _ID_CRC32,
        ):
            continue
        else:
            raise MediaScrubError(
                f"webm Info child 0x{cid:x} outside allowlist"
            )
    body = _emit_uint(_ID_TIMESTAMP_SCALE, timestamp_scale)
    if duration is not None:
        body += _emit_float(_ID_DURATION, duration)
    return _emit_element(_ID_INFO, body), timestamp_scale, duration


def _rebuild_tracks(
    view: memoryview, tracks: _WebmElement,
) -> tuple[bytes, list[_WebmTrack]]:
    children = _iter_children(view, tracks.body_start, tracks.body_end)
    track_bytes: list[bytes] = []
    parsed: list[_WebmTrack] = []
    numbers: set[int] = set()
    for child in children:
        if child.identifier in (_ID_VOID, _ID_CRC32):
            continue
        if child.identifier != _ID_TRACK_ENTRY:
            raise MediaScrubError(
                f"webm Tracks child 0x{child.identifier:x} outside allowlist"
            )
        entry_bytes, entry = _rebuild_track_entry(view, child)
        if entry.number in numbers:
            raise MediaScrubError("webm Tracks contains duplicate TrackNumber")
        numbers.add(entry.number)
        parsed.append(entry)
        track_bytes.append(entry_bytes)
    if not parsed:
        raise MediaScrubError("webm Tracks contains no TrackEntry")
    return _emit_element(_ID_TRACKS, b"".join(track_bytes)), parsed


def _rebuild_track_entry(
    view: memoryview, entry: _WebmElement,
) -> tuple[bytes, _WebmTrack]:
    children = _iter_children(view, entry.body_start, entry.body_end)
    number: int | None = None
    uid: int | None = None
    kind: int | None = None
    codec_id: bytes | None = None
    codec_private: bytes | None = None
    codec_delay: int | None = None
    seek_pre_roll: int | None = None
    default_duration: int | None = None
    flag_enabled = 1
    flag_default = 1
    flag_forced = 0
    flag_lacing = 0
    language = "und"
    video_bytes = b""
    audio_bytes = b""
    width: int | None = None
    height: int | None = None
    audio_channels: int | None = None
    for child in children:
        cid = child.identifier
        if cid == _ID_TRACK_NUMBER:
            number = _parse_uint(view, child, "TrackNumber")
        elif cid == _ID_TRACK_UID:
            uid = _parse_uint(view, child, "TrackUID")
        elif cid == _ID_TRACK_TYPE:
            kind = _parse_uint(view, child, "TrackType")
        elif cid == _ID_CODEC_ID:
            codec_id = bytes(view[child.body_start:child.body_end])
        elif cid == _ID_CODEC_PRIVATE:
            if child.size > _WEBM_MAX_CODEC_PRIVATE:
                raise MediaScrubError(
                    "webm CodecPrivate exceeds scrubber cap"
                )
            codec_private = bytes(view[child.body_start:child.body_end])
        elif cid == _ID_CODEC_DELAY:
            codec_delay = _parse_uint(view, child, "CodecDelay")
        elif cid == _ID_SEEK_PRE_ROLL:
            seek_pre_roll = _parse_uint(view, child, "SeekPreRoll")
        elif cid == _ID_DEFAULT_DURATION:
            default_duration = _parse_uint(view, child, "DefaultDuration")
        elif cid == _ID_FLAG_ENABLED:
            flag_enabled = _parse_uint(view, child, "FlagEnabled")
            if flag_enabled not in (0, 1):
                raise MediaScrubError("webm FlagEnabled must be 0 or 1")
        elif cid == _ID_FLAG_DEFAULT:
            flag_default = _parse_uint(view, child, "FlagDefault")
            if flag_default not in (0, 1):
                raise MediaScrubError("webm FlagDefault must be 0 or 1")
        elif cid == _ID_FLAG_FORCED:
            flag_forced = _parse_uint(view, child, "FlagForced")
            if flag_forced not in (0, 1):
                raise MediaScrubError("webm FlagForced must be 0 or 1")
        elif cid == _ID_FLAG_LACING:
            flag_lacing = _parse_uint(view, child, "FlagLacing")
            if flag_lacing not in (0, 1):
                raise MediaScrubError("webm FlagLacing must be 0 or 1")
        elif cid == _ID_LANGUAGE:
            language = _parse_ascii(view, child, "Language")
        elif cid == _ID_VIDEO:
            video_bytes, width, height = _rebuild_video(view, child)
        elif cid == _ID_AUDIO:
            audio_bytes, audio_channels = _rebuild_audio(view, child)
        elif cid == _ID_CONTENT_ENCODINGS:
            # WIKI-225 REVIEW1 MAJOR: silent-drop leaves the ENCODED
            # CodecPrivate and frame bytes intact — a downstream
            # decoder can't reverse the transform because the
            # declaration is gone, and the payload can still hide
            # compressed / encrypted / header-stripped opaque bytes.
            # Rejecting the track is the only safe move without a full
            # per-transform reverser.
            raise MediaScrubError(
                "webm ContentEncodings not supported (compression/encryption "
                "would require reversing every declared transform)"
            )
        elif cid in (
            _ID_NAME, _ID_LANGUAGE_IETF,
            _ID_MAX_BLOCK_ADDITION_ID, _ID_MIN_CACHE, _ID_MAX_CACHE,
            _ID_VOID, _ID_CRC32,
        ):
            # Drop identity / non-load-bearing cache hints.
            continue
        else:
            raise MediaScrubError(
                f"webm TrackEntry child 0x{cid:x} outside allowlist"
            )
    if number is None or number == 0:
        raise MediaScrubError("webm TrackEntry missing positive TrackNumber")
    if uid is None or uid == 0:
        raise MediaScrubError("webm TrackEntry missing positive TrackUID")
    if kind not in (_TRACK_TYPE_VIDEO, _TRACK_TYPE_AUDIO):
        raise MediaScrubError(
            f"webm TrackType {kind!r} outside allowlist (video, audio)"
        )
    if codec_id is None:
        raise MediaScrubError("webm TrackEntry missing CodecID")
    if kind == _TRACK_TYPE_VIDEO:
        if codec_id not in _WEBM_VIDEO_CODECS:
            raise MediaScrubError(
                f"webm video CodecID {codec_id!r} outside VP8/VP9 allowlist"
            )
        if not video_bytes:
            raise MediaScrubError("webm video track missing Video element")
    else:
        if codec_id not in _WEBM_AUDIO_CODECS:
            raise MediaScrubError(
                f"webm audio CodecID {codec_id!r} outside Opus allowlist"
            )
        if not audio_bytes:
            raise MediaScrubError("webm audio track missing Audio element")

    # Per-codec CodecPrivate policy. VP8/VP9 in WebM do not carry a
    # meaningful CodecPrivate — the sequence header rides in the first
    # keyframe. A_OPUS requires a 19-byte OpusHead identification packet
    # whose fields we rebuild from parsed integers and cross-check
    # against TrackEntry.
    if codec_id in (b"V_VP8", b"V_VP9"):
        if codec_private is not None:
            raise MediaScrubError(
                f"webm {codec_id!r} does not accept CodecPrivate (sequence "
                "headers must ride in-band with the first keyframe)"
            )
        codec_private_bytes = b""
    elif codec_id == b"A_OPUS":
        if codec_private is None:
            raise MediaScrubError(
                "webm A_OPUS TrackEntry missing OpusHead CodecPrivate"
            )
        codec_private_bytes = _rebuild_opus_head(
            codec_private, expected_channels=audio_channels,
        )
    else:
        # Should not reach — allowlist above already gates codec_id.
        raise MediaScrubError(
            f"webm CodecID {codec_id!r} has no CodecPrivate policy"
        )

    body = (
        _emit_uint(_ID_TRACK_NUMBER, number)
        + _emit_uint(_ID_TRACK_UID, uid)
        + _emit_uint(_ID_TRACK_TYPE, kind)
        + _emit_uint(_ID_FLAG_ENABLED, flag_enabled)
        + _emit_uint(_ID_FLAG_DEFAULT, flag_default)
        + _emit_uint(_ID_FLAG_FORCED, flag_forced)
        + _emit_uint(_ID_FLAG_LACING, flag_lacing)
        + _emit_element(_ID_LANGUAGE, language.encode("ascii"))
        + _emit_element(_ID_CODEC_ID, codec_id)
    )
    if codec_private_bytes:
        body += _emit_element(_ID_CODEC_PRIVATE, codec_private_bytes)
    if codec_delay is not None:
        body += _emit_uint(_ID_CODEC_DELAY, codec_delay)
    if seek_pre_roll is not None:
        body += _emit_uint(_ID_SEEK_PRE_ROLL, seek_pre_roll)
    if default_duration is not None and default_duration > 0:
        body += _emit_uint(_ID_DEFAULT_DURATION, default_duration)
    if video_bytes:
        body += video_bytes
    if audio_bytes:
        body += audio_bytes
    return (
        _emit_element(_ID_TRACK_ENTRY, body),
        _WebmTrack(number, kind, codec_id, width, height),
    )


def _rebuild_video(
    view: memoryview, video: _WebmElement,
) -> tuple[bytes, int, int]:
    children = _iter_children(view, video.body_start, video.body_end)
    pixel_width: int | None = None
    pixel_height: int | None = None
    display_width: int | None = None
    display_height: int | None = None
    display_unit: int | None = None
    flag_interlaced = 2  # 0 = progressive (WebM legacy), 2 = spec default progressive
    for child in children:
        cid = child.identifier
        if cid == _ID_PIXEL_WIDTH:
            pixel_width = _parse_uint(view, child, "PixelWidth")
        elif cid == _ID_PIXEL_HEIGHT:
            pixel_height = _parse_uint(view, child, "PixelHeight")
        elif cid == _ID_DISPLAY_WIDTH:
            display_width = _parse_uint(view, child, "DisplayWidth")
        elif cid == _ID_DISPLAY_HEIGHT:
            display_height = _parse_uint(view, child, "DisplayHeight")
        elif cid == _ID_DISPLAY_UNIT:
            display_unit = _parse_uint(view, child, "DisplayUnit")
        elif cid == _ID_FLAG_INTERLACED:
            flag_interlaced = _parse_uint(view, child, "FlagInterlaced")
            if flag_interlaced not in (0, 1, 2):
                raise MediaScrubError("webm FlagInterlaced must be 0, 1, or 2")
        elif cid in (
            _ID_PIXEL_CROP_BOTTOM, _ID_PIXEL_CROP_TOP,
            _ID_PIXEL_CROP_LEFT, _ID_PIXEL_CROP_RIGHT,
            _ID_STEREO_MODE, _ID_ALPHA_MODE, _ID_COLOUR,
            _ID_VOID, _ID_CRC32,
        ):
            continue
        else:
            raise MediaScrubError(
                f"webm Video child 0x{cid:x} outside allowlist"
            )
    if not pixel_width or not pixel_height:
        raise MediaScrubError("webm Video missing PixelWidth or PixelHeight")
    if pixel_width > _WEBM_MAX_DIMENSION or pixel_height > _WEBM_MAX_DIMENSION:
        raise MediaScrubError(
            f"webm Video dimensions exceed {_WEBM_MAX_DIMENSION}"
        )
    if display_width is not None and display_width > _WEBM_MAX_DIMENSION:
        raise MediaScrubError("webm DisplayWidth exceeds scrubber cap")
    if display_height is not None and display_height > _WEBM_MAX_DIMENSION:
        raise MediaScrubError("webm DisplayHeight exceeds scrubber cap")
    if flag_interlaced == 1:
        raise MediaScrubError("webm interlaced video is not accepted")
    body = (
        _emit_uint(_ID_FLAG_INTERLACED, flag_interlaced)
        + _emit_uint(_ID_PIXEL_WIDTH, pixel_width)
        + _emit_uint(_ID_PIXEL_HEIGHT, pixel_height)
    )
    if display_width is not None:
        body += _emit_uint(_ID_DISPLAY_WIDTH, display_width)
    if display_height is not None:
        body += _emit_uint(_ID_DISPLAY_HEIGHT, display_height)
    if display_unit is not None:
        body += _emit_uint(_ID_DISPLAY_UNIT, display_unit)
    return _emit_element(_ID_VIDEO, body), pixel_width, pixel_height


def _rebuild_audio(
    view: memoryview, audio: _WebmElement,
) -> tuple[bytes, int]:
    children = _iter_children(view, audio.body_start, audio.body_end)
    sampling_frequency: float | None = None
    output_sampling_frequency: float | None = None
    channels: int | None = None
    bit_depth: int | None = None
    for child in children:
        cid = child.identifier
        if cid == _ID_SAMPLING_FREQUENCY:
            sampling_frequency = _parse_float(view, child, "SamplingFrequency")
        elif cid == _ID_OUTPUT_SAMPLING_FREQUENCY:
            output_sampling_frequency = _parse_float(
                view, child, "OutputSamplingFrequency",
            )
        elif cid == _ID_CHANNELS:
            channels = _parse_uint(view, child, "Channels")
        elif cid == _ID_BIT_DEPTH:
            bit_depth = _parse_uint(view, child, "BitDepth")
        elif cid in (_ID_VOID, _ID_CRC32):
            continue
        else:
            raise MediaScrubError(
                f"webm Audio child 0x{cid:x} outside allowlist"
            )
    if sampling_frequency is None or not (0 < sampling_frequency <= _WEBM_MAX_SAMPLE_RATE):
        raise MediaScrubError("webm Audio SamplingFrequency out of range")
    if channels is None or not (1 <= channels <= _WEBM_MAX_CHANNELS):
        raise MediaScrubError("webm Audio Channels out of range")
    if bit_depth is not None and bit_depth > 64:
        raise MediaScrubError("webm Audio BitDepth exceeds 64")
    if output_sampling_frequency is not None and not (
        0 < output_sampling_frequency <= _WEBM_MAX_SAMPLE_RATE
    ):
        raise MediaScrubError("webm Audio OutputSamplingFrequency out of range")
    body = (
        _emit_float(_ID_SAMPLING_FREQUENCY, sampling_frequency)
        + _emit_uint(_ID_CHANNELS, channels)
    )
    if output_sampling_frequency is not None:
        body += _emit_float(_ID_OUTPUT_SAMPLING_FREQUENCY, output_sampling_frequency)
    if bit_depth is not None:
        body += _emit_uint(_ID_BIT_DEPTH, bit_depth)
    return _emit_element(_ID_AUDIO, body), channels


# ---------------------------------------------------------------------------
# CodecPrivate — per-codec field-level rebuild
# ---------------------------------------------------------------------------


_OPUS_HEAD_MAGIC: Final = b"OpusHead"
_OPUS_HEAD_MIN_LEN: Final = 19


def _rebuild_opus_head(body: bytes, *, expected_channels: int | None) -> bytes:
    """Parse an OpusHead identification packet (RFC 7845 §5.1) and emit
    a canonical rebuild. Only channel mapping family 0 (mono/stereo) is
    accepted — families 1 and 255 use a channel mapping table whose
    stream count and coupled count would need their own rebuild.
    """
    if len(body) < _OPUS_HEAD_MIN_LEN:
        raise MediaScrubError(
            f"webm A_OPUS CodecPrivate length {len(body)} shorter than 19-byte OpusHead"
        )
    if body[:8] != _OPUS_HEAD_MAGIC:
        raise MediaScrubError("webm A_OPUS CodecPrivate is missing OpusHead magic")
    version = body[8]
    # RFC 7845 §5.1: high 4 bits reserved for major version = 0. Only
    # accept version 1 (the sole shipped major).
    if version >> 4 != 0 or (version & 0x0F) != 1:
        raise MediaScrubError(
            f"webm OpusHead version 0x{version:02x} unsupported"
        )
    channel_count = body[9]
    if channel_count not in (1, 2):
        raise MediaScrubError(
            f"webm OpusHead ChannelCount {channel_count} outside {{1,2}} "
            "(channel-mapping-family 0 accepts only mono/stereo)"
        )
    if expected_channels is not None and expected_channels != channel_count:
        raise MediaScrubError(
            f"webm OpusHead ChannelCount {channel_count} disagrees with "
            f"Audio/Channels {expected_channels}"
        )
    pre_skip = struct.unpack("<H", body[10:12])[0]
    input_sample_rate = struct.unpack("<I", body[12:16])[0]
    if input_sample_rate and not 8000 <= input_sample_rate <= _WEBM_MAX_SAMPLE_RATE:
        raise MediaScrubError(
            f"webm OpusHead InputSampleRate {input_sample_rate} out of range"
        )
    output_gain = struct.unpack("<h", body[16:18])[0]
    channel_mapping_family = body[18]
    if channel_mapping_family != 0:
        raise MediaScrubError(
            f"webm OpusHead ChannelMappingFamily {channel_mapping_family} "
            "outside allowlist (only family 0 accepted)"
        )
    if len(body) != _OPUS_HEAD_MIN_LEN:
        raise MediaScrubError(
            f"webm OpusHead has {len(body) - _OPUS_HEAD_MIN_LEN} trailing bytes "
            "past family-0 header"
        )
    return (
        _OPUS_HEAD_MAGIC
        + bytes([version, channel_count])
        + struct.pack("<H", pre_skip)
        + struct.pack("<I", input_sample_rate)
        + struct.pack("<h", output_gain)
        + bytes([0])  # channel_mapping_family, canonical zero
    )


# ---------------------------------------------------------------------------
# Per-codec frame validators
# ---------------------------------------------------------------------------
#
# WIKI-225 REVIEW1 BLOCKER 2: the block parser copied codec-frame bytes
# unchanged. A mutated VP9 payload with "ATTACKER-FRAME-METADATA" ASCII
# survived every envelope check. Every accepted codec now goes through
# a bounded validator that rejects frames whose header bits do not
# match the codec's spec. Interior spectral/motion-vector bytes remain
# opaque — validating those requires a full decoder — but the codec
# header check catches crude byte-swap attacks and non-codec payloads.


def _validate_vp9_frame(frame: bytes) -> None:
    """VP9 uncompressed header — VP9 bitstream spec, section 6.2.

    First byte layout: frame_marker (2 bits, always 0b10) + profile
    (2 bits, 0-3) + profile-3 reserved bit / show_existing_frame + …
    For keyframes we can also validate the 24-bit frame_sync_code
    (0x498342) that follows the profile bits.
    """
    if not frame:
        raise MediaScrubError("webm VP9 frame is empty")
    first = frame[0]
    frame_marker = first >> 6
    if frame_marker != 0b10:
        raise MediaScrubError(
            f"webm VP9 frame_marker 0x{frame_marker:x} is not 0b10"
        )
    profile = (first >> 4) & 0x03
    # profile 3 pushes the show_existing_frame bit one position further,
    # so the frame_type bit ordering differs. Both profiles-space entries
    # are valid; the frame_marker check is the load-bearing envelope
    # guard for arbitrary-byte payloads.
    if profile == 3:
        # bit 3 is a reserved zero; bit 2 is show_existing_frame
        if first & 0b00001000:
            raise MediaScrubError("webm VP9 profile-3 reserved bit non-zero")
    # Keyframe detection is profile-dependent; the sync code lives after
    # the leading show_frame / error_resilient bits when frame_type == 0
    # (KEY_FRAME). For all profiles the sync code appears at byte offset
    # 1..4 in the canonical uncompressed header.
    if len(frame) >= 4 and frame[1:4] == b"\x49\x83\x42":
        # Keyframe with well-formed sync code — good.
        return


def _validate_vp8_frame(frame: bytes) -> None:
    """VP8 uncompressed data chunk header — RFC 6386, section 9.1.

    Bit 0 of byte 0 = frame_type (0 = key frame, 1 = inter). Keyframes
    additionally carry the 3-byte start code 0x9d 0x01 0x2a immediately
    after the 3-byte frame_tag.
    """
    if not frame:
        raise MediaScrubError("webm VP8 frame is empty")
    if len(frame) < 3:
        raise MediaScrubError("webm VP8 frame_tag truncated")
    frame_tag = int.from_bytes(frame[0:3], "little")
    frame_type = frame_tag & 0x1
    version = (frame_tag >> 1) & 0x7
    if version > 3:
        raise MediaScrubError(f"webm VP8 version {version} outside 0..3")
    if frame_type == 0:  # key frame
        if len(frame) < 6:
            raise MediaScrubError("webm VP8 keyframe missing start code")
        if frame[3:6] != b"\x9d\x01\x2a":
            raise MediaScrubError("webm VP8 keyframe start code not 9d 01 2a")


def _validate_opus_packet(packet: bytes) -> None:
    """Opus packet framing — RFC 6716 section 3.

    TOC byte + one of four internal framings (code 0..3). We validate
    the TOC / length-prefix / padding structure and reject packets
    whose declared frame sizes overflow the payload. Individual frame
    bodies remain opaque codec data — Opus is a heavily compressed
    speech / music codec and does not admit envelope-level validation
    of the audio samples themselves.
    """
    if not packet:
        raise MediaScrubError("webm A_OPUS packet is empty")
    toc = packet[0]
    frame_count_code = toc & 0x3
    frames_body = packet[1:]
    if frame_count_code == 0:
        # Exactly one frame, uses all remaining bytes.
        if not frames_body:
            raise MediaScrubError("webm A_OPUS code-0 packet has no frame body")
    elif frame_count_code == 1:
        # Two equal-length CBR frames.
        if len(frames_body) & 1 or not frames_body:
            raise MediaScrubError(
                "webm A_OPUS code-1 packet payload must be even and non-empty"
            )
    elif frame_count_code == 2:
        # Two VBR frames: 1..2 byte length of first frame, then both.
        length, header_len = _opus_read_length(frames_body, 0)
        first_end = header_len + length
        if first_end > len(frames_body):
            raise MediaScrubError(
                "webm A_OPUS code-2 first-frame length overruns payload"
            )
        # Remainder is the second frame; must be > 0 bytes.
        if first_end == len(frames_body):
            raise MediaScrubError(
                "webm A_OPUS code-2 packet missing second frame"
            )
    else:  # code 3 — signalled M frames + optional padding
        if not frames_body:
            raise MediaScrubError(
                "webm A_OPUS code-3 packet missing frame-count byte"
            )
        fc_byte = frames_body[0]
        m = fc_byte & 0x3F
        vbr = bool(fc_byte & 0x80)
        padding_flag = bool(fc_byte & 0x40)
        if m == 0:
            raise MediaScrubError("webm A_OPUS code-3 frame count is zero")
        cursor = 1
        padding = 0
        if padding_flag:
            # 1+ padding-length bytes: values 0..254 add directly, 255
            # extends into the next byte. Reject unbounded chains.
            while True:
                if cursor >= len(frames_body):
                    raise MediaScrubError(
                        "webm A_OPUS code-3 padding length truncated"
                    )
                b = frames_body[cursor]
                cursor += 1
                padding += b
                if b < 255:
                    break
                if padding > len(packet):
                    raise MediaScrubError(
                        "webm A_OPUS code-3 padding exceeds packet size"
                    )
        if vbr:
            # (m-1) length-prefixed frames, last frame fills remainder.
            for _ in range(m - 1):
                length, header_len = _opus_read_length(frames_body, cursor)
                cursor += header_len + length
                if cursor > len(frames_body) - padding:
                    raise MediaScrubError(
                        "webm A_OPUS code-3 VBR frame length overruns payload"
                    )
        # CBR + VBR both must leave >= 1 byte for the trailing frame(s).
        remaining = len(frames_body) - cursor - padding
        if remaining < 0:
            raise MediaScrubError(
                "webm A_OPUS code-3 payload leaves negative remainder"
            )


def _opus_read_length(payload: bytes, offset: int) -> tuple[int, int]:
    """Read an Opus internal length field: 1 or 2 bytes."""
    if offset >= len(payload):
        raise MediaScrubError("webm A_OPUS length field truncated")
    first = payload[offset]
    if first < 252:
        return first, 1
    if offset + 1 >= len(payload):
        raise MediaScrubError("webm A_OPUS 2-byte length truncated")
    return first + payload[offset + 1] * 4, 2


_WEBM_FRAME_VALIDATORS: Final = {
    b"V_VP8": _validate_vp8_frame,
    b"V_VP9": _validate_vp9_frame,
    b"A_OPUS": _validate_opus_packet,
}


# ---------------------------------------------------------------------------
# Cluster + Block
# ---------------------------------------------------------------------------


def _rebuild_cluster(
    view: memoryview, cluster: _WebmElement, tracks: list[_WebmTrack],
) -> bytes:
    children = _iter_children(view, cluster.body_start, cluster.body_end)
    timestamp_seen = False
    timestamp = 0
    block_count = 0
    body = bytearray()
    track_codecs = {track.number: track.codec_id for track in tracks}
    for child in children:
        cid = child.identifier
        if cid == _ID_TIMESTAMP:
            timestamp = _parse_uint(view, child, "Cluster/Timestamp")
            timestamp_seen = True
            body.extend(_emit_uint(_ID_TIMESTAMP, timestamp))
        elif cid == _ID_SIMPLE_BLOCK:
            if block_count >= _WEBM_MAX_BLOCKS:
                raise MediaScrubError("webm Cluster exceeds scrubber block cap")
            body.extend(_rebuild_simple_block(view, child, track_codecs))
            block_count += 1
        elif cid == _ID_BLOCK_GROUP:
            if block_count >= _WEBM_MAX_BLOCKS:
                raise MediaScrubError("webm Cluster exceeds scrubber block cap")
            body.extend(_rebuild_block_group(view, child, track_codecs))
            block_count += 1
        elif cid in (_ID_POSITION, _ID_PREV_SIZE, _ID_VOID, _ID_CRC32):
            # Position and PrevSize refer to Segment-absolute offsets that
            # move once we rebuild. Drop them; players tolerate absence.
            continue
        else:
            raise MediaScrubError(
                f"webm Cluster child 0x{cid:x} outside allowlist"
            )
    if not timestamp_seen:
        raise MediaScrubError("webm Cluster missing Timestamp")
    if block_count == 0:
        raise MediaScrubError("webm Cluster contains no SimpleBlock or BlockGroup")
    return _emit_element(_ID_CLUSTER, bytes(body))


def _rebuild_simple_block(
    view: memoryview, block: _WebmElement, track_codecs: dict[int, bytes],
) -> bytes:
    payload, track_number, frame_offset = _parse_block_body(
        view, block, block_label="SimpleBlock",
    )
    codec = track_codecs.get(track_number)
    if codec is None:
        raise MediaScrubError(
            f"webm SimpleBlock references unknown TrackNumber {track_number}"
        )
    _validate_frame_bytes(codec, payload[frame_offset:], "SimpleBlock")
    return _emit_element(_ID_SIMPLE_BLOCK, payload)


def _rebuild_block_group(
    view: memoryview, group: _WebmElement, track_codecs: dict[int, bytes],
) -> bytes:
    children = _iter_children(view, group.body_start, group.body_end)
    block_bytes: bytes | None = None
    duration: int | None = None
    reference: int | None = None
    discard_padding: int | None = None
    for child in children:
        cid = child.identifier
        if cid == _ID_BLOCK:
            if block_bytes is not None:
                raise MediaScrubError("webm BlockGroup has duplicate Block")
            payload, track_number, frame_offset = _parse_block_body(
                view, child, block_label="Block",
            )
            codec = track_codecs.get(track_number)
            if codec is None:
                raise MediaScrubError(
                    f"webm Block references unknown TrackNumber {track_number}"
                )
            _validate_frame_bytes(codec, payload[frame_offset:], "Block")
            block_bytes = _emit_element(_ID_BLOCK, payload)
        elif cid == _ID_BLOCK_DURATION:
            duration = _parse_uint(view, child, "BlockDuration")
        elif cid == _ID_REFERENCE_BLOCK:
            reference = _parse_signed_int(view, child, "ReferenceBlock")
        elif cid == _ID_DISCARD_PADDING:
            discard_padding = _parse_signed_int(view, child, "DiscardPadding")
        elif cid in (_ID_BLOCK_ADDITIONS, _ID_VOID, _ID_CRC32):
            continue
        else:
            raise MediaScrubError(
                f"webm BlockGroup child 0x{cid:x} outside allowlist"
            )
    if block_bytes is None:
        raise MediaScrubError("webm BlockGroup missing Block")
    body = bytearray(block_bytes)
    if duration is not None:
        body.extend(_emit_uint(_ID_BLOCK_DURATION, duration))
    if reference is not None:
        body.extend(_emit_signed_int(_ID_REFERENCE_BLOCK, reference))
    if discard_padding is not None:
        body.extend(_emit_signed_int(_ID_DISCARD_PADDING, discard_padding))
    return _emit_element(_ID_BLOCK_GROUP, bytes(body))


def _parse_signed_int(view: memoryview, element: _WebmElement, label: str) -> int:
    size = element.size
    if size == 0:
        return 0
    if size > _WEBM_MAX_INT_BYTES:
        raise MediaScrubError(f"webm {label} int payload exceeds 8 bytes")
    value = 0
    for index in range(size):
        value = (value << 8) | view[element.body_start + index]
    if view[element.body_start] & 0x80:
        value -= 1 << (size * 8)
    return value


def _emit_signed_int(identifier: int, value: int) -> bytes:
    if value == 0:
        payload = b"\x00"
    else:
        width = max(1, (value.bit_length() + 8) // 8)
        payload = value.to_bytes(width, "big", signed=True)
    if len(payload) > _WEBM_MAX_INT_BYTES:
        raise MediaScrubError("webm signed int exceeds 8-byte capacity")
    return _emit_element(identifier, payload)


def _parse_block_body(
    view: memoryview, block: _WebmElement, *, block_label: str,
) -> tuple[bytes, int, int]:
    """Rebuild a Block or SimpleBlock envelope from validated header fields.

    Returns the rebuilt payload, the parsed track number, and the byte
    offset within the rebuilt payload where the codec frame body begins
    (so the caller can hand it to a per-codec validator).

    Layout: VINT track number, int16 timestamp (relative to Cluster), one
    flag byte, then a single frame body. Lacing (flag bits 1..3 non-zero)
    is rejected — a single frame per Block is the scrubber's strict subset.
    """
    if block.size < 4:
        raise MediaScrubError(f"webm {block_label} payload too short")
    payload = bytes(view[block.body_start:block.body_end])
    track_number, track_width = _read_vint(
        memoryview(payload), 0, len(payload), is_id=False,
    )
    if track_number == 0:
        raise MediaScrubError(f"webm {block_label} track number is zero")
    fixed = 2 + 1  # timestamp + flags
    if len(payload) < track_width + fixed + 1:
        raise MediaScrubError(f"webm {block_label} payload missing frame body")
    flags = payload[track_width + 2]
    lacing = (flags >> 1) & 0x3
    if lacing != 0:
        raise MediaScrubError(
            f"webm {block_label} lacing is not accepted by scrubber"
        )
    if block_label == "Block" and (flags & 0xF0):
        raise MediaScrubError("webm Block flags reserved bits set")
    if block_label == "SimpleBlock" and (flags & 0x70):
        # SimpleBlock: bit 7 keyframe, bit 0 discardable. Bits 4-6 reserved
        # in Matroska; the WebM specialization keeps them zero.
        raise MediaScrubError("webm SimpleBlock flags reserved bits set")
    frame_len = len(payload) - track_width - fixed
    if frame_len > _WEBM_MAX_FRAME_SIZE:
        raise MediaScrubError(
            f"webm {block_label} frame exceeds {_WEBM_MAX_FRAME_SIZE} bytes"
        )
    canonical_track = _emit_vint_size(track_number)
    rebuilt = (
        canonical_track
        + payload[track_width:track_width + 2]
        + bytes([flags])
        + payload[track_width + fixed:]
    )
    frame_offset = len(canonical_track) + fixed
    return rebuilt, track_number, frame_offset


def _validate_frame_bytes(codec: bytes, frame: bytes, block_label: str) -> None:
    validator = _WEBM_FRAME_VALIDATORS.get(codec)
    if validator is None:  # pragma: no cover — codec allowlist gates this
        raise MediaScrubError(
            f"webm {block_label} codec {codec!r} has no frame validator"
        )
    validator(frame)
