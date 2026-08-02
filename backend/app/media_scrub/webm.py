"""WebM (EBML/Matroska subset) scrubbing — strict-subset reconstruction.

Every stored byte in the output is either (a) an EBML VINT encoding a
validated identifier / size / integer field, (b) a UTF-8 DocType string
selected from an allowlist, or (c) a canonical VP8 keyframe. No element
body is byte-copied without a per-element rebuild that reads specific
fields.

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
                             = V_VP8 keyframes only
        Cluster              rebuild Timestamp + SimpleBlock /
                             BlockGroup; VP8 keyframes decode and re-encode
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
from io import BytesIO
from typing import Final

from PIL import Image, UnidentifiedImageError

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
_ID_RANGE: Final = 0x55B9

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
_WEBM_MAX_BLOCKS: Final = 1 << 20
_WEBM_MAX_FRAME_SIZE: Final = 1 << 24
_WEBM_MAX_FRAME_PIXELS: Final = 4096 * 4096
_WEBM_MAX_DECODED_PIXELS: Final = 32 * 1024 * 1024
_WEBM_MAX_DECODED_FRAMES: Final = 4096
_WEBM_MIN_DECODE_PIXELS_PER_FRAME: Final = 160 * 120

# Canonical single-frame Block flags. Rebuilt VP8 frames are keyframes,
# visible, and not discardable. Block has no keyframe or discardable bit.
_BLOCK_FLAG_INVISIBLE: Final = 0x08
_BLOCK_FLAG_LACING: Final = 0x06
_SIMPLE_BLOCK_FLAG_KEYFRAME: Final = 0x80
_SIMPLE_BLOCK_FLAG_DISCARDABLE: Final = 0x01
_SIMPLE_BLOCK_ALLOWED_FLAGS: Final = (
    _SIMPLE_BLOCK_FLAG_KEYFRAME
    | _BLOCK_FLAG_INVISIBLE
    | _SIMPLE_BLOCK_FLAG_DISCARDABLE
)
_BLOCK_ALLOWED_FLAGS: Final = _BLOCK_FLAG_INVISIBLE

# Track type in the supported WebM subset.
_TRACK_TYPE_VIDEO: Final = 1

# VP8 remains as a keyframe-only subset. Pillow/libwebp fully decodes each
# accepted frame and emits a new canonical VP8 keyframe. All other codecs
# reject because their opaque compressed bytes could preserve source data.
_WEBM_VIDEO_CODECS: Final = frozenset({b"V_VP8"})


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
    enabled: bool
    default: bool
    default_duration_ns: int | None
    width: int | None
    height: int | None


@dataclass
class _WebmDecodeBudget:
    remaining_pixels: int
    remaining_frames: int

    def charge(self, track: _WebmTrack) -> None:
        if track.kind != _TRACK_TYPE_VIDEO:
            return
        if track.width is None or track.height is None:  # pragma: no cover
            raise MediaScrubError("webm video track is missing dimensions")
        if self.remaining_frames <= 0:
            raise MediaScrubError(
                f"webm decoded video exceeds {_WEBM_MAX_DECODED_FRAMES} frame budget"
            )
        pixels = max(
            track.width * track.height,
            _WEBM_MIN_DECODE_PIXELS_PER_FRAME,
        )
        if pixels > self.remaining_pixels:
            raise MediaScrubError(
                f"webm decoded video exceeds {_WEBM_MAX_DECODED_PIXELS} pixel budget"
            )
        self.remaining_frames -= 1
        self.remaining_pixels -= pixels


@dataclass(frozen=True)
class _WebmTimeline:
    timestamp_scale: int
    declared_duration_ticks: float | None

    def validate_block(
        self,
        cluster_timestamp: int,
        relative_timestamp: int,
        block_duration_ticks: int | None = None,
        default_duration_ns: int | None = None,
    ) -> None:
        absolute_ticks = cluster_timestamp + relative_timestamp
        if absolute_ticks < 0:
            raise MediaScrubError("webm block absolute timestamp is negative")
        if block_duration_ticks is not None and block_duration_ticks <= 0:
            raise MediaScrubError("webm BlockDuration must be positive")
        absolute_ns = absolute_ticks * self.timestamp_scale
        duration_ns = (
            block_duration_ticks * self.timestamp_scale
            if block_duration_ticks is not None
            else default_duration_ns or 0
        )
        end_ns = absolute_ns + duration_ns
        max_duration_ns = _WEBM_MAX_DURATION_MS * 1_000_000
        if end_ns > max_duration_ns:
            raise MediaScrubError("webm block timeline exceeds seven-day cap")
        if (
            self.declared_duration_ticks is not None
            and end_ns
            > self.declared_duration_ticks * self.timestamp_scale
        ):
            raise MediaScrubError(
                "webm block timeline exceeds declared Duration"
            )


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
    version: int | None = None
    read_version: int | None = None
    max_id_length: int | None = None
    max_size_length: int | None = None
    doc_type_version: int | None = None
    doc_type_read_version: int | None = None
    doc_type: str | None = None
    seen: set[int] = set()
    for child in children:
        cid = child.identifier
        if cid != _ID_VOID:
            if cid in seen:
                raise MediaScrubError(
                    f"webm EBML header has duplicate child 0x{cid:x}"
                )
            seen.add(cid)
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
    supported = {
        "EBMLVersion": (version, 1),
        "EBMLReadVersion": (read_version, 1),
        "EBMLMaxIDLength": (max_id_length, 4),
        "EBMLMaxSizeLength": (max_size_length, 8),
    }
    for label, (actual, expected) in supported.items():
        if actual != expected:
            raise MediaScrubError(
                f"webm {label} must equal supported value {expected}, got {actual!r}"
            )
    if doc_type_version is None or doc_type_read_version is None:
        raise MediaScrubError("webm EBML header missing DocType version field")
    if not 1 <= doc_type_version <= 4:
        raise MediaScrubError(
            f"webm DocTypeVersion {doc_type_version} outside supported range 1..4"
        )
    if not 1 <= doc_type_read_version <= 2:
        raise MediaScrubError(
            "webm DocTypeReadVersion outside supported range 1..2"
        )
    if read_version > version or doc_type_read_version > doc_type_version:
        raise MediaScrubError("webm read version exceeds declared version")
    header_body = (
        _emit_uint(_ID_EBML_VERSION, 1)
        + _emit_uint(_ID_EBML_READ_VERSION, 1)
        + _emit_uint(_ID_EBML_MAX_ID_LENGTH, 4)
        + _emit_uint(_ID_EBML_MAX_SIZE_LENGTH, 8)
        + _emit_element(_ID_DOC_TYPE, b"webm")
        + _emit_uint(_ID_DOC_TYPE_VERSION, 2)
        + _emit_uint(_ID_DOC_TYPE_READ_VERSION, 2)
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
    framed_track_numbers: set[int] = set()
    decode_budget = _WebmDecodeBudget(
        _WEBM_MAX_DECODED_PIXELS,
        _WEBM_MAX_DECODED_FRAMES,
    )
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
            if not info_seen or not tracks_seen:
                raise MediaScrubError("webm Cluster must follow Info and Tracks")
            cluster_seen = True
            cluster_bytes, cluster_tracks = _rebuild_cluster(
                view,
                child,
                tracks,
                decode_budget,
                _WebmTimeline(timestamp_scale, duration_ticks),
            )
            clusters.append(cluster_bytes)
            framed_track_numbers.update(cluster_tracks)
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

    enabled_video_tracks = [
        track
        for track in tracks
        if track.kind == _TRACK_TYPE_VIDEO and track.enabled
    ]
    if len(enabled_video_tracks) != 1:
        raise MediaScrubError(
            "webm video artifact requires exactly one enabled video track"
        )
    selected_track = enabled_video_tracks[0]
    if any(
        track.number != selected_track.number and track.default
        for track in tracks
    ):
        raise MediaScrubError(
            "webm non-selected video tracks cannot be default tracks"
        )
    if selected_track.number not in framed_track_numbers:
        raise MediaScrubError(
            "webm enabled video track requires a validated frame"
        )

    duration_ms: int | None = None
    if duration_ticks is not None:
        duration_ns = duration_ticks * timestamp_scale
        if duration_ns < 0 or duration_ns > _WEBM_MAX_DURATION_MS * 1_000_000:
            raise MediaScrubError("webm duration exceeds scrubber cap")
        duration_ms = int(round(duration_ns / 1_000_000))

    width, height = selected_track.width, selected_track.height
    body = info_bytes + tracks_bytes + b"".join(clusters)
    return body, width, height, duration_ms


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
    if len(parsed) != 1:
        raise MediaScrubError(
            "webm strict VP8 subset requires exactly one TrackEntry"
        )
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
    language_seen = False
    video_bytes = b""
    video_element: _WebmElement | None = None
    video_seen = False
    audio_seen = False
    width: int | None = None
    height: int | None = None
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
            if language_seen:
                raise MediaScrubError("webm TrackEntry has duplicate Language")
            language_seen = True
            if child.size > _WEBM_MAX_STRING_BYTES:
                raise MediaScrubError(
                    f"webm Language string exceeds {_WEBM_MAX_STRING_BYTES} bytes"
                )
            # Language is metadata, not a decoder input. Drop the caller value
            # and emit the canonical WebM default below.
        elif cid == _ID_VIDEO:
            if video_seen:
                raise MediaScrubError("webm TrackEntry has duplicate Video")
            video_seen = True
            video_element = child
        elif cid == _ID_AUDIO:
            if audio_seen:
                raise MediaScrubError("webm TrackEntry has duplicate Audio")
            audio_seen = True
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
    if kind != _TRACK_TYPE_VIDEO:
        raise MediaScrubError(
            f"webm TrackType {kind!r} outside video-only allowlist"
        )
    if codec_id is None:
        raise MediaScrubError("webm TrackEntry missing CodecID")
    if codec_id not in _WEBM_VIDEO_CODECS:
        raise MediaScrubError(
            f"webm video CodecID {codec_id!r} outside VP8 allowlist"
        )
    if not video_seen:
        raise MediaScrubError("webm video track missing Video element")
    if audio_seen:
        raise MediaScrubError("webm video track cannot contain Audio")
    if codec_delay is not None or seek_pre_roll is not None:
        raise MediaScrubError(
            "webm VP8 track cannot contain CodecDelay or SeekPreRoll"
        )

    if codec_private is not None:
        raise MediaScrubError(
            "webm V_VP8 does not accept CodecPrivate; sequence headers "
            "must ride in-band with the first keyframe"
        )
    if default_duration is not None and not (
        1 <= default_duration <= _WEBM_MAX_DURATION_MS * 1_000_000
    ):
        raise MediaScrubError("webm DefaultDuration exceeds seven-day cap")
    assert video_element is not None
    video_bytes, width, height = _rebuild_video(view, video_element)

    body = (
        _emit_uint(_ID_TRACK_NUMBER, number)
        + _emit_uint(_ID_TRACK_UID, number)
        + _emit_uint(_ID_TRACK_TYPE, kind)
        + _emit_uint(_ID_FLAG_ENABLED, flag_enabled)
        + _emit_uint(_ID_FLAG_DEFAULT, flag_default)
        + _emit_uint(_ID_FLAG_FORCED, flag_forced)
        + _emit_uint(_ID_FLAG_LACING, flag_lacing)
        + _emit_element(_ID_LANGUAGE, b"und")
        + _emit_element(_ID_CODEC_ID, codec_id)
    )
    if default_duration is not None:
        body += _emit_uint(_ID_DEFAULT_DURATION, default_duration)
    if video_bytes:
        body += video_bytes
    return (
        _emit_element(_ID_TRACK_ENTRY, body),
        _WebmTrack(
            number,
            kind,
            codec_id,
            bool(flag_enabled),
            bool(flag_default),
            default_duration,
            width,
            height,
        ),
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
    colour_bytes = b""
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
        ):
            if _parse_uint(view, child, "PixelCrop") != 0:
                raise MediaScrubError(
                    "webm nonzero PixelCrop is outside the strict VP8 subset"
                )
        elif cid == _ID_STEREO_MODE:
            if _parse_uint(view, child, "StereoMode") != 0:
                raise MediaScrubError(
                    "webm nonzero StereoMode is outside the strict VP8 subset"
                )
        elif cid == _ID_ALPHA_MODE:
            if _parse_uint(view, child, "AlphaMode") != 0:
                raise MediaScrubError(
                    "webm nonzero AlphaMode is outside the strict VP8 subset"
                )
        elif cid == _ID_COLOUR:
            if colour_bytes:
                raise MediaScrubError("webm Video has duplicate Colour")
            colour_bytes = _rebuild_colour(view, child)
        elif cid in (_ID_VOID, _ID_CRC32):
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
    body += colour_bytes
    return _emit_element(_ID_VIDEO, body), pixel_width, pixel_height


def _rebuild_colour(view: memoryview, colour: _WebmElement) -> bytes:
    """Accept only VP8's canonical limited-range declaration."""
    children = _iter_children(view, colour.body_start, colour.body_end)
    if len(children) != 1 or children[0].identifier != _ID_RANGE:
        raise MediaScrubError(
            "webm Colour is outside the strict VP8 subset"
        )
    colour_range = _parse_uint(view, children[0], "Colour/Range")
    if colour_range != 1:
        raise MediaScrubError(
            "webm Colour Range must be canonical limited range 1"
        )
    return _emit_element(_ID_COLOUR, _emit_uint(_ID_RANGE, 1))


