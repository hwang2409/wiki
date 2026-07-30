from __future__ import annotations

import base64
import io
import struct
from dataclasses import dataclass
from typing import Final

from PIL import Image, ImageOps

MAX_SIDE: Final = 12_000
MAX_PIXELS: Final = 40_000_000

PREVIEW_MAX_SIDE: Final = 24
PREVIEW_QUALITY: Final = 55

_JPEG_SOI: Final = b"\xff\xd8"
_JPEG_EOI: Final = b"\xff\xd9"
_JPEG_APP_KEEP: Final = {
    0xE0,  # APP0 JFIF
    0xEE,  # APP14 Adobe (colour transform hint)
}
# APP2 may hold ICC profiles; keep those, drop others.
_JPEG_APP_STRIP_UNCONDITIONAL: Final = {0xE1, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xEB, 0xEC, 0xED, 0xEF}
_JPEG_SOF_MARKERS: Final = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}

_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_PNG_STRIP_CHUNKS: Final = {b"eXIf", b"tEXt", b"iTXt", b"zTXt", b"tIME"}

_WEBP_STRIP_CHUNKS: Final = {b"EXIF", b"XMP "}

_PIL_FORMAT_BY_MIME: Final = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
}

Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class ImageScrubError(ValueError):
    """Raised when image bytes cannot be decoded or safely re-encoded."""


@dataclass(frozen=True)
class ScrubResult:
    data: bytes
    mime: str
    width: int
    height: int
    preview_base64: str | None


def probe_dimensions(data: bytes, mime: str) -> tuple[int, int]:
    """Parse image header only; never allocates pixel memory."""
    if mime == "image/png":
        return _probe_png(data)
    if mime == "image/jpeg":
        return _probe_jpeg(data)
    if mime == "image/webp":
        return _probe_webp(data)
    raise ImageScrubError(f"unsupported mime: {mime}")


