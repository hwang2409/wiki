"""MP4 timing validation helpers."""
from __future__ import annotations

import struct

from .base import MediaScrubError
from ._mp4_sample_entry import (
    _Mp4TrackDimensions,
    _parse_avc_sample_config_from_entry,
    _sample_entry_pasp_ratio,
    _validate_avc_dimensions,
)
from ._mp4_primitives import Mp4Atom as _Mp4Atom, parse_container as _parse_container


_MP4_MAX_TABLE_ENTRIES = 4096
_CANONICAL_FULLBOX_FLAGS = b"\x00\x00\x00"
_MP4_ROTATION_SWAP_MATRICES = frozenset({
    (0, 0x00010000, 0, -0x00010000, 0, 0, 0, 0, 0x40000000),
    (0, -0x00010000, 0, 0x00010000, 0, 0, 0, 0, 0x40000000),
})


def _validate_zero_flags(box_type: bytes, flags: bytes) -> bytes:
    allowed_mask = 0x00000F if box_type == b"tkhd" else 0
    if len(flags) != 3 or int.from_bytes(flags, "big") & ~allowed_mask:
        raise MediaScrubError(f"mp4 {box_type.decode()} fullbox flags are non-canonical")
    return flags


def _handler_type(data: bytes, atom: _Mp4Atom) -> bytes:
    body = data[atom.body_start:atom.body_end]
    if len(body) < 24 or body[0] != 0:
        raise MediaScrubError("mp4 hdlr header is invalid")
    _validate_zero_flags(b"hdlr", body[1:4])
    handler_type = body[8:12]
    if handler_type not in (b"vide", b"soun"):
        raise MediaScrubError(
            f"mp4 hdlr track type {handler_type!r} is outside scrubber scope"
        )
    return handler_type



def _timing_atom_body(
    data: bytes, atom: _Mp4Atom, label: bytes,
    versions: tuple[int, ...] = (0,),
) -> bytes:
    body = data[atom.body_start:atom.body_end]
    if len(body) < 4 or body[0] not in versions:
        raise MediaScrubError(f"mp4 {label.decode()} version is unsupported")
    if body[1:4] != _CANONICAL_FULLBOX_FLAGS:
        raise MediaScrubError(f"mp4 {label.decode()} fullbox flags are non-canonical")
    return body


def _parse_mvhd_timing(data: bytes, atom: _Mp4Atom) -> tuple[int, int]:
    body = data[atom.body_start:atom.body_end]
    if len(body) == 100 and body[0] == 0:
        _timing_atom_body(data, atom, b"mvhd")
        timescale, duration = struct.unpack(">II", body[12:20])
    elif len(body) == 112 and body[0] == 1:
        _timing_atom_body(data, atom, b"mvhd", versions=(1,))
        timescale = struct.unpack(">I", body[20:24])[0]
        duration = struct.unpack(">Q", body[24:32])[0]
    else:
        raise MediaScrubError("mp4 mvhd body length or version is invalid")
    if timescale == 0:
        raise MediaScrubError("mp4 mvhd timescale must be positive")
    if duration == 0:
        raise MediaScrubError("mp4 mvhd duration must be positive")
    return timescale, duration


def _parse_mdhd_timing(data: bytes, atom: _Mp4Atom) -> tuple[int, int]:
    body = data[atom.body_start:atom.body_end]
    if len(body) == 24 and body[0] == 0:
        _timing_atom_body(data, atom, b"mdhd")
        timescale, duration = struct.unpack(">II", body[12:20])
    elif len(body) == 36 and body[0] == 1:
        _timing_atom_body(data, atom, b"mdhd", versions=(1,))
        timescale = struct.unpack(">I", body[20:24])[0]
        duration = struct.unpack(">Q", body[24:32])[0]
    else:
        raise MediaScrubError("mp4 mdhd body length or version is invalid")
    if timescale == 0:
        raise MediaScrubError("mp4 mdhd timescale must be positive")
    if duration == 0:
        raise MediaScrubError("mp4 mdhd duration must be positive")
    return timescale, duration


def _parse_tkhd_duration(data: bytes, atom: _Mp4Atom) -> int:
    body = data[atom.body_start:atom.body_end]
    if len(body) == 84 and body[0] == 0:
        _validate_zero_flags(b"tkhd", body[1:4])
        duration = struct.unpack(">I", body[20:24])[0]
    elif len(body) == 96 and body[0] == 1:
        _validate_zero_flags(b"tkhd", body[1:4])
        duration = struct.unpack(">Q", body[28:36])[0]
    else:
        raise MediaScrubError("mp4 tkhd body length or version is invalid")
    if duration == 0:
        raise MediaScrubError("mp4 tkhd duration must be positive")
    return duration


