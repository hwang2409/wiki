"""MP4 (ISO Base Media File Format) scrubbing — strict-subset reconstruction.

Round 6 completes the reconstruction principle at every nesting level:
every byte in the stored output is either (a) a struct.pack of a
validated field the parser understood, or (b) a `free` box body filled
with zeros. Copying input bytes wholesale is not allowed at any level.
Containers whose children cannot all be rebuilt from validated fields
cause the WHOLE FILE to be rejected — that is the strict-subset
acceptance fallback the reviewer named.

Chain:
    ftyp                            copied field-by-field (major brand,
                                    version, compatible brands validated
                                    as 4-byte tokens; length matches)
    moov -> mvhd + trak+ [+ mvex]
      mvhd                          fields validated + re-packed
      trak                          tkhd + edts? + mdia rebuilt
        tkhd                        fields validated + re-packed
        mdia -> mdhd + hdlr + minf  all rebuilt
          minf -> vmhd/smhd/... +
                  dinf + stbl
            dinf                    canonical rebuild (dref/url,
                                    self-referencing). Any non-canonical
                                    shape → file rejected.
                                    stbl -> stsd + boxes    stsd rebuilt from parsed sample
                                    entries (avc1/hev1); other stbl
                                    boxes header-validated but their
                                    bodies are BOUNDED by the parsed
                                    size (fully parsing every codec
                                    config table is out of scope; we
                                    strict-allowlist the box types).
    mdat                            sample data — required, non-empty
    moof/sidx/styp/mfra             emitted as parsed (playback aux)
    skip / udta / meta / uuid /
    anything unknown at top level   → `free` box (bytes zeroed)

`skip` is deliberately NOT in the top-level allowlist: the reviewer
found `skip` content survived in-place. Under reconstruction, skip
becomes a same-size `free` with zeroed body.

For the rebuilt moov to keep mdat's sample offsets valid without
having to rewrite stco/co64, the shrinkage of moov (from dropped
metadata + skinnier sample entries) is filled with a `free` child of
the exact delta. mdat sits at the same absolute offset as before.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

from .base import MediaScrubError, MediaScrubResult
from ._h264 import canonicalise_nal_with_ids, parse_slice_pps_id


@dataclass(frozen=True)
class _Mp4Atom:
    start: int
    size: int
    header_len: int
    type: bytes
    body_start: int
    body_end: int


@dataclass(frozen=True)
class _Mp4SampleRange:
    start: int
    end: int
    description_index: int
    avc_config: tuple[int, set[int], bool] | None


_MP4_FREE_MIN_SIZE: Final = 8

_MP4_TOPLEVEL_PLAYBACK: Final = {
    b"moov", b"mdat", b"moof", b"mfra",
}
# Sample-entry types we know how to rebuild field-by-field. Unknown types
# fall to strict-subset reject.
# Only avc1 has a complete field-level codec and sample scrubber. avc3 has
# in-band parameter sets, and the other codecs need separate parsers.
_MP4_VISUAL_ENTRIES: Final = {b"avc1"}
# AAC `mp4a` entries require an `esds` parser, which is outside this strict
# reconstruction scope. Reject them instead of accepting an opaque config.
_MP4_AUDIO_ENTRIES: Final = set()
# Inner boxes inside a sample entry that we accept. Each one gets its
# Every sample-entry inner box in this allowlist has a field-level
# rebuild via struct.pack — no raw body copy path remains for anything
# the scrubber claims to support (round-8 review). Codec configurations
# for containers we do not fully field-decode yet (hvcC/vpcC/av1C/esds
# and the encryption tree sinf/schm/schi/tenc) are OUT — files using
# them are rejected under strict-subset acceptance.
_MP4_SAMPLE_ENTRY_INNER_ALLOWED: Final = {
    b"avcC", b"btrt", b"pasp", b"colr",
}
_MP4_SAMPLE_ENTRY_REQUIRED_CONFIG: Final = {
    b"avc1": b"avcC",
}
_MP4_MAX_SAMPLES: Final = 16_777_216
# Additional stbl children beyond stsd. Every allowed type below has a
# field-level rebuild via struct.pack that emits exactly the parsed
# entry_count worth of entries — trailing bytes cannot survive because
# they are not written. Rare stbl types (stsh, sdtp, sbgp, sgpd, subs,
# saiz, saio, padb, stz2, cslg) are not in the allowlist; a file that
# uses one of those gets rejected by the walker at the stbl level via
# strict-subset acceptance.
_MP4_STBL_TABLE_TYPES: Final = {
    b"stts", b"ctts", b"stsc", b"stsz", b"stco", b"co64", b"stss",
}


def scrub_mp4(data: bytes) -> MediaScrubResult:
    if len(data) < 16:
        raise MediaScrubError("mp4 payload too small")

    top_atoms = _parse_container(data, 0, len(data))
    if not top_atoms or top_atoms[0].type != b"ftyp":
        raise MediaScrubError("mp4 payload missing ftyp box at offset 0")
    if top_atoms[0].size < 16:
        raise MediaScrubError("mp4 ftyp too small")

    # Run the structural rebuild first. This preserves the parser's precise
    # errors for malformed moov children before sample ownership checks.
    for atom in top_atoms:
        if atom.type == b"moov":
            _rebuilt, _trak, _mvhd, stsd_ok = _rebuild_moov(
                data, atom.body_start, atom.body_end,
            )
            if not _trak:
                raise MediaScrubError("mp4 moov missing trak")
            if not stsd_ok:
                raise MediaScrubError("mp4 stbl/stsd has no valid sample entry")
    for atom in top_atoms:
        if atom.type == b"mdat" and atom.body_start == atom.body_end:
            raise MediaScrubError("mp4 mdat body is empty")

    sample_ranges = _collect_sample_ranges(data, top_atoms)
    mdat_ranges = [
        (atom.body_start, atom.body_end)
        for atom in top_atoms if atom.type == b"mdat"
    ]
    _validate_sample_ranges(data, sample_ranges, mdat_ranges)

    ftyp = _rebuild_ftyp(data, top_atoms[0])
    if len(ftyp) != top_atoms[0].size:
        raise MediaScrubError("mp4 ftyp rebuild size mismatch")

    out_parts: list[bytes] = [ftyp]
    moov_seen = False
    trak_seen = False
    mvhd_seen = False
    stsd_ok = False
    mdat_non_empty = False
    duration_ms: int | None = None
    dims: tuple[int, int] | None = None

    for atom in top_atoms[1:]:
        if atom.type == b"ftyp":
            raise MediaScrubError("mp4 duplicate ftyp box")
        if atom.type == b"moov":
            if moov_seen:
                raise MediaScrubError("mp4 duplicate moov box")
            moov_seen = True
            moov_body = data[atom.body_start:atom.body_end]
            duration_ms = _mvhd_duration(moov_body)
            dims = _tkhd_dims_from_moov(moov_body)
            rebuilt_body, tr, mv, st = _rebuild_moov(data, atom.body_start, atom.body_end)
            trak_seen |= tr
            mvhd_seen |= mv
            stsd_ok |= st
            rebuilt = _pack(b"moov", rebuilt_body)
            delta = atom.size - len(rebuilt)
            if delta < 0:
                raise MediaScrubError("mp4 rebuilt moov exceeds original size")
            if delta > 0:
                padded_body = rebuilt_body + _free(delta)
                rebuilt = _pack(b"moov", padded_body)
            if len(rebuilt) != atom.size:
                raise MediaScrubError("mp4 rebuilt moov size mismatch after padding")
            out_parts.append(rebuilt)
        elif atom.type == b"mdat":
            body_len = atom.body_end - atom.body_start
            if body_len == 0:
                raise MediaScrubError("mp4 mdat body is empty")
            mdat_non_empty = True
            scrubbed_body = bytearray(body_len)
            for sample_range in sample_ranges:
                sample_start = sample_range.start
                sample_end = sample_range.end
                if sample_start < atom.body_start or sample_end > atom.body_end:
                    continue
                start = sample_start - atom.body_start
                end = sample_end - atom.body_start
                sample = data[sample_start:sample_end]
                if sample_range.avc_config is not None:
                    sample = _canonicalise_avc_sample(
                        sample, *sample_range.avc_config,
                    )
                scrubbed_body[start:end] = sample
            out_parts.append(data[atom.start:atom.body_start] + scrubbed_body)
        elif atom.type == b"sidx":
            # Round-7 review: sidx body was copied through opaquely. Now
            # rebuilt from parsed uint fields. If the rebuilt size differs
            # from the original, pad with a `free` block to preserve
            # top-level layout (mdat offsets pointing past sidx must stay
            # correct).
            rebuilt_sidx = _rebuild_sidx(data, atom)
            delta = atom.size - len(rebuilt_sidx)
            if delta < 0:
                raise MediaScrubError("mp4 rebuilt sidx exceeds original size")
            out_parts.append(rebuilt_sidx)
            if delta > 0:
                out_parts.append(_free(delta))
        elif atom.type == b"styp":
            # styp mirrors ftyp: major_brand + minor_version + N compatible
            # brand tokens. Rebuild via struct.pack so trailing junk cannot
            # survive.
            rebuilt_styp = _rebuild_ftyp_like(b"styp", data, atom)
            if len(rebuilt_styp) != atom.size:
                raise MediaScrubError("mp4 rebuilt styp size mismatch")
            out_parts.append(rebuilt_styp)
        elif atom.type in _MP4_TOPLEVEL_PLAYBACK:
            # moof / mfra / moov (moov is handled above). moof + mfra are
            # fragment boxes carrying byte-position tables that would need
            # dedicated parsers to rebuild safely; wiki artifact fixtures
            # are not fragmented in practice. If we hit one, reject rather
            # than copy through — R7 strict-subset acceptance.
            if atom.type in (b"moof", b"mfra"):
                raise MediaScrubError(
                    f"mp4 fragmented playback box {atom.type!r} not supported by scrubber"
                )
            out_parts.append(data[atom.start:atom.body_end])
        else:
            # skip, udta, meta, uuid, and any unknown top-level atom are
            # replaced by a same-size `free`. Round-6 review flagged that
            # `skip` content survived when we allowlisted it as playback;
            # `skip` is now handled here (bytes zeroed) like every other
            # non-playback top-level atom.
            out_parts.append(_free(atom.size))

    if not moov_seen:
        raise MediaScrubError("mp4 payload missing moov box")
    if not mvhd_seen:
        raise MediaScrubError("mp4 moov missing mvhd box")
    if not trak_seen:
        raise MediaScrubError("mp4 moov missing trak box")
    if not stsd_ok:
        raise MediaScrubError(
            "mp4 trak missing valid stbl/stsd sample entry (full chain required)"
        )
    if not mdat_non_empty:
        raise MediaScrubError("mp4 payload missing non-empty mdat box")

    width, height = dims if dims is not None else (None, None)
    return MediaScrubResult(
        data=b"".join(out_parts),
        mime="video/mp4",
        duration_ms=duration_ms,
        width=width,
        height=height,
    )


def _collect_sample_ranges(
    data: bytes, top_atoms: list[_Mp4Atom],
) -> list[_Mp4SampleRange]:
    """Return absolute byte ranges owned by all non-fragmented samples."""
    ranges: list[_Mp4SampleRange] = []
    for moov in (atom for atom in top_atoms if atom.type == b"moov"):
        for trak in _parse_container(data, moov.body_start, moov.body_end):
            if trak.type != b"trak":
                continue
            for mdia in _parse_container(data, trak.body_start, trak.body_end):
                if mdia.type != b"mdia":
                    continue
                for minf in _parse_container(data, mdia.body_start, mdia.body_end):
                    if minf.type != b"minf":
                        continue
                    for stbl in _parse_container(data, minf.body_start, minf.body_end):
                        if stbl.type == b"stbl":
                            ranges.extend(
                                _collect_sample_ranges_from_stbl(
                                    data, stbl.body_start, stbl.body_end,
                                )
                            )
    return ranges


def _collect_sample_ranges_from_stbl(
    data: bytes, body_start: int, body_end: int,
) -> list[_Mp4SampleRange]:
    tables = {
        atom.type: atom
        for atom in _parse_container(data, body_start, body_end)
        if atom.type in {b"stsc", b"stsz", b"stco", b"co64"}
    }
    if not tables:
        return []
    if b"stsc" not in tables or b"stsz" not in tables:
        raise MediaScrubError("mp4 sample tables missing stsc or stsz")
    if b"stco" in tables and b"co64" in tables:
        raise MediaScrubError("mp4 sample tables carry both stco and co64")
    offset_atom = tables.get(b"stco") or tables.get(b"co64")
    if offset_atom is None:
        raise MediaScrubError("mp4 sample tables missing stco or co64")

    sample_descriptions = _sample_description_configs(data, body_start, body_end)
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
    offset_width = 4 if offset_atom.type == b"stco" else 8
    if len(offset_body) != 8 + chunk_count * offset_width:
        raise MediaScrubError("mp4 chunk offset table length does not match entries")
    chunk_offsets = [
        int.from_bytes(
            offset_body[8 + index * offset_width:8 + (index + 1) * offset_width],
            "big",
        )
        for index in range(chunk_count)
    ]

    stsz_body = fullbox_body(tables[b"stsz"], "stsz")
    if len(stsz_body) < 12:
        raise MediaScrubError("mp4 stsz body too short")
    sample_size, sample_count = struct.unpack(">II", stsz_body[4:12])
    if sample_count > _MP4_MAX_SAMPLES:
        raise MediaScrubError(
            f"mp4 stsz sample_count {sample_count} exceeds scrubber limit"
        )
    if sample_size:
        if len(stsz_body) != 12:
            raise MediaScrubError("mp4 uniform stsz body has trailing bytes")
        sample_sizes: list[int] | None = None
    else:
        if len(stsz_body) != 12 + sample_count * 4:
            raise MediaScrubError("mp4 stsz sample-size table length does not match entries")
        sample_sizes = None

    ranges: list[_Mp4SampleRange] = []
    sample_index = 0
    for chunk_number, chunk_start in enumerate(chunk_offsets, start=1):
        matching = [
            entry for entry in stsc_entries if entry[0] <= chunk_number
        ]
        if not matching:
            raise MediaScrubError("mp4 stsc does not describe every chunk")
        _first_chunk, samples_per_chunk, description_index = matching[-1]
        avc_config = sample_descriptions[description_index - 1]
        if sample_index + samples_per_chunk > sample_count:
            raise MediaScrubError("mp4 stsc describes more samples than stsz")
        sample_start = chunk_start
        for local_index in range(samples_per_chunk):
            if sample_size:
                sample_size_value = sample_size
            else:
                size_offset = 12 + (sample_index + local_index) * 4
                sample_size_value = struct.unpack(
                    ">I", stsz_body[size_offset:size_offset + 4],
                )[0]
            sample_end = sample_start + sample_size_value
            if sample_end < sample_start:
                raise MediaScrubError("mp4 sample range arithmetic overflow")
            ranges.append(
                _Mp4SampleRange(
                    sample_start, sample_end, description_index, avc_config,
                )
            )
            sample_start = sample_end
        sample_index += samples_per_chunk
    if sample_index != sample_count:
        raise MediaScrubError("mp4 stsc does not describe every stsz sample")
    return ranges


def _sample_description_configs(
    data: bytes, body_start: int, body_end: int,
) -> list[tuple[int, set[int], bool] | None]:
    """Return one codec configuration per stsd sample-description index."""
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
    entries: list[tuple[int, set[int], bool] | None] = []
    offset = 8
    for _ in range(entry_count):
        if offset + 8 > len(body):
            raise MediaScrubError("mp4 stsd entry header truncated")
        entry_size = struct.unpack(">I", body[offset:offset + 4])[0]
        entry_type = body[offset + 4:offset + 8]
        if entry_size < 8 or offset + entry_size > len(body):
            raise MediaScrubError("mp4 stsd entry size out of bounds")
        if entry_type != b"avc1":
            raise MediaScrubError(
                f"mp4 sample entry type {entry_type!r} is outside scrubber scope"
            )
        entry = body[offset:offset + entry_size]
        inner_start = 16 + 70
        inner_offset = inner_start
        avcc_body: bytes | None = None
        while inner_offset < len(entry):
            if inner_offset + 8 > len(entry):
                raise MediaScrubError("mp4 avc sample inner header truncated")
            box_size = struct.unpack(">I", entry[inner_offset:inner_offset + 4])[0]
            box_type = entry[inner_offset + 4:inner_offset + 8]
            if box_size < 8 or inner_offset + box_size > len(entry):
                raise MediaScrubError("mp4 avc sample inner box out of bounds")
            if box_type == b"avcC":
                if avcc_body is not None:
                    raise MediaScrubError("mp4 avc1 sample entry has duplicate avcC")
                avcc_body = entry[inner_offset + 8:inner_offset + box_size]
            inner_offset += box_size
        if avcc_body is None:
            raise MediaScrubError("mp4 avc1 sample entry requires exactly one avcC")
        entries.append(_parse_avc_sample_config(avcc_body, True))
        offset += entry_size
    return entries


def _parse_avc_sample_config(
    body: bytes, require_parameter_sets: bool,
) -> tuple[int, set[int], bool]:
    if len(body) < 7 or body[0] != 1:
        raise MediaScrubError("mp4 avcC sample configuration header is invalid")
    length_size_minus_one = body[4] & 0x03
    if length_size_minus_one not in (0, 1, 3):
        raise MediaScrubError("mp4 avcC lengthSizeMinusOne must be 0, 1, or 3")
    num_sps = body[5] & 0x1F
    offset = 6
    sps_ids: set[int] = set()
    for _ in range(num_sps):
        if offset + 2 > len(body):
            raise MediaScrubError("mp4 avcC SPS length field truncated")
        size = struct.unpack(">H", body[offset:offset + 2])[0]
        offset += 2
        if offset + size > len(body):
            raise MediaScrubError("mp4 avcC SPS extends past body")
        _canonical, sps_id, _ = canonicalise_nal_with_ids(body[offset:offset + size], expected_nal_type=7)
        sps_ids.add(sps_id)
        offset += size
    if offset >= len(body):
        raise MediaScrubError("mp4 avcC missing PPS count")
    num_pps = body[offset]
    offset += 1
    pps_ids: set[int] = set()
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
        if pps_sps_id not in sps_ids:
            raise MediaScrubError("mp4 avcC PPS references an SPS identifier absent from avcC")
        pps_ids.add(pps_id)
        offset += size
    if require_parameter_sets and (not sps_ids or not pps_ids):
        raise MediaScrubError("mp4 avc1 avcC requires SPS and PPS records")
    return (length_size_minus_one + 1, pps_ids, require_parameter_sets)


def _canonicalise_avc_sample(
    sample: bytes, length_size: int, pps_ids: set[int], require_pps: bool,
) -> bytes:
    if not sample:
        return b""
    output = bytearray()
    offset = 0
    nal_count = 0
    while offset < len(sample):
        if offset + length_size > len(sample):
            raise MediaScrubError("mp4 AVC sample NAL length is truncated")
        nal_size = int.from_bytes(sample[offset:offset + length_size], "big")
        offset += length_size
        if nal_size == 0 or offset + nal_size > len(sample):
            raise MediaScrubError("mp4 AVC sample NAL length is out of bounds")
        nal = sample[offset:offset + nal_size]
        nal_type = nal[0] & 0x1F
        if nal_type in (1, 5):
            pps_id = parse_slice_pps_id(nal)
            if require_pps and pps_id not in pps_ids:
                raise MediaScrubError("mp4 AVC slice references an unknown PPS identifier")
        if nal_type == 6:
            # Keep the declared NAL length and replace the SEI RBSP with an
            # empty, canonical payload. This removes user-data metadata.
            nal = nal[:1] + (b"\x80" + b"\x00" * (len(nal) - 2) if len(nal) >= 2 else b"")
        output.extend(nal_size.to_bytes(length_size, "big"))
        output.extend(nal)
        offset += nal_size
        nal_count += 1
    if nal_count == 0:
        raise MediaScrubError("mp4 AVC sample has no NAL units")
    return bytes(output)


def _validate_sample_ranges(
    data: bytes, sample_ranges: list[_Mp4SampleRange],
    mdat_ranges: list[tuple[int, int]],
) -> None:
    for sample_range in sample_ranges:
        start = sample_range.start
        end = sample_range.end
        if (
            start < 0 or end < start or end > len(data)
            or not any(start >= mdat_start and end <= mdat_end for mdat_start, mdat_end in mdat_ranges)
        ):
            raise MediaScrubError(
                "mp4 sample range is not contained by one mdat box"
            )
        if sample_range.avc_config is not None:
            _canonicalise_avc_sample(
                data[start:end], *sample_range.avc_config,
            )


# ---------------------------------------------------------------------------
# Header + emit primitives
# ---------------------------------------------------------------------------

def _read_header(view: memoryview, offset: int, end: int) -> tuple[int, bytes, int, int]:
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


def _parse_container(data: bytes, offset: int, end: int) -> list[_Mp4Atom]:
    view = memoryview(data)
    atoms: list[_Mp4Atom] = []
    while offset < end:
        size, atom_type, header_len, atom_end = _read_header(view, offset, end)
        atoms.append(
            _Mp4Atom(
                start=offset,
                size=size,
                header_len=header_len,
                type=atom_type,
                body_start=offset + header_len,
                body_end=atom_end,
            )
        )
        offset = atom_end
    return atoms


def _pack(atom_type: bytes, body: bytes) -> bytes:
    total = 8 + len(body)
    if total > 0xFFFFFFFF:  # pragma: no cover
        raise MediaScrubError("mp4 rebuilt atom size overflows 32 bits")
    return struct.pack(">I", total) + atom_type + body


def _free(total_size: int) -> bytes:
    """Emit a `free` box that occupies exactly `total_size` bytes, body zeroed."""
    if total_size < _MP4_FREE_MIN_SIZE:
        raise MediaScrubError(
            f"mp4 free padding requires ≥{_MP4_FREE_MIN_SIZE} bytes, got {total_size}"
        )
    return struct.pack(">I", total_size) + b"free" + b"\x00" * (total_size - _MP4_FREE_MIN_SIZE)


# ---------------------------------------------------------------------------
# ftyp — rebuild from parsed 4-byte tokens (major_brand, minor_version, then
# every 4-byte compatible_brand). Anything past those brands is rejected.
# ---------------------------------------------------------------------------

def _rebuild_ftyp(data: bytes, atom: _Mp4Atom) -> bytes:
    return _rebuild_ftyp_like(b"ftyp", data, atom)


def _rebuild_ftyp_like(atom_type: bytes, data: bytes, atom: _Mp4Atom) -> bytes:
    """Rebuild an ftyp-shaped box (ftyp or styp) from parsed 4-byte tokens."""
    body = data[atom.body_start:atom.body_end]
    if len(body) < 8:
        raise MediaScrubError(f"mp4 {atom_type!r} body too short")
    major_brand = body[:4]
    minor_version = struct.unpack(">I", body[4:8])[0]
    tail = body[8:]
    if len(tail) % 4 != 0:
        raise MediaScrubError(
            f"mp4 {atom_type!r} compatible_brands not a multiple of 4 bytes"
        )
    compat = [tail[i:i + 4] for i in range(0, len(tail), 4)]
    rebuilt_body = major_brand + struct.pack(">I", minor_version) + b"".join(compat)
    return _pack(atom_type, rebuilt_body)


def _rebuild_sidx(data: bytes, atom: _Mp4Atom) -> bytes:
    """Segment Index Box (sidx) — rebuild from parsed fields only.

    Layout per ISO/IEC 14496-12:
      full box header (v+flags 4 bytes)
      reference_ID (uint32)
      timescale (uint32)
      earliest_presentation_time (v0: uint32, v1: uint64)
      first_offset (v0: uint32, v1: uint64)
      reserved (uint16 = 0)
      reference_count (uint16)
      per-reference (12 bytes):
        [reference_type(1 bit) + referenced_size(31 bits)] uint32
        subsegment_duration uint32
        [starts_with_SAP(1) + SAP_type(3) + SAP_delta_time(28)] uint32
    """
    body = data[atom.body_start:atom.body_end]
    if len(body) < 4:
        raise MediaScrubError("mp4 sidx body too short for full-box header")
    version = body[0]
    flags = _validate_fullbox_flags(b"sidx", body[1:4])
    if version == 0:
        fixed_len = 4 + 4 + 4 + 4 + 4 + 2 + 2
    elif version == 1:
        fixed_len = 4 + 4 + 4 + 8 + 8 + 2 + 2
    else:
        raise MediaScrubError(f"mp4 sidx unknown version {version}")
    if len(body) < fixed_len:
        raise MediaScrubError("mp4 sidx body shorter than fixed header")
    reference_id = struct.unpack(">I", body[4:8])[0]
    timescale = struct.unpack(">I", body[8:12])[0]
    if version == 0:
        earliest_pt = struct.unpack(">I", body[12:16])[0]
        first_offset = struct.unpack(">I", body[16:20])[0]
        # reserved at 20:22, reference_count at 22:24
        reference_count = struct.unpack(">H", body[22:24])[0]
    else:
        earliest_pt = struct.unpack(">Q", body[12:20])[0]
        first_offset = struct.unpack(">Q", body[20:28])[0]
        reference_count = struct.unpack(">H", body[30:32])[0]
    if reference_count > 0xFFFF:  # pragma: no cover — uint16 max already
        raise MediaScrubError("mp4 sidx reference count implausible")
    references_start = fixed_len
    expected_len = fixed_len + reference_count * 12
    if len(body) < expected_len:
        raise MediaScrubError("mp4 sidx references extend past body")
    if len(body) > expected_len:
        raise MediaScrubError(
            f"mp4 sidx body has {len(body) - expected_len} trailing bytes past declared references"
        )
    references: list[bytes] = []
    offset = references_start
    for _ in range(reference_count):
        rt_size = struct.unpack(">I", body[offset:offset + 4])[0]
        sub_duration = struct.unpack(">I", body[offset + 4:offset + 8])[0]
        sap = struct.unpack(">I", body[offset + 8:offset + 12])[0]
        references.append(struct.pack(">III", rt_size, sub_duration, sap))
        offset += 12
    if version == 0:
        header_bytes = (
            bytes([0]) + flags
            + struct.pack(">II", reference_id, timescale)
            + struct.pack(">II", earliest_pt, first_offset)
            + struct.pack(">HH", 0, reference_count)
        )
    else:
        header_bytes = (
            bytes([1]) + flags
            + struct.pack(">II", reference_id, timescale)
            + struct.pack(">QQ", earliest_pt, first_offset)
            + struct.pack(">HH", 0, reference_count)
        )
    return _pack(b"sidx", header_bytes + b"".join(references))


# ---------------------------------------------------------------------------
# moov + inner rewrites
# ---------------------------------------------------------------------------

def _rebuild_moov(
    data: bytes, body_start: int, body_end: int,
) -> tuple[bytes, bool, bool, bool]:
    atoms = _parse_container(data, body_start, body_end)
    parts: list[bytes] = []
    trak_seen = False
    mvhd_seen = False
    stsd_ok = False
    for atom in atoms:
        if atom.type == b"mvhd":
            mvhd_seen = True
            parts.append(_rebuild_mvhd(data, atom))
        elif atom.type == b"trak":
            trak_body, stsd_in_trak = _rebuild_trak(data, atom.body_start, atom.body_end)
            trak_seen = True
            stsd_ok |= stsd_in_trak
            parts.append(_pack(b"trak", trak_body))
        elif atom.type == b"mvex":
            # mvex indicates a fragmented movie. The scrubber does not
            # yet rebuild fragment defaults field-by-field, and copying
            # the body would reintroduce unparsed bytes. Strict-subset
            # acceptance: reject fragmented MP4s (same policy as moof at
            # the top level).
            raise MediaScrubError(
                "mp4 mvex box present — fragmented playback not supported by scrubber"
            )
        # Everything else in moov (udta, meta, uuid, iods, hoisted anything) → drop
    return b"".join(parts), trak_seen, mvhd_seen, stsd_ok


def _rebuild_mvhd(data: bytes, atom: _Mp4Atom) -> bytes:
    body = data[atom.body_start:atom.body_end]
    if len(body) < 1:
        raise MediaScrubError("mp4 mvhd empty")
    version = body[0]
    if version == 0:
        if len(body) != 100:
            raise MediaScrubError(
                f"mp4 mvhd v0 body length {len(body)} not the 100-byte spec size"
            )
        flags = _validate_fullbox_flags(b"mvhd", body[1:4])
        creation = struct.unpack(">I", body[4:8])[0]
        modification = struct.unpack(">I", body[8:12])[0]
        timescale = struct.unpack(">I", body[12:16])[0]
        duration = struct.unpack(">I", body[16:20])[0]
        rate = struct.unpack(">I", body[20:24])[0]
        volume = struct.unpack(">H", body[24:26])[0]
        matrix = body[36:72]
        next_track_id = struct.unpack(">I", body[96:100])[0]
        rebuilt = (
            bytes([0]) + flags
            + struct.pack(">II", creation, modification)
            + struct.pack(">II", timescale, duration)
            + struct.pack(">IH", rate, volume)
            + b"\x00" * 10
            + matrix
            + b"\x00" * 24
            + struct.pack(">I", next_track_id)
        )
        return _pack(b"mvhd", rebuilt)
    if version == 1:
        if len(body) != 112:
            raise MediaScrubError(
                f"mp4 mvhd v1 body length {len(body)} not the 112-byte spec size"
            )
        flags = _validate_fullbox_flags(b"mvhd", body[1:4])
        creation = struct.unpack(">Q", body[4:12])[0]
        modification = struct.unpack(">Q", body[12:20])[0]
        timescale = struct.unpack(">I", body[20:24])[0]
        duration = struct.unpack(">Q", body[24:32])[0]
        rate = struct.unpack(">I", body[32:36])[0]
        volume = struct.unpack(">H", body[36:38])[0]
        matrix = body[48:84]
        next_track_id = struct.unpack(">I", body[108:112])[0]
        rebuilt = (
            bytes([1]) + flags
            + struct.pack(">QQ", creation, modification)
            + struct.pack(">IQ", timescale, duration)
            + struct.pack(">IH", rate, volume)
            + b"\x00" * 10
            + matrix
            + b"\x00" * 24
            + struct.pack(">I", next_track_id)
        )
        return _pack(b"mvhd", rebuilt)
    raise MediaScrubError(f"mp4 mvhd unknown version {version}")


def _rebuild_trak(
    data: bytes, body_start: int, body_end: int,
) -> tuple[bytes, bool]:
    atoms = _parse_container(data, body_start, body_end)
    parts: list[bytes] = []
    stsd_ok = False
    for atom in atoms:
        if atom.type == b"tkhd":
            parts.append(_rebuild_tkhd(data, atom))
        elif atom.type == b"edts":
            parts.append(_rebuild_edts(data, atom))
        elif atom.type == b"mdia":
            mdia_body, stsd_in_mdia = _rebuild_mdia(data, atom.body_start, atom.body_end)
            parts.append(_pack(b"mdia", mdia_body))
            stsd_ok |= stsd_in_mdia
    return b"".join(parts), stsd_ok


def _rebuild_tkhd(data: bytes, atom: _Mp4Atom) -> bytes:
    body = data[atom.body_start:atom.body_end]
    if len(body) < 1:
        raise MediaScrubError("mp4 tkhd empty")
    version = body[0]
    if version == 0:
        if len(body) != 84:
            raise MediaScrubError(
                f"mp4 tkhd v0 body length {len(body)} not the 84-byte spec size"
            )
        flags = _validate_fullbox_flags(b"tkhd", body[1:4])
        creation = struct.unpack(">I", body[4:8])[0]
        modification = struct.unpack(">I", body[8:12])[0]
        track_id = struct.unpack(">I", body[12:16])[0]
        duration = struct.unpack(">I", body[20:24])[0]
        layer = struct.unpack(">H", body[32:34])[0]
        alt_group = struct.unpack(">H", body[34:36])[0]
        volume = struct.unpack(">H", body[36:38])[0]
        matrix = body[40:76]
        width = struct.unpack(">I", body[76:80])[0]
        height = struct.unpack(">I", body[80:84])[0]
        rebuilt = (
            bytes([0]) + flags
            + struct.pack(">III", creation, modification, track_id)
            + b"\x00" * 4
            + struct.pack(">I", duration)
            + b"\x00" * 8
            + struct.pack(">HHHH", layer, alt_group, volume, 0)
            + matrix
            + struct.pack(">II", width, height)
        )
        return _pack(b"tkhd", rebuilt)
    if version == 1:
        if len(body) != 96:
            raise MediaScrubError(
                f"mp4 tkhd v1 body length {len(body)} not the 96-byte spec size"
            )
        flags = _validate_fullbox_flags(b"tkhd", body[1:4])
        creation = struct.unpack(">Q", body[4:12])[0]
        modification = struct.unpack(">Q", body[12:20])[0]
        track_id = struct.unpack(">I", body[20:24])[0]
        duration = struct.unpack(">Q", body[28:36])[0]
        layer = struct.unpack(">H", body[44:46])[0]
        alt_group = struct.unpack(">H", body[46:48])[0]
        volume = struct.unpack(">H", body[48:50])[0]
        matrix = body[52:88]
        width = struct.unpack(">I", body[88:92])[0]
        height = struct.unpack(">I", body[92:96])[0]
        rebuilt = (
            bytes([1]) + flags
            + struct.pack(">QQ", creation, modification)
            + struct.pack(">I", track_id)
            + b"\x00" * 4
            + struct.pack(">Q", duration)
            + b"\x00" * 8
            + struct.pack(">HHHH", layer, alt_group, volume, 0)
            + matrix
            + struct.pack(">II", width, height)
        )
        return _pack(b"tkhd", rebuilt)
    raise MediaScrubError(f"mp4 tkhd unknown version {version}")


def _rebuild_mdia(
    data: bytes, body_start: int, body_end: int,
) -> tuple[bytes, bool]:
    atoms = _parse_container(data, body_start, body_end)
    parts: list[bytes] = []
    stsd_ok = False
    for atom in atoms:
        if atom.type == b"mdhd":
            parts.append(_rebuild_mdhd(data, atom))
        elif atom.type == b"hdlr":
            parts.append(_rebuild_hdlr(data, atom))
        elif atom.type == b"minf":
            minf_body, stsd_in_minf = _rebuild_minf(data, atom.body_start, atom.body_end)
            parts.append(_pack(b"minf", minf_body))
            stsd_ok |= stsd_in_minf
    return b"".join(parts), stsd_ok


def _rebuild_edts(data: bytes, atom: _Mp4Atom) -> bytes:
    # edts contains exactly one elst (Edit List Box). Rebuild elst from
    # parsed entries; reject anything else.
    children = _parse_container(data, atom.body_start, atom.body_end)
    if len(children) != 1 or children[0].type != b"elst":
        raise MediaScrubError("mp4 edts must contain exactly one elst child")
    elst = children[0]
    body = data[elst.body_start:elst.body_end]
    if len(body) < 8:
        raise MediaScrubError("mp4 elst body too short")
    version = body[0]
    flags = _validate_fullbox_flags(b"elst", body[1:4])
    entry_count = struct.unpack(">I", body[4:8])[0]
    if version == 0:
        entry_size = 12  # segment_duration(4) + media_time(4 signed) + rate(4 fixed)
    elif version == 1:
        entry_size = 20  # segment_duration(8) + media_time(8 signed) + rate(4 fixed)
    else:
        raise MediaScrubError(f"mp4 elst unknown version {version}")
    if entry_count > 0xFFFF:
        raise MediaScrubError("mp4 elst entry count implausible")
    expected = 8 + entry_count * entry_size
    if len(body) != expected:
        raise MediaScrubError(
            f"mp4 elst body length {len(body)} does not match "
            f"declared {entry_count} entries (expected {expected})"
        )
    entries: list[bytes] = []
    off = 8
    for _ in range(entry_count):
        if version == 0:
            seg_dur = struct.unpack(">I", body[off:off + 4])[0]
            media_time = struct.unpack(">i", body[off + 4:off + 8])[0]
            rate = struct.unpack(">I", body[off + 8:off + 12])[0]
            entries.append(struct.pack(">IiI", seg_dur, media_time, rate))
        else:
            seg_dur = struct.unpack(">Q", body[off:off + 8])[0]
            media_time = struct.unpack(">q", body[off + 8:off + 16])[0]
            rate = struct.unpack(">I", body[off + 16:off + 20])[0]
            entries.append(struct.pack(">QqI", seg_dur, media_time, rate))
        off += entry_size
    elst_body = (
        bytes([version]) + flags
        + struct.pack(">I", entry_count)
        + b"".join(entries)
    )
    return _pack(b"edts", _pack(b"elst", elst_body))


def _rebuild_mdhd(data: bytes, atom: _Mp4Atom) -> bytes:
    body = data[atom.body_start:atom.body_end]
    if len(body) < 1:
        raise MediaScrubError("mp4 mdhd empty")
    version = body[0]
    if version == 0:
        # v0 body: 4 v+flags + 4 creation + 4 modification + 4 timescale
        # + 4 duration + 2 language + 2 pre_defined = 24 bytes
        if len(body) != 24:
            raise MediaScrubError(
                f"mp4 mdhd v0 body length {len(body)} not the 24-byte spec size"
            )
        flags = _validate_fullbox_flags(b"mdhd", body[1:4])
        creation = struct.unpack(">I", body[4:8])[0]
        modification = struct.unpack(">I", body[8:12])[0]
        timescale = struct.unpack(">I", body[12:16])[0]
        duration = struct.unpack(">I", body[16:20])[0]
        language = struct.unpack(">H", body[20:22])[0]
        rebuilt = (
            bytes([0]) + flags
            + struct.pack(">IIII", creation, modification, timescale, duration)
            + struct.pack(">HH", language, 0)
        )
        return _pack(b"mdhd", rebuilt)
    if version == 1:
        # v1: creation/modification/duration widen to 8 bytes each = 36 bytes
        if len(body) != 36:
            raise MediaScrubError(
                f"mp4 mdhd v1 body length {len(body)} not the 36-byte spec size"
            )
        flags = _validate_fullbox_flags(b"mdhd", body[1:4])
        creation = struct.unpack(">Q", body[4:12])[0]
        modification = struct.unpack(">Q", body[12:20])[0]
        timescale = struct.unpack(">I", body[20:24])[0]
        duration = struct.unpack(">Q", body[24:32])[0]
        language = struct.unpack(">H", body[32:34])[0]
        rebuilt = (
            bytes([1]) + flags
            + struct.pack(">QQ", creation, modification)
            + struct.pack(">IQ", timescale, duration)
            + struct.pack(">HH", language, 0)
        )
        return _pack(b"mdhd", rebuilt)
    raise MediaScrubError(f"mp4 mdhd unknown version {version}")


def _rebuild_hdlr(data: bytes, atom: _Mp4Atom) -> bytes:
    body = data[atom.body_start:atom.body_end]
    # hdlr: v+flags(4) + pre_defined(4) + handler_type(4) + reserved(12) +
    # name (null-terminated UTF-8 string, may be empty). Fixed prefix
    # is 24 bytes; anything past that is the name.
    if len(body) < 24:
        raise MediaScrubError(
            f"mp4 hdlr body length {len(body)} shorter than 24-byte header"
        )
    version = body[0]
    if version != 0:
        raise MediaScrubError(f"mp4 hdlr unknown version {version}")
    flags = _validate_fullbox_flags(b"hdlr", body[1:4])
    handler_type = body[8:12]
    # Rebuild reserved fields as zeros; keep the handler_type token (real
    # value — decoders route on it) and emit an empty name so any
    # encoder-identity string ("VideoHandler" from ffmpeg, arbitrary from
    # a hostile fixture) cannot survive.
    rebuilt = (
        bytes([0]) + flags
        + b"\x00" * 4  # pre_defined
        + handler_type
        + b"\x00" * 12  # reserved
        + b"\x00"  # name = ""
    )
    return _pack(b"hdlr", rebuilt)


def _rebuild_minf(
    data: bytes, body_start: int, body_end: int,
) -> tuple[bytes, bool]:
    atoms = _parse_container(data, body_start, body_end)
    parts: list[bytes] = []
    stsd_ok = False
    for atom in atoms:
        if atom.type == b"vmhd":
            parts.append(_rebuild_vmhd(data, atom))
        elif atom.type == b"smhd":
            parts.append(_rebuild_smhd(data, atom))
        elif atom.type == b"nmhd":
            parts.append(_rebuild_nmhd(data, atom))
        elif atom.type == b"hmhd":
            parts.append(_rebuild_hmhd(data, atom))
        elif atom.type == b"dinf":
            # Canonical rebuild — the reviewer flagged that unknown
            # children inside dinf survived. We only accept the shape
            # ffmpeg + every mainstream muxer emits (dref containing a
            # single self-referencing url).
            parts.append(_rebuild_dinf(data, atom.body_start, atom.body_end))
        elif atom.type == b"stbl":
            stbl_body, stsd_in_stbl = _rebuild_stbl(data, atom.body_start, atom.body_end)
            parts.append(_pack(b"stbl", stbl_body))
            stsd_ok |= stsd_in_stbl
    return b"".join(parts), stsd_ok


def _rebuild_vmhd(data: bytes, atom: _Mp4Atom) -> bytes:
    # vmhd body: v+flags(4) + graphicsmode(2) + opcolor(6) = 12 bytes
    body = data[atom.body_start:atom.body_end]
    if len(body) != 12:
        raise MediaScrubError(
            f"mp4 vmhd body length {len(body)} not the 12-byte spec size"
        )
    if body[0] != 0:
        raise MediaScrubError(f"mp4 vmhd unknown version {body[0]}")
    flags = _validate_fullbox_flags(b"vmhd", body[1:4])
    if flags != b"\x00\x00\x01":
        raise MediaScrubError(
            "mp4 vmhd fullbox flags must be exactly 0x000001"
        )
    graphics_mode = struct.unpack(">H", body[4:6])[0]
    opcolor = struct.unpack(">HHH", body[6:12])
    rebuilt = (
        bytes([0]) + flags
        + struct.pack(">H", graphics_mode)
        + struct.pack(">HHH", *opcolor)
    )
    return _pack(b"vmhd", rebuilt)


def _rebuild_smhd(data: bytes, atom: _Mp4Atom) -> bytes:
    # smhd body: v+flags(4) + balance(2) + reserved(2) = 8 bytes
    body = data[atom.body_start:atom.body_end]
    if len(body) != 8:
        raise MediaScrubError(
            f"mp4 smhd body length {len(body)} not the 8-byte spec size"
        )
    if body[0] != 0:
        raise MediaScrubError(f"mp4 smhd unknown version {body[0]}")
    flags = _validate_fullbox_flags(b"smhd", body[1:4])
    balance = struct.unpack(">h", body[4:6])[0]
    rebuilt = bytes([0]) + flags + struct.pack(">h", balance) + b"\x00\x00"
    return _pack(b"smhd", rebuilt)


def _rebuild_nmhd(data: bytes, atom: _Mp4Atom) -> bytes:
    # nmhd body: v+flags(4). Fixed 4 bytes.
    body = data[atom.body_start:atom.body_end]
    if len(body) != 4:
        raise MediaScrubError(
            f"mp4 nmhd body length {len(body)} not the 4-byte spec size"
        )
    if body[0] != 0:
        raise MediaScrubError(f"mp4 nmhd unknown version {body[0]}")
    flags = _validate_fullbox_flags(b"nmhd", body[1:4])
    return _pack(b"nmhd", bytes([0]) + flags)


def _rebuild_hmhd(data: bytes, atom: _Mp4Atom) -> bytes:
    # hmhd body: v+flags(4) + maxPDUsize(2) + avgPDUsize(2) + maxbitrate(4)
    # + avgbitrate(4) + reserved(4) = 20 bytes
    body = data[atom.body_start:atom.body_end]
    if len(body) != 20:
        raise MediaScrubError(
            f"mp4 hmhd body length {len(body)} not the 20-byte spec size"
        )
    if body[0] != 0:
        raise MediaScrubError(f"mp4 hmhd unknown version {body[0]}")
    flags = _validate_fullbox_flags(b"hmhd", body[1:4])
    max_pdu = struct.unpack(">H", body[4:6])[0]
    avg_pdu = struct.unpack(">H", body[6:8])[0]
    max_bitrate = struct.unpack(">I", body[8:12])[0]
    avg_bitrate = struct.unpack(">I", body[12:16])[0]
    rebuilt = (
        bytes([0]) + flags
        + struct.pack(">HH", max_pdu, avg_pdu)
        + struct.pack(">II", max_bitrate, avg_bitrate)
        + b"\x00" * 4
    )
    return _pack(b"hmhd", rebuilt)


def _rebuild_dinf(data: bytes, body_start: int, body_end: int) -> bytes:
    children = _parse_container(data, body_start, body_end)
    if len(children) != 1 or children[0].type != b"dref":
        raise MediaScrubError("mp4 dinf must contain exactly one dref child")
    dref = children[0]
    dref_body = data[dref.body_start:dref.body_end]
    if len(dref_body) < 8:
        raise MediaScrubError("mp4 dref body too short")
    # v0 flags(4) entry_count(4) then entries
    version = dref_body[0]
    if version != 0:
        raise MediaScrubError(f"mp4 dref unknown version {version}")
    entry_count = struct.unpack(">I", dref_body[4:8])[0]
    if entry_count == 0:
        raise MediaScrubError("mp4 dref must declare at least one entry")
    if entry_count > 0xFFFF:
        raise MediaScrubError("mp4 dref entry count implausible")
    entries: list[bytes] = []
    offset = 8
    for _ in range(entry_count):
        if offset + 8 > len(dref_body):
            raise MediaScrubError("mp4 dref entry header runs past body")
        entry_size = struct.unpack(">I", dref_body[offset:offset + 4])[0]
        entry_type = dref_body[offset + 4:offset + 8]
        if entry_size < 12 or offset + entry_size > len(dref_body):
            raise MediaScrubError("mp4 dref entry size out of bounds")
        entry_body = dref_body[offset + 8:offset + entry_size]
        # url  entry: v(1) + f(3). If self-contained (flag bit 0), no
        # location string follows. urn entry: same header + optional
        # name+location strings. We accept ONLY self-contained url.
        if entry_type != b"url ":
            raise MediaScrubError(
                f"mp4 dref entry type {entry_type!r} outside allowlist (only url is accepted)"
            )
        if len(entry_body) < 4:
            raise MediaScrubError("mp4 dref url entry too short")
        entry_flags = struct.unpack(">I", entry_body[:4])[0] & 0x00FFFFFF
        if not (entry_flags & 0x000001):
            raise MediaScrubError(
                "mp4 dref url entry is not self-contained (external references rejected)"
            )
        # Rebuild the URL entry: only the version+flags field, no location.
        rebuilt_entry = _pack(b"url ", bytes([0]) + b"\x00\x00\x01")
        entries.append(rebuilt_entry)
        offset += entry_size
    rebuilt_dref = bytes([0]) + b"\x00\x00\x00" + struct.pack(">I", len(entries)) + b"".join(entries)
    return _pack(b"dinf", _pack(b"dref", rebuilt_dref))


def _rebuild_stbl(
    data: bytes, body_start: int, body_end: int,
) -> tuple[bytes, bool]:
    atoms = _parse_container(data, body_start, body_end)
    parts: list[bytes] = []
    stsd_ok = False
    # Round-9 review: stbl singleton tables must not repeat. Pre-R9 the
    # walker accepted arbitrary duplicates so an attacker could stack
    # many empty stss/ctts/... boxes with independent 3-byte flags
    # smuggled through each header. Track seen types and reject dupes.
    seen: set[bytes] = set()
    for atom in atoms:
        if atom.type in seen:
            raise MediaScrubError(
                f"mp4 stbl carries duplicate {atom.type!r} table"
            )
        seen.add(atom.type)
        if atom.type == b"stsd":
            stsd_body = _rebuild_stsd(data, atom.body_start, atom.body_end)
            if stsd_body is not None:
                parts.append(_pack(b"stsd", stsd_body))
                stsd_ok = True
        elif atom.type in _MP4_STBL_TABLE_TYPES:
            parts.append(_rebuild_stbl_table(atom.type, data, atom))
        else:
            # Round-8 review: pre-R8 stbl copied any allowlisted child
            # opaquely, so table slack rode through. Now every allowed
            # type has a struct.pack rebuild; anything else rejects the
            # file (strict-subset acceptance).
            raise MediaScrubError(
                f"mp4 stbl child {atom.type!r} not supported by scrubber"
            )
    return b"".join(parts), stsd_ok


_CANONICAL_FULLBOX_FLAGS = b"\x00\x00\x00"


# Round-10 review: FullBox flag rebuilders across the mp4 tree captured
# the input v+flags bytes then re-emitted them verbatim. The 24-bit flags
# field is reserved for most box types (spec says shall be zero), so an
# attacker could smuggle 3 bytes through the metadata scrub. This helper
# enforces a per-box allowed-mask: bits outside the mask reject the file,
# and the returned 3 bytes are canonical (bits inside the mask preserved).
# For boxes with mask == 0, the return value is always three zero bytes.
_MP4_FULLBOX_ALLOWED_FLAG_MASK: Final = {
    b"sidx": 0,
    b"mvhd": 0,
    b"elst": 0,
    b"tkhd": 0x00000F,  # track_enabled(1) + in_movie(2) + in_preview(4) + size_is_aspect_ratio(8)
    b"mdhd": 0,
    b"hdlr": 0,
    b"vmhd": 0x000001,  # flags shall be 0x000001 (no_lean_ahead) per 14496-12
    b"smhd": 0,
    b"nmhd": 0,
    b"hmhd": 0,
}


def _validate_fullbox_flags(box_type: bytes, flags_bytes: bytes) -> bytes:
    """Reject FullBox flag bytes with reserved bits set; return canonical
    3-byte flags (bits outside the allowed mask are zero on output).
    """
    if len(flags_bytes) != 3:
        raise MediaScrubError(
            f"mp4 {box_type!r} fullbox flags field must be 3 bytes"
        )
    allowed_mask = _MP4_FULLBOX_ALLOWED_FLAG_MASK.get(box_type)
    if allowed_mask is None:
        raise MediaScrubError(
            f"mp4 {box_type!r} has no fullbox flag policy defined"
        )
    flags_val = int.from_bytes(flags_bytes, "big")
    if flags_val & ~allowed_mask:
        raise MediaScrubError(
            f"mp4 {box_type!r} fullbox flags 0x{flags_val:06x} has reserved bits set "
            f"(allowed mask 0x{allowed_mask:06x})"
        )
    canonical = flags_val & allowed_mask
    return canonical.to_bytes(3, "big")


def _rebuild_stbl_table(box_type: bytes, data: bytes, atom: _Mp4Atom) -> bytes:
    """Rebuild an stbl child table from parsed entry_count entries.

    Every table box in stbl (stts/ctts/stsc/stsz/stco/co64/stss) is a
    fullbox with a 4-byte entry_count followed by exactly N fixed-size
    entries. Round-9 review found two issues in the R8 rebuild:
      1. version+flags bytes were captured then re-emitted verbatim, so
         an attacker could smuggle 3 bytes through each table header.
         Fix: require flags == 0, emit canonical zeros; require
         version == 0 (or version in {0,1} for ctts) and preserve the
         version so signed sample offsets remain valid.
      2. The rebuild decoded each entry to a Python int and re-packed
         it, so a valid multi-million-entry stsz cost hundreds of MB
         RSS. Fix: after validating the declared entry_count against the
         body length, splice the entry payload verbatim as bytes -- the
         encoding is fixed-size big-endian, and there is no room for
         non-canonical variation.
    """
    body = memoryview(data)[atom.body_start:atom.body_end]
    if len(body) < 8:
        raise MediaScrubError(f"mp4 {box_type!r} body too short for header")
    version = body[0]
    if bytes(body[1:4]) != _CANONICAL_FULLBOX_FLAGS:
        raise MediaScrubError(f"mp4 {box_type!r} fullbox flags non-zero")

    def _canonical_header(v: int, entry_count: int) -> bytes:
        return bytes([v]) + _CANONICAL_FULLBOX_FLAGS + struct.pack(">I", entry_count)

    if box_type == b"stsz":
        if len(body) < 12:
            raise MediaScrubError("mp4 stsz body too short")
        if version != 0:
            raise MediaScrubError(f"mp4 stsz unknown version {version}")
        sample_size = struct.unpack(">I", bytes(body[4:8]))[0]
        sample_count = struct.unpack(">I", bytes(body[8:12]))[0]
        if sample_count > _MP4_MAX_SAMPLES:
            raise MediaScrubError(
                f"mp4 stsz sample_count {sample_count} exceeds scrubber limit"
            )
        if sample_size != 0:
            if len(body) != 12:
                raise MediaScrubError(
                    f"mp4 stsz uniform-size body length {len(body)} differs from expected 12"
                )
            return _pack(
                b"stsz",
                bytes([0]) + _CANONICAL_FULLBOX_FLAGS
                + struct.pack(">II", sample_size, sample_count),
            )
        expected = 12 + sample_count * 4
        if len(body) != expected:
            raise MediaScrubError(
                f"mp4 stsz body length {len(body)} differs from expected {expected} "
                f"(sample_count={sample_count})"
            )
        return _pack(
            b"stsz",
            bytes([0]) + _CANONICAL_FULLBOX_FLAGS
            + struct.pack(">II", 0, sample_count)
            + bytes(body[12:]),
        )

    if box_type == b"ctts":
        # ctts version 0: unsigned uint32 sample_offset. version 1: signed
        # int32 sample_offset. Both are 8 bytes per entry and canonical
        # in big-endian; splice verbatim, preserve the version.
        if version not in (0, 1):
            raise MediaScrubError(f"mp4 ctts unknown version {version}")
        entry_count = struct.unpack(">I", bytes(body[4:8]))[0]
        expected = 8 + entry_count * 8
        if len(body) != expected:
            raise MediaScrubError(
                f"mp4 ctts body length {len(body)} differs from expected {expected}"
            )
        return _pack(b"ctts", _canonical_header(version, entry_count) + bytes(body[8:]))

    if version != 0:
        raise MediaScrubError(f"mp4 {box_type!r} unknown version {version}")
    entry_count = struct.unpack(">I", bytes(body[4:8]))[0]
    entry_size_by_type = {
        b"stts": 8,
        b"stsc": 12,
        b"stco": 4,
        b"co64": 8,
        b"stss": 4,
    }
    entry_size = entry_size_by_type.get(box_type)
    if entry_size is None:
        raise MediaScrubError(f"mp4 stbl table dispatch missing case for {box_type!r}")
    expected = 8 + entry_count * entry_size
    if len(body) != expected:
        raise MediaScrubError(
            f"mp4 {box_type.decode('ascii', 'replace')} body length {len(body)} "
            f"differs from expected {expected}"
        )
    return _pack(box_type, _canonical_header(0, entry_count) + bytes(body[8:]))


def _rebuild_stsd(
    data: bytes, body_start: int, body_end: int,
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
        rebuilt = _rebuild_sample_entry(entry_type, entry_bytes)
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

def _rebuild_sample_entry(entry_type: bytes, entry_bytes: bytes) -> bytes:
    if len(entry_bytes) < 16:
        raise MediaScrubError("mp4 sample entry too short for base header")
    # size + type already validated by caller; parse reserved + dref_idx.
    reserved6 = entry_bytes[8:14]
    if reserved6 != b"\x00" * 6:
        raise MediaScrubError("mp4 sample entry reserved-6 bytes non-zero")
    data_ref_index = struct.unpack(">H", entry_bytes[14:16])[0]
    if data_ref_index == 0:
        raise MediaScrubError("mp4 sample entry data_reference_index must be ≥1")

    if entry_type in _MP4_VISUAL_ENTRIES:
        fixed = _rebuild_visual_sample_entry_fixed(entry_bytes)
        inner_start = 16 + 70
    elif entry_type in _MP4_AUDIO_ENTRIES:
        fixed, inner_start = _rebuild_audio_sample_entry_fixed(entry_bytes)
    else:
        raise MediaScrubError(
            f"mp4 sample entry type {entry_type!r} outside allowlist"
        )

    inner_boxes = _walk_sample_entry_inner_boxes(
        entry_bytes,
        inner_start,
        entry_type=entry_type,
    )
    required_config = _MP4_SAMPLE_ENTRY_REQUIRED_CONFIG[entry_type]
    inner_types = [box[4:8] for box in inner_boxes]
    if inner_types.count(required_config) != 1:
        raise MediaScrubError(
            f"mp4 {entry_type.decode('ascii')} sample entry requires exactly one "
            f"{required_config.decode('ascii')} configuration box"
        )
    body = (
        b"\x00" * 6
        + struct.pack(">H", data_ref_index)
        + fixed
        + b"".join(inner_boxes)
    )
    return _pack(entry_type, body)


def _rebuild_visual_sample_entry_fixed(entry_bytes: bytes) -> bytes:
    # 70-byte visual sample entry portion. Fields per ISO/IEC 14496-12.
    if len(entry_bytes) < 16 + 70:
        raise MediaScrubError("mp4 visual sample entry too short")
    body = entry_bytes[16:16 + 70]
    # 2 pre_defined + 2 reserved + 12 pre_defined = 16 bytes of zeros
    width = struct.unpack(">H", body[16:18])[0]
    height = struct.unpack(">H", body[18:20])[0]
    horiz_res = struct.unpack(">I", body[20:24])[0]
    vert_res = struct.unpack(">I", body[24:28])[0]
    # 4 bytes reserved
    frame_count = struct.unpack(">H", body[32:34])[0]
    # compressor_name is a Pascal string (1 length byte + up to 31 chars,
    # zero padded to 32). This can carry the encoder identity ("Lavc..."),
    # so we drop it entirely — 32 bytes of zeros.
    depth = struct.unpack(">H", body[66:68])[0]
    pre_defined = struct.unpack(">h", body[68:70])[0]
    return (
        b"\x00" * 16
        + struct.pack(">HH", width, height)
        + struct.pack(">II", horiz_res, vert_res)
        + b"\x00" * 4
        + struct.pack(">H", frame_count)
        + b"\x00" * 32
        + struct.pack(">Hh", depth, pre_defined)
    )


def _rebuild_audio_sample_entry_fixed(entry_bytes: bytes) -> tuple[bytes, int]:
    # 20-byte audio sample entry (v0) portion. Fields:
    #   reserved (8 bytes: 2 uint32)
    #   channel_count (2 bytes)
    #   sample_size (2 bytes)
    #   pre_defined (2 bytes)
    #   reserved (2 bytes)
    #   sample_rate (4 bytes, in 16.16 fixed)
    if len(entry_bytes) < 16 + 20:
        raise MediaScrubError("mp4 audio sample entry too short")
    body = entry_bytes[16:16 + 20]
    channel_count = struct.unpack(">H", body[8:10])[0]
    sample_size = struct.unpack(">H", body[10:12])[0]
    sample_rate_fixed = struct.unpack(">I", body[16:20])[0]
    fixed = (
        b"\x00" * 8
        + struct.pack(">HH", channel_count, sample_size)
        + b"\x00" * 4
        + struct.pack(">I", sample_rate_fixed)
    )
    return fixed, 16 + 20


# All sample-entry inner boxes now get a field-level rebuild — see the
# individual _rebuild_inner_* helpers below plus _rebuild_inner_avcC in
# the walker section. Codec-config formats we do NOT field-decode yet
# (hvcC/vpcC/av1C/esds) are absent from _MP4_SAMPLE_ENTRY_INNER_ALLOWED,
# so files that use them are rejected under strict-subset acceptance.


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
    return _pack(b"pasp", struct.pack(">II", h_spacing, v_spacing))


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
) -> list[bytes]:
    """Rebuild each inner box from parsed fields. No opaque body copy
    path remains: every allowlisted type dispatches to a struct.pack
    rebuild that reads specific fields; anything not in the allowlist
    (hvcC/vpcC/av1C/esds/sinf/schm/schi/tenc/anything unknown) rejects
    the whole file — strict-subset acceptance.
    """
    out: list[bytes] = []
    offset = inner_start
    end = len(entry_bytes)
    while offset < end:
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
        body = entry_bytes[offset + 8:offset + box_size]
        if box_type == b"avcC":
            out.append(
                _rebuild_inner_avcC(
                    body,
                    require_parameter_sets=entry_type == b"avc1",
                )
            )
        elif box_type == b"btrt":
            out.append(_rebuild_inner_btrt(body))
        elif box_type == b"pasp":
            out.append(_rebuild_inner_pasp(body))
        elif box_type == b"colr":
            out.append(_rebuild_inner_colr(body))
        else:  # pragma: no cover — allowlist above already gated
            raise MediaScrubError(
                f"mp4 sample entry inner box {box_type!r} missing rebuilder"
            )
        offset += box_size
    return out


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
        if profile not in (100, 110, 122, 144, 44, 83, 86, 118, 128, 138, 139, 134, 135):
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

def _mvhd_duration(moov_payload: bytes) -> int | None:
    view = memoryview(moov_payload)
    offset = 0
    end = len(moov_payload)
    while offset < end:
        try:
            _size, atom_type, header_len, atom_end = _read_header(view, offset, end)
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
            body_start = payload_start + 4 + 8
            if body_start + 8 > atom_end:
                return None
            timescale, duration = struct.unpack(
                ">II", moov_payload[body_start:body_start + 8],
            )
        elif version == 1:
            body_start = payload_start + 4 + 16
            if body_start + 12 > atom_end:
                return None
            timescale = struct.unpack(">I", moov_payload[body_start:body_start + 4])[0]
            duration = struct.unpack(
                ">Q", moov_payload[body_start + 4:body_start + 12],
            )[0]
        else:
            return None
        if timescale == 0:
            return None
        return int(round(duration * 1000 / timescale))
    return None


def _tkhd_dims_from_moov(moov_payload: bytes) -> tuple[int, int] | None:
    view = memoryview(moov_payload)
    offset = 0
    end = len(moov_payload)
    while offset < end:
        try:
            _size, atom_type, header_len, atom_end = _read_header(view, offset, end)
        except MediaScrubError:
            return None
        if atom_type == b"trak":
            dims = _tkhd_dims_from_trak(
                view, offset + header_len, atom_end, moov_payload,
            )
            if dims is not None:
                return dims
        offset = atom_end
    return None


def _tkhd_dims_from_trak(
    view: memoryview,
    payload_start: int,
    trak_end: int,
    source: bytes,
) -> tuple[int, int] | None:
    offset = payload_start
    while offset < trak_end:
        try:
            _size, atom_type, header_len, atom_end = _read_header(view, offset, trak_end)
        except MediaScrubError:
            return None
        if atom_type != b"tkhd":
            offset = atom_end
            continue
        payload_at = offset + header_len
        if payload_at + 1 > atom_end:
            return None
        version = source[payload_at]
        pre_matrix = 4 + (28 if version == 0 else 40) + 2 + 2 + 2 + 2
        matrix = 36
        dims_at = payload_at + pre_matrix + matrix
        if dims_at + 8 > atom_end:
            return None
        width_fixed, height_fixed = struct.unpack(">II", source[dims_at:dims_at + 8])
        width = width_fixed >> 16
        height = height_fixed >> 16
        if width > 0 and height > 0:
            return width, height
        return None
    return None
