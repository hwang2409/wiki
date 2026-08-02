"""AVC sample-description and sample-NAL validation for MP4."""
from __future__ import annotations

from collections.abc import Iterator
import struct

from .base import MediaScrubError
from ._h264 import (
    _validate_nal_header,
    canonicalise_aud,
    canonicalise_filler_nal,
    canonicalise_nal_with_ids,
    canonicalise_sps_with_picture_bounds,
    parse_slice_header_with_first_mb,
)
from ._mp4_primitives import Mp4Atom as _Mp4Atom, parse_container as _parse_container


_MP4_MAX_TABLE_ENTRIES = 4096
_MP4_MAX_BOXES_PER_CONTAINER = 4096
_MP4_AVC_SAMPLE_NAL_TYPES = {1, 5, 6, 9, 12}
_CANONICAL_FULLBOX_FLAGS = b"\x00\x00\x00"
_MP4_SAMPLE_ENTRY_INNER_ALLOWED = {b"avcC", b"btrt", b"pasp", b"colr"}
_Mp4Sar = tuple[int, int]


def _iter_sample_entry_inner_boxes(
    entry_bytes: bytes, inner_start: int,
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
        if box_type not in _MP4_SAMPLE_ENTRY_INNER_ALLOWED:
            raise MediaScrubError(
                f"mp4 sample entry inner box {box_type!r} outside allowlist"
            )
        yield box_type, entry_bytes[offset + 8:offset + box_size]
        offset += box_size


def _sample_description_configs(
    data: bytes, body_start: int, body_end: int, handler_type: bytes,
) -> list[tuple[int, dict[int, tuple[int, int]], bool] | None]:
    """Return one codec configuration per stsd sample-description index."""
    if handler_type != b"vide":
        raise MediaScrubError(
            "mp4 audio tracks are outside the video-only MP4 scrubber"
        )
    stsd_atoms = [
        atom for atom in _parse_container(data, body_start, body_end)
        if atom.type == b"stsd"
    ]
    if len(stsd_atoms) != 1:
        raise MediaScrubError("mp4 stbl must contain exactly one stsd")
    body = data[stsd_atoms[0].body_start:stsd_atoms[0].body_end]
    if len(body) < 8 or body[0] != 0 or body[1:4] != _CANONICAL_FULLBOX_FLAGS:
        raise MediaScrubError("mp4 stsd has non-canonical fullbox header")
    entry_count = struct.unpack(">I", body[4:8])[0]
    if entry_count == 0:
        return []
    if entry_count > _MP4_MAX_TABLE_ENTRIES:
        raise MediaScrubError(
            f"mp4 stsd entry count exceeds {_MP4_MAX_TABLE_ENTRIES}"
        )
    entries: list[tuple[int, dict[int, tuple[int, int]], bool] | None] = []
    offset = 8
    for _ in range(entry_count):
        if offset + 8 > len(body):
            raise MediaScrubError("mp4 stsd entry header truncated")
        entry_size = struct.unpack(">I", body[offset:offset + 4])[0]
        entry_type = body[offset + 4:offset + 8]
        if entry_size < 8 or offset + entry_size > len(body):
            raise MediaScrubError("mp4 stsd entry size out of bounds")
        entry = body[offset:offset + entry_size]
        expected_entry_type = b"avc1"
        if entry_type != expected_entry_type:
            raise MediaScrubError(
                f"mp4 {handler_type.decode('ascii')} track cannot use "
                f"{entry_type.decode('ascii', 'replace')} sample entry; "
                "outside scrubber scope"
            )
        config, _dimensions, _vui_sar = _parse_avc_sample_config_from_entry(entry)
        entries.append(config)
        offset += entry_size
    return entries


def _parse_avc_sample_config_from_entry(
    entry: bytes,
) -> tuple[tuple[int, dict[int, tuple[int, int]], bool], tuple[int, int], _Mp4Sar | None]:
    avcc_body: bytes | None = None
    seen: set[bytes] = set()
    for box_type, box_body in _iter_sample_entry_inner_boxes(entry, 16 + 70):
        if box_type in seen:
            raise MediaScrubError(
                f"mp4 avc1 sample entry has duplicate {box_type.decode('ascii', 'replace')}"
            )
        seen.add(box_type)
        if box_type == b"avcC":
            avcc_body = box_body
    if avcc_body is None:
        raise MediaScrubError("mp4 avc1 sample entry requires exactly one avcC")
    config, dimensions, vui_sar = _parse_avc_sample_config(avcc_body, True)
    entry_width = struct.unpack(">H", entry[32:34])[0]
    entry_height = struct.unpack(">H", entry[34:36])[0]
    if dimensions != (entry_width, entry_height):
        raise MediaScrubError(
            "mp4 avc1 sample entry dimensions do not match SPS dimensions"
        )
    return config, dimensions, vui_sar


def _parse_avc_sample_config(
    body: bytes, require_parameter_sets: bool,
) -> tuple[tuple[int, dict[int, tuple[int, int]], bool], tuple[int, int], _Mp4Sar | None]:
    if len(body) < 7 or body[0] != 1:
        raise MediaScrubError("mp4 avcC sample configuration header is invalid")
    length_size_minus_one = body[4] & 0x03
    if length_size_minus_one not in (0, 1, 3):
        raise MediaScrubError("mp4 avcC lengthSizeMinusOne must be 0, 1, or 3")
    num_sps = body[5] & 0x1F
    offset = 6
    sps_ids: set[int] = set()
    sps_picture_bounds: dict[int, int] = {}
    sps_dimensions: tuple[int, int] | None = None
    sps_vui_sar: _Mp4Sar | None = None
    for _ in range(num_sps):
        if offset + 2 > len(body):
            raise MediaScrubError("mp4 avcC SPS length field truncated")
        size = struct.unpack(">H", body[offset:offset + 2])[0]
        offset += 2
        if offset + size > len(body):
            raise MediaScrubError("mp4 avcC SPS extends past body")
        _canonical, sps_id, dimensions, vui_sar, picture_mbs = canonicalise_sps_with_picture_bounds(
            body[offset:offset + size],
        )
        if sps_dimensions is not None and dimensions != sps_dimensions:
            raise MediaScrubError("mp4 avcC SPS dimensions do not agree")
        if sps_dimensions is not None and vui_sar != sps_vui_sar:
            raise MediaScrubError("mp4 avcC SPS VUI SAR values do not agree")
        sps_dimensions = dimensions
        sps_vui_sar = vui_sar
        sps_ids.add(sps_id)
        sps_picture_bounds[sps_id] = picture_mbs
        offset += size
    if offset >= len(body):
        raise MediaScrubError("mp4 avcC missing PPS count")
    num_pps = body[offset]
    offset += 1
    pps_sps_ids: dict[int, tuple[int, int]] = {}
    for _ in range(num_pps):
        if offset + 2 > len(body):
            raise MediaScrubError("mp4 avcC PPS length field truncated")
        size = struct.unpack(">H", body[offset:offset + 2])[0]
        offset += 2
        if offset + size > len(body):
            raise MediaScrubError("mp4 avcC PPS extends past body")
        _canonical, pps_id, pps_sps_id = canonicalise_nal_with_ids(
            body[offset:offset + size], expected_nal_type=8,
        )
        if pps_sps_id is None or pps_sps_id not in sps_ids:
            raise MediaScrubError("mp4 avcC PPS references an SPS identifier absent from avcC")
        pps_sps_ids[pps_id] = (pps_sps_id, sps_picture_bounds[pps_sps_id])
        offset += size
    if require_parameter_sets and not sps_ids:
        raise MediaScrubError("mp4 avc1 avcC requires at least one SPS")
    if require_parameter_sets and not pps_sps_ids:
        raise MediaScrubError("mp4 avc1 avcC requires at least one PPS")
    if sps_dimensions is None:
        raise MediaScrubError("mp4 avcC has no SPS dimensions")
    return (
        (length_size_minus_one + 1, pps_sps_ids, require_parameter_sets),
        sps_dimensions,
        sps_vui_sar,
    )


def _canonicalise_avc_sample(
    sample: bytes, length_size: int,
    pps_sps_bounds: dict[int, tuple[int, int]], require_pps: bool,
) -> bytes:
    if not sample:
        return b""
    output = bytearray()
    offset = 0
    nal_count = 0
    has_coded_slice = False
    while offset < len(sample):
        if offset + length_size > len(sample):
            raise MediaScrubError("mp4 AVC sample NAL length is truncated")
        nal_size = int.from_bytes(sample[offset:offset + length_size], "big")
        offset += length_size
        if nal_size == 0 or offset + nal_size > len(sample):
            raise MediaScrubError("mp4 AVC sample NAL length is out of bounds")
        nal = sample[offset:offset + nal_size]
        nal_type = nal[0] & 0x1F
        if nal_type not in _MP4_AVC_SAMPLE_NAL_TYPES:
            if nal_type in (7, 8):
                raise MediaScrubError(
                    "mp4 avc1 sample contains an in-band parameter-set NAL"
                )
            raise MediaScrubError(
                f"mp4 avc1 sample contains unsupported NAL type {nal_type}"
            )
        nal_ref_idc = _validate_nal_header(
            nal, nal_type,
            require_nonzero_ref=nal_type == 5,
            require_zero_ref=nal_type in (6, 9, 12),
        )
        if nal_type in (1, 5):
            has_coded_slice = True
            first_mb_in_slice, _slice_type, pps_id = parse_slice_header_with_first_mb(nal)
            if require_pps and pps_id not in pps_sps_bounds:
                raise MediaScrubError("mp4 AVC slice references an unknown PPS identifier")
            if require_pps and first_mb_in_slice >= pps_sps_bounds[pps_id][1]:
                raise MediaScrubError(
                    "mp4 AVC slice first_mb_in_slice is outside the coded picture"
                )
        if nal_type == 9:
            canonical = canonicalise_aud(nal)
            if len(canonical) != len(nal):
                raise MediaScrubError("mp4 AVC AUD rebuild changed its length")
            nal = canonical
        elif nal_type == 12:
            nal = canonicalise_filler_nal(nal)
        elif nal_type == 6:
            # Keep the declared NAL length and replace SEI with a valid
            # length-preserving filler-data NAL. Filler data has one or more
            # 0xff bytes followed by rbsp_trailing_bits (0x80).
            if len(nal) < 3:
                raise MediaScrubError(
                    "mp4 AVC SEI is too short for a canonical filler NAL"
                )
            nal = b"\x0c" + b"\xff" * (len(nal) - 2) + b"\x80"
        output.extend(nal_size.to_bytes(length_size, "big"))
        output.extend(nal)
        offset += nal_size
        nal_count += 1
    if nal_count == 0:
        raise MediaScrubError("mp4 AVC sample has no NAL units")
    if not has_coded_slice:
        raise MediaScrubError("mp4 AVC sample has no coded slice")
    return bytes(output)


# ---------------------------------------------------------------------------