def _parse_stts_timing(data: bytes, stbl_atoms: list[_Mp4Atom]) -> tuple[int, int, int]:
    stts_atoms = [atom for atom in stbl_atoms if atom.type == b"stts"]
    if len(stts_atoms) != 1:
        raise MediaScrubError("mp4 stbl requires exactly one stts table")
    body = data[stts_atoms[0].body_start:stts_atoms[0].body_end]
    _timing_atom_body(data, stts_atoms[0], b"stts")
    if len(body) < 8:
        raise MediaScrubError("mp4 stts body too short")
    entry_count = struct.unpack(">I", body[4:8])[0]
    if entry_count > _MP4_MAX_TABLE_ENTRIES or len(body) != 8 + entry_count * 8:
        raise MediaScrubError("mp4 stts body length is invalid")
    sample_count = 0
    duration = 0
    max_delta = 0
    for offset in range(8, len(body), 8):
        run_count, delta = struct.unpack(">II", body[offset:offset + 8])
        if run_count == 0 or delta == 0:
            raise MediaScrubError("mp4 stts contains a zero-valued run")
        sample_count += run_count
        duration += run_count * delta
        max_delta = max(max_delta, delta)
    if sample_count == 0 or duration == 0:
        raise MediaScrubError("mp4 stts has no positive media duration")
    return sample_count, duration, max_delta


def _parse_edit_duration(
    data: bytes, edts_atoms: list[_Mp4Atom], mdhd_duration: int,
) -> int | None:
    if not edts_atoms:
        return None
    if len(edts_atoms) != 1:
        raise MediaScrubError("mp4 trak carries duplicate edts boxes")
    children = _parse_container(data, edts_atoms[0].body_start, edts_atoms[0].body_end)
    if len(children) != 1 or children[0].type != b"elst":
        raise MediaScrubError("mp4 edts must contain exactly one elst child")
    atom = children[0]
    body = data[atom.body_start:atom.body_end]
    if len(body) < 8 or body[0] not in (0, 1):
        raise MediaScrubError("mp4 elst timing header is invalid")
    _validate_zero_flags(b"elst", body[1:4])
    count = struct.unpack(">I", body[4:8])[0]
    entry_size = 12 if body[0] == 0 else 20
    if count == 0 or count > _MP4_MAX_TABLE_ENTRIES or len(body) != 8 + count * entry_size:
        raise MediaScrubError("mp4 elst timing entries are invalid")
    duration = 0
    for offset in range(8, len(body), entry_size):
        if body[0] == 0:
            segment_duration, media_time, rate = struct.unpack(">IiI", body[offset:offset + 12])
        else:
            segment_duration, media_time, rate = struct.unpack(">QqI", body[offset:offset + 20])
        if segment_duration == 0 or rate != 0x00010000:
            raise MediaScrubError("mp4 elst contains unsupported timing")
        if media_time < -1 or (media_time >= mdhd_duration and media_time != -1):
            raise MediaScrubError("mp4 elst media_time is outside mdhd duration")
        duration += segment_duration
    return duration


def _validated_movie_duration(data: bytes, moov: _Mp4Atom) -> int:
    """Validate movie timing and return duration derived from track samples."""
    children = _parse_container(data, moov.body_start, moov.body_end)
    mvhd_atoms = [atom for atom in children if atom.type == b"mvhd"]
    if len(mvhd_atoms) != 1:
        raise MediaScrubError("mp4 moov requires exactly one mvhd child")
    movie_timescale, movie_duration = _parse_mvhd_timing(data, mvhd_atoms[0])
    validated_duration: int | None = None
    for trak in (atom for atom in children if atom.type == b"trak"):
        trak_children = _parse_container(data, trak.body_start, trak.body_end)
        tkhd_atoms = [atom for atom in trak_children if atom.type == b"tkhd"]
        mdia_atoms = [atom for atom in trak_children if atom.type == b"mdia"]
        if len(tkhd_atoms) != 1 or len(mdia_atoms) != 1:
            raise MediaScrubError("mp4 trak timing chain is incomplete")
        mdia_children = _parse_container(data, mdia_atoms[0].body_start, mdia_atoms[0].body_end)
        mdhd_atoms = [atom for atom in mdia_children if atom.type == b"mdhd"]
        hdlr_atoms = [atom for atom in mdia_children if atom.type == b"hdlr"]
        minf_atoms = [atom for atom in mdia_children if atom.type == b"minf"]
        if len(mdhd_atoms) != 1 or len(hdlr_atoms) != 1 or len(minf_atoms) != 1:
            raise MediaScrubError("mp4 mdia timing chain is incomplete")
        handler = _handler_type(data, hdlr_atoms[0])
        if handler not in (b"vide", b"soun"):
            continue
        mdhd_timescale, mdhd_duration = _parse_mdhd_timing(data, mdhd_atoms[0])
        minf_children = _parse_container(data, minf_atoms[0].body_start, minf_atoms[0].body_end)
        stbl_atoms = [atom for atom in minf_children if atom.type == b"stbl"]
        if len(stbl_atoms) != 1:
            raise MediaScrubError("mp4 minf timing chain is incomplete")
        stbl_children = _parse_container(data, stbl_atoms[0].body_start, stbl_atoms[0].body_end)
        _sample_count, stts_duration, max_delta = _parse_stts_timing(data, stbl_children)
        if mdhd_duration < stts_duration or mdhd_duration - stts_duration > max_delta:
            raise MediaScrubError("mp4 mdhd duration does not match stts timing")
        edit_duration = _parse_edit_duration(
            data,
            [atom for atom in trak_children if atom.type == b"edts"],
            mdhd_duration,
        )
        if edit_duration is None:
            expected_movie_duration = round(stts_duration * movie_timescale / mdhd_timescale)
        else:
            expected_movie_duration = edit_duration
        tkhd_duration = _parse_tkhd_duration(data, tkhd_atoms[0])
        if tkhd_duration != movie_duration:
            raise MediaScrubError("mp4 mvhd and tkhd durations differ")
        if abs(tkhd_duration - expected_movie_duration) > 1:
            raise MediaScrubError("mp4 track duration does not match stts and edits")
        expected_ms = round(expected_movie_duration * 1000 / movie_timescale)
        # Prefer the video track's duration when both are present — a
        # visible playback length is what users see. Audio-only files
        # fall back to the audio timing.
        if handler == b"vide":
            validated_duration = expected_ms
        elif validated_duration is None:
            validated_duration = expected_ms
    if validated_duration is None:
        raise MediaScrubError("mp4 moov has no supported vide/soun track timing")
    return validated_duration