def _probe_png(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != _PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise ImageScrubError("PNG payload missing IHDR")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _probe_jpeg(data: bytes) -> tuple[int, int]:
    if len(data) < 4 or data[:2] != _JPEG_SOI:
        raise ImageScrubError("JPEG payload missing SOI")
    view = memoryview(data)
    offset = 2
    end = len(data)
    while offset + 3 < end:
        if view[offset] != 0xFF:
            raise ImageScrubError("malformed JPEG marker sequence")
        while offset < end and view[offset] == 0xFF:
            offset += 1
        if offset >= end:
            break
        marker = view[offset]
        offset += 1
        if marker == 0xD8 or marker == 0xD9 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > end:
            raise ImageScrubError("truncated JPEG segment")
        length = struct.unpack(">H", bytes(view[offset:offset + 2]))[0]
        if length < 2 or offset + length > end:
            raise ImageScrubError("invalid JPEG segment length")
        if marker in _JPEG_SOF_MARKERS:
            payload = bytes(view[offset + 2:offset + length])
            if len(payload) < 5:
                raise ImageScrubError("truncated JPEG SOF segment")
            height, width = struct.unpack(">HH", payload[1:5])
            return width, height
        offset += length
        if marker == 0xDA:  # SOS — dimensions must precede this
            break
    raise ImageScrubError("JPEG payload missing SOF marker")


def _probe_webp(data: bytes) -> tuple[int, int]:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ImageScrubError("WEBP payload missing RIFF/WEBP header")
    chunk_id = data[12:16]
    if chunk_id == b"VP8X":
        # canvas dims are 24-bit LE, encoded as value + 1
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk_id == b"VP8 ":
        # simple lossy: 3-byte marker at 20..23, then width/height BE (14 bits each)
        width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        return width, height
    if chunk_id == b"VP8L":
        # lossless: byte 20 is signature 0x2F, next 4 bytes hold packed dims
        if data[20] != 0x2F:
            raise ImageScrubError("invalid VP8L signature")
        packed = struct.unpack("<I", data[21:25])[0]
        width = (packed & 0x3FFF) + 1
        height = ((packed >> 14) & 0x3FFF) + 1
        return width, height
    raise ImageScrubError(f"unknown WEBP chunk: {chunk_id!r}")


def _jpeg_exif_orientation(data: bytes) -> int:
    """Return EXIF Orientation for a JPEG, defaulting to 1 (upright)."""
    view = memoryview(data)
    offset = 2
    end = len(data)
    while offset + 3 < end:
        if view[offset] != 0xFF:
            return 1
        while offset < end and view[offset] == 0xFF:
            offset += 1
        if offset >= end:
            return 1
        marker = view[offset]
        offset += 1
        if marker == 0xD8 or marker == 0xD9 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > end:
            return 1
        length = struct.unpack(">H", bytes(view[offset:offset + 2]))[0]
        if length < 2 or offset + length > end:
            return 1
        segment = bytes(view[offset + 2:offset + length])
        offset += length
        if marker == 0xDA:
            return 1
        if marker == 0xE1 and segment.startswith(b"Exif\x00\x00"):
            return _read_orientation_from_exif(segment[6:])
    return 1


def _read_orientation_from_exif(tiff: bytes) -> int:
    if len(tiff) < 12:
        return 1
    if tiff[:2] == b"II":
        endian = "<"
    elif tiff[:2] == b"MM":
        endian = ">"
    else:
        return 1
    (magic,) = struct.unpack(f"{endian}H", tiff[2:4])
    if magic != 0x002A:
        return 1
    (ifd_offset,) = struct.unpack(f"{endian}I", tiff[4:8])
    if ifd_offset + 2 > len(tiff):
        return 1
    (count,) = struct.unpack(f"{endian}H", tiff[ifd_offset:ifd_offset + 2])
    entry_offset = ifd_offset + 2
    for _ in range(count):
        if entry_offset + 12 > len(tiff):
            return 1
        tag, kind = struct.unpack(f"{endian}HH", tiff[entry_offset:entry_offset + 4])
        if tag == 0x0112:  # Orientation
            value_bytes = tiff[entry_offset + 8:entry_offset + 12]
            if kind == 3:  # SHORT
                (value,) = struct.unpack(f"{endian}H", value_bytes[:2])
                if 1 <= value <= 8:
                    return value
        entry_offset += 12
    return 1


def _strip_png_metadata(data: bytes) -> bytes:
    """Return PNG bytes with metadata chunks (eXIf/tEXt/iTXt/zTXt/tIME) removed."""
    if data[:8] != _PNG_SIGNATURE:
        raise ImageScrubError("PNG payload missing signature")
    output = bytearray(_PNG_SIGNATURE)
    offset = 8
    end = len(data)
    while offset + 12 <= end:
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        chunk_type = data[offset + 4:offset + 8]
        chunk_end = offset + 8 + length + 4  # length + type + payload + crc
        if chunk_end > end:
            raise ImageScrubError("truncated PNG chunk")
        if chunk_type not in _PNG_STRIP_CHUNKS:
            output.extend(data[offset:chunk_end])
        offset = chunk_end
        if chunk_type == b"IEND":
            break
    return bytes(output)


def _strip_jpeg_metadata(data: bytes) -> bytes:
    """Return JPEG bytes with EXIF/XMP/IPTC APP segments removed. Preserves JFIF, ICC, Adobe."""
    if data[:2] != _JPEG_SOI:
        raise ImageScrubError("JPEG payload missing SOI")
    output = bytearray(_JPEG_SOI)
    offset = 2
    end = len(data)
    while offset < end:
        if data[offset] != 0xFF:
            raise ImageScrubError("malformed JPEG marker sequence")
        start = offset
        while offset < end and data[offset] == 0xFF:
            offset += 1
        if offset >= end:
            raise ImageScrubError("truncated JPEG marker")
        marker = data[offset]
        offset += 1
        if marker == 0xD9:  # EOI
            output.extend(data[start:offset])
            break
        if 0xD0 <= marker <= 0xD7 or marker == 0xD8:
            output.extend(data[start:offset])
            continue
        if marker == 0xDA:  # SOS: keep header + all entropy data through EOI
            length = struct.unpack(">H", data[offset:offset + 2])[0]
            payload_end = offset + length
            output.extend(data[start:payload_end])
            # Scan entropy-coded segment until next non-restart marker
            scan = payload_end
            while scan < end:
                if data[scan] != 0xFF:
                    scan += 1
                    continue
                lookahead = scan + 1
                if lookahead >= end:
                    scan = lookahead
                    break
                next_byte = data[lookahead]
                if next_byte == 0x00 or 0xD0 <= next_byte <= 0xD7:
                    scan = lookahead + 1
                    continue
                break
            output.extend(data[payload_end:scan])
            offset = scan
            continue
        if offset + 2 > end:
            raise ImageScrubError("truncated JPEG segment length")
        length = struct.unpack(">H", data[offset:offset + 2])[0]
        if length < 2 or offset + length > end:
            raise ImageScrubError("invalid JPEG segment length")
        payload = data[offset + 2:offset + length]
        segment_end = offset + length
        drop = False
        if marker == 0xE1 and (payload.startswith(b"Exif\x00\x00") or payload.startswith(b"http://ns.adobe.com/xap/")):
            drop = True
        elif marker == 0xE2 and not payload.startswith(b"ICC_PROFILE\x00"):
            drop = True
        elif marker in _JPEG_APP_STRIP_UNCONDITIONAL and marker not in (0xE1, 0xE2):
            drop = True
        # Also strip COM (0xFE) which often carries editor comments.
        elif marker == 0xFE:
            drop = True
        if not drop:
            output.extend(data[start:segment_end])
        offset = segment_end
    return bytes(output)


def _strip_webp_metadata(data: bytes) -> bytes:
    """Return WEBP bytes with EXIF/XMP chunks removed."""
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ImageScrubError("WEBP payload missing RIFF/WEBP header")
    output = bytearray(data[8:12])  # WEBP marker after we replace the header size
    offset = 12
    end = len(data)
    while offset + 8 <= end:
        chunk_id = data[offset:offset + 4]
        length = struct.unpack("<I", data[offset + 4:offset + 8])[0]
        padded = length + (length & 1)
        chunk_end = offset + 8 + padded
        if chunk_end > end:
            raise ImageScrubError("truncated WEBP chunk")
        if chunk_id not in _WEBP_STRIP_CHUNKS:
            output.extend(data[offset:chunk_end])
        offset = chunk_end
    payload = bytes(output)
    size = struct.pack("<I", len(payload))
    return b"RIFF" + size + payload


_METADATA_STRIPPERS = {
    "image/png": _strip_png_metadata,
    "image/jpeg": _strip_jpeg_metadata,
    "image/webp": _strip_webp_metadata,
}


def _rotate_and_reencode(data: bytes, mime: str) -> bytes:
    """Fallback path: decode, apply orientation, drop metadata, re-encode."""
    expected = _PIL_FORMAT_BY_MIME[mime]
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        if source.format != expected:
            raise ImageScrubError(
                f"payload declares {mime} but decodes as {source.format}"
            )
        icc = source.info.get("icc_profile")
        oriented = ImageOps.exif_transpose(source) or source
        if oriented.mode not in {"1", "L", "LA", "P", "PA", "RGB", "RGBA", "CMYK", "I;16"}:
            oriented = oriented.convert("RGBA" if "A" in oriented.mode else "RGB")
        buffer = io.BytesIO()
        save_kwargs: dict[str, object] = {"format": expected}
        if icc:
            save_kwargs["icc_profile"] = icc
        if expected == "JPEG":
            if oriented.mode not in {"RGB", "L", "CMYK"}:
                oriented = oriented.convert("RGB")
            save_kwargs.update(quality=95, optimize=True, progressive=True, subsampling=0)
        elif expected == "PNG":
            save_kwargs["optimize"] = True
        elif expected == "WEBP":
            save_kwargs.update(quality=95, method=4)
        oriented.save(buffer, **save_kwargs)
    return buffer.getvalue()


def _generate_preview(scrubbed: bytes, mime: str, width: int, height: int) -> str | None:
    """Return a tiny base64 preview that browsers can render blurred behind
    the full image. Returns None if generation fails or is not worthwhile."""
    if width <= 0 or height <= 0:
        return None
    long_side = max(width, height)
    if long_side <= PREVIEW_MAX_SIDE:
        return None
    scale = PREVIEW_MAX_SIDE / long_side
    target = (max(1, round(width * scale)), max(1, round(height * scale)))
    try:
        with Image.open(io.BytesIO(scrubbed)) as source:
            source.load()
            source.thumbnail(target, Image.Resampling.BILINEAR)
            preview = source.convert("RGB")
            buffer = io.BytesIO()
            preview.save(buffer, format="JPEG", quality=PREVIEW_QUALITY, optimize=True)
    except Exception:  # noqa: BLE001
        return None
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def scrub_image(data: bytes, mime: str) -> ScrubResult:
    """Validate + strip metadata from image bytes. Rotates pixels if the source
    was tagged with a non-upright EXIF orientation, otherwise keeps the original
    pixel stream and only rewrites the container to drop metadata segments.

    Enforces side + total-pixel caps before decoding to defend against
    decompression bombs.
    """
    expected = _PIL_FORMAT_BY_MIME.get(mime)
    if expected is None:
        raise ImageScrubError(f"unsupported mime: {mime}")

    width, height = probe_dimensions(data, mime)
    if width <= 0 or height <= 0:
        raise ImageScrubError("image reports non-positive dimensions")
    if width > MAX_SIDE or height > MAX_SIDE:
        raise ImageScrubError(
            f"image exceeds {MAX_SIDE}px side limit ({width}x{height})"
        )
    if width * height > MAX_PIXELS:
        raise ImageScrubError(
            f"image exceeds {MAX_PIXELS // 1_000_000}MP pixel limit ({width}x{height})"
        )

    orientation = 1
    if mime == "image/jpeg":
        try:
            orientation = _jpeg_exif_orientation(data)
        except (struct.error, ValueError, IndexError):
            orientation = 1

    try:
        if orientation != 1:
            scrubbed = _rotate_and_reencode(data, mime)
            width, height = probe_dimensions(scrubbed, mime)
        else:
            scrubbed = _METADATA_STRIPPERS[mime](data)
    except ImageScrubError:
        raise
    except Exception as exc:  # noqa: BLE001 — normalise Pillow's many exceptions
        raise ImageScrubError(f"could not scrub image bytes: {exc}") from exc

    preview = _generate_preview(scrubbed, mime, width, height)
    return ScrubResult(
        data=scrubbed,
        mime=mime,
        width=width,
        height=height,
        preview_base64=preview,
    )


def scrub_image_bytes(data: bytes, mime: str) -> bytes:
    """Backwards-compatible wrapper returning just the scrubbed bytes."""
    return scrub_image(data, mime).data
