"""MP4 atom walking and canonical box emission primitives."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

from .base import MediaScrubError


@dataclass(frozen=True)
class Mp4Atom:
    start: int
    size: int
    header_len: int
    type: bytes
    body_start: int
    body_end: int


FREE_MIN_SIZE: Final = 8
MAX_BOXES_PER_CONTAINER: Final = 4096


def read_header(view: memoryview, offset: int, end: int) -> tuple[int, bytes, int, int]:
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


def parse_container(data: bytes, offset: int, end: int) -> list[Mp4Atom]:
    view = memoryview(data)
    atoms: list[Mp4Atom] = []
    while offset < end:
        if len(atoms) >= MAX_BOXES_PER_CONTAINER:
            raise MediaScrubError(
                f"mp4 container has more than {MAX_BOXES_PER_CONTAINER} child boxes"
            )
        size, atom_type, header_len, atom_end = read_header(view, offset, end)
        atoms.append(
            Mp4Atom(
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


def pack(atom_type: bytes, body: bytes) -> bytes:
    total = 8 + len(body)
    if total > 0xFFFFFFFF:  # pragma: no cover
        raise MediaScrubError("mp4 rebuilt atom size overflows 32 bits")
    return struct.pack(">I", total) + atom_type + body


def pack_with_header(atom_type: bytes, body: bytes, header_len: int) -> bytes:
    if header_len == 8:
        return pack(atom_type, body)
    if header_len != 16:
        raise MediaScrubError("mp4 atom header width is unsupported")
    total = 16 + len(body)
    if total > 0xFFFFFFFFFFFFFFFF:  # pragma: no cover
        raise MediaScrubError("mp4 rebuilt atom size overflows 64 bits")
    return struct.pack(">I", 1) + atom_type + struct.pack(">Q", total) + body


def free(total_size: int) -> bytes:
    """Emit a zeroed free box with exactly ``total_size`` bytes."""
    if total_size < FREE_MIN_SIZE:
        raise MediaScrubError(
            f"mp4 free padding requires ≥{FREE_MIN_SIZE} bytes, got {total_size}"
        )
    return struct.pack(">I", total_size) + b"free" + b"\x00" * (total_size - FREE_MIN_SIZE)