def _tkhd_dimensions_from_atom(
    data: bytes, atom: _Mp4Atom,
) -> _Mp4TrackDimensions | None:
    """Read positive 16.16 video dimensions and rotation from one tkhd."""
    body = data[atom.body_start:atom.body_end]
    if not body:
        return None
    version = body[0]
    if version == 0 and len(body) == 84:
        matrix = body[40:76]
        width_fixed, height_fixed = struct.unpack(">II", body[76:84])
    elif version == 1 and len(body) == 96:
        matrix = body[52:88]
        width_fixed, height_fixed = struct.unpack(">II", body[88:96])
    else:
        raise MediaScrubError("mp4 tkhd dimensions cannot be read")
    if width_fixed & 0xFFFF or height_fixed & 0xFFFF:
        raise MediaScrubError(
            "mp4 tkhd dimensions must have zero fractional bits"
        )
    width = width_fixed >> 16
    height = height_fixed >> 16
    if width == 0 or height == 0:
        return None
    values = struct.unpack(">9i", matrix)
    return width, height, values in _MP4_ROTATION_SWAP_MATRICES

def _display_dims_from_moov(
    data: bytes, moov: _Mp4Atom,
) -> tuple[int, int] | None:
    """Return validated display dimensions for the first video track."""
    moov_children = _parse_container(data, moov.body_start, moov.body_end)
    for trak in (atom for atom in moov_children if atom.type == b"trak"):
        trak_children = _parse_container(data, trak.body_start, trak.body_end)
        tkhd_atoms = [atom for atom in trak_children if atom.type == b"tkhd"]
        mdia_atoms = [atom for atom in trak_children if atom.type == b"mdia"]
        if len(tkhd_atoms) != 1 or len(mdia_atoms) != 1:
            continue
        mdia_children = _parse_container(
            data, mdia_atoms[0].body_start, mdia_atoms[0].body_end,
        )
        hdlr_atoms = [atom for atom in mdia_children if atom.type == b"hdlr"]
        minf_atoms = [atom for atom in mdia_children if atom.type == b"minf"]
        if len(hdlr_atoms) != 1 or len(minf_atoms) != 1:
            continue
        if _handler_type(data, hdlr_atoms[0]) != b"vide":
            continue
        minf_children = _parse_container(
            data, minf_atoms[0].body_start, minf_atoms[0].body_end,
        )
        stbl_atoms = [atom for atom in minf_children if atom.type == b"stbl"]
        if len(stbl_atoms) != 1:
            continue
        stbl_children = _parse_container(
            data, stbl_atoms[0].body_start, stbl_atoms[0].body_end,
        )
        stsd_atoms = [atom for atom in stbl_children if atom.type == b"stsd"]
        if len(stsd_atoms) != 1:
            continue
        body = data[stsd_atoms[0].body_start:stsd_atoms[0].body_end]
        if len(body) < 16:
            continue
        entry_size = struct.unpack(">I", body[8:12])[0]
        if entry_size < 8 or 8 + entry_size > len(body):
            continue
        entry = body[8:8 + entry_size]
        _config, sps_dimensions, vui_sar = _parse_avc_sample_config_from_entry(entry)
        pasp_ratio = _sample_entry_pasp_ratio(entry)
        track_dimensions = _tkhd_dimensions_from_atom(data, tkhd_atoms[0])
        if track_dimensions is None:
            return None
        return _validate_avc_dimensions(
            sps_dimensions, pasp_ratio, vui_sar, track_dimensions,
        )
    return None
