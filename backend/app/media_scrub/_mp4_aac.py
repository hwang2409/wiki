"""AAC-LC sample-description and raw_data_block validation for MP4.

Strict-subset acceptance rules (WIKI-225, learning from WIKI-190 R21-R23):

    Sample entry (mp4a) — every byte in the stored output is emitted by
    ``_rebuild_audio_sample_entry_fixed`` or the field-level esds
    rebuilder. The 20-byte audio sample entry header is rebuilt from
    parsed integer fields (channel_count / sample_size / sample_rate);
    the esds fullbox is rebuilt as a canonical ES/DecoderConfig/DSI
    triplet where the DecoderSpecificInfo body is the 2-byte AAC-LC
    AudioSpecificConfig (no optional extension bits, no descriptor
    slack). AudioSpecificConfig is validated: AudioObjectType MUST be
    2 (AAC-LC), samplingFrequencyIndex 0..12, channelConfiguration in
    {1, 2}. Explicit ASC frequencies, SBR/PS extensions, and non-zero
    GA-specific-config flags are rejected — the strict subset does not
    trust an implicit decoder to interpret them.

    Sample data (raw_data_block) — for every sample we validate that
    the bit stream envelope has the shape

        <channel element> (SCE for mono, CPE for stereo)
                                                    // one channel elt
        <opaque spectral data>                     // codec-defined
        <ID_END (3 bits = 0b111)>
        <byte-alignment padding (0..7 zero bits)>

    Detection is byte-aligned only — no per-bit iteration over the
    sample body (WIKI-190 R23 CPU hole). We check:

        1. The first 3 bits (top of byte 0) are the channel element id
           matching channelConfiguration.
        2. The last non-zero byte, after stripping its trailing zero
           bits (the byte_alignment padding), ends with 0b111 (ID_END).
        3. All bytes past the ID_END bit are zero (padding).

    Files whose leading syntactic element is FIL / DSE / PCE therefore
    reject immediately — those are the vectors that carry encoder
    identity strings (``Lavc``-style), arbitrary data-stream bytes, or
    program-config comments. Between the channel element header and
    ID_END the sample body is opaque codec spectral data; we return it
    byte-for-byte because bit-level rewriting is not feasible without a
    full AAC-LC decoder (and inventing one is what tripped R21-R23).
    Callers that need stronger interior validation must supply a file
    whose channel element is followed only by ID_END; the shape above
    is exactly what ``ffmpeg -fflags +bitexact -flags:a +bitexact``
    produces for AAC-LC mono/stereo.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

from .base import MediaScrubError
from ._mp4_primitives import pack as _pack


_AAC_MAX_ASC_BYTES: Final = 64
_AAC_ID_SCE: Final = 0
_AAC_ID_CPE: Final = 1
_AAC_ID_LFE: Final = 3
_AAC_ID_DSE: Final = 4
_AAC_ID_PCE: Final = 5
_AAC_ID_FIL: Final = 6
_AAC_ID_END: Final = 7
_AAC_MAX_SAMPLE_BYTES: Final = 1 << 20  # 1 MB per sample cap — well past AAC-LC frame limits


@dataclass(frozen=True)
class Mp4AacConfig:
    sampling_index: int
    channel_configuration: int


_Mp4AacConfig = Mp4AacConfig


# ---------------------------------------------------------------------------
# MP4 descriptor primitives (used by esds)
# ---------------------------------------------------------------------------


def _read_mp4_descriptor(
    data: bytes, offset: int, end: int,
) -> tuple[int, bytes, int]:
    if offset >= end:
        raise MediaScrubError("mp4 esds descriptor tag is truncated")
    tag = data[offset]
    offset += 1
    length = 0
    for _ in range(4):
        if offset >= end:
            raise MediaScrubError("mp4 esds descriptor length is truncated")
        value = data[offset]
        offset += 1
        length = (length << 7) | (value & 0x7F)
        if not value & 0x80:
            break
    else:
        raise MediaScrubError("mp4 esds descriptor length uses more than 4 bytes")
    descriptor_end = offset + length
    if descriptor_end > end:
        raise MediaScrubError("mp4 esds descriptor extends past body")
    return tag, data[offset:descriptor_end], descriptor_end


def _pack_mp4_descriptor(tag: int, body: bytes) -> bytes:
    if not 0 <= tag <= 0xFF:
        raise MediaScrubError("mp4 esds descriptor tag is outside one byte")
    if len(body) > 0x0FFFFFFF:
        raise MediaScrubError("mp4 esds descriptor length exceeds 4-byte capacity")
    groups = [len(body) & 0x7F]
    remaining = len(body) >> 7
    while remaining:
        groups.append(remaining & 0x7F)
        remaining >>= 7
    length = bytes(
        value | (0x80 if index < len(groups) - 1 else 0)
        for index, value in enumerate(reversed(groups))
    )
    return bytes([tag]) + length + body


# ---------------------------------------------------------------------------
# AudioSpecificConfig — AAC-LC only, 2-byte canonical form
# ---------------------------------------------------------------------------


def _canonical_aac_lc_config(config: bytes) -> tuple[bytes, int, int]:
    """Validate AAC-LC AudioSpecificConfig and emit its canonical 2 bytes.

    The optional extension and padding bits are not needed for AAC-LC.
    Dropping them prevents opaque descriptor slack from reaching output.
    """
    if not 2 <= len(config) <= _AAC_MAX_ASC_BYTES:
        raise MediaScrubError(
            f"mp4 mp4a AudioSpecificConfig length {len(config)} outside 2..{_AAC_MAX_ASC_BYTES}"
        )
    value = int.from_bytes(config, "big")
    bit_count = len(config) * 8
    cursor = bit_count

    def read(width: int) -> int:
        nonlocal cursor
        if cursor < width:
            raise MediaScrubError("mp4 mp4a AudioSpecificConfig is truncated")
        cursor -= width
        return (value >> cursor) & ((1 << width) - 1)

    audio_object_type = read(5)
    if audio_object_type == 31:
        raise MediaScrubError("mp4 mp4a extended AudioObjectType is unsupported")
    frequency_index = read(4)
    if frequency_index == 15:
        raise MediaScrubError(
            "mp4 mp4a explicit AudioSpecificConfig frequency is unsupported"
        )
    channel_configuration = read(4)
    if audio_object_type != 2:
        raise MediaScrubError(
            f"mp4 mp4a AudioObjectType {audio_object_type} is not AAC-LC"
        )
    if frequency_index > 12:
        raise MediaScrubError(
            f"mp4 mp4a samplingFrequencyIndex {frequency_index} outside 0..12"
        )
    if channel_configuration not in (1, 2):
        raise MediaScrubError(
            f"mp4 mp4a channelConfiguration {channel_configuration} outside {{1,2}}"
        )
    # GASpecificConfig for AAC-LC: frameLengthFlag, dependsOnCoreCoder,
    # extensionFlag — all must be zero for the strict subset.
    if read(1) != 0 or read(1) != 0 or read(1) != 0:
        raise MediaScrubError(
            "mp4 mp4a AAC-LC GASpecificConfig flags are unsupported"
        )
    # Trailing bits, if any, carry an implicit backwards-compatible SBR
    # signalling block (syncExtensionType 0x2B7 followed by AOT=5 and
    # sampling-frequency-index). We do NOT emit them in the rebuilt
    # DSI — the canonical output is 2 bytes. Reading them here confirms
    # they are well-formed if present; anything else rejects the file.
    if cursor >= 11:
        sync = read(11)
        if sync != 0x2B7:
            raise MediaScrubError(
                f"mp4 mp4a AudioSpecificConfig has unknown syncExtensionType 0x{sync:x}"
            )
        if cursor < 5:
            raise MediaScrubError(
                "mp4 mp4a AudioSpecificConfig SBR extension too short for AOT"
            )
        ext_aot = read(5)
        if ext_aot != 5:
            raise MediaScrubError(
                f"mp4 mp4a AudioSpecificConfig SBR extension AOT {ext_aot} is not 5 (SBR)"
            )
        # Optional 1-bit sbrPresentFlag, then 4-bit extensionSamplingFrequencyIndex.
        if cursor >= 1:
            sbr_present = read(1)
            if sbr_present:
                if cursor < 4:
                    raise MediaScrubError(
                        "mp4 mp4a SBR extension missing extensionSamplingFrequencyIndex"
                    )
                ext_freq = read(4)
                if ext_freq > 12:
                    raise MediaScrubError(
                        f"mp4 mp4a extensionSamplingFrequencyIndex {ext_freq} outside 0..12"
                    )
    # Any remaining bits must be zero padding — otherwise an unknown
    # extension block is present and we refuse to interpret it.
    while cursor > 0:
        cursor -= 1
        if (value >> cursor) & 1:
            raise MediaScrubError(
                "mp4 mp4a AudioSpecificConfig has non-zero bits past known extensions"
            )
    canonical = (audio_object_type << 11) | (frequency_index << 7) | (channel_configuration << 3)
    return struct.pack(">H", canonical), frequency_index, channel_configuration


def _parse_aac_config_from_esds(body: bytes) -> Mp4AacConfig:
    """Extract an AAC-LC config from an mp4a esds body without rebuilding.

    Used by the sample-plan builder to route sample validation. The
    field-level rebuild (which also destroys descriptor slack) happens
    in the sample-entry walker; both paths share the same underlying
    ``_canonical_aac_lc_config`` so a file that decodes here also
    reconstructs there.
    """
    _flags, _es_id, _decoder_body, config_body = _walk_esds(body)
    _canonical, sampling_index, channel_configuration = _canonical_aac_lc_config(
        config_body,
    )
    return Mp4AacConfig(sampling_index, channel_configuration)


def _walk_esds(body: bytes) -> tuple[bytes, int, bytes, bytes]:
    """Parse esds body → (flags, ES id, DecoderConfig body, DSI body).

    Every descriptor is required to be tightly packed — no trailing
    slack — because a descriptor length field that overreaches would
    let attacker bytes ride along inside the ES/decoder container.
    """
    if len(body) < 4:
        raise MediaScrubError("mp4 esds body too short")
    flags = body[1:4]
    if len(flags) != 3 or int.from_bytes(flags, "big"):
        raise MediaScrubError("mp4 esds fullbox flags must be zero")
    version = body[0]
    if version != 0:
        raise MediaScrubError(f"mp4 esds unknown version {version}")
    tag, es_body, offset = _read_mp4_descriptor(body, 4, len(body))
    if tag != 0x03 or offset != len(body):
        raise MediaScrubError(
            "mp4 esds requires exactly one ES descriptor filling the body"
        )
    if len(es_body) < 3:
        raise MediaScrubError("mp4 esds ES descriptor body too short")
    es_id = struct.unpack(">H", es_body[:2])[0]
    es_flags = es_body[2]
    if es_flags != 0:
        raise MediaScrubError(
            "mp4 esds optional ES descriptor fields (stream dependency / URL / OCR) are unsupported"
        )
    decoder_tag, decoder_body, offset = _read_mp4_descriptor(es_body, 3, len(es_body))
    if decoder_tag != 0x04:
        raise MediaScrubError(
            "mp4 esds ES descriptor requires a DecoderConfigDescriptor"
        )
    sl_tag, sl_body, offset = _read_mp4_descriptor(es_body, offset, len(es_body))
    if sl_tag != 0x06 or offset != len(es_body):
        raise MediaScrubError(
            "mp4 esds requires a trailing SLConfigDescriptor and no extra descriptors"
        )
    if sl_body != b"\x02":
        raise MediaScrubError(
            "mp4 esds SLConfigDescriptor must be the canonical MP4 predefined body (0x02)"
        )
    if len(decoder_body) < 13:
        raise MediaScrubError("mp4 esds DecoderConfigDescriptor body too short")
    object_type = decoder_body[0]
    stream_flags = decoder_body[1]
    if object_type != 0x40:
        raise MediaScrubError(
            f"mp4 esds decoder objectTypeIndication 0x{object_type:02x} is not MPEG-4 audio"
        )
    if stream_flags != 0x15:
        raise MediaScrubError(
            f"mp4 esds decoder streamType/upStream/reserved flags 0x{stream_flags:02x} are not canonical AudioStream"
        )
    config_tag, config_body, config_end = _read_mp4_descriptor(
        decoder_body, 13, len(decoder_body),
    )
    if config_tag != 0x05 or config_end != len(decoder_body):
        raise MediaScrubError(
            "mp4 esds requires exactly one DecoderSpecificInfo filling the DecoderConfigDescriptor"
        )
    return bytes(flags), es_id, decoder_body, config_body


def _rebuild_inner_esds(body: bytes) -> bytes:
    """Rebuild an esds fullbox from parsed descriptor fields only."""
    flags, es_id, decoder_body, config_body = _walk_esds(body)
    buffer_size = decoder_body[2:5]
    max_bitrate = struct.unpack(">I", decoder_body[5:9])[0]
    avg_bitrate = struct.unpack(">I", decoder_body[9:13])[0]
    canonical_config, _freq_index, _channels = _canonical_aac_lc_config(config_body)
    canonical_decoder = (
        b"\x40\x15"
        + buffer_size
        + struct.pack(">II", max_bitrate, avg_bitrate)
        + _pack_mp4_descriptor(0x05, canonical_config)
    )
    canonical_es = (
        struct.pack(">HB", es_id, 0)
        + _pack_mp4_descriptor(0x04, canonical_decoder)
        + _pack_mp4_descriptor(0x06, b"\x02")
    )
    return _pack(b"esds", bytes([0]) + flags + _pack_mp4_descriptor(0x03, canonical_es))


# ---------------------------------------------------------------------------
# Raw data block sample validation
# ---------------------------------------------------------------------------


def _canonicalise_aac_sample(sample: bytes, config: Mp4AacConfig) -> bytes:
    """Validate the raw_data_block envelope and return the sample bytes.

    See the module docstring for the acceptance rules. We do not
    rewrite anything: a well-formed sample passes through byte-for-byte
    (byte-aligned copy), and a malformed one raises.
    """
    if not sample:
        raise MediaScrubError("mp4 AAC sample is empty")
    if len(sample) > _AAC_MAX_SAMPLE_BYTES:
        raise MediaScrubError(
            f"mp4 AAC sample size {len(sample)} exceeds scrubber cap {_AAC_MAX_SAMPLE_BYTES}"
        )
    expected_leading = _AAC_ID_SCE if config.channel_configuration == 1 else _AAC_ID_CPE
    leading = sample[0] >> 5
    if leading != expected_leading:
        raise MediaScrubError(
            f"mp4 AAC raw_data_block first element id {leading} does not match "
            f"channelConfiguration {config.channel_configuration} "
            f"(expected id {expected_leading})"
        )
    # Locate the last non-zero byte (byte_alignment padding is all zeros).
    tail_index = len(sample) - 1
    while tail_index >= 0 and sample[tail_index] == 0:
        tail_index -= 1
    if tail_index < 0:
        raise MediaScrubError("mp4 AAC sample body is entirely zero")
    last_byte = sample[tail_index]
    # Strip bit-level trailing zeros inside the last non-zero byte —
    # the low bits of `last_byte` are padding, the higher bits carry
    # ID_END. Because last_byte != 0, this loop terminates.
    trailing_bit_zeros = 0
    scratch = last_byte
    while not (scratch & 1):
        scratch >>= 1
        trailing_bit_zeros += 1
    significant_bit_len = (tail_index + 1) * 8 - trailing_bit_zeros
    if significant_bit_len < 3:
        raise MediaScrubError("mp4 AAC raw_data_block cannot fit an ID_END marker")
    total_bits = len(sample) * 8
    # Compare the three bits immediately preceding the padding to ID_END.
    end_bit_start = significant_bit_len - 3
    # Read those three bits without allocating an int over the whole sample:
    # they may span at most two bytes.
    end_bit_end = significant_bit_len
    end_byte_start = end_bit_start // 8
    end_byte_end = (end_bit_end + 7) // 8
    span = sample[end_byte_start:end_byte_end]
    span_value = int.from_bytes(span, "big")
    span_bit_width = len(span) * 8
    shift = span_bit_width - ((end_bit_start % 8) + 3)
    end3 = (span_value >> shift) & 0b111
    if end3 != _AAC_ID_END:
        raise MediaScrubError(
            "mp4 AAC raw_data_block does not end with ID_END + byte_alignment"
        )
    # Total-bits sanity: byte-alignment adds 0..7 zero bits after ID_END.
    padding_bits = total_bits - significant_bit_len
    if not 0 <= padding_bits <= 7:
        raise MediaScrubError(
            f"mp4 AAC byte_alignment padding {padding_bits} bits outside 0..7"
        )
    return sample
