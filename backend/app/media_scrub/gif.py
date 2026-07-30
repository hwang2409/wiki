"""GIF87a/GIF89a scrubbing — extension allowlist rebuild.

Round 7: no extension body may be byte-copied. Only two extensions survive
scrub, and both are re-serialized from parsed fields:

  * Graphic Control Extension (0x21 0xF9): 4-byte body carrying packed
    disposal + user_input + transparent flags, delay_time (uint16 LE),
    transparent_color_index (uint8). Rebuilt via struct.pack.
  * NETSCAPE2.0 looping Application Extension (0x21 0xFF, ident
    "NETSCAPE2.0"): 3-byte sub-block carrying sub_block_index (0x01),
    loop_count (uint16 LE). Rebuilt via struct.pack.

Everything else — Comment (0xFE), Plain Text (0x01), XMP, Adobe, and every
other Application Extension — is dropped. Image data (0x2C) is not an
extension and is decoded through EOI, then re-encoded from its validated
pixel indices.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

from .base import GIF_MAX_PIXELS, MediaScrubError, MediaScrubResult

_GIF_HEADER87: Final = b"GIF87a"
_GIF_HEADER89: Final = b"GIF89a"
_GIF_TRAILER: Final = 0x3B
_GIF_EXT_INTRO: Final = 0x21
_GIF_GRAPHIC_CONTROL_LABEL: Final = 0xF9
_GIF_COMMENT_LABEL: Final = 0xFE
_GIF_PLAIN_TEXT_LABEL: Final = 0x01
_GIF_APP_EXT_LABEL: Final = 0xFF
_GIF_NETSCAPE_IDENT: Final = b"NETSCAPE2.0"
_GIF_IMAGE_DESCRIPTOR: Final = 0x2C

# GCE packed byte layout per GIF89a:
#   bits 7-5: reserved (must be 000)
#   bits 4-2: disposal method (0-3 defined; 4-7 reserved)
#   bit 1:   user_input_flag
#   bit 0:   transparent_color_flag
_GIF_GCE_RESERVED_MASK: Final = 0b1110_0000
_GIF_GCE_DISPOSAL_MASK: Final = 0b0001_1100
_GIF_GCE_DISPOSAL_SHIFT: Final = 2
_GIF_GCE_USER_INPUT_MASK: Final = 0b0000_0010
_GIF_GCE_TRANSPARENT_MASK: Final = 0b0000_0001
_GIF_GCE_DISPOSAL_MAX: Final = 3  # spec defines 0..3; 4..7 reserved
_GIF_IMAGE_DESCRIPTOR_RESERVED_MASK: Final = 0b0001_1000
_GIF_IMAGE_DESCRIPTOR_ALLOWED_MASK: Final = 0b1110_0111


@dataclass(frozen=True)
class _PendingGCE:
    """Parsed + validated Graphic Control Extension awaiting the next image
    descriptor so the transparent_color_index can be validated against the
    active color table (LCT overrides GCT). Round-10 review: pre-R10 the
    packed byte and transparent index were emitted verbatim, leaking 11
    ignored attacker-controlled bits per frame."""

    packed_canonical: int
    delay_time: int
    transparent_index: int  # canonical 0 when transparency flag clear
    transparent_flag: bool

    def to_bytes(self) -> bytes:
        return (
            bytes([_GIF_EXT_INTRO, _GIF_GRAPHIC_CONTROL_LABEL, 0x04])
            + bytes([self.packed_canonical])
            + struct.pack("<H", self.delay_time)
            + bytes([self.transparent_index, 0x00])
        )


def _parse_gce_body(body: bytes) -> _PendingGCE:
    """Parse a GCE sub-block body into validated canonical fields.

    Reserved bits reject; reserved disposal codes reject; transparent
    index is normalised to zero when the transparent flag is clear so the
    ignored input byte cannot pass through storage.
    """
    if len(body) != 4:
        raise MediaScrubError("gif graphic control extension malformed")
    packed_in = body[0]
    if packed_in & _GIF_GCE_RESERVED_MASK:
        raise MediaScrubError(
            f"gif GCE packed byte 0x{packed_in:02x} has reserved bits (7-5) set"
        )
    disposal = (packed_in & _GIF_GCE_DISPOSAL_MASK) >> _GIF_GCE_DISPOSAL_SHIFT
    if disposal > _GIF_GCE_DISPOSAL_MAX:
        raise MediaScrubError(
            f"gif GCE disposal method {disposal} is reserved (only 0..3 defined)"
        )
    user_input = (packed_in & _GIF_GCE_USER_INPUT_MASK) >> 1
    transparent_flag = bool(packed_in & _GIF_GCE_TRANSPARENT_MASK)
    delay_time = struct.unpack("<H", body[1:3])[0]
    raw_index = body[3]
    canonical_index = raw_index if transparent_flag else 0
    packed_canonical = (
        (disposal << _GIF_GCE_DISPOSAL_SHIFT)
        | (user_input << 1)
        | (1 if transparent_flag else 0)
    )
    return _PendingGCE(
        packed_canonical=packed_canonical,
        delay_time=delay_time,
        transparent_index=canonical_index,
        transparent_flag=transparent_flag,
    )


def scrub_gif(data: bytes) -> MediaScrubResult:
    if len(data) < 13:
        raise MediaScrubError("gif payload too small")
    header = data[:6]
    if header not in (_GIF_HEADER87, _GIF_HEADER89):
        raise MediaScrubError("gif payload missing GIF87a/GIF89a header")

    width, height = struct.unpack("<HH", data[6:10])
    packed = data[10]
    background_color_index = data[11]
    pixel_aspect_ratio = data[12]
    global_ct_flag = packed & 0x80
    global_ct_entries = 1 << ((packed & 0x07) + 1) if global_ct_flag else 0
    global_ct_size = 3 * global_ct_entries
    canonical_packed = packed if global_ct_flag else 0
    canonical_background_color_index = background_color_index if global_ct_flag else 0

    lsd_end = 13 + global_ct_size
    if lsd_end > len(data):
        raise MediaScrubError("gif global color table extends past payload")
    # LSD + GCT are 6 (header) + 7 (LSD fields) + gct_size = 13 + gct_size
    # bytes. Header + LSD are already validated field-by-field via struct
    # unpack above. The GCT is a raw palette blob — bounded by size but
    # contains no metadata surface (colour entries only). It's byte-copied
    # here because the palette is what image_data indexes into; changing
    # any byte would corrupt the pixels.
    out = bytearray(header)
    out.extend(struct.pack("<HH", width, height))
    out.append(canonical_packed)
    out.append(canonical_background_color_index)
    out.append(pixel_aspect_ratio)
    if global_ct_flag:
        out.extend(data[13:lsd_end])

    image_seen = False
    pending_gce: _PendingGCE | None = None
    offset = lsd_end
    end = len(data)
    while offset < end:
        marker = data[offset]
        if marker == _GIF_TRAILER:
            offset += 1
            break
        if marker == _GIF_EXT_INTRO:
            if offset + 2 > end:
                raise MediaScrubError("gif extension truncated")
            label = data[offset + 1]
            sub_start = offset + 2
            block_end, sub_blocks = _gif_walk_extension_subblocks(
                data, sub_start, end,
            )
            if label == _GIF_GRAPHIC_CONTROL_LABEL:
                if len(sub_blocks) != 1:
                    raise MediaScrubError("gif graphic control extension malformed")
                if pending_gce is not None:
                    # Two GCEs in a row: only the last one applies to the
                    # following image per spec. Drop the earlier attacker-
                    # smuggled GCE header rather than emitting it.
                    pass
                pending_gce = _parse_gce_body(sub_blocks[0])
            else:
                if label == _GIF_PLAIN_TEXT_LABEL:
                    # Plain Text is a Graphic Rendering Block. A pending GCE
                    # applies to it, so it must not reach the next image after
                    # this extension is dropped.
                    pending_gce = None
                rebuilt = _rebuild_extension(label, sub_blocks)
                if rebuilt is not None:
                    out.extend(rebuilt)
            offset = block_end
            continue
        if marker == _GIF_IMAGE_DESCRIPTOR:
            block_end = _emit_image_descriptor(
                data, offset, end, out,
                pending_gce=pending_gce,
                global_ct_entries=global_ct_entries,
            )
            pending_gce = None
            offset = block_end
            image_seen = True
            continue
        raise MediaScrubError(f"unexpected gif block marker: 0x{marker:02x}")
    if not image_seen:
        raise MediaScrubError("gif payload has no image data")
    if pending_gce is not None:
        # A trailing GCE with no following image is not preserved (has no
        # image to control). Silently drop rather than emit an orphan.
        pass
    out.append(_GIF_TRAILER)
    return MediaScrubResult(
        data=bytes(out),
        mime="image/gif",
        duration_ms=None,
        width=int(width) or None,
        height=int(height) or None,
    )


def _gif_walk_extension_subblocks(
    data: bytes, start: int, end: int,
) -> tuple[int, list[bytes]]:
    """Walk an extension's sub-block chain. Return (offset_past_terminator, blocks)."""
    offset = start
    blocks: list[bytes] = []
    while offset < end:
        length = data[offset]
        offset += 1
        if length == 0:
            return offset, blocks
        block_end = offset + length
        if block_end > end:
            raise MediaScrubError("gif sub-block extends past payload")
        blocks.append(data[offset:block_end])
        offset = block_end
    raise MediaScrubError("gif sub-block chain missing terminator")


