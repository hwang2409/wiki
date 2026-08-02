"""Bounded MP4 sample ownership planning and range iteration."""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator
import struct
from dataclasses import dataclass

from .base import MediaScrubError
from ._mp4_primitives import Mp4Atom as _Mp4Atom, parse_container as _parse_container
from ._mp4_avc import _sample_description_configs


_MP4_MAX_SAMPLES = 16_777_216
_MP4_MAX_CHUNKS = 65_536
_MP4_MAX_TRACK_MDAT_GROUPS = 65_536
_MP4_MAX_TABLE_ENTRIES = 4096
_CANONICAL_FULLBOX_FLAGS = b"\x00\x00\x00"


@dataclass(frozen=True)
class Mp4SampleRange:
    start: int
    end: int
    description_index: int
    codec_config: tuple[int, dict[int, tuple[int, int]], bool] | None


@dataclass(frozen=True)
class Mp4TrackSamplePlan:
    chunks_by_mdat: dict[
        int,
        tuple[tuple[int, int, int, int, int, tuple[int, dict[int, tuple[int, int]], bool]
                    | None, int], ...],
    ]
    sample_size: int
    sample_sizes: memoryview | None
    sample_count: int


_Mp4SampleRange = Mp4SampleRange
_Mp4TrackSamplePlan = Mp4TrackSamplePlan


def _handler_type(data: bytes, atom: _Mp4Atom) -> bytes:
    body = data[atom.body_start:atom.body_end]
    if len(body) < 24 or body[0] != 0:
        raise MediaScrubError("mp4 hdlr header is invalid")
    if body[1:4] != _CANONICAL_FULLBOX_FLAGS:
        raise MediaScrubError("mp4 hdlr fullbox flags are non-canonical")
    handler_type = body[8:12]
    if handler_type not in (b"vide", b"soun"):
        raise MediaScrubError(
            f"mp4 hdlr track type {handler_type!r} is outside scrubber scope"
        )
    return handler_type


def _build_sample_plan(
    data: bytes,
    top_atoms: list[_Mp4Atom],
    mdat_ranges: list[tuple[int, int]],
) -> tuple[_Mp4TrackSamplePlan, ...]:
    """Build one bounded chunk plan and validate ownership once."""
    track_plans: list[_Mp4TrackSamplePlan] = []
    track_mdat_groups = 0
    ownership: list[
        tuple[int, int, int, int, int,
              tuple[int, dict[int, tuple[int, int]], bool] | None, int]
    ] = []
    mdat_starts = [start for start, _end in mdat_ranges]
    for moov in (atom for atom in top_atoms if atom.type == b"moov"):
        for trak in _parse_container(data, moov.body_start, moov.body_end):
            if trak.type != b"trak":
                continue
            for mdia in _parse_container(data, trak.body_start, trak.body_end):
                if mdia.type != b"mdia":
                    continue
                mdia_atoms = _parse_container(data, mdia.body_start, mdia.body_end)
                hdlr_atoms = [atom for atom in mdia_atoms if atom.type == b"hdlr"]
                if len(hdlr_atoms) != 1:
                    raise MediaScrubError("mp4 mdia requires exactly one hdlr child")
                handler_type = _handler_type(data, hdlr_atoms[0])
                for minf in mdia_atoms:
                    if minf.type != b"minf":
                        continue
                    for stbl in _parse_container(data, minf.body_start, minf.body_end):
                        if stbl.type == b"stbl":
                            track_plan = _build_sample_plan_from_stbl(
                                data, stbl.body_start, stbl.body_end, mdat_ranges,
                                mdat_starts, handler_type,
                            )
                            if track_plan is not None:
                                track_mdat_groups += len(track_plan.chunks_by_mdat)
                                if track_mdat_groups > _MP4_MAX_TRACK_MDAT_GROUPS:
                                    raise MediaScrubError(
                                        "mp4 track/mdat group count exceeds scrubber limit"
                                    )
                                track_chunks = tuple(
                                    chunk
                                    for group in track_plan.chunks_by_mdat.values()
                                    for chunk in group
                                )
                                if len(ownership) + len(track_chunks) > _MP4_MAX_CHUNKS:
                                    raise MediaScrubError(
                                        f"mp4 has more than {_MP4_MAX_CHUNKS} sample chunks"
                                    )
                                track_plans.append(track_plan)
                                ownership.extend(track_chunks)
    ownership.sort(key=lambda item: item[0])
    previous: tuple[int, int] | None = None
    for chunk in ownership:
        chunk_range = (chunk[0], chunk[1])
        if previous is not None and chunk_range[0] < previous[1]:
            raise MediaScrubError("mp4 sample chunks overlap across or within tracks")
        previous = chunk_range
    return tuple(track_plans)


