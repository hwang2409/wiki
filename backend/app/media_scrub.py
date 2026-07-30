"""Container-level metadata scrubbing for video and audio artifacts.

Same policy as image_scrub: never trust the declared mime, sniff magic bytes,
enforce hard byte caps *before* any parsing, then destroy user-metadata boxes
in place. Every scrub path leaves file-level byte offsets intact so any
absolute-offset table inside the container (mp4 stco/co64, wav data chunk,
mp3 frame stream) still points at the right bytes after storage.

Deliberately bounded-memory: everything is byte-slice work over the input
buffer. No pixel or frame decode happens here — the outer caller has already
enforced a size cap that keeps the whole buffer resident in memory.

WebM/Ogg are intentionally rejected: their metadata surface (Matroska Tags
elements, Vorbis/Opus Comment packets) requires nontrivial EBML VINT / Ogg
page-stream rewriting that we haven't implemented, and silent pass-through
of possibly-tagged bytes is the one outcome the artifact policy forbids.

Every function raises MediaScrubError on any malformed input; that is caught
by the artifact tool and returned as ArtifactValidationError.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final


VIDEO_MIMES: Final = {
    "video/mp4": "mp4",
    "image/gif": "gif",
}
AUDIO_MIMES: Final = {
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
}

# Streaming waveform cap. Peaks are stored as unsigned 8-bit values.
WAVEFORM_MAX_PEAKS: Final = 512


class MediaScrubError(ValueError):
    """Raised when the payload cannot be validated or safely scrubbed."""


@dataclass(frozen=True)
class MediaScrubResult:
    data: bytes
    mime: str
    duration_ms: int | None
    width: int | None
    height: int | None
    peaks: list[int] | None = None


def scrub_video(data: bytes, mime: str) -> MediaScrubResult:
    if mime == "video/mp4":
        return _scrub_mp4(data)
    if mime == "image/gif":
        return _scrub_gif(data)
    raise MediaScrubError(f"unsupported video mime: {mime}")


def scrub_audio(data: bytes, mime: str) -> MediaScrubResult:
    if mime == "audio/wav":
        return _scrub_wav(data)
    if mime == "audio/mpeg":
        return _scrub_mp3(data)
    raise MediaScrubError(f"unsupported audio mime: {mime}")


# ---------------------------------------------------------------------------
# MP4 / ISO Base Media File Format
# ---------------------------------------------------------------------------
#
# Round-5 review: in-place byte surgery on the raw MP4 kept spawning
# boundary bugs (trailing bytes past declared entries, out-of-chain
# atoms surviving, unknown-type atoms slipping through). Replaced with
# structural reconstruction — the stored file is emitted from the parsed
# atom tree, and ONLY atoms reached via the canonical ISOBMFF chain are
# written back:
#
#     ftyp
#     moov -> mvhd + trak+ (each trak -> tkhd + edts? + mdia
#                             mdia  -> mdhd + hdlr + minf
#                             minf  -> vmhd/smhd/nmhd/hmhd/dinf + stbl
#                             stbl  -> stsd + (stts, stsc, stco, co64, stsz, …)
#                             stsd  -> re-emitted from parsed sample entries)
#     mdat, moof, sidx, styp, mfra    (played back as-is; carry no metadata)
#     everything else at the top level -> replaced with a `free` box of
#                                        the same length
#
# The last rule preserves the absolute byte offsets that stco/co64
# entries inside stbl point at (mdat sample offsets must survive). To
# keep the moov atom the same total size after we drop metadata
# children, we also emit a `free` box inside moov whose length exactly
# fills the shrinkage. Nothing else about the file layout changes.
#
# Unknown atoms, trailing bytes past declared entry counts, and boxes
# stashed outside their canonical parent cannot survive because they
# are never written by the emitter.


@dataclass(frozen=True)
class _Mp4Atom:
    start: int
    size: int
    header_len: int
    type: bytes
    body_start: int
    body_end: int


_MP4_TOPLEVEL_PLAYBACK: Final = {
    b"moov", b"mdat", b"moof", b"sidx", b"styp", b"mfra", b"skip",
}
_MP4_MOOV_KEEP: Final = {b"mvhd", b"trak", b"mvex"}
_MP4_TRAK_KEEP: Final = {b"tkhd", b"edts", b"mdia"}
_MP4_MDIA_KEEP: Final = {b"mdhd", b"hdlr", b"minf"}
_MP4_MINF_KEEP: Final = {b"vmhd", b"smhd", b"nmhd", b"hmhd", b"dinf", b"stbl"}
_MP4_STBL_KEEP: Final = {
    b"stsd", b"stts", b"ctts", b"cslg", b"stsc", b"stco", b"co64",
    b"stsz", b"stz2", b"stss", b"stsh", b"sdtp", b"sbgp", b"sgpd",
    b"subs", b"saiz", b"saio", b"padb",
}
_MP4_FREE_MIN_SIZE: Final = 8  # a `free` box header alone is 8 bytes


def _mp4_parse_container(data: bytes, offset: int, end: int) -> list[_Mp4Atom]:
    """Parse a container's direct children. Raises on any malformed size."""
    view = memoryview(data)
    atoms: list[_Mp4Atom] = []
    while offset < end:
        size, atom_type, header_len, atom_end = _mp4_read_header(view, offset, end)
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


