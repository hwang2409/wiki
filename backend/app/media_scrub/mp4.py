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
                                    entries (avc1); other stbl
                                    boxes header-validated but their
                                    bodies are BOUNDED by the parsed
                                    size (fully parsing every codec
                                    config table is out of scope; we
                                    strict-allowlist the box types).
    mdat                            sample data — required, non-empty
    sidx                            rebuilt from parsed fields
    styp/moof/mfra                  rejected (fragmented playback unsupported)
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
from typing import Final

from .base import MediaScrubError, MediaScrubResult
from ._mp4_primitives import (
    Mp4Atom as _Mp4Atom,
    free as _free,
    pack as _pack,
    pack_with_header as _pack_with_header,
    parse_container as _parse_container,
)
from ._mp4_aac import (
    Mp4AacConfig as _Mp4AacConfig,
    _canonicalise_aac_sample,
)
from ._mp4_avc import (
    _canonicalise_avc_sample,
)
from ._mp4_sample_plan import (
    _build_sample_plan,
    _iter_sample_plan_ranges,
)
from ._mp4_timing import (
    _display_dims_from_moov,
    _tkhd_dimensions_from_atom,
    _validated_movie_duration,
)
from ._mp4_sample_entry import (
    _iter_sample_entry_inner_boxes,
    _rebuild_stsd,
    _parse_avc_sample_config_from_entry,
    _sample_entry_pasp_ratio,
    _validate_avc_dimensions,
)
_MP4_TOPLEVEL_PLAYBACK: Final = {
    b"moov", b"mdat", b"moof", b"mfra",
}
# Sample-entry types we know how to rebuild field-by-field. Unknown types
# fall to strict-subset reject.
# avc3 has in-band parameter sets, and other codecs need separate parsers.
_MP4_VISUAL_ENTRIES: Final = {b"avc1"}
_MP4_AUDIO_ENTRIES: Final = {b"mp4a"}
# Inner boxes inside a sample entry that we accept. Each one gets its
# Every sample-entry inner box in this allowlist has a field-level
# rebuild via struct.pack — no raw body copy path remains for anything
# the scrubber claims to support (round-8 review). Codec configurations
# for containers we do not fully field-decode yet (hvcC/vpcC/av1C
# and the encryption tree sinf/schm/schi/tenc) are OUT — files using
# them are rejected under strict-subset acceptance. WIKI-225 adds
# esds for AAC-LC mp4a entries; the rebuilder in ``_mp4_aac.py``
# validates the AudioSpecificConfig field-by-field.
_MP4_SAMPLE_ENTRY_INNER_ALLOWED: Final = {
    b"avcC", b"btrt", b"pasp", b"colr", b"esds",
}
_MP4_SAMPLE_ENTRY_REQUIRED_CONFIG: Final = {
    b"avc1": b"avcC",
    b"mp4a": b"esds",
}
_MP4_MAX_SAMPLES: Final = 16_777_216
_MP4_AVC_SAMPLE_NAL_TYPES: Final = {1, 5, 6, 9, 12}
_MP4_MAX_BOXES_PER_CONTAINER: Final = 4096
_MP4_MAX_CHUNKS: Final = 65_536
_MP4_MAX_TRACK_MDAT_GROUPS: Final = 65_536
# Tables that are rebuilt entry-by-entry are capped separately from sample
# tables. This keeps their temporary Python object lists bounded.
_MP4_MAX_TABLE_ENTRIES: Final = 4096
_MP4_BRAND_ALLOWLIST: Final = frozenset({
    b"avc1", b"isom", b"iso2", b"iso5", b"iso6", b"mp41", b"mp42",
})
_MP4_MAX_COMPATIBLE_BRANDS: Final = 32
# Additional stbl children beyond stsd. Every allowed type below has a
# field-level rebuild via struct.pack that emits exactly the parsed
# entry_count worth of entries — trailing bytes cannot survive because
# they are not written. Rare stbl types (stsh, sdtp, sbgp, sgpd, subs,
# saiz, saio, padb, stz2, cslg) are not in the allowlist; a file that
# uses one of those gets rejected by the walker at the stbl level via
# strict-subset acceptance.
_MP4_STBL_TABLE_TYPES: Final = {
    b"stts", b"ctts", b"stsc", b"stsz", b"stco", b"co64", b"stss",
    b"sgpd", b"sbgp",
}

_MP4_IDENTITY_MATRIX: Final = (
    0x00010000, 0, 0,
    0, 0x00010000, 0,
    0, 0, 0x40000000,
)
_MP4_ROTATION_MATRICES: Final = frozenset({
    _MP4_IDENTITY_MATRIX,
    (0, 0x00010000, 0, -0x00010000, 0, 0, 0, 0, 0x40000000),
    (-0x00010000, 0, 0, 0, -0x00010000, 0, 0, 0, 0x40000000),
    (0, -0x00010000, 0, 0x00010000, 0, 0, 0, 0, 0x40000000),
})
_MP4_ROTATION_SWAP_MATRICES: Final = frozenset({
    (0, 0x00010000, 0, -0x00010000, 0, 0, 0, 0, 0x40000000),
    (0, -0x00010000, 0, 0x00010000, 0, 0, 0, 0, 0x40000000),
})
_Mp4TrackDimensions = tuple[int, int, bool]
_Mp4Sar = tuple[int, int]


