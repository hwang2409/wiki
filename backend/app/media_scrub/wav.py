"""WAV (RIFF/WAVE) scrubbing — full structural reconstruction.

Round 6 completes the rebuild-not-copy principle: fmt is emitted with a
struct.pack'd layout containing exactly the parsed fields (nothing else),
and fact is emitted from its single parsed uint32. Trailing bytes inside
a PCM fmt chunk cannot appear in the output because we only pack the
16 bytes the PCM spec allows. Same for extensible: exactly 40 bytes.
"""
from __future__ import annotations

import math
import struct
from typing import Final

from .base import MediaScrubError, MediaScrubResult, WAVEFORM_MAX_PEAKS

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

# Round-10 review: fmt cross-field consistency lockdown. WAVE_FORMAT_PCM
# containers are byte-aligned at 8/16/24/32 bits; IEEE float is 32 or 64.
# block_align must equal channels * (bits/8) and byte_rate must equal
# sample_rate * block_align; the data chunk must contain an integer
# number of frames. Pre-R10 the rebuild trusted the byte_rate field, so a
# mutation to byte_rate=1 caused a half-second fixture to report a
# duration of 16000000ms via the len(data) * 1000 / byte_rate formula.
_WAV_PCM_ALLOWED_BITS: Final = frozenset({8, 16, 24, 32})
_WAV_FLOAT_ALLOWED_BITS: Final = frozenset({32, 64})


def scrub_wav(data: bytes) -> MediaScrubResult:
    if len(data) < 12:
        raise MediaScrubError("wav payload too small")
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise MediaScrubError("wav payload missing RIFF/WAVE header")

    declared_riff_size = struct.unpack("<I", data[4:8])[0]
    if declared_riff_size + 8 != len(data):
        raise MediaScrubError(
            f"wav RIFF size {declared_riff_size} + 8 does not match payload length {len(data)}"
        )

    rebuilt_fmt: bytes | None = None
    data_payload: bytes | None = None
    rebuilt_fact: bytes | None = None
    fmt_channels = 0
    fmt_sample_rate = 0
    fmt_byte_rate = 0
    fmt_bits = 0
    fmt_block_align = 0
    fmt_format_code = _WAV_FORMAT_PCM

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
            raise MediaScrubError("wav odd chunk missing pad byte inside RIFF")
        payload = data[payload_start:payload_end]
        if chunk_id == b"fmt ":
            if rebuilt_fmt is not None:
                raise MediaScrubError("wav duplicate fmt chunk")
            (rebuilt_fmt, fmt_channels, fmt_sample_rate,
             fmt_byte_rate, fmt_bits, fmt_block_align,
             fmt_format_code) = _wav_rebuild_fmt(payload)
        elif chunk_id == b"data":
            if data_payload is not None:
                raise MediaScrubError("wav duplicate data chunk")
            data_payload = payload
        elif chunk_id == b"fact":
            if rebuilt_fact is not None:
                raise MediaScrubError("wav duplicate fact chunk")
            rebuilt_fact = _wav_rebuild_fact(payload)
        # All other chunks are dropped.
        offset = payload_end + pad

    if rebuilt_fmt is None:
        raise MediaScrubError("wav payload missing fmt chunk")
    if data_payload is None or len(data_payload) == 0:
        raise MediaScrubError("wav payload missing data chunk")

    data_bytes = len(data_payload)
    # Round-10 review: reject data chunks whose length is not an integer
    # number of frames. A misaligned data chunk under any format is a
    # decoder-visible corruption that the scrubber must not preserve.
    if fmt_block_align <= 0 or data_bytes % fmt_block_align != 0:
        raise MediaScrubError(
            f"wav data chunk length {data_bytes} is not aligned to "
            f"block_align {fmt_block_align}"
        )
    frames = data_bytes // fmt_block_align
    # After R10 fmt cross-field validation, byte_rate == sample_rate *
    # block_align exactly, so both duration formulas below are equivalent.
    # Use the frames-based formula (it survives a future refactor where we
    # drop the mandatory byte_rate emission).
    duration_ms = int(round(frames * 1000 / fmt_sample_rate)) if frames else 0
    peaks = _wav_stream_peaks(
        data_payload, 0, len(data_payload), fmt_channels, fmt_bits,
        fmt_format_code,
    )

    body = bytearray(b"WAVE")
    _wav_emit_chunk(body, b"fmt ", rebuilt_fmt)
    if rebuilt_fact is not None:
        _wav_emit_chunk(body, b"fact", rebuilt_fact)
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


