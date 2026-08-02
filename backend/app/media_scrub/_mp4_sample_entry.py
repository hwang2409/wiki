"""Field-level AVC sample-entry rebuilding for MP4."""
from __future__ import annotations

import math
from collections.abc import Iterator
import struct

from .base import MediaScrubError
from ._h264 import (
    _HIGH_PROFILES,
    _validate_nal_header,
    canonicalise_nal_with_ids,
    canonicalise_sps_with_dimensions,
)
from ._mp4_aac import _rebuild_inner_esds
from ._mp4_avc import _parse_avc_sample_config_from_entry
from ._mp4_primitives import pack as _pack


_MP4_MAX_TABLE_ENTRIES = 4096
_MP4_MAX_BOXES_PER_CONTAINER = 4096
_MP4_VISUAL_ENTRIES = {b"avc1"}
_MP4_AUDIO_ENTRIES = {b"mp4a"}
_MP4_VISUAL_INNER_ALLOWED = frozenset({b"avcC", b"btrt", b"pasp", b"colr"})
_MP4_AUDIO_INNER_ALLOWED = frozenset({b"esds", b"btrt"})
_MP4_SAMPLE_ENTRY_INNER_ALLOWED = _MP4_VISUAL_INNER_ALLOWED | _MP4_AUDIO_INNER_ALLOWED
_MP4_SAMPLE_ENTRY_REQUIRED_CONFIG = {b"avc1": b"avcC", b"mp4a": b"esds"}
_CANONICAL_FULLBOX_FLAGS = b"\x00\x00\x00"
_MP4_FULLBOX_ALLOWED_FLAG_MASK = {
    b"avcC": 0,
}
_Mp4TrackDimensions = tuple[int, int, bool]
_Mp4Sar = tuple[int, int]


def _rebuild_stsd(
    data: bytes, body_start: int, body_end: int, handler_type: bytes,
    track_dimensions: _Mp4TrackDimensions | None,
) -> bytes | None:
    payload = data[body_start:body_end]
    if len(payload) < 8:
        return None
    version = payload[0]
    if version != 0:
        raise MediaScrubError(f"mp4 stsd unknown version {version}")
    entry_count = struct.unpack(">I", payload[4:8])[0]
    if entry_count == 0:
        return None
    if entry_count > _MP4_MAX_TABLE_ENTRIES:
        raise MediaScrubError(
            f"mp4 stsd entry count exceeds {_MP4_MAX_TABLE_ENTRIES}"
        )
    remaining = len(payload) - 8
    if entry_count > remaining // 8:
        return None
    entries: list[bytes] = []
    offset = 8
    for _ in range(entry_count):
        if offset + 8 > len(payload):
            return None
        entry_size = struct.unpack(">I", payload[offset:offset + 4])[0]
        entry_type = payload[offset + 4:offset + 8]
        if entry_type == b"\x00\x00\x00\x00":
            return None
        if entry_size < 8 or offset + entry_size > len(payload):
            return None
        entry_bytes = payload[offset:offset + entry_size]
        if handler_type == b"vide":
            expected_entry_type = b"avc1"
        elif handler_type == b"soun":
            expected_entry_type = b"mp4a"
        else:
            raise MediaScrubError(
                f"mp4 hdlr track type {handler_type!r} is outside scrubber scope"
            )
        if entry_type != expected_entry_type:
            raise MediaScrubError(
                f"mp4 {handler_type.decode('ascii')} track cannot use "
                f"{entry_type.decode('ascii', 'replace')} sample entry; "
                "outside scrubber scope"
            )
        rebuilt = _rebuild_sample_entry(entry_type, entry_bytes, track_dimensions)
        entries.append(rebuilt)
        offset += entry_size
    return bytes([0]) + b"\x00\x00\x00" + struct.pack(">I", len(entries)) + b"".join(entries)