def _rebuild_extension(label: int, sub_blocks: list[bytes]) -> bytes | None:
    """Rebuild an extension from parsed sub-blocks. Return None to drop.

    Graphic Control Extensions are handled inline in scrub_gif so the
    transparent_color_index can be validated against the image's active
    color table before emission — do not dispatch them here.
    """
    if label == _GIF_APP_EXT_LABEL:
        # The application identifier lives in the first sub-block (must be
        # exactly 11 bytes: 8-byte identifier + 3-byte auth code).
        if not sub_blocks or len(sub_blocks[0]) != 11:
            return None  # unknown application extension → drop
        if sub_blocks[0] != _GIF_NETSCAPE_IDENT:
            return None  # anything except NETSCAPE2.0 (XMP, Adobe, …) → drop
        # NETSCAPE2.0 looping extension: sub_block_index(1)=0x01,
        # loop_count(2 LE). Rebuild from those parsed fields only.
        if len(sub_blocks) < 2 or len(sub_blocks[1]) != 3:
            raise MediaScrubError("gif NETSCAPE2.0 extension malformed")
        sub_block_index = sub_blocks[1][0]
        if sub_block_index != 0x01:
            raise MediaScrubError("gif NETSCAPE2.0 sub-block index unexpected")
        loop_count = struct.unpack("<H", sub_blocks[1][1:3])[0]
        return (
            bytes([_GIF_EXT_INTRO, _GIF_APP_EXT_LABEL, 0x0B])
            + _GIF_NETSCAPE_IDENT
            + bytes([0x03, 0x01])
            + struct.pack("<H", loop_count)
            + bytes([0x00])
        )
    # Comment (0xFE), Plain Text (0x01), and every other label → drop.
    return None