def _mp4_pack(atom_type: bytes, body: bytes) -> bytes:
    """Emit a fresh 32-bit-sized atom. Callers must keep total < 2**32."""
    total = 8 + len(body)
    if total > 0xFFFFFFFF:  # pragma: no cover — enforced by upstream 40 MB cap
        raise MediaScrubError("mp4 rebuilt atom size overflows 32 bits")
    return struct.pack(">I", total) + atom_type + body


def _mp4_free(total_size: int) -> bytes:
    """Emit a `free` box that occupies exactly `total_size` bytes."""
    if total_size < _MP4_FREE_MIN_SIZE:
        raise MediaScrubError(
            f"mp4 free padding requires ≥{_MP4_FREE_MIN_SIZE} bytes, got {total_size}"
        )
    return struct.pack(">I", total_size) + b"free" + b"\x00" * (total_size - _MP4_FREE_MIN_SIZE)


def _scrub_mp4(data: bytes) -> MediaScrubResult:
    if len(data) < 16:
        raise MediaScrubError("mp4 payload too small")

    top_atoms = _mp4_parse_container(data, 0, len(data))
    if not top_atoms or top_atoms[0].type != b"ftyp":
        raise MediaScrubError("mp4 payload missing ftyp box at offset 0")

    out_parts: list[bytes] = [data[top_atoms[0].start:top_atoms[0].body_end]]
    moov_seen = False
    trak_seen = False
    mvhd_seen = False
    stsd_ok = False
    duration_ms: int | None = None
    dims: tuple[int, int] | None = None

    for atom in top_atoms[1:]:
        if atom.type == b"ftyp":
            raise MediaScrubError("mp4 duplicate ftyp box")
        if atom.type == b"moov":
            if moov_seen:
                raise MediaScrubError("mp4 duplicate moov box")
            moov_seen = True
            moov_payload = data[atom.body_start:atom.body_end]
            duration_ms = _mp4_extract_moov_duration(moov_payload)
            dims = _mp4_extract_moov_dims(moov_payload)
            rebuilt_body, tr, mv, st = _mp4_rewrite_moov(data, atom.body_start, atom.body_end)
            trak_seen |= tr
            mvhd_seen |= mv
            stsd_ok |= st
            rebuilt = _mp4_pack(b"moov", rebuilt_body)
            delta = atom.size - len(rebuilt)
            if delta < 0:
                # Reconstruction should never grow moov (we only drop).
                # Refusing here means a fixture went out of contract.
                raise MediaScrubError("mp4 rebuilt moov exceeds original size")
            if delta > 0:
                # Pad the moov body with a `free` child of exactly `delta`
                # bytes so the total moov size matches the original. This
                # keeps every stco/co64 sample-offset inside stbl valid
                # without having to rewrite offsets — the free box is a
                # first-class ISOBMFF construct that decoders skip past.
                padded_body = rebuilt_body + _mp4_free(delta)
                rebuilt = _mp4_pack(b"moov", padded_body)
            if len(rebuilt) != atom.size:
                raise MediaScrubError("mp4 rebuilt moov size mismatch after padding")
            out_parts.append(rebuilt)
        elif atom.type == b"mdat":
            # Sample data — passes through untouched; stco/co64 point here.
            out_parts.append(data[atom.start:atom.body_end])
        elif atom.type in _MP4_TOPLEVEL_PLAYBACK:
            # Fragmented playback / auxiliary boxes; no metadata carried
            # here in practice, and their internal offsets are relative.
            out_parts.append(data[atom.start:atom.body_end])
        else:
            # Everything else — udta, meta, uuid, and any wholly unknown
            # top-level atom — becomes a same-size `free` box. That
            # destroys the payload bytes AND preserves file layout so
            # any absolute-offset table pointing past this atom stays
            # valid.
            out_parts.append(_mp4_free(atom.size))

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

    width, height = dims if dims is not None else (None, None)
    return MediaScrubResult(
        data=b"".join(out_parts),
        mime="video/mp4",
        duration_ms=duration_ms,
        width=width,
        height=height,
    )


