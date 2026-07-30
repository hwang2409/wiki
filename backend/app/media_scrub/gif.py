"""GIF87a/GIF89a scrubbing — strip XMP Application Extension blocks."""
from __future__ import annotations

import struct
from typing import Final

from .base import MediaScrubError, MediaScrubResult

_GIF_HEADER87: Final = b"GIF87a"
_GIF_HEADER89: Final = b"GIF89a"
_GIF_TRAILER: Final = 0x3B
_GIF_EXT_INTRO: Final = 0x21
_GIF_APP_EXT: Final = 0xFF
_GIF_XMP_IDENT: Final = b"XMP DataXMP"


def scrub_gif(data: bytes) -> MediaScrubResult:
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