# ---------------------------------------------------------------------------
# Per-codec frame rebuilders
# ---------------------------------------------------------------------------
def _validate_vp8_keyframe_header(
    frame: bytes, *, expected_width: int, expected_height: int,
) -> None:
    """Validate the complete uncompressed header of one VP8 keyframe."""
    if len(frame) < 10:
        raise MediaScrubError("webm VP8 keyframe header is truncated")
    frame_tag = int.from_bytes(frame[0:3], "little")
    if frame_tag & 0x1:
        raise MediaScrubError(
            "webm VP8 interframes are outside the canonical keyframe-only subset"
        )
    version = (frame_tag >> 1) & 0x7
    if version > 3:
        raise MediaScrubError(f"webm VP8 version {version} outside 0..3")
    if not (frame_tag & 0x10):
        raise MediaScrubError("webm VP8 keyframe has show_frame cleared")
    first_partition_size = frame_tag >> 5
    if first_partition_size < 7 or first_partition_size > len(frame) - 3:
        raise MediaScrubError("webm VP8 first partition size is invalid")
    if frame[3:6] != b"\x9d\x01\x2a":
        raise MediaScrubError("webm VP8 keyframe start code not 9d 01 2a")
    width = int.from_bytes(frame[6:8], "little") & 0x3FFF
    height = int.from_bytes(frame[8:10], "little") & 0x3FFF
    if (width, height) != (expected_width, expected_height):
        raise MediaScrubError(
            f"webm VP8 frame dimensions {(width, height)} disagree with "
            f"TrackEntry {(expected_width, expected_height)}"
        )