def _canonical_matrix(box_type: bytes, matrix: bytes) -> bytes:
    if len(matrix) != 36:
        raise MediaScrubError(f"mp4 {box_type.decode('ascii')} matrix is truncated")
    values = struct.unpack(">9i", matrix)
    if values not in _MP4_ROTATION_MATRICES:
        raise MediaScrubError(
            f"mp4 {box_type.decode('ascii')} matrix is not a canonical identity or rotation"
        )
    return struct.pack(">9i", *values)


def scrub_mp4(data: bytes) -> MediaScrubResult:
    if len(data) < 16:
        raise MediaScrubError("mp4 payload too small")

    top_atoms = _parse_container(data, 0, len(data))
    if not top_atoms or top_atoms[0].type != b"ftyp":
        raise MediaScrubError("mp4 payload missing ftyp box at offset 0")
    if top_atoms[0].size < 16:
        raise MediaScrubError("mp4 ftyp too small")
    if sum(atom.type == b"moov" for atom in top_atoms) > 1:
        raise MediaScrubError("mp4 duplicate moov box")

    # Run the structural rebuild first. This preserves the parser's precise
    # errors for malformed moov children before sample ownership checks.
    for atom in top_atoms:
        if atom.type == b"moov":
            _rebuilt, _trak, _mvhd, stsd_ok, track_ok = _rebuild_moov(
                data, atom.body_start, atom.body_end,
            )
            duration_ms = _validated_movie_duration(data, atom)
            if not _trak:
                raise MediaScrubError("mp4 moov missing trak")
            if not stsd_ok:
                raise MediaScrubError("mp4 stbl/stsd has no valid sample entry")
            if not track_ok:
                raise MediaScrubError(
                    "mp4 payload requires at least one supported vide/avc1 or soun/mp4a track"
                )
    for atom in top_atoms:
        if atom.type == b"mdat" and atom.body_start == atom.body_end:
            raise MediaScrubError("mp4 mdat body is empty")

    mdat_ranges = [
        (atom.body_start, atom.body_end)
        for atom in top_atoms if atom.type == b"mdat"
    ]
    sample_plan = _build_sample_plan(data, top_atoms, mdat_ranges)

    ftyp = _rebuild_ftyp(data, top_atoms[0])
    if len(ftyp) != top_atoms[0].size:
        raise MediaScrubError("mp4 ftyp rebuild size mismatch")

    out_parts: list[bytes] = [ftyp]
    moov_seen = False
    trak_seen = False
    mvhd_seen = False
    stsd_ok = False
    track_ok = False
    mdat_non_empty = False
    mdat_index = 0
    dims: tuple[int, int] | None = None

    for atom in top_atoms[1:]:
        if atom.type == b"ftyp":
            raise MediaScrubError("mp4 duplicate ftyp box")
        if atom.type == b"moov":
            if moov_seen:
                raise MediaScrubError("mp4 duplicate moov box")
            moov_seen = True
            moov_body = data[atom.body_start:atom.body_end]
            dims = _display_dims_from_moov(data, atom)
            rebuilt_body, tr, mv, st, track = _rebuild_moov(
                data, atom.body_start, atom.body_end,
            )
            trak_seen |= tr
            mvhd_seen |= mv
            stsd_ok |= st
            track_ok |= track
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
            for sample_range in _iter_sample_plan_ranges(sample_plan, mdat_index):
                sample_start = sample_range.start
                sample_end = sample_range.end
                if sample_start < atom.body_start or sample_end > atom.body_end:
                    continue
                start = sample_start - atom.body_start
                end = sample_end - atom.body_start
                sample = data[sample_start:sample_end]
                if isinstance(sample_range.codec_config, _Mp4AacConfig):
                    sample = _canonicalise_aac_sample(
                        sample, sample_range.codec_config,
                    )
                elif sample_range.codec_config is not None:
                    sample = _canonicalise_avc_sample(
                        sample, *sample_range.codec_config,
                    )
                scrubbed_body[start:end] = sample
            out_parts.append(data[atom.start:atom.body_start] + scrubbed_body)
            mdat_index += 1
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
            raise MediaScrubError(
                "mp4 styp box is not supported for non-fragmented scrubber"
            )
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
    if not track_ok:
        raise MediaScrubError(
            "mp4 payload requires at least one supported vide/avc1 or soun/mp4a track"
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
    if len(compat) > _MP4_MAX_COMPATIBLE_BRANDS:
        raise MediaScrubError(
            f"mp4 {atom_type!r} has too many compatible brands"
        )
    if major_brand not in _MP4_BRAND_ALLOWLIST and not any(
        brand in _MP4_BRAND_ALLOWLIST for brand in compat
    ):
        raise MediaScrubError(
            f"mp4 {atom_type!r} has no supported major or compatible brand"
        )
    canonical_compat: list[bytes] = []
    seen: set[bytes] = set()
    for brand in compat:
        if brand not in _MP4_BRAND_ALLOWLIST:
            raise MediaScrubError(
                f"mp4 {atom_type!r} brand {brand!r} is outside the allowlist"
            )
        if brand not in seen:
            seen.add(brand)
            canonical_compat.append(brand)
        else:
            raise MediaScrubError(
                f"mp4 {atom_type!r} compatible brands must be unique"
            )
    canonical_major = (
        major_brand if major_brand in _MP4_BRAND_ALLOWLIST
        else canonical_compat[0]
    )
    rebuilt_body = (
        canonical_major + struct.pack(">I", minor_version)
        + b"".join(canonical_compat)
    )
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
    if reference_id == 0:
        raise MediaScrubError("mp4 sidx reference_id must be positive")
    timescale = struct.unpack(">I", body[8:12])[0]
    if timescale == 0:
        raise MediaScrubError("mp4 sidx timescale must be positive")
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
    if reference_count > _MP4_MAX_TABLE_ENTRIES:
        raise MediaScrubError(
            f"mp4 sidx reference count exceeds {_MP4_MAX_TABLE_ENTRIES}"
        )
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
    target = atom.body_end + first_offset
    if target > len(data):
        raise MediaScrubError("mp4 sidx first_offset points past payload")
    for _ in range(reference_count):
        rt_size = struct.unpack(">I", body[offset:offset + 4])[0]
        reference_type = (rt_size >> 31) & 1
        referenced_size = rt_size & 0x7FFFFFFF
        if reference_type != 0:
            raise MediaScrubError(
                "mp4 sidx reference_type 1 is outside the nonfragmented subset"
            )
        if referenced_size == 0 or target + referenced_size > len(data):
            raise MediaScrubError("mp4 sidx reference range exceeds payload")
        sub_duration = struct.unpack(">I", body[offset + 4:offset + 8])[0]
        sap = struct.unpack(">I", body[offset + 8:offset + 12])[0]
        starts_with_sap = (sap >> 31) & 1
        sap_type = (sap >> 28) & 0x7
        sap_delta_time = sap & 0x0FFFFFFF
        if starts_with_sap:
            if sap_type not in (1, 2, 3):
                raise MediaScrubError("mp4 sidx SAP type is reserved")
        elif sap_type != 0 or sap_delta_time != 0:
            raise MediaScrubError("mp4 sidx SAP fields are non-canonical")
        references.append(struct.pack(">III", rt_size, sub_duration, sap))
        target += referenced_size
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
    return _pack_with_header(
        b"sidx", header_bytes + b"".join(references), atom.header_len,
    )


# ---------------------------------------------------------------------------
# moov + inner rewrites
# ---------------------------------------------------------------------------

def _rebuild_moov(
    data: bytes, body_start: int, body_end: int,
) -> tuple[bytes, bool, bool, bool, bool]:
    atoms = _parse_container(data, body_start, body_end)
    mvhd_atoms = [atom for atom in atoms if atom.type == b"mvhd"]
    trak_atoms = [atom for atom in atoms if atom.type == b"trak"]
    if len(mvhd_atoms) != 1:
        raise MediaScrubError("mp4 moov requires exactly one mvhd child")
    if not trak_atoms:
        raise MediaScrubError("mp4 moov missing trak")
    parts: list[bytes] = []
    trak_seen = False
    mvhd_seen = False
    stsd_ok = False
    video_ok = False
    rebuilt_traks: dict[int, bytes] = {}
    for atom in trak_atoms:
        trak_body, stsd_in_trak, video_in_trak = _rebuild_trak(
            data, atom.body_start, atom.body_end,
        )
        rebuilt_traks[atom.start] = _pack(b"trak", trak_body)
        trak_seen = True
        stsd_ok |= stsd_in_trak
        video_ok |= video_in_trak
    for atom in atoms:
        if atom.type == b"mvhd":
            mvhd_seen = True
            parts.append(_rebuild_mvhd(data, atom))
        elif atom.type == b"trak":
            parts.append(rebuilt_traks[atom.start])
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
    return b"".join(parts), trak_seen, mvhd_seen, stsd_ok, video_ok


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
        timescale = struct.unpack(">I", body[12:16])[0]
        duration = struct.unpack(">I", body[16:20])[0]
        if timescale == 0:
            raise MediaScrubError("mp4 mvhd timescale must be positive")
        rate = struct.unpack(">I", body[20:24])[0]
        volume = struct.unpack(">H", body[24:26])[0]
        matrix = _canonical_matrix(b"mvhd", body[36:72])
        next_track_id = struct.unpack(">I", body[96:100])[0]
        rebuilt = (
            bytes([0]) + flags
            + struct.pack(">II", 0, 0)
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
        timescale = struct.unpack(">I", body[20:24])[0]
        duration = struct.unpack(">Q", body[24:32])[0]
        if timescale == 0:
            raise MediaScrubError("mp4 mvhd timescale must be positive")
        rate = struct.unpack(">I", body[32:36])[0]
        volume = struct.unpack(">H", body[36:38])[0]
        matrix = _canonical_matrix(b"mvhd", body[48:84])
        next_track_id = struct.unpack(">I", body[108:112])[0]
        rebuilt = (
            bytes([1]) + flags
            + struct.pack(">QQ", 0, 0)
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
) -> tuple[bytes, bool, bool]:
    atoms = _parse_container(data, body_start, body_end)
    tkhd_atoms = [atom for atom in atoms if atom.type == b"tkhd"]
    mdia_atoms = [atom for atom in atoms if atom.type == b"mdia"]
    edts_atoms = [atom for atom in atoms if atom.type == b"edts"]
    if len(tkhd_atoms) != 1:
        raise MediaScrubError(
            "mp4 trak requires exactly one tkhd child; stbl/stsd chain incomplete"
        )
    if len(mdia_atoms) != 1:
        raise MediaScrubError(
            "mp4 trak requires exactly one mdia child; stbl/stsd chain incomplete"
        )
    if len(edts_atoms) > 1:
        raise MediaScrubError("mp4 trak has duplicate edts children")
    track_dimensions = _tkhd_dimensions_from_atom(data, tkhd_atoms[0])
    mdia_body, stsd_ok, video_ok = _rebuild_mdia(
        data, mdia_atoms[0].body_start, mdia_atoms[0].body_end,
        track_dimensions,
    )
    parts = [_rebuild_tkhd(data, tkhd_atoms[0])]
    if edts_atoms:
        parts.append(_rebuild_edts(data, edts_atoms[0]))
    parts.append(_pack(b"mdia", mdia_body))
    return b"".join(parts), stsd_ok, video_ok


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
        track_id = struct.unpack(">I", body[12:16])[0]
        duration = struct.unpack(">I", body[20:24])[0]
        layer = struct.unpack(">H", body[32:34])[0]
        alt_group = struct.unpack(">H", body[34:36])[0]
        volume = struct.unpack(">H", body[36:38])[0]
        matrix = _canonical_matrix(b"tkhd", body[40:76])
        width_fixed = struct.unpack(">I", body[76:80])[0]
        height_fixed = struct.unpack(">I", body[80:84])[0]
        if width_fixed & 0xFFFF or height_fixed & 0xFFFF:
            raise MediaScrubError(
                "mp4 tkhd v0 dimensions must have zero fractional bits"
            )
        rebuilt = (
            bytes([0]) + flags
            + struct.pack(">III", 0, 0, track_id)
            + b"\x00" * 4
            + struct.pack(">I", duration)
            + b"\x00" * 8
            + struct.pack(">HHHH", layer, alt_group, volume, 0)
            + matrix
            + struct.pack(">II", width_fixed, height_fixed)
        )
        return _pack(b"tkhd", rebuilt)
    if version == 1:
        if len(body) != 96:
            raise MediaScrubError(
                f"mp4 tkhd v1 body length {len(body)} not the 96-byte spec size"
            )
        flags = _validate_fullbox_flags(b"tkhd", body[1:4])
        track_id = struct.unpack(">I", body[20:24])[0]
        duration = struct.unpack(">Q", body[28:36])[0]
        layer = struct.unpack(">H", body[44:46])[0]
        alt_group = struct.unpack(">H", body[46:48])[0]
        volume = struct.unpack(">H", body[48:50])[0]
        matrix = _canonical_matrix(b"tkhd", body[52:88])
        width_fixed = struct.unpack(">I", body[88:92])[0]
        height_fixed = struct.unpack(">I", body[92:96])[0]
        if width_fixed & 0xFFFF or height_fixed & 0xFFFF:
            raise MediaScrubError(
                "mp4 tkhd v1 dimensions must have zero fractional bits"
            )
        rebuilt = (
            bytes([1]) + flags
            + struct.pack(">QQ", 0, 0)
            + struct.pack(">I", track_id)
            + b"\x00" * 4
            + struct.pack(">Q", duration)
            + b"\x00" * 8
            + struct.pack(">HHHH", layer, alt_group, volume, 0)
            + matrix
            + struct.pack(">II", width_fixed, height_fixed)
        )
        return _pack(b"tkhd", rebuilt)
    raise MediaScrubError(f"mp4 tkhd unknown version {version}")




def _rebuild_mdia(
    data: bytes, body_start: int, body_end: int,
    track_dimensions: _Mp4TrackDimensions | None,
) -> tuple[bytes, bool, bool]:
    atoms = _parse_container(data, body_start, body_end)
    mdhd_atoms = [atom for atom in atoms if atom.type == b"mdhd"]
    hdlr_atoms = [atom for atom in atoms if atom.type == b"hdlr"]
    minf_atoms = [atom for atom in atoms if atom.type == b"minf"]
    if len(mdhd_atoms) != 1:
        raise MediaScrubError(
            "mp4 mdia requires exactly one mdhd child; stbl/stsd chain incomplete"
        )
    if len(hdlr_atoms) != 1:
        raise MediaScrubError(
            "mp4 mdia requires exactly one hdlr child; stbl/stsd chain incomplete"
        )
    if len(minf_atoms) != 1:
        raise MediaScrubError(
            "mp4 mdia requires exactly one minf child; stbl/stsd chain incomplete"
        )
    handler_type = _handler_type(data, hdlr_atoms[0])
    minf_body, stsd_ok, video_ok = _rebuild_minf(
        data, minf_atoms[0].body_start, minf_atoms[0].body_end, handler_type,
        track_dimensions,
    )
    return (
        _rebuild_mdhd(data, mdhd_atoms[0])
        + _rebuild_hdlr(data, hdlr_atoms[0])
        + _pack(b"minf", minf_body),
        stsd_ok,
        video_ok,
    )


def _handler_type(data: bytes, atom: _Mp4Atom) -> bytes:
    body = data[atom.body_start:atom.body_end]
    if len(body) < 24 or body[0] != 0:
        raise MediaScrubError("mp4 hdlr header is invalid")
    _validate_fullbox_flags(b"hdlr", body[1:4])
    handler_type = body[8:12]
    if handler_type not in (b"vide", b"soun"):
        raise MediaScrubError(
            f"mp4 hdlr track type {handler_type!r} is outside scrubber scope"
        )
    return handler_type


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
    if entry_count > _MP4_MAX_TABLE_ENTRIES:
        raise MediaScrubError(
            f"mp4 elst entry count exceeds {_MP4_MAX_TABLE_ENTRIES}"
        )
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
        if timescale == 0:
            raise MediaScrubError("mp4 mdhd timescale must be positive")
        language = struct.unpack(">H", body[20:22])[0]
        rebuilt = (
            bytes([0]) + flags
            + struct.pack(">IIII", 0, 0, timescale, duration)
            + struct.pack(">HH", 0, 0)
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
        if timescale == 0:
            raise MediaScrubError("mp4 mdhd timescale must be positive")
        language = struct.unpack(">H", body[32:34])[0]
        rebuilt = (
            bytes([1]) + flags
            + struct.pack(">QQ", 0, 0)
            + struct.pack(">IQ", timescale, duration)
            + struct.pack(">HH", 0, 0)
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
    data: bytes, body_start: int, body_end: int, handler_type: bytes,
    track_dimensions: _Mp4TrackDimensions | None,
) -> tuple[bytes, bool, bool]:
    atoms = _parse_container(data, body_start, body_end)
    required_header = b"vmhd" if handler_type == b"vide" else b"smhd"
    header_atoms = [atom for atom in atoms if atom.type == required_header]
    if len(header_atoms) != 1:
        raise MediaScrubError(
            f"mp4 minf requires exactly one {required_header.decode()} child"
        )
    dinf_atoms = [atom for atom in atoms if atom.type == b"dinf"]
    stbl_atoms = [atom for atom in atoms if atom.type == b"stbl"]
    if len(dinf_atoms) != 1:
        raise MediaScrubError("mp4 minf requires exactly one dinf child")
    if len(stbl_atoms) != 1:
        raise MediaScrubError("mp4 minf requires exactly one stbl child")
    if handler_type == b"vide":
        media_header = _rebuild_vmhd(data, header_atoms[0])
    else:
        media_header = _rebuild_smhd(data, header_atoms[0])
    dinf = _rebuild_dinf(data, dinf_atoms[0].body_start, dinf_atoms[0].body_end)
    stbl_body, stsd_ok, video_ok = _rebuild_stbl(
        data, stbl_atoms[0].body_start, stbl_atoms[0].body_end,
        handler_type, track_dimensions,
    )
    return media_header + dinf + _pack(b"stbl", stbl_body), stsd_ok, video_ok


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
    if entry_count != 1:
        raise MediaScrubError(
            "mp4 dref must contain exactly one self-contained url entry"
        )
    if entry_count > 0xFFFF:
        raise MediaScrubError("mp4 dref entry count implausible")
    if entry_count > _MP4_MAX_TABLE_ENTRIES:
        raise MediaScrubError(
            f"mp4 dref entry count exceeds {_MP4_MAX_TABLE_ENTRIES}"
        )
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
    handler_type: bytes, track_dimensions: _Mp4TrackDimensions | None,
) -> tuple[bytes, bool, bool]:
    atoms = _parse_container(data, body_start, body_end)
    parts: list[bytes] = []
    stsd_ok = False
    video_ok = False
    # Round-9 review: stbl singleton tables must not repeat. Pre-R9 the
    # walker accepted arbitrary duplicates so an attacker could stack
    # many empty stss/ctts/... boxes with independent 3-byte flags
    # smuggled through each header. Track seen types and reject dupes.
    seen: set[bytes] = set()
    _validate_stbl_sample_tables(data, atoms)
    for atom in atoms:
        if atom.type in seen:
            raise MediaScrubError(
                f"mp4 stbl carries duplicate {atom.type!r} table"
            )
        seen.add(atom.type)
        if atom.type == b"stsd":
            stsd_body = _rebuild_stsd(
                data, atom.body_start, atom.body_end, handler_type,
                track_dimensions,
            )
            if stsd_body is not None:
                parts.append(_pack(b"stsd", stsd_body))
                stsd_ok = True
                # ``track_ok`` in the caller: at least one supported
                # video or audio track has a full rebuild.
                video_ok = handler_type in (b"vide", b"soun")
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
    return b"".join(parts), stsd_ok, video_ok


def _validate_stbl_sample_tables(data: bytes, atoms: list[_Mp4Atom]) -> None:
    """Require decoder-playable sample timing and index tables."""
    by_type = {atom.type: atom for atom in atoms}
    stts_atoms = [atom for atom in atoms if atom.type == b"stts"]
    stsz_atoms = [atom for atom in atoms if atom.type == b"stsz"]
    if len(stts_atoms) != 1:
        raise MediaScrubError("mp4 stbl requires exactly one stts table")
    if len(stsz_atoms) != 1:
        raise MediaScrubError("mp4 stbl requires exactly one stsz table")

    def body_for(
        atom: _Mp4Atom, label: str, versions: tuple[int, ...] = (0,),
    ) -> memoryview:
        body = memoryview(data)[atom.body_start:atom.body_end]
        if len(body) < 8:
            raise MediaScrubError(f"mp4 {label} body too short")
        if bytes(body[1:4]) != _CANONICAL_FULLBOX_FLAGS:
            raise MediaScrubError(f"mp4 {label} fullbox flags non-zero")
        if body[0] not in versions:
            raise MediaScrubError(f"mp4 {label} version is unsupported")
        return body

    stsz_body = body_for(stsz_atoms[0], "stsz")
    if len(stsz_body) < 12:
        raise MediaScrubError("mp4 stsz body too short")
    sample_size = struct.unpack(">I", bytes(stsz_body[4:8]))[0]
    sample_count = struct.unpack(">I", bytes(stsz_body[8:12]))[0]
    if sample_count > _MP4_MAX_SAMPLES:
        raise MediaScrubError(
            f"mp4 stsz sample_count {sample_count} exceeds scrubber limit"
        )
    expected_stsz = 12 if sample_size else 12 + sample_count * 4
    if len(stsz_body) != expected_stsz:
        raise MediaScrubError(
            f"mp4 stsz body length {len(stsz_body)} differs from expected {expected_stsz}"
        )

    def validate_count_table(
        atom: _Mp4Atom, label: str, entry_size: int,
        versions: tuple[int, ...] = (0,),
    ) -> int:
        body = body_for(atom, label, versions)
        entry_count = struct.unpack(">I", bytes(body[4:8]))[0]
        if entry_count > _MP4_MAX_TABLE_ENTRIES:
            raise MediaScrubError(
                f"mp4 {label} entry count exceeds {_MP4_MAX_TABLE_ENTRIES}"
            )
        expected = 8 + entry_count * entry_size
        if len(body) != expected:
            raise MediaScrubError(
                f"mp4 {label} body length {len(body)} differs from expected {expected}"
            )
        return entry_count

    stts_body = body_for(stts_atoms[0], "stts")
    stts_count = validate_count_table(stts_atoms[0], "stts", 8)
    total_samples = 0
    offset = 8
    for _ in range(stts_count):
        run_count, delta = struct.unpack(">II", stts_body[offset:offset + 8])
        if run_count == 0 or delta == 0:
            raise MediaScrubError("mp4 stts contains a zero-valued run")
        total_samples += run_count
        offset += 8
    if total_samples != sample_count:
        raise MediaScrubError(
            f"mp4 stts sample_count {total_samples} does not match stsz sample_count {sample_count}"
        )

    ctts_atom = by_type.get(b"ctts")
    if ctts_atom is not None:
        ctts_body = body_for(ctts_atom, "ctts", versions=(0, 1))
        ctts_count = validate_count_table(ctts_atom, "ctts", 8, versions=(0, 1))
        total_ctts_samples = 0
        for offset in range(8, 8 + ctts_count * 8, 8):
            run_count = struct.unpack(">I", ctts_body[offset:offset + 4])[0]
            if run_count == 0:
                raise MediaScrubError("mp4 ctts contains a zero-count run")
            total_ctts_samples += run_count
        if total_ctts_samples != sample_count:
            raise MediaScrubError(
                f"mp4 ctts sample_count {total_ctts_samples} does not match "
                f"stsz sample_count {sample_count}"
            )

    stss_atom = by_type.get(b"stss")
    if stss_atom is not None:
        stss_body = body_for(stss_atom, "stss")
        stss_count = validate_count_table(stss_atom, "stss", 4)
        for offset in range(8, 8 + stss_count * 4, 4):
            sample_number = struct.unpack(">I", stss_body[offset:offset + 4])[0]
            if sample_number == 0 or sample_number > sample_count:
                raise MediaScrubError("mp4 stss sample index is outside stsz")

    for chunk_type in (b"stco", b"co64"):
        chunk_atom = by_type.get(chunk_type)
        if chunk_atom is None:
            continue
        chunk_body = body_for(chunk_atom, chunk_type.decode("ascii"))
        chunk_count = struct.unpack(">I", chunk_body[4:8])[0]
        if chunk_count > _MP4_MAX_CHUNKS:
            raise MediaScrubError(
                f"mp4 chunk count {chunk_count} exceeds scrubber limit {_MP4_MAX_CHUNKS}"
            )
        expected = 8 + chunk_count * (4 if chunk_type == b"stco" else 8)
        if len(chunk_body) != expected:
            raise MediaScrubError(
                f"mp4 {chunk_type.decode('ascii')} body length {len(chunk_body)} "
                f"differs from expected {expected}"
            )

    sgpd_atom = by_type.get(b"sgpd")
    sbgp_atom = by_type.get(b"sbgp")
    if (sgpd_atom is None) != (sbgp_atom is None):
        raise MediaScrubError("mp4 sgpd and sbgp must appear as a pair")
    if sgpd_atom is not None and sbgp_atom is not None:
        sgpd_body = body_for(sgpd_atom, "sgpd", versions=(1,))
        if len(sgpd_body) < 16:
            raise MediaScrubError("mp4 sgpd body too short")
        if bytes(sgpd_body[4:8]) != b"roll":
            raise MediaScrubError("mp4 sgpd supports only roll grouping")
        default_length = struct.unpack(">I", sgpd_body[8:12])[0]
        sgpd_count = struct.unpack(">I", sgpd_body[12:16])[0]
        if default_length != 2 or sgpd_count == 0:
            raise MediaScrubError("mp4 sgpd requires non-empty 2-byte roll entries")
        if sgpd_count > _MP4_MAX_TABLE_ENTRIES:
            raise MediaScrubError("mp4 sgpd entry count exceeds scrubber limit")
        if len(sgpd_body) != 16 + sgpd_count * 2:
            raise MediaScrubError("mp4 sgpd body length is invalid")

        sbgp_body = body_for(sbgp_atom, "sbgp")
        if len(sbgp_body) < 12:
            raise MediaScrubError("mp4 sbgp body too short")
        if bytes(sbgp_body[4:8]) != b"roll":
            raise MediaScrubError("mp4 sbgp supports only roll grouping")
        sbgp_count = struct.unpack(">I", sbgp_body[8:12])[0]
        if sbgp_count == 0 or sbgp_count > _MP4_MAX_TABLE_ENTRIES:
            raise MediaScrubError("mp4 sbgp requires non-empty bounded runs")
        if len(sbgp_body) != 12 + sbgp_count * 8:
            raise MediaScrubError("mp4 sbgp body length is invalid")
        grouped_samples = 0
        for offset in range(12, 12 + sbgp_count * 8, 8):
            run_count, description_index = struct.unpack(">II", sbgp_body[offset:offset + 8])
            if run_count == 0:
                raise MediaScrubError("mp4 sbgp contains a zero-count run")
            if description_index > sgpd_count:
                raise MediaScrubError(
                    "mp4 sbgp group_description_index is outside sgpd"
                )
            grouped_samples += run_count
        if grouped_samples != sample_count:
            raise MediaScrubError(
                "mp4 sbgp sample count does not match stsz sample count"
            )


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
      2. Raw table tails carried ignored bytes through the scrub. Rebuild
         every supported entry from validated integer fields instead.
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
        if sample_count == 0:
            raise MediaScrubError("mp4 stsz must contain at least one sample")
        entries = bytearray(sample_count * 4)
        for index in range(sample_count):
            sample_value = struct.unpack(">I", body[12 + index * 4:16 + index * 4])[0]
            if sample_value == 0:
                raise MediaScrubError("mp4 stsz variable sample sizes must be non-zero")
            struct.pack_into(">I", entries, index * 4, sample_value)
        return _pack(
            b"stsz",
            bytes([0]) + _CANONICAL_FULLBOX_FLAGS
            + struct.pack(">II", 0, sample_count)
            + entries,
        )

    if box_type == b"ctts":
        # ctts version 0: unsigned uint32 sample_offset. version 1: signed
        # int32 sample_offset. Repack every run and preserve the version.
        if version not in (0, 1):
            raise MediaScrubError(f"mp4 ctts unknown version {version}")
        entry_count = struct.unpack(">I", bytes(body[4:8]))[0]
        if entry_count > _MP4_MAX_TABLE_ENTRIES:
            raise MediaScrubError(
                f"mp4 ctts entry count exceeds {_MP4_MAX_TABLE_ENTRIES}"
            )
        expected = 8 + entry_count * 8
        if len(body) != expected:
            raise MediaScrubError(
                f"mp4 ctts body length {len(body)} differs from expected {expected}"
            )
        if entry_count == 0:
            raise MediaScrubError("mp4 ctts must contain at least one run")
        entries = bytearray(entry_count * 8)
        for index in range(entry_count):
            offset = 8 + index * 8
            run_count = struct.unpack(">I", body[offset:offset + 4])[0]
            if run_count == 0:
                raise MediaScrubError("mp4 ctts contains a zero-count run")
            sample_offset = struct.unpack(
                ">I" if version == 0 else ">i", body[offset + 4:offset + 8]
            )[0]
            struct.pack_into(">I", entries, index * 8, run_count)
            struct.pack_into(">I" if version == 0 else ">i", entries, index * 8 + 4, sample_offset)
        return _pack(b"ctts", _canonical_header(version, entry_count) + entries)

    if box_type == b"sgpd":
        if version != 1 or len(body) < 16:
            raise MediaScrubError("mp4 sgpd requires version 1")
        grouping_type = bytes(body[4:8])
        default_length = struct.unpack(">I", bytes(body[8:12]))[0]
        entry_count = struct.unpack(">I", bytes(body[12:16]))[0]
        if entry_count > _MP4_MAX_TABLE_ENTRIES:
            raise MediaScrubError(
                f"mp4 sgpd entry count exceeds {_MP4_MAX_TABLE_ENTRIES}"
            )
        if grouping_type != b"roll" or default_length != 2:
            raise MediaScrubError("mp4 sgpd supports only roll entries of length 2")
        expected = 16 + entry_count * default_length
        if len(body) != expected:
            raise MediaScrubError(
                f"mp4 sgpd body length {len(body)} differs from expected {expected}"
            )
        if entry_count == 0:
            raise MediaScrubError("mp4 sgpd must contain at least one entry")
        entries = bytearray(entry_count * 2)
        for index in range(entry_count):
            value = struct.unpack(">h", body[16 + index * 2:18 + index * 2])[0]
            struct.pack_into(">h", entries, index * 2, value)
        return _pack(
            b"sgpd",
            bytes([1]) + _CANONICAL_FULLBOX_FLAGS
            + grouping_type
            + struct.pack(">II", default_length, entry_count)
            + entries,
        )

    if box_type == b"sbgp":
        if version != 0 or len(body) < 12:
            raise MediaScrubError("mp4 sbgp requires version 0")
        grouping_type = bytes(body[4:8])
        entry_count = struct.unpack(">I", bytes(body[8:12]))[0]
        if entry_count > _MP4_MAX_TABLE_ENTRIES:
            raise MediaScrubError(
                f"mp4 sbgp entry count exceeds {_MP4_MAX_TABLE_ENTRIES}"
            )
        if grouping_type != b"roll":
            raise MediaScrubError("mp4 sbgp supports only roll entries")
        expected = 12 + entry_count * 8
        if len(body) != expected:
            raise MediaScrubError(
                f"mp4 sbgp body length {len(body)} differs from expected {expected}"
            )
        if entry_count == 0:
            raise MediaScrubError("mp4 sbgp must contain at least one run")
        entries = bytearray(entry_count * 8)
        for index in range(entry_count):
            offset = 12 + index * 8
            run_count, description_index = struct.unpack(">II", body[offset:offset + 8])
            if run_count == 0:
                raise MediaScrubError("mp4 sbgp contains a zero-count run")
            struct.pack_into(">II", entries, index * 8, run_count, description_index)
        return _pack(
            b"sbgp",
            bytes([0]) + _CANONICAL_FULLBOX_FLAGS
            + grouping_type
            + struct.pack(">I", entry_count)
            + entries,
        )

    if version != 0:
        raise MediaScrubError(f"mp4 {box_type!r} unknown version {version}")
    entry_count = struct.unpack(">I", bytes(body[4:8]))[0]
    if box_type in (b"stco", b"co64") and entry_count > _MP4_MAX_CHUNKS:
        raise MediaScrubError(
            f"mp4 chunk count {entry_count} exceeds scrubber limit {_MP4_MAX_CHUNKS}"
        )
    if box_type not in (b"stco", b"co64") and entry_count > _MP4_MAX_TABLE_ENTRIES:
        raise MediaScrubError(
            f"mp4 {box_type.decode('ascii', 'replace')} entry count exceeds "
            f"{_MP4_MAX_TABLE_ENTRIES}"
        )
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
    if entry_count == 0:
        raise MediaScrubError(f"mp4 {box_type!r} must contain at least one entry")
    entries = bytearray(entry_count * entry_size)
    for index in range(entry_count):
        offset = 8 + index * entry_size
        if box_type == b"stts":
            run_count, delta = struct.unpack(">II", body[offset:offset + 8])
            if run_count == 0 or delta == 0:
                raise MediaScrubError("mp4 stts contains a zero-valued run")
            struct.pack_into(">II", entries, index * entry_size, run_count, delta)
        elif box_type == b"stsc":
            first_chunk, samples_per_chunk, description_index = struct.unpack(">III", body[offset:offset + 12])
            if first_chunk == 0 or samples_per_chunk == 0 or description_index == 0:
                raise MediaScrubError("mp4 stsc entries are invalid: zero-valued field")
            struct.pack_into(">III", entries, index * entry_size, first_chunk, samples_per_chunk, description_index)
        elif box_type == b"stco":
            chunk_offset = struct.unpack(">I", body[offset:offset + 4])[0]
            if chunk_offset == 0:
                raise MediaScrubError("mp4 stco contains a zero offset")
            struct.pack_into(">I", entries, index * entry_size, chunk_offset)
        elif box_type == b"co64":
            chunk_offset = struct.unpack(">Q", body[offset:offset + 8])[0]
            if chunk_offset == 0:
                raise MediaScrubError("mp4 co64 contains a zero offset")
            struct.pack_into(">Q", entries, index * entry_size, chunk_offset)
        else:
            sample_number = struct.unpack(">I", body[offset:offset + 4])[0]
            if sample_number == 0:
                raise MediaScrubError("mp4 stss contains a zero sample index")
            struct.pack_into(">I", entries, index * entry_size, sample_number)
    return _pack(box_type, _canonical_header(0, entry_count) + entries)
