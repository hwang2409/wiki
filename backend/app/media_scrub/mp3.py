"""MP3 scrubbing — strip ID3v1, ID3v2, and APEv2 tags."""
from __future__ import annotations

import struct
from typing import Final

from .base import MediaScrubError, MediaScrubResult

_ID3V2_MAGIC: Final = b"ID3"
_ID3V1_MAGIC: Final = b"TAG"
_APE_MAGIC: Final = b"APETAGEX"
_APE_HEADER_FOOTER_LEN: Final = 32
# APEv2 flag bit indices per the official spec.
_APE_FLAG_IS_HEADER: Final = 1 << 31
_APE_FLAG_NO_FOOTER: Final = 1 << 30
_APE_FLAG_HAS_HEADER: Final = 1 << 29
_APE_ITEM_COUNT_MAX: Final = 0xFFFF
_APE_TAG_SIZE_MAX: Final = 32 * 1024 * 1024

_MP3_BITRATE_V1_L3: Final = (
    0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0,
)
_MP3_BITRATE_V2_L3: Final = (
    0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0,
)
_MP3_SAMPLE_RATE_V1: Final = (44100, 48000, 32000, 0)
_MP3_SAMPLE_RATE_V2: Final = (22050, 24000, 16000, 0)
_MP3_SAMPLE_RATE_V25: Final = (11025, 12000, 8000, 0)
_MP3_XING_MAGICS: Final = (b"Info", b"Xing")


def scrub_mp3(data: bytes) -> MediaScrubResult:
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
    # Front-of-file APEv2 (header form). This one variant only appears at
    # the very front so it advances `start`; every other trailing tag is
    # handled by the fixpoint loop below.
    start = _mp3_strip_ape_header(data, start, end)
    # Fixpoint trailing-tag strip: round-6 review flagged that an ID3v1
    # placed BEFORE an APEv2 footer survived because the ID3v1 branch
    # only ran once and only if it was the tail. Loop across ID3v1,
    # APEv2 footer, Lyrics3v1, and Lyrics3v2 until nothing changes, so
    # any ordering (ID3v1→APE, APE→ID3v1, Lyrics3→ID3v1→APE, …) is
    # peeled cleanly off the tail.
    end = _mp3_strip_trailing_tags_to_fixpoint(data, start, end)
    if end - start < 4:
        raise MediaScrubError("mp3 has no audio frames after tag strip")

    # Round-7 review: the previous validator only walked the first three
    # frames. An attacker could pad the tail with unstructured bytes and
    # they would survive because the walk never reached them. Full-stream
    # validation now walks EVERY frame and requires the final walk step
    # to land exactly on `end` — no untracked bytes may exist in the
    # frame stream at all.
    rebuilt_frames = _mp3_validate_full_frame_stream(data, start, end)
    return MediaScrubResult(
        data=rebuilt_frames,
        mime="audio/mpeg",
        duration_ms=None,
        width=None,
        height=None,
    )


def _mp3_read_ape_meta(data: bytes, preamble_at: int) -> tuple[int, int, int]:
    if preamble_at < 0 or preamble_at + _APE_HEADER_FOOTER_LEN > len(data):
        raise MediaScrubError("mp3 APEv2 preamble out of bounds")
    tag_size, item_count, flags = struct.unpack(
        "<III", data[preamble_at + 12:preamble_at + 24],
    )
    if tag_size < _APE_HEADER_FOOTER_LEN:
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
    if end - start < _APE_HEADER_FOOTER_LEN:
        return start
    if data[start:start + 8] != _APE_MAGIC:
        return start
    tag_size, _item_count, flags = _mp3_read_ape_meta(data, start)
    if flags & _APE_FLAG_IS_HEADER == 0:
        raise MediaScrubError("mp3 APEv2 marker at start is not a header")
    advance = _APE_HEADER_FOOTER_LEN + tag_size
    if advance > end - start:
        raise MediaScrubError("mp3 APEv2 header size larger than payload")
    return start + advance