def _wrap_vp8_as_webp(frame: bytes) -> bytes:
    chunk = b"VP8 " + struct.pack("<I", len(frame)) + frame
    if len(frame) & 1:
        chunk += b"\x00"
    riff_body = b"WEBP" + chunk
    return b"RIFF" + struct.pack("<I", len(riff_body)) + riff_body


def _extract_vp8_from_webp(payload: bytes) -> bytes:
    if len(payload) < 20 or payload[:4] != b"RIFF" or payload[8:12] != b"WEBP":
        raise MediaScrubError("webm VP8 canonical encoder returned invalid WebP")
    declared_size = struct.unpack("<I", payload[4:8])[0]
    if declared_size + 8 != len(payload):
        raise MediaScrubError("webm VP8 canonical WebP size is inconsistent")
    cursor = 12
    vp8_frame: bytes | None = None
    while cursor < len(payload):
        if cursor + 8 > len(payload):
            raise MediaScrubError("webm VP8 canonical WebP chunk is truncated")
        tag = payload[cursor:cursor + 4]
        size = struct.unpack("<I", payload[cursor + 4:cursor + 8])[0]
        body_start = cursor + 8
        body_end = body_start + size
        padded_end = body_end + (size & 1)
        if padded_end > len(payload):
            raise MediaScrubError("webm VP8 canonical WebP chunk exceeds payload")
        if tag == b"VP8 ":
            if vp8_frame is not None:
                raise MediaScrubError("webm VP8 canonical WebP has duplicate VP8 chunks")
            vp8_frame = payload[body_start:body_end]
        else:
            raise MediaScrubError(
                f"webm VP8 canonical WebP returned unsupported chunk {tag!r}"
            )
        cursor = padded_end
    if vp8_frame is None:
        raise MediaScrubError("webm VP8 canonical WebP is missing VP8 chunk")
    return vp8_frame