def _mp4_read_header(
    view: memoryview, offset: int, end: int
) -> tuple[int, bytes, int, int]:
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


def _mp4_rewrite_moov(
    data: bytes, body_start: int, body_end: int,
) -> tuple[bytes, bool, bool, bool]:
    """Emit only mvhd, trak, and mvex children. Return (body, trak_seen, mvhd_seen, stsd_ok)."""
    atoms = _mp4_parse_container(data, body_start, body_end)
    parts: list[bytes] = []
    trak_seen = False
    mvhd_seen = False
    stsd_ok = False
    for atom in atoms:
        if atom.type == b"mvhd":
            mvhd_seen = True
            parts.append(data[atom.start:atom.body_end])
        elif atom.type == b"trak":
            trak_body, stsd_in_trak = _mp4_rewrite_trak(data, atom.body_start, atom.body_end)
            trak_seen = True
            stsd_ok |= stsd_in_trak
            parts.append(_mp4_pack(b"trak", trak_body))
        elif atom.type == b"mvex":
            parts.append(data[atom.start:atom.body_end])
        # every other child (udta, meta, uuid, iods, hoisted anything) — drop
    return b"".join(parts), trak_seen, mvhd_seen, stsd_ok


def _mp4_rewrite_trak(
    data: bytes, body_start: int, body_end: int,
) -> tuple[bytes, bool]:
    atoms = _mp4_parse_container(data, body_start, body_end)
    parts: list[bytes] = []
    stsd_ok = False
    for atom in atoms:
        if atom.type in (b"tkhd", b"edts"):
            parts.append(data[atom.start:atom.body_end])
        elif atom.type == b"mdia":
            mdia_body, stsd_in_mdia = _mp4_rewrite_mdia(data, atom.body_start, atom.body_end)
            parts.append(_mp4_pack(b"mdia", mdia_body))
            stsd_ok |= stsd_in_mdia
    return b"".join(parts), stsd_ok


def _mp4_rewrite_mdia(
    data: bytes, body_start: int, body_end: int,
) -> tuple[bytes, bool]:
    atoms = _mp4_parse_container(data, body_start, body_end)
    parts: list[bytes] = []
    stsd_ok = False
    for atom in atoms:
        if atom.type in (b"mdhd", b"hdlr"):
            parts.append(data[atom.start:atom.body_end])
        elif atom.type == b"minf":
            minf_body, stsd_in_minf = _mp4_rewrite_minf(data, atom.body_start, atom.body_end)
            parts.append(_mp4_pack(b"minf", minf_body))
            stsd_ok |= stsd_in_minf
    return b"".join(parts), stsd_ok


