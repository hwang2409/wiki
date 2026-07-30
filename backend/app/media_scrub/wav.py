"""WAV (RIFF/WAVE) scrubbing — full structural reconstruction.

Round 6 completes the rebuild-not-copy principle: fmt is emitted with a
struct.pack'd layout containing exactly the parsed fields (nothing else),
and fact is emitted from its single parsed uint32. Trailing bytes inside
a PCM fmt chunk cannot appear in the output because we only pack the
16 bytes the PCM spec allows. Same for extensible: exactly 40 bytes.
"""
from __future__ import annotations

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
            rebuilt_fmt, fmt_channels, fmt_sample_rate, fmt_byte_rate, fmt_bits = (
                _wav_rebuild_fmt(payload)
            )
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
    if fmt_byte_rate:
        duration_ms = int(round(data_bytes * 1000 / fmt_byte_rate))
    elif fmt_sample_rate and fmt_channels and fmt_bits:
        frames = data_bytes // max(1, (fmt_channels * fmt_bits // 8))
        duration_ms = int(round(frames * 1000 / fmt_sample_rate)) if frames else 0
    else:
        duration_ms = None
    peaks = _wav_stream_peaks(
        data_payload, 0, len(data_payload), fmt_channels, fmt_bits,
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


def _wav_rebuild_fmt(payload: bytes) -> tuple[bytes, int, int, int, int]:
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
        if subformat not in (_WAV_KSDATAFORMAT_PCM, _WAV_KSDATAFORMAT_IEEE_FLOAT):
            raise MediaScrubError(
                f"wav SubFormat GUID {subformat.hex()} is not PCM or float"
            )
        rebuilt = (
            struct.pack("<HHIIHH", format_code, channels, sample_rate, byte_rate, block_align, bits)
            + struct.pack("<HHI", 22, valid_bits, channel_mask)
            + subformat
        )
        return rebuilt, channels, sample_rate, byte_rate, bits

    if format_code not in _WAV_ALLOWED_FORMATS:
        raise MediaScrubError(
            f"wav format code {format_code} is not PCM (1), float (3), or extensible"
        )
    # PCM/float layout is exactly 16 bytes. Anything past that in the input
    # was trailing junk (or hostile). Rebuild emits exactly the 16 bytes we
    # validated.
    rebuilt = struct.pack(
        "<HHIIHH", format_code, channels, sample_rate, byte_rate, block_align, bits,
    )
    return rebuilt, channels, sample_rate, byte_rate, bits


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
) -> list[int] | None:
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
        f = frame_index
        while f < bucket_end:
            sample_start = f * frame_stride
            if bits == 8:
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