# ---------------------------------------------------------------------------
# Sample entry rebuild
# ---------------------------------------------------------------------------
#
# Each entry: size(4) + type(4) + reserved(6) + data_reference_index(2) +
# codec-specific fixed header + inner boxes. The reviewer flagged that
# unknown inner boxes survived because the sample entry was copied
# opaquely. We now emit the fixed header via struct.pack from parsed
# fields and walk inner boxes, allowlisting known codec-config types.
# Anything outside the allowlist rejects the file.


def _validate_avc_dimensions(
    sps_dimensions: tuple[int, int],
    pasp_ratio: _Mp4Sar | None,
    vui_sar: _Mp4Sar | None,
    track_dimensions: _Mp4TrackDimensions,
) -> tuple[int, int]:
    """Validate tkhd and return post-matrix display dimensions.

    Coded dimensions come from the SPS and sample entry. The effective SAR
    comes from pasp, or VUI when pasp is absent. Both sources must agree.
    With pasp, tkhd stores presentation dimensions. With VUI-only SAR, tkhd
    stores coded dimensions and the SAR changes only the reported display
    width. Rotation swaps only the reported display dimensions.
    """
    if pasp_ratio is not None and vui_sar is not None and pasp_ratio != vui_sar:
        raise MediaScrubError("mp4 pasp and SPS VUI SAR values do not agree")
    effective_sar = pasp_ratio or vui_sar or (1, 1)
    h_spacing, v_spacing = effective_sar
    coded_width, coded_height = sps_dimensions
    tkhd_width, tkhd_height, matrix_swaps_display = track_dimensions
    if pasp_ratio is None:
        valid_tkhd = tkhd_width == coded_width and tkhd_height == coded_height
    else:
        valid_tkhd = (
            tkhd_width * v_spacing == coded_width * h_spacing
            and tkhd_height == coded_height
        )
    if not valid_tkhd:
        raise MediaScrubError(
            "mp4 avc1 presentation dimensions do not match tkhd after SAR"
        )
    display_width_numerator = coded_width * h_spacing
    if display_width_numerator % v_spacing:
        raise MediaScrubError("mp4 effective SAR does not produce integer display width")
    display_width = display_width_numerator // v_spacing
    display_height = coded_height
    if matrix_swaps_display:
        return display_height, display_width
    return display_width, display_height

def _rebuild_sample_entry(
    entry_type: bytes,
    entry_bytes: bytes,
    track_dimensions: _Mp4TrackDimensions | None = None,
) -> bytes:
    if len(entry_bytes) < 16:
        raise MediaScrubError("mp4 sample entry too short for base header")
    # size + type already validated by caller; parse reserved + dref_idx.
    reserved6 = entry_bytes[8:14]
    if reserved6 != b"\x00" * 6:
        raise MediaScrubError("mp4 sample entry reserved-6 bytes non-zero")
    data_ref_index = struct.unpack(">H", entry_bytes[14:16])[0]
    if data_ref_index != 1:
        raise MediaScrubError(
            "mp4 sample entry data_reference_index must be canonical index 1"
        )

    if entry_type in _MP4_VISUAL_ENTRIES:
        fixed = _rebuild_visual_sample_entry_fixed(entry_bytes)
        inner_start = 16 + 70
        inner_allowed = _MP4_VISUAL_INNER_ALLOWED
    elif entry_type in _MP4_AUDIO_ENTRIES:
        fixed = _rebuild_audio_sample_entry_fixed(entry_bytes)
        inner_start = 16 + 20
        inner_allowed = _MP4_AUDIO_INNER_ALLOWED
    else:
        raise MediaScrubError(
            f"mp4 sample entry type {entry_type!r} outside allowlist"
        )

    if entry_type == b"avc1":
        pasp_ratio = _sample_entry_pasp_ratio(entry_bytes)
        _config, sps_dimensions, vui_sar = _parse_avc_sample_config_from_entry(entry_bytes)
        sample_dimensions = (
            struct.unpack(">H", entry_bytes[32:34])[0],
            struct.unpack(">H", entry_bytes[34:36])[0],
        )
        if sps_dimensions != sample_dimensions:
            raise MediaScrubError(
                "mp4 avc1 sample entry dimensions do not match SPS dimensions"
            )
        if track_dimensions is None:
            raise MediaScrubError(
                "mp4 avc1 SPS dimensions do not match tkhd track dimensions"
            )
        _validate_avc_dimensions(
            sps_dimensions, pasp_ratio, vui_sar, track_dimensions,
        )

    inner_payload, inner_types = _walk_sample_entry_inner_boxes(
        entry_bytes,
        inner_start,
        entry_type=entry_type,
        allowed=inner_allowed,
    )
    required_config = _MP4_SAMPLE_ENTRY_REQUIRED_CONFIG[entry_type]
    if required_config not in inner_types:
        raise MediaScrubError(
            f"mp4 {entry_type.decode('ascii')} sample entry requires exactly one "
            f"{required_config.decode('ascii')} configuration box"
        )
    body = (
        b"\x00" * 6
        + struct.pack(">H", data_ref_index)
        + fixed
        + inner_payload
    )
    return _pack(entry_type, body)


