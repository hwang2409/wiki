from __future__ import annotations

import io
from typing import Final

from PIL import Image, ImageOps

_SAFE_MODES: Final = {"1", "L", "LA", "P", "PA", "RGB", "RGBA", "CMYK", "I;16"}
_PIL_FORMAT_BY_MIME: Final = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
}


class ImageScrubError(ValueError):
    """Raised when image bytes cannot be decoded or safely re-encoded."""


def scrub_image_bytes(data: bytes, mime: str) -> bytes:
    """Return re-encoded image bytes with EXIF/XMP/GPS metadata removed.

    Rotation encoded via EXIF Orientation is baked into the pixel data before
    the metadata is dropped, so a photo captured in portrait mode continues to
    render right-side up after the scrub. ICC colour profiles are preserved.
    """
    expected = _PIL_FORMAT_BY_MIME.get(mime)
    if expected is None:
        raise ImageScrubError(f"unsupported mime: {mime}")
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            if source.format != expected:
                raise ImageScrubError(
                    f"payload declares {mime} but decodes as {source.format}"
                )
            icc = source.info.get("icc_profile")
            oriented = ImageOps.exif_transpose(source) or source
            if oriented.mode not in _SAFE_MODES:
                oriented = oriented.convert("RGBA" if "A" in oriented.mode else "RGB")
            buffer = io.BytesIO()
            save_kwargs: dict[str, object] = {"format": expected}
            if icc:
                save_kwargs["icc_profile"] = icc
            if expected == "JPEG":
                if oriented.mode not in {"RGB", "L", "CMYK"}:
                    oriented = oriented.convert("RGB")
                save_kwargs.update(
                    quality=92,
                    optimize=True,
                    progressive=True,
                    subsampling=0,
                )
            elif expected == "PNG":
                save_kwargs["optimize"] = True
            elif expected == "WEBP":
                save_kwargs.update(quality=92, method=4)
            oriented.save(buffer, **save_kwargs)
    except ImageScrubError:
        raise
    except Exception as exc:  # noqa: BLE001 — Pillow raises many exception classes
        raise ImageScrubError(f"could not decode image bytes: {exc}") from exc
    return buffer.getvalue()