def _rebuild_vp8_frame(
    frame: bytes, *, expected_width: int, expected_height: int,
) -> bytes:
    """Fully decode one VP8 keyframe and emit a new canonical keyframe."""
    if expected_width * expected_height > _WEBM_MAX_FRAME_PIXELS:
        raise MediaScrubError(
            f"webm VP8 frame exceeds {_WEBM_MAX_FRAME_PIXELS} decoded pixels"
        )
    _validate_vp8_keyframe_header(
        frame, expected_width=expected_width, expected_height=expected_height,
    )
    try:
        with Image.open(BytesIO(_wrap_vp8_as_webp(frame))) as image:
            image.load()
            if image.size != (expected_width, expected_height):
                raise MediaScrubError(
                    f"webm VP8 decoded dimensions {image.size} disagree with TrackEntry"
                )
            canonical_image = image.convert("RGB")
            output = BytesIO()
            canonical_image.save(
                output,
                format="WEBP",
                quality=100,
                method=6,
                exact=True,
            )
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
        raise MediaScrubError("webm VP8 frame failed full decode") from exc
    rebuilt = _extract_vp8_from_webp(output.getvalue())
    _validate_vp8_keyframe_header(
        rebuilt, expected_width=expected_width, expected_height=expected_height,
    )
    if len(rebuilt) > _WEBM_MAX_FRAME_SIZE:
        raise MediaScrubError("webm canonical VP8 frame exceeds scrubber cap")
    return rebuilt