def _build_sample_plan_from_stbl(
    data: bytes,
    body_start: int,
    body_end: int,
    mdat_ranges: list[tuple[int, int]],
    mdat_starts: list[int],
    handler_type: bytes,
) -> _Mp4TrackSamplePlan | None:
    tables = {
        atom.type: atom
        for atom in _parse_container(data, body_start, body_end)
        if atom.type in {b"stsc", b"stsz", b"stco", b"co64"}
    }
    if not tables:
        return None
    if b"stsc" not in tables or b"stsz" not in tables:
        raise MediaScrubError("mp4 sample tables missing stsc or stsz")
    if b"stco" in tables and b"co64" in tables:
        raise MediaScrubError("mp4 sample tables carry both stco and co64")
    offset_atom = tables.get(b"stco") or tables.get(b"co64")
    if offset_atom is None:
        raise MediaScrubError("mp4 sample tables missing stco or co64")

    sample_descriptions = _sample_description_configs(
        data, body_start, body_end, handler_type,
    )
    if not sample_descriptions:
        raise MediaScrubError("mp4 stbl/stsd has no sample descriptions")

    def fullbox_body(atom: _Mp4Atom, label: str) -> bytes:
        body = data[atom.body_start:atom.body_end]
        if len(body) < 8 or body[0] != 0 or body[1:4] != _CANONICAL_FULLBOX_FLAGS:
            raise MediaScrubError(
                f"mp4 {label} fullbox flags or header are non-canonical"
            )
        return body

    stsc_body = fullbox_body(tables[b"stsc"], "stsc")
    stsc_count = struct.unpack(">I", stsc_body[4:8])[0]
    if stsc_count > _MP4_MAX_TABLE_ENTRIES:
        raise MediaScrubError(
            f"mp4 stsc entry count exceeds {_MP4_MAX_TABLE_ENTRIES}"
        )
    if len(stsc_body) != 8 + stsc_count * 12 or stsc_count == 0:
        raise MediaScrubError("mp4 stsc body length does not match entries")
    stsc_entries: list[tuple[int, int, int]] = []
    offset = 8
    previous_first_chunk = 0
    for _ in range(stsc_count):
        first_chunk, samples_per_chunk, description_index = struct.unpack(
            ">III", stsc_body[offset:offset + 12]
        )
        if (
            first_chunk == 0
            or first_chunk <= previous_first_chunk
            or samples_per_chunk == 0
            or description_index == 0
        ):
            raise MediaScrubError("mp4 stsc entries are invalid")
        if description_index > len(sample_descriptions):
            raise MediaScrubError(
                "mp4 stsc description_index is outside stsd entries"
            )
        stsc_entries.append((first_chunk, samples_per_chunk, description_index))
        previous_first_chunk = first_chunk
        offset += 12

    offset_body = fullbox_body(offset_atom, offset_atom.type.decode("ascii"))
    chunk_count = struct.unpack(">I", offset_body[4:8])[0]
    if chunk_count > _MP4_MAX_CHUNKS:
        raise MediaScrubError(
            f"mp4 chunk count {chunk_count} exceeds scrubber limit {_MP4_MAX_CHUNKS}"
        )
    offset_width = 4 if offset_atom.type == b"stco" else 8
    if len(offset_body) != 8 + chunk_count * offset_width:
        raise MediaScrubError("mp4 chunk offset table length does not match entries")
    stsz_body = fullbox_body(tables[b"stsz"], "stsz")
    if len(stsz_body) < 12:
        raise MediaScrubError("mp4 stsz body too short")
    sample_size, sample_count = struct.unpack(">II", stsz_body[4:12])
    if sample_count > _MP4_MAX_SAMPLES:
        raise MediaScrubError(
            f"mp4 stsz sample_count {sample_count} exceeds scrubber limit"
        )
    if sample_count > len(data):
        raise MediaScrubError(
            f"mp4 stsz sample_count {sample_count} exceeds input-size bound {len(data)}"
        )
    if sample_size:
        if len(stsz_body) != 12:
            raise MediaScrubError("mp4 uniform stsz body has trailing bytes")
        sample_sizes: memoryview | None = None
    else:
        if len(stsz_body) != 12 + sample_count * 4:
            raise MediaScrubError("mp4 stsz sample-size table length does not match entries")
        sample_sizes = memoryview(stsz_body)

    def stsc_for_chunk(chunk_number: int, cursor: int) -> tuple[int, int, int, int]:
        while (
            cursor + 1 < len(stsc_entries)
            and stsc_entries[cursor + 1][0] <= chunk_number
        ):
            cursor += 1
        first_chunk, samples_per_chunk, description_index = stsc_entries[cursor]
        if first_chunk > chunk_number:
            raise MediaScrubError("mp4 stsc does not describe every chunk")
        return cursor, samples_per_chunk, description_index, first_chunk

    def size_at(sample_index: int) -> int:
        if sample_size:
            return sample_size
        assert sample_sizes is not None
        size_offset = 12 + sample_index * 4
        return struct.unpack(">I", sample_sizes[size_offset:size_offset + 4])[0]

    # Validate every complete chunk before yielding one sample range. This
    # rejects forged sample counts before range expansion and checks compact
    # chunk ownership before any per-sample work.
    sample_index = 0
    stsc_cursor = 0
    chunks_by_mdat: dict[int, list[tuple[
        int, int, int, int, int,
        tuple[int, dict[int, tuple[int, int]], bool] | None, int,
    ]]] = {}
    for chunk_number in range(1, chunk_count + 1):
        stsc_cursor, samples_per_chunk, description_index, _ = stsc_for_chunk(
            chunk_number, stsc_cursor,
        )
        if sample_index + samples_per_chunk > sample_count:
            raise MediaScrubError("mp4 stsc describes more samples than stsz")
        chunk_start = int.from_bytes(
            offset_body[8 + (chunk_number - 1) * offset_width:
                        8 + chunk_number * offset_width],
            "big",
        )
        if sample_size:
            chunk_end = chunk_start + samples_per_chunk * sample_size
        else:
            chunk_end = chunk_start
            for index in range(samples_per_chunk):
                sample_size_at = size_at(sample_index + index)
                if sample_size_at == 0:
                    raise MediaScrubError("mp4 sample size must be positive")
                chunk_end += sample_size_at
        if chunk_start < 0 or chunk_end < chunk_start or chunk_end > len(data):
            raise MediaScrubError(
                "mp4 chunk extent exceeds input size; sample range is not contained"
            )
        mdat_index = bisect_right(mdat_starts, chunk_start) - 1
        if (
            mdat_index < 0
            or chunk_start < mdat_ranges[mdat_index][0]
            or chunk_end > mdat_ranges[mdat_index][1]
        ):
            raise MediaScrubError("mp4 chunk extent is not contained by one mdat box")
        chunk = (
            chunk_start, chunk_end, sample_index, samples_per_chunk,
            description_index, sample_descriptions[description_index - 1], mdat_index,
        )
        chunks_by_mdat.setdefault(mdat_index, []).append(chunk)
        sample_index += samples_per_chunk
    if sample_index != sample_count:
        raise MediaScrubError("mp4 stsc does not describe every stsz sample")

    return _Mp4TrackSamplePlan(
        {index: tuple(group) for index, group in chunks_by_mdat.items()},
        sample_size,
        sample_sizes,
        sample_count,
    )


def _iter_sample_plan_ranges(
    plan: tuple[_Mp4TrackSamplePlan, ...],
    mdat_index: int,
) -> Iterator[_Mp4SampleRange]:
    for track in plan:
        chunks = track.chunks_by_mdat.get(mdat_index, ())
        for (
            chunk_start, _chunk_end, sample_index, samples_per_chunk,
            description_index, codec_config, _chunk_mdat_index,
        ) in chunks:
            sample_start = chunk_start
            for local_index in range(samples_per_chunk):
                if track.sample_size:
                    size = track.sample_size
                else:
                    assert track.sample_sizes is not None
                    size_offset = 12 + (sample_index + local_index) * 4
                    size = struct.unpack(">I", track.sample_sizes[size_offset:size_offset + 4])[0]
                sample_end = sample_start + size
                yield _Mp4SampleRange(
                    sample_start, sample_end, description_index, codec_config,
                )
                sample_start = sample_end