def _mp4_rewrite_minf(
    data: bytes, body_start: int, body_end: int,
) -> tuple[bytes, bool]:
    atoms = _mp4_parse_container(data, body_start, body_end)
    parts: list[bytes] = []
    stsd_ok = False
    for atom in atoms:
        if atom.type in (b"vmhd", b"smhd", b"nmhd", b"hmhd", b"dinf"):
            parts.append(data[atom.start:atom.body_end])
        elif atom.type == b"stbl":
            stbl_body, stsd_in_stbl = _mp4_rewrite_stbl(data, atom.body_start, atom.body_end)
            parts.append(_mp4_pack(b"stbl", stbl_body))
            stsd_ok |= stsd_in_stbl
    return b"".join(parts), stsd_ok


def _mp4_rewrite_stbl(
    data: bytes, body_start: int, body_end: int,
) -> tuple[bytes, bool]:
    atoms = _mp4_parse_container(data, body_start, body_end)
    parts: list[bytes] = []
    stsd_ok = False
    for atom in atoms:
        if atom.type == b"stsd":
            stsd_body = _mp4_rewrite_stsd(data, atom.body_start, atom.body_end)
            if stsd_body is not None:
                parts.append(_mp4_pack(b"stsd", stsd_body))
                stsd_ok = True
        elif atom.type in _MP4_STBL_KEEP:
            parts.append(data[atom.start:atom.body_end])
    return b"".join(parts), stsd_ok


def _mp4_rewrite_stsd(
    data: bytes, body_start: int, body_end: int,
) -> bytes | None:
    """Emit the version/flags header + entry count + fully-parsed entries.

    Any bytes past the last declared entry (attacker markers, misaligned
    padding) are simply not written to the output — they cannot survive.
    Returns None if the descriptor is unparseable; caller treats that as
    "no valid stsd" and the whole-file check fails.
    """
    payload = data[body_start:body_end]
    if len(payload) < 8:
        return None
    version_flags = payload[:4]
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
        entries.append(payload[offset:offset + entry_size])
        offset += entry_size
    return version_flags + struct.pack(">I", len(entries)) + b"".join(entries)


def _mp4_extract_moov_duration(moov_payload: bytes) -> int | None:
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


def _mp4_extract_moov_dims(moov_payload: bytes) -> tuple[int, int] | None:
    view = memoryview(moov_payload)
    offset = 0
    end = len(moov_payload)
    while offset < end:
        try:
            _size, atom_type, header_len, atom_end = _mp4_read_header(view, offset, end)
        except MediaScrubError:
            return None
        if atom_type == b"trak":
            dims = _mp4_extract_tkhd_dims(
                view, offset + header_len, atom_end, moov_payload,
            )
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


# ---------------------------------------------------------------------------
# GIF (GIF87a / GIF89a)
# ---------------------------------------------------------------------------

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

    image_seen = False
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
            sub_end, _drop = _gif_walk_subblocks(data, data_start + 1, end, None)
            out.extend(data[offset:sub_end])
            offset = sub_end
            image_seen = True
            continue
        raise MediaScrubError(f"unexpected gif block marker: 0x{marker:02x}")
    if not image_seen:
        raise MediaScrubError("gif payload has no image data")
    if offset != end and out[-1] != _GIF_TRAILER:
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
#
# Round-5 review: in-place scrub silently accepted files whose RIFF size
# undercounted trailing bytes and odd-sized chunks whose pad byte lived
# past the RIFF boundary. Replaced with structural reconstruction: parse
# every chunk under a validated RIFF size, keep only fmt/data/fact, then
# emit the file from scratch with computed sizes and correct padding.
# Nothing outside the parsed structure can survive because nothing else
# is written.