def _mp3_strip_ape_footer(data: bytes, start: int, end: int) -> int:
    if end - start < _APE_HEADER_FOOTER_LEN:
        return end
    footer_start = end - _APE_HEADER_FOOTER_LEN
    if data[footer_start:footer_start + 8] != _APE_MAGIC:
        return end
    tag_size, _item_count, flags = _mp3_read_ape_meta(data, footer_start)
    if flags & _APE_FLAG_IS_HEADER:
        raise MediaScrubError("mp3 APEv2 marker at end is a header")
    trim = tag_size
    if flags & _APE_FLAG_HAS_HEADER:
        trim += _APE_HEADER_FOOTER_LEN
    if trim > end - start:
        raise MediaScrubError("mp3 APEv2 footer size larger than payload")
    return end - trim


_LYRICS3_BEGIN: Final = b"LYRICSBEGIN"
_LYRICS3V1_END: Final = b"LYRICSEND"
_LYRICS3V2_MAGIC: Final = b"LYRICS200"
_LYRICS3V2_SIZE_LEN: Final = 6


def _mp3_strip_trailing_tags_to_fixpoint(data: bytes, start: int, end: int) -> int:
    """Peel ID3v1, APEv2, Lyrics3v1, and Lyrics3v2 off the tail until the
    end position stops moving. Order-independent — the loop keeps stripping
    whichever tag currently sits at the tail.
    """
    while True:
        prev_end = end
        # ID3v1: fixed 128-byte block ending with "TAG" at offset end-128.
        if end - start >= 128 and data[end - 128:end - 125] == _ID3V1_MAGIC:
            end -= 128
            continue
        # APEv2 footer sits at end-32 with an APETAGEX preamble. The
        # helper returns a smaller `end` if it stripped, and raises on
        # malformed sizes. Nothing to do here if no preamble.
        stripped = _mp3_strip_ape_footer(data, start, end)
        if stripped != end:
            end = stripped
            continue
        # Lyrics3v2 spec layout (bottom-up): the LAST 9 bytes are the
        # ASCII marker "LYRICS200"; the 6 bytes immediately before it are
        # a decimal size counting all bytes between (and including) the
        # leading "LYRICSBEGIN" and the size digits themselves. The
        # round-7 parser had the marker and size positions swapped, so
        # spec-conformant tags were rejected. Fixed here.
        if (
            end - start >= 15
            and data[end - len(_LYRICS3V2_MAGIC):end] == _LYRICS3V2_MAGIC
        ):
            size_start = end - len(_LYRICS3V2_MAGIC) - _LYRICS3V2_SIZE_LEN
            size_field = data[size_start:size_start + _LYRICS3V2_SIZE_LEN]
            try:
                tag_span = int(size_field.decode("ascii"))
            except ValueError as exc:
                raise MediaScrubError("mp3 Lyrics3v2 size not ASCII digits") from exc
            if tag_span <= 0:
                raise MediaScrubError("mp3 Lyrics3v2 size not positive")
            # tag_span covers LYRICSBEGIN + items + the 6-digit size
            # field (but NOT the LYRICS200 marker). LYRICSBEGIN sits
            # `tag_span` bytes before the size field's end.
            begin_at = size_start + _LYRICS3V2_SIZE_LEN - tag_span
            if begin_at < start:
                raise MediaScrubError("mp3 Lyrics3v2 tag size larger than payload")
            if data[begin_at:begin_at + len(_LYRICS3_BEGIN)] != _LYRICS3_BEGIN:
                raise MediaScrubError(
                    "mp3 Lyrics3v2 LYRICSBEGIN marker missing at declared tag start"
                )
            end = begin_at
            continue
        # Lyrics3v1: ends with "LYRICSEND" (no size field). Walk back to
        # the matching LYRICSBEGIN marker. Refuse if we can't find one.
        if end - start >= len(_LYRICS3V1_END) and data[end - len(_LYRICS3V1_END):end] == _LYRICS3V1_END:
            search_end = end - len(_LYRICS3V1_END)
            begin_at = data.rfind(_LYRICS3_BEGIN, start, search_end)
            if begin_at < 0:
                raise MediaScrubError(
                    "mp3 Lyrics3v1 LYRICSEND without matching LYRICSBEGIN"
                )
            end = begin_at
            continue
        if end == prev_end:
            return end


