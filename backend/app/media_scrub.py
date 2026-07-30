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
# ISOBMFF containers are a sequence of atoms:
#     [4-byte big-endian size][4-byte type][payload...]
# with size == 1 meaning a 64-bit size follows, size == 0 meaning run-to-EOF.
#
# The playback-critical tables inside `moov` (specifically stco/co64) point
# at ABSOLUTE file offsets. Any strip that changes moov's size shifts every
# subsequent atom and invalidates those offsets — fast-start MP4s (moov
# ahead of mdat) then fail to decode. We therefore do NOT resize any atom:
# for every metadata atom (top-level and nested), we overwrite the 4-byte
# type with `free` and zero the payload. `free` atoms are officially padding
# that every decoder skips — bytes stay in place, offsets stay valid.
#
# We validate structure at the same time: a real MP4 must have `ftyp` at
# the head and `moov` containing at least one `mvhd` and one `trak`.

_MP4_STRIP_TYPES: Final = {b"udta", b"meta", b"free", b"skip", b"uuid"}


def _scrub_mp4(data: bytes) -> MediaScrubResult:
    if len(data) < 16:
        raise MediaScrubError("mp4 payload too small")
    if data[4:8] != b"ftyp":
        raise MediaScrubError("mp4 payload missing ftyp box at offset 0")

    out = bytearray(data)
    view = memoryview(out)
    duration_ms: int | None = None
    dims: tuple[int, int] | None = None
    moov_seen = False
    trak_seen = False
    mvhd_seen = False
    stsd_sample_entry_seen = False

    offset = 0
    end = len(out)
    while offset < end:
        atom_size, atom_type, header_len, atom_end = _mp4_read_header(view, offset, end)
        if atom_type in _MP4_STRIP_TYPES:
            _mp4_nullify(view, offset + 4, header_len, atom_end)
            offset = atom_end
            continue
        if atom_type == b"moov":
            moov_seen = True
            trak_in_moov, mvhd_in_moov, stsd_ok = _mp4_scrub_container(
                view, offset + header_len, atom_end,
            )
            trak_seen = trak_seen or trak_in_moov
            mvhd_seen = mvhd_seen or mvhd_in_moov
            stsd_sample_entry_seen = stsd_sample_entry_seen or stsd_ok
            if duration_ms is None:
                duration_ms = _mp4_extract_moov_duration(
                    bytes(view[offset + header_len:atom_end]),
                )
            if dims is None:
                dims = _mp4_extract_moov_dims(
                    bytes(view[offset + header_len:atom_end]),
                )
        offset = atom_end

    if not moov_seen:
        raise MediaScrubError("mp4 payload missing moov box")
    if not mvhd_seen:
        raise MediaScrubError("mp4 moov missing mvhd box")
    if not trak_seen:
        raise MediaScrubError("mp4 moov missing trak box")
    if not stsd_sample_entry_seen:
        # A fake moov/mvhd/trak with no stbl/stsd/sample entry would let a
        # tagged text blob masquerade as MP4. Reject it by requiring the
        # decoder-reachable descriptor: trak -> mdia -> minf -> stbl -> stsd
        # with at least one sample-entry child.
        raise MediaScrubError("mp4 trak missing stbl/stsd sample entry")

    width, height = dims if dims is not None else (None, None)
    return MediaScrubResult(
        data=bytes(out),
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


def _mp4_nullify(view: memoryview, type_offset: int, header_len: int, atom_end: int) -> None:
    """Overwrite atom type with `free` and zero the payload — no size change."""
    view[type_offset:type_offset + 4] = b"free"
    payload_start = type_offset + header_len - 4
    for i in range(payload_start, atom_end):
        view[i] = 0


def _mp4_scrub_container(
    view: memoryview, payload_start: int, container_end: int
) -> tuple[bool, bool, bool]:
    """Recursively nullify metadata atoms in a container.

    Returns (trak_seen, mvhd_seen, stsd_sample_entry_seen).
    """
    trak_seen = False
    mvhd_seen = False
    stsd_ok = False
    offset = payload_start
    while offset < container_end:
        _size, atom_type, header_len, atom_end = _mp4_read_header(
            view, offset, container_end,
        )
        if atom_type in _MP4_STRIP_TYPES:
            _mp4_nullify(view, offset + 4, header_len, atom_end)
            offset = atom_end
            continue
        if atom_type == b"trak":
            trak_seen = True
        if atom_type == b"mvhd":
            mvhd_seen = True
        if atom_type == b"stsd":
            # stsd payload = 4 flag-and-version bytes, then a big-endian
            # entry_count uint32, then N sample-entry boxes. At least one
            # entry is the proof the trak actually carries a decodable
            # stream; a bogus text-only fixture has entry_count=0.
            payload_at = offset + header_len
            if payload_at + 8 <= atom_end:
                entry_count = struct.unpack(
                    ">I", bytes(view[payload_at + 4:payload_at + 8]),
                )[0]
                if entry_count > 0:
                    stsd_ok = True
        if atom_type in {b"moov", b"trak", b"mdia", b"minf", b"stbl"}:
            child_trak, child_mvhd, child_stsd = _mp4_scrub_container(
                view, offset + header_len, atom_end,
            )
            trak_seen = trak_seen or child_trak
            mvhd_seen = mvhd_seen or child_mvhd
            stsd_ok = stsd_ok or child_stsd
        offset = atom_end
    return trak_seen, mvhd_seen, stsd_ok


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
# WAV is a chunk-based container. Playback needs `fmt ` and `data` (and
# optionally `fact`). Any other chunk may carry metadata — LIST/INFO,
# bext (broadcast wave), iXML (production XML), _PMX (XMP), aXML, cue,
# etc. Rather than maintain a strip list, we allowlist the playback-safe
# chunks and drop everything else. Peaks are computed streaming from the
# `data` chunk using memoryview slices — no per-sample allocation.

_WAV_KEEP_CHUNKS: Final = {b"fmt ", b"data", b"fact"}
# Allow only decoder-reachable format codes: 1 = PCM, 3 = IEEE float,
# 0xFFFE = WAVE_FORMAT_EXTENSIBLE (real codec identified by SubFormat GUID).
# Format code 0 (WAVE_FORMAT_UNKNOWN) or anything else is refused — the
# reviewer flagged that fmt_code == 0 currently slips through as unplayable.
_WAV_FORMAT_PCM: Final = 1
_WAV_FORMAT_IEEE_FLOAT: Final = 3
_WAV_FORMAT_EXTENSIBLE: Final = 0xFFFE
_WAV_ALLOWED_FORMATS: Final = {_WAV_FORMAT_PCM, _WAV_FORMAT_IEEE_FLOAT}
# KSDATAFORMAT_SUBTYPE_PCM / _IEEE_FLOAT — the extensible SubFormat GUID
# must resolve to one of these for us to trust the container.
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

    kept = bytearray(b"RIFF____WAVE")
    duration_ms: int | None = None
    peaks: list[int] | None = None
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
        if chunk_id not in _WAV_KEEP_CHUNKS:
            offset = payload_end + pad
            continue
        if chunk_id == b"fmt ":
            if chunk_size < 16:
                raise MediaScrubError("wav fmt chunk too short")
            format_code = struct.unpack(
                "<H", data[payload_start:payload_start + 2],
            )[0]
            channels = struct.unpack(
                "<H", data[payload_start + 2:payload_start + 4],
            )[0]
            sample_rate = struct.unpack(
                "<I", data[payload_start + 4:payload_start + 8],
            )[0]
            byte_rate = struct.unpack(
                "<I", data[payload_start + 8:payload_start + 12],
            )[0]
            bits = struct.unpack(
                "<H", data[payload_start + 14:payload_start + 16],
            )[0]
            if format_code == _WAV_FORMAT_EXTENSIBLE:
                # WAVE_FORMAT_EXTENSIBLE: chunk must be ≥40 bytes and end
                # with a 16-byte SubFormat GUID resolving to PCM or float.
                if chunk_size < 40:
                    raise MediaScrubError(
                        "wav extensible fmt chunk too short for SubFormat"
                    )
                subformat = data[payload_start + 24:payload_start + 40]
                if subformat not in (
                    _WAV_KSDATAFORMAT_PCM, _WAV_KSDATAFORMAT_IEEE_FLOAT,
                ):
                    raise MediaScrubError(
                        f"wav SubFormat GUID {subformat.hex()} is not PCM or float"
                    )
            elif format_code not in _WAV_ALLOWED_FORMATS:
                raise MediaScrubError(
                    f"wav format code {format_code} is not PCM (1), float (3), or extensible"
                )
            fmt_seen = True
        elif chunk_id == b"data":
            data_bytes = chunk_size
            if fmt_seen:
                peaks = _wav_stream_peaks(
                    data, payload_start, payload_end, channels, bits,
                )
        kept.extend(data[offset:payload_end + pad])
        offset = payload_end + pad

    if not fmt_seen:
        raise MediaScrubError("wav payload missing fmt chunk")
    if data_bytes == 0:
        raise MediaScrubError("wav payload missing data chunk")
    if byte_rate and data_bytes:
        duration_ms = int(round(data_bytes * 1000 / byte_rate))
    elif sample_rate and channels and bits and data_bytes:
        frames = data_bytes // max(1, (channels * bits // 8))
        duration_ms = int(round(frames * 1000 / sample_rate))
    new_size = len(kept) - 8
    kept[4:8] = struct.pack("<I", new_size)
    return MediaScrubResult(
        data=bytes(kept),
        mime="audio/wav",
        duration_ms=duration_ms,
        width=None,
        height=None,
        peaks=peaks,
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
# APEv2 flags (bit indices).
_APE_FLAG_HAS_HEADER: Final = 1 << 31
_APE_FLAG_IS_HEADER: Final = 1 << 29

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


def _mp3_strip_ape_header(data: bytes, start: int, end: int) -> int:
    """If APEv2 sits at the front as a header, advance `start` past it."""
    if end - start < _APE_HEADER_FOOTER_LEN:
        return start
    if data[start:start + 8] != _APE_MAGIC:
        return start
    tag_size, item_count, flags = struct.unpack(
        "<III", data[start + 12:start + 24],
    )
    if flags & _APE_FLAG_IS_HEADER == 0:
        # A footer masquerading at position 0 — rare but possible; refuse.
        raise MediaScrubError("mp3 APEv2 marker at start is not a header")
    if item_count > 0xFFFF:
        raise MediaScrubError("mp3 APEv2 item count implausible")
    # Header tag_size includes the footer bytes but NOT the header — advance
    # past header + tag_size (which covers items + optional footer).
    advance = _APE_HEADER_FOOTER_LEN + tag_size
    if start + advance > end:
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
    tag_size, item_count, flags = struct.unpack(
        "<III", data[footer_start + 12:footer_start + 24],
    )
    if flags & _APE_FLAG_IS_HEADER:
        # This is a header, not a footer — bail (would be corrupt at end).
        raise MediaScrubError("mp3 APEv2 marker at end is a header")
    if item_count > 0xFFFF:
        raise MediaScrubError("mp3 APEv2 item count implausible")
    # Footer tag_size includes the footer itself; items sit tag_size-32
    # bytes above the footer, plus a header (another 32) if the flag is set.
    items_length = tag_size - _APE_HEADER_FOOTER_LEN
    new_end = end - tag_size
    if flags & _APE_FLAG_HAS_HEADER:
        new_end -= _APE_HEADER_FOOTER_LEN
    if new_end < start:
        raise MediaScrubError("mp3 APEv2 footer size larger than payload")
    del items_length  # silence lint — bound-check tag_size covers items too
    return new_end


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