# ---------------------------------------------------------------------------
# Cluster + Block
# ---------------------------------------------------------------------------


def _rebuild_cluster(
    view: memoryview,
    cluster: _WebmElement,
    tracks: list[_WebmTrack],
    decode_budget: _WebmDecodeBudget,
    timeline: _WebmTimeline,
) -> tuple[bytes, set[int]]:
    children = _iter_children(view, cluster.body_start, cluster.body_end)
    timestamp_children = [
        child for child in children if child.identifier == _ID_TIMESTAMP
    ]
    if not timestamp_children:
        raise MediaScrubError("webm Cluster missing Timestamp")
    if len(timestamp_children) != 1:
        raise MediaScrubError("webm Cluster has duplicate Timestamp")
    timestamp = _parse_uint(view, timestamp_children[0], "Cluster/Timestamp")
    block_count = 0
    body = bytearray(_emit_uint(_ID_TIMESTAMP, timestamp))
    framed_track_numbers: set[int] = set()
    track_map = {track.number: track for track in tracks}
    for child in children:
        cid = child.identifier
        if cid == _ID_TIMESTAMP:
            continue
        elif cid == _ID_SIMPLE_BLOCK:
            if block_count >= _WEBM_MAX_BLOCKS:
                raise MediaScrubError("webm Cluster exceeds scrubber block cap")
            block_bytes, track_number = _rebuild_simple_block(
                view,
                child,
                track_map,
                decode_budget,
                timeline,
                timestamp,
            )
            body.extend(block_bytes)
            framed_track_numbers.add(track_number)
            block_count += 1
        elif cid == _ID_BLOCK_GROUP:
            if block_count >= _WEBM_MAX_BLOCKS:
                raise MediaScrubError("webm Cluster exceeds scrubber block cap")
            group_bytes, track_number = _rebuild_block_group(
                view,
                child,
                track_map,
                decode_budget,
                timeline,
                timestamp,
            )
            body.extend(group_bytes)
            framed_track_numbers.add(track_number)
            block_count += 1
        elif cid in (_ID_POSITION, _ID_PREV_SIZE, _ID_VOID, _ID_CRC32):
            # Position and PrevSize refer to Segment-absolute offsets that
            # move once we rebuild. Drop them; players tolerate absence.
            continue
        else:
            raise MediaScrubError(
                f"webm Cluster child 0x{cid:x} outside allowlist"
            )
    if block_count == 0:
        raise MediaScrubError("webm Cluster contains no SimpleBlock or BlockGroup")
    return _emit_element(_ID_CLUSTER, bytes(body)), framed_track_numbers