def _mp3_validate_full_frame_stream(data: bytes, start: int, end: int) -> bytes:
    """Walk every MPEG frame between `start` and `end`. The walk must land
    exactly on `end` — any unaccounted-for byte in the payload rejects the
    file. This is the round-7 fix for the pre-R7 validator that only
    checked the first three frames.
    """
    offset = start
    frames_seen = 0
    stream_signature: tuple[int, int] | None = None
    rebuilt = bytearray(data[start:end])
    while offset < end:
        frame_len = _mp3_frame_length(data, offset, end)
        if frame_len is None:
            raise MediaScrubError(
                f"mp3 frame stream broken at offset {offset - start} "
                f"({frames_seen} frames validated)"
            )
        signature = ((data[offset + 1] >> 3) & 0x03, (data[offset + 1] >> 1) & 0x03)
        if stream_signature is None:
            stream_signature = signature
        elif signature != stream_signature:
            raise MediaScrubError(
                "mp3 frame stream changes MPEG version or layer"
            )
        frame_offset = offset - start
        frame = data[offset:offset + frame_len]
        if frames_seen == 0:
            header = frame[:4]
            side_info_start = _mp3_side_info_start(header)
            metadata_end = _mp3_xing_metadata_end(frame, side_info_start)
            if metadata_end is not None:
                rebuilt[frame_offset + side_info_start:frame_offset + metadata_end] = (
                    b"\x00" * (metadata_end - side_info_start)
                )
            else:
                ancillary_start = _mp3_layer3_main_data_end(frame, header)
                rebuilt[frame_offset + ancillary_start:frame_offset + frame_len] = (
                    b"\x00" * (frame_len - ancillary_start)
                )
        else:
            ancillary_start = _mp3_layer3_main_data_end(frame, frame[:4])
            rebuilt[frame_offset + ancillary_start:frame_offset + frame_len] = (
                b"\x00" * (frame_len - ancillary_start)
            )
        offset += frame_len
        frames_seen += 1
    if offset != end:
        raise MediaScrubError(
            f"mp3 frame stream ends {offset - end} bytes past declared end"
        )
    if frames_seen < 1:
        raise MediaScrubError("mp3 frame stream contains zero frames")
    return bytes(rebuilt)