_WAV_KEEP_CHUNKS: Final = {b"fmt ", b"data", b"fact"}
_WAV_FORMAT_PCM: Final = 1
_WAV_FORMAT_IEEE_FLOAT: Final = 3
_WAV_FORMAT_EXTENSIBLE: Final = 0xFFFE
_WAV_ALLOWED_FORMATS: Final = {_WAV_FORMAT_PCM, _WAV_FORMAT_IEEE_FLOAT}
_WAV_KSDATAFORMAT_PCM: Final = (
    b"\x01\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
)
_WAV_KSDATAFORMAT_IEEE_FLOAT: Final = (
    b"\x03\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
)


def _scrub_wav(data: bytes) -> MediaScrubResult:
    if len(data) < 12:
        raise MediaScrubError("wav payload too small")
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise MediaScrubError("wav payload missing RIFF/WAVE header")

    # RIFF size covers every byte after itself: 4 bytes for "WAVE" plus
    # every chunk. Any mismatch — undercount (trailing attacker bytes past
    # RIFF end) OR overcount (RIFF claims bytes we don't have) — is
    # sufficient to reject the file. We refuse to guess where the "real"
    # end lies.
    declared_riff_size = struct.unpack("<I", data[4:8])[0]
    if declared_riff_size + 8 != len(data):
        raise MediaScrubError(
            f"wav RIFF size {declared_riff_size} + 8 does not match payload length {len(data)}"
        )

    fmt_payload: bytes | None = None
    data_payload: bytes | None = None
    fact_payload: bytes | None = None

    offset = 12
    end = len(data)
    while offset < end:
        if offset + 8 > end:
            raise MediaScrubError("wav chunk header truncated inside RIFF")
        chunk_id = data[offset:offset + 4]
        chunk_size = struct.unpack("<I", data[offset + 4:offset + 8])[0]
        payload_start = offset + 8
        payload_end = payload_start + chunk_size
        if payload_end > end:
            raise MediaScrubError("wav chunk extends past RIFF end")
        pad = chunk_size & 1
        if pad and payload_end + pad > end:
            # Odd-sized chunk whose mandatory pad byte lives outside RIFF.
            raise MediaScrubError("wav odd chunk missing pad byte inside RIFF")
        payload = data[payload_start:payload_end]
        if chunk_id == b"fmt ":
            if fmt_payload is not None:
                raise MediaScrubError("wav duplicate fmt chunk")
            _wav_validate_fmt(payload)
            fmt_payload = payload
        elif chunk_id == b"data":
            if data_payload is not None:
                raise MediaScrubError("wav duplicate data chunk")
            data_payload = payload
        elif chunk_id == b"fact":
            if fact_payload is not None:
                raise MediaScrubError("wav duplicate fact chunk")
            fact_payload = payload
        # All other chunks are dropped — never written to the output.
        offset = payload_end + pad

    if fmt_payload is None:
        raise MediaScrubError("wav payload missing fmt chunk")
    if data_payload is None or len(data_payload) == 0:
        raise MediaScrubError("wav payload missing data chunk")

    channels, sample_rate, byte_rate, bits = _wav_fmt_fields(fmt_payload)
    data_bytes = len(data_payload)
    if byte_rate:
        duration_ms = int(round(data_bytes * 1000 / byte_rate))
    elif sample_rate and channels and bits:
        frames = data_bytes // max(1, (channels * bits // 8))
        duration_ms = int(round(frames * 1000 / sample_rate)) if frames else 0
    else:
        duration_ms = None
    peaks = _wav_stream_peaks(data_payload, 0, len(data_payload), channels, bits)

    # Emit chunks in canonical playback order (fmt, fact?, data). Every
    # chunk carries its computed length + its own pad byte. Nothing outside
    # this list can survive because nothing else is written.
    body = bytearray(b"WAVE")
    _wav_emit_chunk(body, b"fmt ", fmt_payload)
    if fact_payload is not None:
        _wav_emit_chunk(body, b"fact", fact_payload)
    _wav_emit_chunk(body, b"data", data_payload)
    riff = bytearray(b"RIFF")
    riff += struct.pack("<I", len(body))
    riff += body
    return MediaScrubResult(
        data=bytes(riff),
        mime="audio/wav",
        duration_ms=duration_ms,
        width=None,
        height=None,
        peaks=peaks,
    )