def _wav_rebuild_fmt(payload: bytes) -> tuple[bytes, int, int, int, int, int, int]:
    """Parse and rebuild fmt from validated fields only.

    Every byte in the returned fmt chunk is either a struct.pack of a
    validated field or (for the extensible SubFormat GUID) a validated
    16-byte allowlisted constant. Trailing bytes past the fields the spec
    defines cannot survive because they are not written.
    """
    if len(payload) < 16:
        raise MediaScrubError("wav fmt chunk too short")
    format_code = struct.unpack("<H", payload[:2])[0]
    channels = struct.unpack("<H", payload[2:4])[0]
    sample_rate = struct.unpack("<I", payload[4:8])[0]
    byte_rate = struct.unpack("<I", payload[8:12])[0]
    block_align = struct.unpack("<H", payload[12:14])[0]
    bits = struct.unpack("<H", payload[14:16])[0]

    if channels == 0 or sample_rate == 0 or bits == 0:
        raise MediaScrubError("wav fmt fields include zero channels/sample_rate/bits")
    if channels > 0xFFFF or sample_rate > 0xFFFFFFFF:
        # struct.unpack already caps channels at 16 bits and sample_rate at 32
        # bits, but keep a defensive guard for future refactors.
        raise MediaScrubError("wav fmt channels or sample_rate exceed field width")

    if format_code == _WAV_FORMAT_EXTENSIBLE:
        if len(payload) < 40:
            raise MediaScrubError("wav extensible fmt chunk too short for SubFormat")
        cb_size = struct.unpack("<H", payload[16:18])[0]
        if cb_size < 22:
            raise MediaScrubError(
                f"wav extensible cbSize {cb_size} smaller than the 22-byte extension"
            )
        if 18 + cb_size > len(payload):
            raise MediaScrubError("wav extensible extension extends past fmt chunk")
        valid_bits = struct.unpack("<H", payload[18:20])[0]
        channel_mask = struct.unpack("<I", payload[20:24])[0]
        subformat = payload[24:40]
        if subformat == _WAV_KSDATAFORMAT_PCM:
            allowed_bits = _WAV_PCM_ALLOWED_BITS
        elif subformat == _WAV_KSDATAFORMAT_IEEE_FLOAT:
            allowed_bits = _WAV_FLOAT_ALLOWED_BITS
        else:
            raise MediaScrubError(
                f"wav SubFormat GUID {subformat.hex()} is not PCM or float"
            )
        _wav_validate_bit_depth_and_alignment(
            bits, allowed_bits, channels, block_align, byte_rate, sample_rate,
            format_label=f"extensible/{subformat.hex()[:8]}",
        )
        if valid_bits == 0 or valid_bits > bits:
            raise MediaScrubError(
                f"wav extensible valid_bits {valid_bits} out of range for container bits {bits}"
            )
        rebuilt = (
            struct.pack("<HHIIHH", format_code, channels, sample_rate, byte_rate, block_align, bits)
            + struct.pack("<HHI", 22, valid_bits, channel_mask)
            + subformat
        )
        effective_format = (
            _WAV_FORMAT_IEEE_FLOAT
            if subformat == _WAV_KSDATAFORMAT_IEEE_FLOAT
            else _WAV_FORMAT_PCM
        )
        return rebuilt, channels, sample_rate, byte_rate, bits, block_align, effective_format

    if format_code == _WAV_FORMAT_PCM:
        allowed_bits = _WAV_PCM_ALLOWED_BITS
    elif format_code == _WAV_FORMAT_IEEE_FLOAT:
        allowed_bits = _WAV_FLOAT_ALLOWED_BITS
    else:
        raise MediaScrubError(
            f"wav format code {format_code} is not PCM (1), float (3), or extensible"
        )
    _wav_validate_bit_depth_and_alignment(
        bits, allowed_bits, channels, block_align, byte_rate, sample_rate,
        format_label={_WAV_FORMAT_PCM: "PCM", _WAV_FORMAT_IEEE_FLOAT: "float"}[format_code],
    )
    # PCM/float layout is exactly 16 bytes. Anything past that in the input
    # was trailing junk (or hostile). Rebuild emits exactly the 16 bytes we
    # validated.
    rebuilt = struct.pack(
        "<HHIIHH", format_code, channels, sample_rate, byte_rate, block_align, bits,
    )
    return rebuilt, channels, sample_rate, byte_rate, bits, block_align, format_code