class _Mp3BitReader:
    __slots__ = ("data", "bit_pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.bit_pos = 0

    def read(self, width: int) -> int:
        if self.bit_pos + width > len(self.data) * 8:
            raise MediaScrubError("mp3 Layer III side information is truncated")
        value = 0
        for _ in range(width):
            value = (value << 1) | (
                self.data[self.bit_pos // 8] >> (7 - self.bit_pos % 8) & 1
            )
            self.bit_pos += 1
        return value


def _mp3_layer3_main_data_end(frame: bytes, header: bytes) -> int:
    """Return the byte after this frame's Layer III main data.

    The side-information lengths describe the exact number of coded main-data
    bits. Bytes after that boundary are ancillary data and are zeroed.
    """
    side_start = 4 + (0 if header[1] & 1 else 2)
    side_length = _mp3_side_info_length(header)
    side_end = side_start + side_length
    if side_end > len(frame):
        raise MediaScrubError("mp3 Layer III side information is truncated")
    reader = _Mp3BitReader(frame[side_start:side_end])
    version_bits = (header[1] >> 3) & 0x03
    channel_mode = (header[3] >> 6) & 0x03
    channels = 1 if channel_mode == 3 else 2
    mpeg1 = version_bits == 3
    reader.read(9 if mpeg1 else 8)  # main_data_begin
    reader.read(5 if mpeg1 and channels == 1 else 3 if mpeg1 else 1 if channels == 1 else 3)
    if mpeg1:
        for _ in range(channels):
            reader.read(4)  # scfsi
    main_data_bits = 0
    for _ in range(2 if mpeg1 else 1):
        for _ in range(channels):
            main_data_bits += reader.read(12)  # part2_3_length
            reader.read(9)  # big_values
            reader.read(8)  # global_gain
            reader.read(4 if mpeg1 else 9)  # scalefac_compress
            switched = reader.read(1)
            if switched:
                block_type = reader.read(2)
                if block_type == 0:
                    raise MediaScrubError("mp3 Layer III reserved block type")
                reader.read(1)  # mixed_block_flag
                reader.read(5 * 2)  # table_select
                reader.read(3 * 3)  # subblock_gain
            else:
                reader.read(5 * 3)  # table_select
                reader.read(4)  # region0_count
                reader.read(3)  # region1_count
            if mpeg1:
                reader.read(1)  # preflag
            reader.read(1)  # scalefac_scale
            reader.read(1)  # count1table_select
    main_data_bytes = (main_data_bits + 7) // 8
    main_data_start = side_end
    # main_data_begin may point into the bit reservoir. In that case the
    # current frame contributes only part of the declared main data, and
    # every byte through the frame end is coded audio. No ancillary region
    # exists in this frame.
    return min(len(frame), main_data_start + main_data_bytes)


def _mp3_side_info_length(header: bytes) -> int:
    version_bits = (header[1] >> 3) & 0x03
    channel_mode = (header[3] >> 6) & 0x03
    mono = channel_mode == 3
    if version_bits == 3:
        return 17 if mono else 32
    return 9 if mono else 17


def _mp3_side_info_start(header: bytes) -> int:
    crc_length = 0 if header[1] & 0x01 else 2
    return 4 + crc_length + _mp3_side_info_length(header)


def _mp3_xing_metadata_end(frame: bytes, start: int) -> int | None:
    if start + 8 > len(frame):
        return None
    magic = frame[start:start + 4]
    if magic == b"VBRI":
        raise MediaScrubError("mp3 VBRI metadata is outside scrubber scope")
    if magic not in _MP3_XING_MAGICS:
        return None
    flags = struct.unpack(">I", frame[start + 4:start + 8])[0]
    offset = start + 8
    if flags & 0x01:
        offset += 4
    if flags & 0x02:
        offset += 4
    if flags & 0x04:
        offset += 100
    if flags & 0x08:
        offset += 4
    if offset > len(frame):
        raise MediaScrubError("mp3 Xing/Info metadata is truncated")
    # LAME and FFmpeg write a nine-byte encoder field after the Xing
    # records. Accept only printable ASCII or an empty padded field.
    encoder = frame[offset:offset + 9]
    if len(encoder) == 9 and (all(byte == 0 for byte in encoder) or all(32 <= byte < 127 for byte in encoder)):
        return offset + 9
    return offset


def _mp3_frame_length(data: bytes, offset: int, end: int) -> int | None:
    if offset + 4 > end:
        return None
    if data[offset] != 0xFF or (data[offset + 1] & 0xE0) != 0xE0:
        return None
    version_bits = (data[offset + 1] >> 3) & 0x03
    layer_bits = (data[offset + 1] >> 1) & 0x03
    # The shipped scrubber supports MPEG Layer III only. The bitrate and
    # frame-length tables below are Layer III tables, so accepting Layer I or
    # II headers would mis-size the walk and expose trailing bytes as audio.
    if version_bits == 1 or layer_bits != 1:
        return None
    bitrate_bits = (data[offset + 2] >> 4) & 0x0F
    sample_rate_bits = (data[offset + 2] >> 2) & 0x03
    padding = (data[offset + 2] >> 1) & 0x01
    if sample_rate_bits == 3 or bitrate_bits in (0, 15):
        return None
    if version_bits == 3:
        bitrate = _MP3_BITRATE_V1_L3[bitrate_bits] * 1000
        sample_rate = _MP3_SAMPLE_RATE_V1[sample_rate_bits]
        samples_per_frame = 1152
    elif version_bits == 2:
        bitrate = _MP3_BITRATE_V2_L3[bitrate_bits] * 1000
        sample_rate = _MP3_SAMPLE_RATE_V2[sample_rate_bits]
        samples_per_frame = 576
    else:
        bitrate = _MP3_BITRATE_V2_L3[bitrate_bits] * 1000
        sample_rate = _MP3_SAMPLE_RATE_V25[sample_rate_bits]
        samples_per_frame = 576
    if bitrate == 0 or sample_rate == 0:
        return None
    frame_length = (samples_per_frame // 8 * bitrate) // sample_rate + padding
    if frame_length < 4:
        return None
    return frame_length