def _rebuild_simple_block(
    view: memoryview,
    block: _WebmElement,
    track_map: dict[int, _WebmTrack],
    decode_budget: _WebmDecodeBudget,
    timeline: _WebmTimeline,
    cluster_timestamp: int,
) -> tuple[bytes, int]:
    payload, track_number, frame_offset, relative_timestamp = _parse_block_body(
        view, block, block_label="SimpleBlock",
    )
    track = track_map.get(track_number)
    if track is None:
        raise MediaScrubError(
            f"webm SimpleBlock references unknown TrackNumber {track_number}"
        )
    timeline.validate_block(
        cluster_timestamp,
        relative_timestamp,
        default_duration_ns=track.default_duration_ns,
    )
    frame = _rebuild_frame_bytes(
        track, payload[frame_offset:], "SimpleBlock", decode_budget,
    )
    rebuilt_payload = payload[:frame_offset] + frame
    return _emit_element(_ID_SIMPLE_BLOCK, rebuilt_payload), track_number


def _rebuild_block_group(
    view: memoryview,
    group: _WebmElement,
    track_map: dict[int, _WebmTrack],
    decode_budget: _WebmDecodeBudget,
    timeline: _WebmTimeline,
    cluster_timestamp: int,
) -> tuple[bytes, int]:
    children = _iter_children(view, group.body_start, group.body_end)
    block_bytes: bytes | None = None
    duration: int | None = None
    block_track_number: int | None = None
    block_relative_timestamp: int | None = None
    block_track: _WebmTrack | None = None
    for child in children:
        cid = child.identifier
        if cid == _ID_BLOCK:
            if block_bytes is not None:
                raise MediaScrubError("webm BlockGroup has duplicate Block")
            (
                payload,
                track_number,
                frame_offset,
                relative_timestamp,
            ) = _parse_block_body(
                view, child, block_label="Block",
            )
            track = track_map.get(track_number)
            if track is None:
                raise MediaScrubError(
                    f"webm Block references unknown TrackNumber {track_number}"
                )
            frame = _rebuild_frame_bytes(
                track, payload[frame_offset:], "Block", decode_budget,
            )
            block_bytes = _emit_element(_ID_BLOCK, payload[:frame_offset] + frame)
            block_track_number = track_number
            block_relative_timestamp = relative_timestamp
            block_track = track
        elif cid == _ID_BLOCK_DURATION:
            if duration is not None:
                raise MediaScrubError("webm BlockGroup has duplicate BlockDuration")
            duration = _parse_uint(view, child, "BlockDuration")
        elif cid == _ID_REFERENCE_BLOCK:
            _parse_signed_int(view, child, "ReferenceBlock")
            raise MediaScrubError(
                "webm ReferenceBlock is outside the VP8 keyframe-only subset"
            )
        elif cid == _ID_DISCARD_PADDING:
            _parse_signed_int(view, child, "DiscardPadding")
            raise MediaScrubError(
                "webm DiscardPadding is outside the video-only subset"
            )
        elif cid == _ID_BLOCK_ADDITIONS:
            raise MediaScrubError(
                "webm BlockAdditions are outside the VP8 keyframe-only subset"
            )
        elif cid in (_ID_VOID, _ID_CRC32):
            continue
        else:
            raise MediaScrubError(
                f"webm BlockGroup child 0x{cid:x} outside allowlist"
            )
    if block_bytes is None:
        raise MediaScrubError("webm BlockGroup missing Block")
    assert block_relative_timestamp is not None
    assert block_track is not None
    timeline.validate_block(
        cluster_timestamp,
        block_relative_timestamp,
        block_duration_ticks=duration,
        default_duration_ns=block_track.default_duration_ns,
    )
    body = bytearray(block_bytes)
    if duration is not None:
        body.extend(_emit_uint(_ID_BLOCK_DURATION, duration))
    assert block_track_number is not None
    return _emit_element(_ID_BLOCK_GROUP, bytes(body)), block_track_number


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
) -> tuple[bytes, int, int, int]:
    """Rebuild a Block or SimpleBlock envelope from validated header fields.

    Returns the rebuilt payload, parsed track number, frame offset, and
    signed timestamp relative to the Cluster.

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
    relative_timestamp = int.from_bytes(
        payload[track_width:track_width + 2], "big", signed=True,
    )
    flags = payload[track_width + 2]
    if flags & _BLOCK_FLAG_LACING:
        raise MediaScrubError(
            f"webm {block_label} lacing is not accepted by scrubber"
        )
    if block_label == "SimpleBlock":
        if flags & ~_SIMPLE_BLOCK_ALLOWED_FLAGS:
            raise MediaScrubError("webm SimpleBlock flags reserved bits set")
        canonical_flags = _SIMPLE_BLOCK_FLAG_KEYFRAME
    else:
        if flags & ~_BLOCK_ALLOWED_FLAGS:
            raise MediaScrubError("webm Block flags reserved bits set")
        canonical_flags = 0
    frame_len = len(payload) - track_width - fixed
    if frame_len > _WEBM_MAX_FRAME_SIZE:
        raise MediaScrubError(
            f"webm {block_label} frame exceeds {_WEBM_MAX_FRAME_SIZE} bytes"
        )
    canonical_track = _emit_vint_size(track_number)
    rebuilt = (
        canonical_track
        + relative_timestamp.to_bytes(2, "big", signed=True)
        + bytes([canonical_flags])
        + payload[track_width + fixed:]
    )
    frame_offset = len(canonical_track) + fixed
    return rebuilt, track_number, frame_offset, relative_timestamp


def _rebuild_frame_bytes(
    track: _WebmTrack,
    frame: bytes,
    block_label: str,
    decode_budget: _WebmDecodeBudget,
) -> bytes:
    if track.codec_id == b"V_VP8":
        if track.width is None or track.height is None:  # pragma: no cover
            raise MediaScrubError("webm VP8 track is missing dimensions")
        decode_budget.charge(track)
        return _rebuild_vp8_frame(
            frame,
            expected_width=track.width,
            expected_height=track.height,
        )
    raise MediaScrubError(
        f"webm {block_label} codec {track.codec_id!r} has no frame rebuilder"
    )