def _rebuild_audio_sample_entry_fixed(entry_bytes: bytes) -> bytes:
    """20-byte v0 audio sample entry — ISO/IEC 14496-12.

    Fields (all big-endian):
        reserved (8 bytes: 2 uint32) — must be zero
        channel_count (uint16)
        sample_size (uint16, must be 16)
        pre_defined (uint16) — dropped, emitted as 0
        reserved (uint16) — dropped, emitted as 0
        sample_rate (uint32, 16.16 fixed point)

    Extended audio sample entries (v1, v2) are rejected — the strict
    subset only accepts the canonical v0 shape ffmpeg produces for
    AAC-LC. Channel count must match a mono / stereo configuration; the
    real per-track channel count comes from esds AudioSpecificConfig
    and is checked there.
    """
    if len(entry_bytes) < 16 + 20:
        raise MediaScrubError("mp4 audio sample entry too short")
    body = entry_bytes[16:16 + 20]
    if body[0:8] != b"\x00" * 8:
        raise MediaScrubError("mp4 audio sample entry reserved-8 bytes non-zero")
    channel_count = struct.unpack(">H", body[8:10])[0]
    sample_size = struct.unpack(">H", body[10:12])[0]
    sample_rate_fixed = struct.unpack(">I", body[16:20])[0]
    if channel_count not in (1, 2):
        raise MediaScrubError(
            f"mp4 audio sample entry channel_count {channel_count} outside {{1,2}}"
        )
    if sample_size != 16:
        raise MediaScrubError(
            f"mp4 audio sample entry sample_size {sample_size} is not the canonical 16"
        )
    if sample_rate_fixed & 0xFFFF:
        raise MediaScrubError(
            "mp4 audio sample entry sample_rate must have zero fractional bits"
        )
    sample_rate = sample_rate_fixed >> 16
    if not 8000 <= sample_rate <= 96000:
        raise MediaScrubError(
            f"mp4 audio sample entry sample_rate {sample_rate} outside 8000..96000"
        )
    return (
        b"\x00" * 8
        + struct.pack(">HH", channel_count, sample_size)
        + b"\x00" * 4
        + struct.pack(">I", sample_rate_fixed)
    )


