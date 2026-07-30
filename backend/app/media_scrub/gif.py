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
extension and passes through with its sub-block chain validated but its
LZW-compressed data untouched (that IS the pixel stream we exist to
preserve).
"""
from __future__ import annotations

import struct
from typing import Final

from .base import MediaScrubError, MediaScrubResult

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
    global_ct_size = 3 * (1 << ((packed & 0x07) + 1)) if global_ct_flag else 0

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
    out.append(packed)
    out.append(background_color_index)
    out.append(pixel_aspect_ratio)
    if global_ct_flag:
        out.extend(data[13:lsd_end])

    image_seen = False
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
            rebuilt = _rebuild_extension(label, sub_blocks)
            if rebuilt is not None:
                out.extend(rebuilt)
            offset = block_end
            continue
        if marker == _GIF_IMAGE_DESCRIPTOR:
            block_end = _emit_image_descriptor(data, offset, end, out)
            offset = block_end
            image_seen = True
            continue
        raise MediaScrubError(f"unexpected gif block marker: 0x{marker:02x}")
    if not image_seen:
        raise MediaScrubError("gif payload has no image data")
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
    """Rebuild an extension from parsed sub-blocks. Return None to drop."""
    if label == _GIF_GRAPHIC_CONTROL_LABEL:
        # Graphic Control Extension: exactly one 4-byte sub-block.
        # Body layout: packed(1), delay_time(2 LE), transparent_color_index(1).
        if len(sub_blocks) != 1 or len(sub_blocks[0]) != 4:
            raise MediaScrubError("gif graphic control extension malformed")
        packed = sub_blocks[0][0]
        delay_time = struct.unpack("<H", sub_blocks[0][1:3])[0]
        transparent_index = sub_blocks[0][3]
        return (
            bytes([_GIF_EXT_INTRO, _GIF_GRAPHIC_CONTROL_LABEL, 0x04])
            + bytes([packed])
            + struct.pack("<H", delay_time)
            + bytes([transparent_index, 0x00])
        )
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


def _emit_image_descriptor(
    data: bytes, offset: int, end: int, out: bytearray,
) -> int:
    """Emit the image descriptor + local color table + LZW image data.

    Every field is parsed from validated positions; the LZW-compressed
    image data is bounded by its sub-block chain (validated in the walk)
    and IS the pixel stream we exist to preserve. Sub-block terminators
    and boundaries are re-emitted from the parsed structure; the pixel
    bytes themselves must remain byte-identical because they encode the
    image the caller asked us to store.
    """
    if offset + 10 > end:
        raise MediaScrubError("gif image descriptor truncated")
    left = struct.unpack("<H", data[offset + 1:offset + 3])[0]
    top = struct.unpack("<H", data[offset + 3:offset + 5])[0]
    img_w = struct.unpack("<H", data[offset + 5:offset + 7])[0]
    img_h = struct.unpack("<H", data[offset + 7:offset + 9])[0]
    local_packed = data[offset + 9]
    local_ct_size = (
        3 * (1 << ((local_packed & 0x07) + 1))
        if local_packed & 0x80
        else 0
    )
    lct_start = offset + 10
    data_start = lct_start + local_ct_size
    if data_start + 1 > end:
        raise MediaScrubError("gif image data truncated")
    lzw_min_code_size = data[data_start]
    # Descriptor field header:
    out.append(_GIF_IMAGE_DESCRIPTOR)
    out.extend(struct.pack("<HH", left, top))
    out.extend(struct.pack("<HH", img_w, img_h))
    out.append(local_packed)
    if local_ct_size:
        out.extend(data[lct_start:lct_start + local_ct_size])
    out.append(lzw_min_code_size)
    # LZW sub-block chain: emit each parsed sub-block header + body, then
    # terminator. Bodies are pixel data (indexed colours) that must survive
    # byte-identical to preserve the image.
    sub_offset = data_start + 1
    while sub_offset < end:
        length = data[sub_offset]
        sub_offset += 1
        out.append(length)
        if length == 0:
            return sub_offset
        block_end = sub_offset + length
        if block_end > end:
            raise MediaScrubError("gif image sub-block extends past payload")
        out.extend(data[sub_offset:block_end])
        sub_offset = block_end
    raise MediaScrubError("gif image sub-block chain missing terminator")