def _wav_emit_chunk(body: bytearray, chunk_id: bytes, payload: bytes) -> None:
    body.extend(chunk_id)
    body.extend(struct.pack("<I", len(payload)))
    body.extend(payload)
    if len(payload) & 1:
        body.append(0)


def _wav_fmt_fields(payload: bytes) -> tuple[int, int, int, int]:
    channels = struct.unpack("<H", payload[2:4])[0]
    sample_rate = struct.unpack("<I", payload[4:8])[0]
    byte_rate = struct.unpack("<I", payload[8:12])[0]
    bits = struct.unpack("<H", payload[14:16])[0]
    return channels, sample_rate, byte_rate, bits


def _wav_validate_fmt(payload: bytes) -> None:
    if len(payload) < 16:
        raise MediaScrubError("wav fmt chunk too short")
    format_code = struct.unpack("<H", payload[:2])[0]
    if format_code == _WAV_FORMAT_EXTENSIBLE:
        if len(payload) < 18:
            raise MediaScrubError("wav extensible fmt chunk missing cbSize field")
        cb_size = struct.unpack("<H", payload[16:18])[0]
        if cb_size < 22:
            raise MediaScrubError(
                f"wav extensible cbSize {cb_size} smaller than the 22-byte extension"
            )
        if 18 + cb_size > len(payload):
            raise MediaScrubError("wav extensible extension extends past fmt chunk")
        if len(payload) < 40:
            raise MediaScrubError("wav extensible fmt chunk too short for SubFormat")
        subformat = payload[24:40]
        if subformat not in (_WAV_KSDATAFORMAT_PCM, _WAV_KSDATAFORMAT_IEEE_FLOAT):
            raise MediaScrubError(
                f"wav SubFormat GUID {subformat.hex()} is not PCM or float"
            )
    elif format_code not in _WAV_ALLOWED_FORMATS:
        raise MediaScrubError(
            f"wav format code {format_code} is not PCM (1), float (3), or extensible"
        )