def _sample_entry_pasp_ratio(entry_bytes: bytes) -> tuple[int, int] | None:
    """Return a validated pixel-aspect ratio from an avc1 entry, if present."""
    ratio: tuple[int, int] | None = None
    for box_type, body in _iter_sample_entry_inner_boxes(
        entry_bytes, 16 + 70, allowed=_MP4_VISUAL_INNER_ALLOWED,
    ):
        if box_type != b"pasp":
            continue
        if len(body) != 8:
            raise MediaScrubError("mp4 pasp body must contain two uint32 values")
        h_spacing, v_spacing = struct.unpack(">II", body)
        if h_spacing == 0 or v_spacing == 0:
            raise MediaScrubError("mp4 pasp spacing values must be positive")
        divisor = math.gcd(h_spacing, v_spacing)
        ratio = (h_spacing // divisor, v_spacing // divisor)
    return ratio


def _rebuild_visual_sample_entry_fixed(entry_bytes: bytes) -> bytes:
    # 70-byte visual sample entry portion. Fields per ISO/IEC 14496-12.
    if len(entry_bytes) < 16 + 70:
        raise MediaScrubError("mp4 visual sample entry too short")
    body = entry_bytes[16:16 + 70]
    # 2 pre_defined + 2 reserved + 12 pre_defined = 16 bytes of zeros
    width = struct.unpack(">H", body[16:18])[0]
    height = struct.unpack(">H", body[18:20])[0]
    # 4 bytes reserved
    # compressor_name is a Pascal string (1 length byte + up to 31 chars,
    # zero padded to 32). This can carry the encoder identity ("Lavc..."),
    # so we drop it entirely — 32 bytes of zeros.
    return (
        b"\x00" * 16
        + struct.pack(">HH", width, height)
        + struct.pack(">II", 0x00480000, 0x00480000)
        + b"\x00" * 4
        + struct.pack(">H", 1)
        + b"\x00" * 32
        + struct.pack(">Hh", 0x0018, -1)
    )


# All sample-entry inner boxes now get a field-level rebuild — see the
# individual _rebuild_inner_* helpers below plus _rebuild_inner_avcC in
# the walker section. Codec-config formats we do NOT field-decode yet
# (hvcC/vpcC/av1C) are absent from _MP4_SAMPLE_ENTRY_INNER_ALLOWED, so files
# that use them are rejected under strict-subset acceptance.


def _rebuild_inner_btrt(body: bytes) -> bytes:
    # btrt body: bufferSizeDB(4) + maxBitrate(4) + avgBitrate(4) = 12 bytes.
    if len(body) != 12:
        raise MediaScrubError(
            f"mp4 btrt body length {len(body)} not the 12-byte spec size"
        )
    buffer_size = struct.unpack(">I", body[0:4])[0]
    max_bitrate = struct.unpack(">I", body[4:8])[0]
    avg_bitrate = struct.unpack(">I", body[8:12])[0]
    return _pack(b"btrt", struct.pack(">III", buffer_size, max_bitrate, avg_bitrate))


def _rebuild_inner_pasp(body: bytes) -> bytes:
    # pasp body: hSpacing(4) + vSpacing(4) = 8 bytes.
    if len(body) != 8:
        raise MediaScrubError(
            f"mp4 pasp body length {len(body)} not the 8-byte spec size"
        )
    h_spacing = struct.unpack(">I", body[0:4])[0]
    v_spacing = struct.unpack(">I", body[4:8])[0]
    if h_spacing == 0 or v_spacing == 0:
        raise MediaScrubError("mp4 pasp spacing values must be positive")
    divisor = math.gcd(h_spacing, v_spacing)
    return _pack(
        b"pasp",
        struct.pack(">II", h_spacing // divisor, v_spacing // divisor),
    )


def _rebuild_inner_colr(body: bytes) -> bytes:
    # colr body starts with a 4-byte colour_type tag. We support the two
    # tags a modern muxer emits:
    #   nclx (7 bytes body after the tag): primaries(2), transfer(2),
    #     matrix(2), full_range_flag byte
    #   nclc (6 bytes body after tag): primaries(2), transfer(2), matrix(2)
    # Any other tag is rejected. Trailing bytes past the declared shape
    # are rejected — no slack survives.
    if len(body) < 4:
        raise MediaScrubError("mp4 colr body too short")
    colour_type = body[0:4]
    payload = body[4:]
    if colour_type == b"nclx":
        if len(payload) != 7:
            raise MediaScrubError(
                f"mp4 colr(nclx) payload length {len(payload)} not the 7-byte spec size"
            )
        primaries = struct.unpack(">H", payload[0:2])[0]
        transfer = struct.unpack(">H", payload[2:4])[0]
        matrix = struct.unpack(">H", payload[4:6])[0]
        full_range = payload[6]
        if full_range & 0x7F:
            raise MediaScrubError(
                "mp4 colr(nclx) full_range_flag has reserved low bits set"
            )
        rebuilt = (
            b"nclx"
            + struct.pack(">HHH", primaries, transfer, matrix)
            + bytes([full_range & 0x80])
        )
    elif colour_type == b"nclc":
        if len(payload) != 6:
            raise MediaScrubError(
                f"mp4 colr(nclc) payload length {len(payload)} not the 6-byte spec size"
            )
        primaries = struct.unpack(">H", payload[0:2])[0]
        transfer = struct.unpack(">H", payload[2:4])[0]
        matrix = struct.unpack(">H", payload[4:6])[0]
        rebuilt = b"nclc" + struct.pack(">HHH", primaries, transfer, matrix)
    else:
        raise MediaScrubError(
            f"mp4 colr colour_type {colour_type!r} outside allowlist (nclx/nclc)"
        )
    return _pack(b"colr", rebuilt)


def _walk_sample_entry_inner_boxes(
    entry_bytes: bytes,
    inner_start: int,
    *,
    entry_type: bytes,
    allowed: frozenset[bytes] | set[bytes] = _MP4_SAMPLE_ENTRY_INNER_ALLOWED,
) -> tuple[bytes, set[bytes]]:
    """Rebuild each inner box from parsed fields. No opaque body copy
    path remains: every allowlisted type dispatches to a struct.pack
    rebuild that reads specific fields; anything not in the allowlist
    (hvcC/vpcC/av1C/sinf/schm/schi/tenc/anything unknown) rejects
    the whole file — strict-subset acceptance.
    """
    out = bytearray()
    seen: set[bytes] = set()
    for box_type, body in _iter_sample_entry_inner_boxes(
        entry_bytes, inner_start, allowed=allowed,
    ):
        if box_type in seen:
            raise MediaScrubError(
                f"mp4 {entry_type.decode('ascii')} sample entry has duplicate "
                f"{box_type.decode('ascii', 'replace')}"
            )
        seen.add(box_type)
        if box_type == b"avcC":
            out.extend(
                _rebuild_inner_avcC(
                    body, require_parameter_sets=entry_type == b"avc1",
                )
            )
        elif box_type == b"btrt":
            out.extend(_rebuild_inner_btrt(body))
        elif box_type == b"pasp":
            out.extend(_rebuild_inner_pasp(body))
        elif box_type == b"colr":
            out.extend(_rebuild_inner_colr(body))
        elif box_type == b"esds":
            out.extend(_rebuild_inner_esds(body))
        else:  # pragma: no cover — allowlist above already gated
            raise MediaScrubError(
                f"mp4 sample entry inner box {box_type!r} missing rebuilder"
            )
    return bytes(out), seen


def _iter_sample_entry_inner_boxes(
    entry_bytes: bytes, inner_start: int,
    allowed: frozenset[bytes] | set[bytes] = _MP4_SAMPLE_ENTRY_INNER_ALLOWED,
) -> Iterator[tuple[bytes, bytes]]:
    offset = inner_start
    end = len(entry_bytes)
    box_count = 0
    while offset < end:
        box_count += 1
        if box_count > _MP4_MAX_BOXES_PER_CONTAINER:
            raise MediaScrubError(
                f"mp4 sample entry has more than {_MP4_MAX_BOXES_PER_CONTAINER} child boxes"
            )
        if offset + 8 > end:
            raise MediaScrubError("mp4 sample entry inner box header truncated")
        box_size = struct.unpack(">I", entry_bytes[offset:offset + 4])[0]
        box_type = entry_bytes[offset + 4:offset + 8]
        if box_size < 8 or offset + box_size > end:
            raise MediaScrubError("mp4 sample entry inner box size out of bounds")
        if box_type not in allowed:
            raise MediaScrubError(
                f"mp4 sample entry inner box {box_type!r} outside allowlist"
            )
        yield box_type, entry_bytes[offset + 8:offset + box_size]
        offset += box_size


def _rebuild_inner_avcC(
    body: bytes,
    *,
    require_parameter_sets: bool = False,
) -> bytes:
    """AVC decoder configuration record — ISO/IEC 14496-15.

    Layout:
        configurationVersion (1) — must be 1
        AVCProfileIndication (1)
        profile_compatibility (1)
        AVCLevelIndication (1)
        reserved(6) | lengthSizeMinusOne(2)  (1)
        reserved(3) | numOfSequenceParameterSets(5)  (1)
        for each SPS:
            sequenceParameterSetLength (uint16 BE)
            sequenceParameterSetNALUnit (that many bytes)
        numOfPictureParameterSets (1)
        for each PPS:
            pictureParameterSetLength (uint16 BE)
            pictureParameterSetNALUnit (that many bytes)
        # Extended for high profiles (100/110/122/144):
        reserved(6) | chroma_format(2) (1)
        reserved(5) | bit_depth_luma_minus8(3) (1)
        reserved(5) | bit_depth_chroma_minus8(3) (1)
        numOfSequenceParameterSetExt (1)
        for each SPS ext:
            sequenceParameterSetExtLength (uint16 BE)
            sequenceParameterSetExtNALUnit

    Every counted array is walked and rebuilt via struct.pack + a
    validated-length body slice; trailing bytes past the last declared
    NAL cause a reject. SPS/PPS/SPSExt bodies are the actual video
    codec data — they're bounded by their own length prefix (a parsed
    field), which is the honest bound available. This is the same
    treatment we apply to mdat sample bytes.
    """
    if len(body) < 7:
        raise MediaScrubError("mp4 avcC body too short for fixed header")
    version = body[0]
    if version != 1:
        raise MediaScrubError(f"mp4 avcC configurationVersion {version} != 1")
    profile = body[1]
    compat = body[2]
    level = body[3]
    lsm_byte = body[4]
    if lsm_byte & 0xFC != 0xFC:
        raise MediaScrubError("mp4 avcC reserved bits above lengthSizeMinusOne non-set")
    if (lsm_byte & 0x03) not in (0, 1, 3):
        raise MediaScrubError(
            "mp4 avcC lengthSizeMinusOne must be 0, 1, or 3"
        )
    num_sps_byte = body[5]
    if num_sps_byte & 0xE0 != 0xE0:
        raise MediaScrubError("mp4 avcC reserved bits above numOfSequenceParameterSets non-set")
    num_sps = num_sps_byte & 0x1F
    offset = 6
    sps_list: list[bytes] = []
    sps_ids: set[int] = set()
    for _ in range(num_sps):
        if offset + 2 > len(body):
            raise MediaScrubError("mp4 avcC SPS length field truncated")
        sps_len = struct.unpack(">H", body[offset:offset + 2])[0]
        offset += 2
        if offset + sps_len > len(body):
            raise MediaScrubError("mp4 avcC SPS body extends past avcC")
        canonical, sps_id, _ = canonicalise_nal_with_ids(
            body[offset:offset + sps_len], expected_nal_type=7,
        )
        if len(canonical) > 0xFFFF:
            raise MediaScrubError("mp4 avcC canonical SPS exceeds uint16 length")
        sps_list.append(canonical)
        sps_ids.add(sps_id)
        offset += sps_len
    if offset >= len(body):
        raise MediaScrubError("mp4 avcC missing numOfPictureParameterSets byte")
    num_pps = body[offset]
    offset += 1
    pps_list: list[bytes] = []
    pps_sps_ids: list[int] = []
    for _ in range(num_pps):
        if offset + 2 > len(body):
            raise MediaScrubError("mp4 avcC PPS length field truncated")
        pps_len = struct.unpack(">H", body[offset:offset + 2])[0]
        offset += 2
        if offset + pps_len > len(body):
            raise MediaScrubError("mp4 avcC PPS body extends past avcC")
        canonical, _pps_id, pps_sps_id = canonicalise_nal_with_ids(
            body[offset:offset + pps_len], expected_nal_type=8,
        )
        if len(canonical) > 0xFFFF:
            raise MediaScrubError("mp4 avcC canonical PPS exceeds uint16 length")
        pps_list.append(canonical)
        if pps_sps_id is None:
            raise MediaScrubError("mp4 PPS did not return an SPS identifier")
        pps_sps_ids.append(pps_sps_id)
        offset += pps_len

    if require_parameter_sets and not sps_list:
        raise MediaScrubError("mp4 avc1 avcC requires at least one SPS")
    if require_parameter_sets and not pps_list:
        raise MediaScrubError("mp4 avc1 avcC requires at least one PPS")
    if require_parameter_sets and any(sps_id not in sps_ids for sps_id in pps_sps_ids):
        raise MediaScrubError(
            "mp4 avc1 PPS references an SPS identifier absent from avcC"
        )

    if sps_list:
        # Multiple-SPS rule: every canonical SPS must share the first SPS's
        # profile/compatibility/level triple, and avcC must match it. avc3
        # may have no SPS because its parameter sets can be in-band.
        canonical_header = sps_list[0][1:4]
        if any(sps[1:4] != canonical_header for sps in sps_list[1:]):
            raise MediaScrubError(
                "mp4 avcC SPS records disagree on profile/compatibility/level"
            )
        if bytes([profile, compat, level]) != canonical_header:
            raise MediaScrubError(
                "mp4 avcC header profile/compatibility/level mismatches canonical SPS"
            )
        profile, compat, level = canonical_header

    rebuilt_body = (
        bytes([1, profile, compat, level, lsm_byte, 0xE0 | num_sps])
        + b"".join(struct.pack(">H", len(sps)) + sps for sps in sps_list)
        + bytes([num_pps])
        + b"".join(struct.pack(">H", len(pps)) + pps for pps in pps_list)
    )

    # High profiles: extended trailer. Not all encoders emit it; if
    # bytes remain we require the profile to be a high one AND the
    # extended fields to be well-formed.
    if offset < len(body):
        if profile not in _HIGH_PROFILES:
            raise MediaScrubError(
                f"mp4 avcC has extended tail but profile {profile} is not a high profile"
            )
        if len(body) - offset < 4:
            raise MediaScrubError("mp4 avcC extended tail too short")
        chroma_byte = body[offset]
        depth_luma_byte = body[offset + 1]
        depth_chroma_byte = body[offset + 2]
        num_sps_ext = body[offset + 3]
        if chroma_byte & 0xFC != 0xFC:
            raise MediaScrubError("mp4 avcC extended chroma reserved bits wrong")
        if depth_luma_byte & 0xF8 != 0xF8:
            raise MediaScrubError("mp4 avcC extended luma-depth reserved bits wrong")
        if depth_chroma_byte & 0xF8 != 0xF8:
            raise MediaScrubError("mp4 avcC extended chroma-depth reserved bits wrong")
        offset += 4
        # Round-9 review: SPS extension NALs (auxiliary picture streams) are
        # rejected outright. They're vanishingly rare on artifact uploads,
        # and canonicalising the SPS-ext NAL RBSP would require another full
        # H.264 auxiliary parser. Strict-subset rejection keeps the surface
        # tight without the parser bloat.
        if num_sps_ext != 0:
            raise MediaScrubError(
                "mp4 avcC declares SPS-ext arrays; auxiliary picture streams not accepted"
            )
        rebuilt_body += bytes([chroma_byte, depth_luma_byte, depth_chroma_byte, 0])

    if offset != len(body):
        raise MediaScrubError(
            f"mp4 avcC has {len(body) - offset} trailing bytes past declared arrays"
        )
    return _pack(b"avcC", rebuilt_body)


# ---------------------------------------------------------------------------
# duration + dims extraction (from parsed moov payload)
# ---------------------------------------------------------------------------