def _wav_validate_bit_depth_and_alignment(
    bits: int, allowed_bits: frozenset[int],
    channels: int, block_align: int, byte_rate: int, sample_rate: int,
    *, format_label: str,
) -> None:
    if bits not in allowed_bits:
        raise MediaScrubError(
            f"wav {format_label} bit depth {bits} not in allowed set {sorted(allowed_bits)}"
        )
    expected_block_align = channels * (bits // 8)
    if block_align != expected_block_align:
        raise MediaScrubError(
            f"wav {format_label} block_align {block_align} != channels*bytes-per-sample "
            f"{expected_block_align} (channels={channels}, bits={bits})"
        )
    expected_byte_rate = sample_rate * block_align
    if byte_rate != expected_byte_rate:
        raise MediaScrubError(
            f"wav {format_label} byte_rate {byte_rate} != sample_rate*block_align "
            f"{expected_byte_rate} (sample_rate={sample_rate}, block_align={block_align})"
        )


def _wav_rebuild_fact(payload: bytes) -> bytes:
    """fact chunk is exactly 4 bytes: dwSampleLength (uint32).

    Anything else that fmt chunks bundled in the input's fact chunk was
    never spec-legal; rebuilt output emits exactly the validated 4-byte
    sample length. Trailing bytes cannot survive because they are not
    written.
    """
    if len(payload) < 4:
        raise MediaScrubError("wav fact chunk too short")
    sample_length = struct.unpack("<I", payload[:4])[0]
    return struct.pack("<I", sample_length)


def _wav_stream_peaks(
    data: bytes,
    payload_start: int,
    payload_end: int,
    channels: int,
    bits: int,
    format_code: int = _WAV_FORMAT_PCM,
) -> list[int] | None:
    if channels <= 0:
        return None
    if format_code == _WAV_FORMAT_IEEE_FLOAT:
        if bits not in _WAV_FLOAT_ALLOWED_BITS:
            return None
    elif bits not in (8, 16, 24, 32):
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
    max_amplitude = 1.0 if format_code == _WAV_FORMAT_IEEE_FLOAT else ((1 << (bits - 1)) if bits > 8 else 128)
    frame_index = 0
    while frame_index < total_frames:
        bucket_end = min(frame_index + bucket_frames, total_frames)
        peak = 0
        f = frame_index
        while f < bucket_end:
            frame_start = f * frame_stride
            for channel in range(channels):
                sample_start = frame_start + channel * bytes_per_sample
                if format_code == _WAV_FORMAT_IEEE_FLOAT:
                    unpack_format = "<f" if bits == 32 else "<d"
                    value = struct.unpack_from(unpack_format, view, sample_start)[0]
                    if not math.isfinite(value):
                        raise MediaScrubError("wav float sample is not finite")
                    value = abs(value)
                elif bits == 8:
                    value = abs(view[sample_start] - 128)
                elif bits == 16:
                    value = abs(int.from_bytes(view[sample_start:sample_start + 2], "little", signed=True))
                elif bits == 24:
                    value = abs(int.from_bytes(view[sample_start:sample_start + 3], "little", signed=True))
                else:  # 32-bit PCM
                    value = abs(int.from_bytes(view[sample_start:sample_start + 4], "little", signed=True))
                if value > peak:
                    peak = value
            f += 1
        normalized = min(255, int(peak * 255 / max_amplitude))
        peaks.append(normalized)
        frame_index = bucket_end
    return peaks