def _wav_stream_peaks(
    data: bytes,
    payload_start: int,
    payload_end: int,
    channels: int,
    bits: int,
) -> list[int] | None:
    """Compute up to WAVEFORM_MAX_PEAKS peaks by streaming abs-max over the data chunk.

    Bounded memory: we take memoryview slices window-by-window and consume
    only 4 bytes at a time for the current sample; peaks output is capped
    at 512 uint8 values regardless of payload size, so a 5-hour WAV and a
    5-second WAV produce the same-sized peaks array. No per-sample copy.
    """
    if channels <= 0 or bits not in (8, 16, 24, 32):
        return None
    bytes_per_sample = bits // 8
    frame_stride = bytes_per_sample * channels
    payload_len = payload_end - payload_start
    if frame_stride <= 0 or payload_len < frame_stride:
        return None
    total_frames = payload_len // frame_stride
    if total_frames == 0:
        return None
    bucket_frames = max(1, (total_frames + WAVEFORM_MAX_PEAKS - 1) // WAVEFORM_MAX_PEAKS)
    peaks: list[int] = []
    view = memoryview(data)[payload_start:payload_end]
    max_amplitude = (1 << (bits - 1)) if bits > 8 else 128
    frame_index = 0
    while frame_index < total_frames:
        bucket_end = min(frame_index + bucket_frames, total_frames)
        peak = 0
        # We only sample the first channel (mono peak) — matches how a
        # scrubber preview visualizes the waveform without inflating the
        # peak array by channel count.
        f = frame_index
        while f < bucket_end:
            sample_start = f * frame_stride
            if bits == 8:
                # unsigned 8-bit, centred at 128
                value = abs(view[sample_start] - 128)
            elif bits == 16:
                value = abs(
                    int.from_bytes(view[sample_start:sample_start + 2], "little", signed=True)
                )
            elif bits == 24:
                value = abs(
                    int.from_bytes(view[sample_start:sample_start + 3], "little", signed=True)
                )
            else:  # 32
                value = abs(
                    int.from_bytes(view[sample_start:sample_start + 4], "little", signed=True)
                )
            if value > peak:
                peak = value
            f += 1
        normalized = min(255, int(peak * 255 / max_amplitude))
        peaks.append(normalized)
        frame_index = bucket_end
    return peaks


# ---------------------------------------------------------------------------
# MP3 (MPEG-1/2 audio, with optional ID3v2 prefix and ID3v1 suffix)
# ---------------------------------------------------------------------------
#
# Playback is a sequence of MPEG audio frames. Metadata surfaces are ID3v2
# at the front (variable-length) and ID3v1 at the back (fixed 128 bytes).
# We strip both and then walk MPEG frame headers to prove the remaining
# bytes are a real frame stream — three consecutive well-formed frames is
# our minimum structural bar (single-sync validation lets random noise
# masquerade as an mp3).

_ID3V2_MAGIC: Final = b"ID3"
_ID3V1_MAGIC: Final = b"TAG"
_APE_MAGIC: Final = b"APETAGEX"
_APE_HEADER_FOOTER_LEN: Final = 32
# APEv2 flag bit indices per the official spec.
#   Bit 31 = "this preamble is a header" (0 = footer)
#   Bit 30 = "the tag has no footer"
#   Bit 29 = "the tag has a header preceding the items"
# Round-3 review sweep caught these were swapped, letting a footer-shaped
# preamble with IS_HEADER set slip through the "is-a-header" refusal.
_APE_FLAG_IS_HEADER: Final = 1 << 31
_APE_FLAG_NO_FOOTER: Final = 1 << 30
_APE_FLAG_HAS_HEADER: Final = 1 << 29

# Bitrate table for MPEG Version 1 Layer III (kbps). Values are per second;
# a frame's byte length is derived from bitrate + sample rate.
_MP3_BITRATE_V1_L3: Final = (
    0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0,
)
_MP3_BITRATE_V2_L3: Final = (
    0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0,
)
_MP3_SAMPLE_RATE_V1: Final = (44100, 48000, 32000, 0)
_MP3_SAMPLE_RATE_V2: Final = (22050, 24000, 16000, 0)
_MP3_SAMPLE_RATE_V25: Final = (11025, 12000, 8000, 0)


def _scrub_mp3(data: bytes) -> MediaScrubResult:
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
    # APEv2 tags can sit at the START (as a header) OR at the END (as a
    # footer, with an optional matching header). Both variants carry the
    # same "APETAGEX" preamble; footer flags identify layout. Strip both.
    start = _mp3_strip_ape_header(data, start, end)
    if end - start >= 128 and data[end - 128:end - 125] == _ID3V1_MAGIC:
        end -= 128
    end = _mp3_strip_ape_footer(data, start, end)
    if end - start < 4:
        raise MediaScrubError("mp3 has no audio frames after tag strip")

    _mp3_require_frames(data, start, end, minimum=3)
    return MediaScrubResult(
        data=bytes(data[start:end]),
        mime="audio/mpeg",
        duration_ms=None,
        width=None,
        height=None,
    )


# Cap item_count and tag_size at reasonable ceilings. Real APE tags never
# get anywhere near these — a 4-billion-item claim (uint32 max) is either
# a bug or a hostile fixture, and we refuse to trust it.
_APE_ITEM_COUNT_MAX: Final = 0xFFFF
_APE_TAG_SIZE_MAX: Final = 32 * 1024 * 1024  # 32 MB is already implausible


def _mp3_read_ape_meta(data: bytes, preamble_at: int) -> tuple[int, int, int]:
    """Read tag_size, item_count, flags from an APETAGEX preamble.

    Enforces every derived bound before returning: preamble fits, size
    fields fall inside plausible ceilings, integer arithmetic on tag_size
    cannot overflow later. Any deviation → refuse to parse (the caller
    turns that into a whole-file rejection — silent pass-through was the
    round-1/2/3 recurrence and we do not repeat it).
    """
    if preamble_at < 0 or preamble_at + _APE_HEADER_FOOTER_LEN > len(data):
        raise MediaScrubError("mp3 APEv2 preamble out of bounds")
    tag_size, item_count, flags = struct.unpack(
        "<III", data[preamble_at + 12:preamble_at + 24],
    )
    if tag_size < _APE_HEADER_FOOTER_LEN:
        # Real APEv2 tag_size always includes at least the 32-byte footer
        # even when the tag holds zero items. Values 0..31 are impossible
        # for a valid tag and would leak metadata bytes if honored.
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
    """If APEv2 sits at the front as a header, advance `start` past it."""
    if end - start < _APE_HEADER_FOOTER_LEN:
        return start
    if data[start:start + 8] != _APE_MAGIC:
        return start
    tag_size, _item_count, flags = _mp3_read_ape_meta(data, start)
    if flags & _APE_FLAG_IS_HEADER == 0:
        # A footer masquerading at position 0 — rare but possible; refuse.
        raise MediaScrubError("mp3 APEv2 marker at start is not a header")
    # Header tag_size includes the items + optional footer but NOT the
    # 32-byte header we already sit on. Advance past both.
    advance = _APE_HEADER_FOOTER_LEN + tag_size
    if advance > end - start:
        raise MediaScrubError("mp3 APEv2 header size larger than payload")
    return start + advance


def _mp3_strip_ape_footer(data: bytes, start: int, end: int) -> int:
    """If APEv2 sits at the tail (with or without a matching header),
    walk backwards past the footer AND any preceding header."""
    if end - start < _APE_HEADER_FOOTER_LEN:
        return end
    footer_start = end - _APE_HEADER_FOOTER_LEN
    if data[footer_start:footer_start + 8] != _APE_MAGIC:
        return end
    tag_size, _item_count, flags = _mp3_read_ape_meta(data, footer_start)
    if flags & _APE_FLAG_IS_HEADER:
        # This is a header, not a footer — bail (would be corrupt at end).
        raise MediaScrubError("mp3 APEv2 marker at end is a header")
    # Footer tag_size includes the footer's own 32 bytes and every item.
    # If a matching header is announced, subtract another 32 for that.
    trim = tag_size
    if flags & _APE_FLAG_HAS_HEADER:
        trim += _APE_HEADER_FOOTER_LEN
    if trim > end - start:
        raise MediaScrubError("mp3 APEv2 footer size larger than payload")
    return end - trim


def _mp3_require_frames(data: bytes, start: int, end: int, *, minimum: int) -> None:
    """Walk N consecutive MPEG frames from `start`; raise if we can't find them."""
    offset = start
    for _ in range(minimum):
        frame_len = _mp3_frame_length(data, offset, end)
        if frame_len is None:
            raise MediaScrubError(
                f"mp3 frame stream broken at offset {offset - start}"
            )
        offset += frame_len
        if offset > end:
            raise MediaScrubError("mp3 frame stream truncated")


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
    if version_bits == 3:  # MPEG-1
        bitrate = _MP3_BITRATE_V1_L3[bitrate_bits] * 1000
        sample_rate = _MP3_SAMPLE_RATE_V1[sample_rate_bits]
        samples_per_frame = 1152
    elif version_bits == 2:  # MPEG-2
        bitrate = _MP3_BITRATE_V2_L3[bitrate_bits] * 1000
        sample_rate = _MP3_SAMPLE_RATE_V2[sample_rate_bits]
        samples_per_frame = 576
    else:  # MPEG-2.5
        bitrate = _MP3_BITRATE_V2_L3[bitrate_bits] * 1000
        sample_rate = _MP3_SAMPLE_RATE_V25[sample_rate_bits]
        samples_per_frame = 576
    if bitrate == 0 or sample_rate == 0:
        return None
    frame_length = (samples_per_frame // 8 * bitrate) // sample_rate + padding
    if frame_length < 4:
        return None
    return frame_length