def _read_gif_lzw_code(data: bytes, bit_offset: int, width: int) -> tuple[int, int]:
    if bit_offset + width > len(data) * 8:
        raise MediaScrubError("gif LZW stream ends before EOI")
    value = 0
    for shift in range(width):
        value |= ((data[(bit_offset + shift) // 8] >> ((bit_offset + shift) % 8)) & 1) << shift
    return value, bit_offset + width


def _decode_gif_lzw(
    compressed: bytes,
    min_code_size: int,
    expected_pixels: int,
) -> bytes:
    if not 2 <= min_code_size <= 8:
        raise MediaScrubError("gif LZW minimum code size outside 2..8")
    clear_code = 1 << min_code_size
    eoi_code = clear_code + 1
    code_size = min_code_size + 1
    next_code = clear_code + 2
    dictionary = {index: bytes([index]) for index in range(clear_code)}
    bit_offset = 0
    pixels = bytearray()
    previous: bytes | None = None
    saw_clear = False
    while True:
        code, bit_offset = _read_gif_lzw_code(compressed, bit_offset, code_size)
        if code == clear_code:
            dictionary = {index: bytes([index]) for index in range(clear_code)}
            code_size = min_code_size + 1
            next_code = clear_code + 2
            previous = None
            saw_clear = True
            continue
        if code == eoi_code:
            if not saw_clear:
                raise MediaScrubError("gif LZW stream has no clear code")
            break
        if not saw_clear or previous is None and code >= clear_code:
            raise MediaScrubError("gif LZW stream has invalid first data code")
        if code < clear_code:
            entry = bytes([code])
        elif code in dictionary:
            entry = dictionary[code]
        elif code == next_code and previous is not None:
            entry = previous + previous[:1]
        else:
            raise MediaScrubError("gif LZW stream references an undefined code")
        if len(pixels) + len(entry) > expected_pixels:
            raise MediaScrubError("gif LZW stream emits more pixels than the image size")
        pixels.extend(entry)
        if previous is not None and next_code < 4096:
            dictionary[next_code] = previous + entry[:1]
            next_code += 1
            if next_code == (1 << code_size) and code_size < 12:
                code_size += 1
        previous = entry
    # Some legacy GIFs end after a short final row. Preserve those accepted
    # streams while still rejecting data that would write past the image.
    return bytes(pixels)


def _encode_gif_lzw(pixels: bytes, min_code_size: int) -> bytes:
    clear_code = 1 << min_code_size
    eoi_code = clear_code + 1
    dictionary = {bytes([index]): index for index in range(clear_code)}
    code_size = min_code_size + 1
    next_code = clear_code + 2
    grow_pending = False
    coded: list[tuple[int, int]] = [(clear_code, code_size)]
    if pixels:
        current = bytes([pixels[0]])
        for pixel in pixels[1:]:
            candidate = current + bytes([pixel])
            if candidate in dictionary:
                current = candidate
                continue
            coded.append((dictionary[current], code_size))
            if grow_pending:
                code_size += 1
                grow_pending = False
            if next_code < 4096:
                dictionary[candidate] = next_code
                next_code += 1
                if next_code == (1 << code_size) and code_size < 12:
                    grow_pending = True
            else:
                coded.append((clear_code, code_size))
                dictionary = {bytes([index]): index for index in range(clear_code)}
                code_size = min_code_size + 1
                next_code = clear_code + 2
                grow_pending = False
            current = bytes([pixel])
        coded.append((dictionary[current], code_size))
    coded.append((eoi_code, code_size))

    output = bytearray()
    bit_offset = 0
    for code, width in coded:
        for shift in range(width):
            if bit_offset % 8 == 0:
                output.append(0)
            output[-1] |= ((code >> shift) & 1) << (bit_offset % 8)
            bit_offset += 1
    return bytes(output)


def _rebuild_gif_lzw(
    blocks: list[bytes], min_code_size: int, expected_pixels: int,
) -> bytes:
    pixels = _decode_gif_lzw(b"".join(blocks), min_code_size, expected_pixels)
    compressed = _encode_gif_lzw(pixels, min_code_size)
    output = bytearray([min_code_size])
    for offset in range(0, len(compressed), 255):
        block = compressed[offset:offset + 255]
        output.append(len(block))
        output.extend(block)
    output.append(0)
    return bytes(output)


def _emit_image_descriptor(
    data: bytes, offset: int, end: int, out: bytearray,
    *,
    pending_gce: _PendingGCE | None,
    global_ct_entries: int,
) -> int:
    """Emit the image descriptor + local color table + LZW image data.

    Every field is parsed from validated positions. The LZW stream is
    decoded through EOI and rebuilt from the validated pixel indices.

    Round-10 review: any preceding Graphic Control Extension is validated
    against this image's active color table (LCT if present, else GCT)
    before we emit the GCE + descriptor pair. A transparent index that
    points past the color table rejects the file rather than pointing at
    undefined palette memory.
    """
    if offset + 10 > end:
        raise MediaScrubError("gif image descriptor truncated")
    left = struct.unpack("<H", data[offset + 1:offset + 3])[0]
    top = struct.unpack("<H", data[offset + 3:offset + 5])[0]
    img_w = struct.unpack("<H", data[offset + 5:offset + 7])[0]
    img_h = struct.unpack("<H", data[offset + 7:offset + 9])[0]
    expected_pixels = img_w * img_h
    if expected_pixels > GIF_MAX_PIXELS:
        raise MediaScrubError(
            f"gif image has {expected_pixels} pixels, above the {GIF_MAX_PIXELS} pixel limit"
        )
    local_packed = data[offset + 9]
    if local_packed & _GIF_IMAGE_DESCRIPTOR_RESERVED_MASK:
        raise MediaScrubError(
            "gif image descriptor packed byte has reserved bits set"
        )
    local_ct_flag = local_packed & 0x80
    local_packed_canonical = (
        local_packed & _GIF_IMAGE_DESCRIPTOR_ALLOWED_MASK
        if local_ct_flag
        else local_packed & 0x40  # interlace remains meaningful without an LCT
    )
    local_ct_entries = (
        1 << ((local_packed & 0x07) + 1)
        if local_ct_flag
        else 0
    )
    local_ct_size = 3 * local_ct_entries
    lct_start = offset + 10
    data_start = lct_start + local_ct_size
    if data_start + 1 > end:
        raise MediaScrubError("gif image data truncated")
    lzw_min_code_size = data[data_start]

    if pending_gce is not None:
        active_ct_entries = local_ct_entries if local_ct_entries else global_ct_entries
        if pending_gce.transparent_flag:
            if active_ct_entries == 0:
                raise MediaScrubError(
                    "gif GCE flags transparency but the image has no active color table"
                )
            if pending_gce.transparent_index >= active_ct_entries:
                raise MediaScrubError(
                    f"gif GCE transparent_color_index {pending_gce.transparent_index} "
                    f"out of range for active color table ({active_ct_entries} entries)"
                )
        out.extend(pending_gce.to_bytes())
    # Descriptor field header:
    out.append(_GIF_IMAGE_DESCRIPTOR)
    out.extend(struct.pack("<HH", left, top))
    out.extend(struct.pack("<HH", img_w, img_h))
    out.append(local_packed_canonical)
    if local_ct_size:
        out.extend(data[lct_start:lct_start + local_ct_size])
    # Decode the image stream through EOI, then emit one deterministic,
    # canonical stream. Bytes in sub-blocks after EOI are not pixel data.
    lzw_blocks: list[bytes] = []
    sub_offset = data_start + 1
    while sub_offset < end:
        length = data[sub_offset]
        sub_offset += 1
        if length == 0:
            out.extend(_rebuild_gif_lzw(
                lzw_blocks,
                lzw_min_code_size,
                expected_pixels,
            ))
            return sub_offset
        block_end = sub_offset + length
        if block_end > end:
            raise MediaScrubError("gif image sub-block extends past payload")
        lzw_blocks.append(data[sub_offset:block_end])
        sub_offset = block_end
    raise MediaScrubError("gif image sub-block chain missing terminator")
