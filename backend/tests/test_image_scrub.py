from __future__ import annotations

import io
import unittest

from PIL import Image, TiffImagePlugin
from PIL.ExifTags import Base as ExifBase, GPS as GpsTag

from backend.app.image_scrub import ImageScrubError, scrub_image_bytes


def _sample_gps_ifd() -> dict[int, object]:
    return {
        GpsTag.GPSLatitudeRef: "N",
        GpsTag.GPSLatitude: (
            TiffImagePlugin.IFDRational(37, 1),
            TiffImagePlugin.IFDRational(46, 1),
            TiffImagePlugin.IFDRational(3060, 100),
        ),
        GpsTag.GPSLongitudeRef: "W",
        GpsTag.GPSLongitude: (
            TiffImagePlugin.IFDRational(122, 1),
            TiffImagePlugin.IFDRational(24, 1),
            TiffImagePlugin.IFDRational(5580, 100),
        ),
    }


def _jpeg_with_exif(orientation: int, width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height), color="white")
    # Give the top-left corner a marker so we can detect rotation.
    for x in range(min(4, width)):
        for y in range(min(4, height)):
            image.putpixel((x, y), (0, 0, 0))
    exif = image.getexif()
    exif[ExifBase.Orientation.value] = orientation
    exif[ExifBase.Make.value] = "TestCam"
    exif[ExifBase.Model.value] = "GPS-Model"
    exif[ExifBase.DateTime.value] = "2026:07:30 12:00:00"
    gps_ifd = exif.get_ifd(ExifBase.GPSInfo.value)
    gps_ifd.update(_sample_gps_ifd())
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif.tobytes(), quality=95)
    return buffer.getvalue()


def _png_with_metadata() -> bytes:
    from PIL import PngImagePlugin

    image = Image.new("RGB", (4, 4), color="white")
    info = PngImagePlugin.PngInfo()
    info.add_text("GPSLatitude", "37.7749")
    info.add_text("GPSLongitude", "-122.4194")
    info.add_text("Software", "TestApp")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", pnginfo=info)
    return buffer.getvalue()


class ImageScrubTests(unittest.TestCase):
    def test_jpeg_gps_and_camera_metadata_stripped(self) -> None:
        original = _jpeg_with_exif(orientation=1, width=8, height=8)
        self.assertIn(b"GPS-Model", original)
        self.assertIn(b"TestCam", original)

        scrubbed = scrub_image_bytes(original, "image/jpeg")

        self.assertNotIn(b"GPS-Model", scrubbed)
        self.assertNotIn(b"TestCam", scrubbed)
        self.assertNotIn(b"GPSLatitude", scrubbed)

        with Image.open(io.BytesIO(scrubbed)) as reopened:
            reopened.load()
            self.assertEqual(reopened.format, "JPEG")
            self.assertEqual(dict(reopened.getexif()), {})
            self.assertEqual(reopened.size, (8, 8))

    def test_jpeg_orientation_is_baked_into_pixels(self) -> None:
        # Orientation 6 tells viewers to rotate 90 degrees clockwise. When we
        # scrub the metadata we must rotate the pixels first, otherwise the
        # image will render on its side.
        original = _jpeg_with_exif(orientation=6, width=16, height=24)
        scrubbed = scrub_image_bytes(original, "image/jpeg")

        with Image.open(io.BytesIO(scrubbed)) as reopened:
            reopened.load()
            self.assertEqual(dict(reopened.getexif()), {})
            # Orientation 6 swaps the axes: source 16x24 becomes 24x16 upright.
            self.assertEqual(reopened.size, (24, 16))
            # The black marker that started at (0..3, 0..3) should land in the
            # top-right corner after a 90-degree clockwise rotation. Allow
            # small JPEG quantization drift on the edge pixels.
            top_right = reopened.getpixel((reopened.width - 2, 1))
            top_left = reopened.getpixel((1, 1))
            self.assertLess(sum(top_right[:3]), 60)
            self.assertGreater(sum(top_left[:3]), 600)

    def test_png_text_metadata_stripped(self) -> None:
        original = _png_with_metadata()
        self.assertIn(b"GPSLatitude", original)

        scrubbed = scrub_image_bytes(original, "image/png")

        self.assertNotIn(b"GPSLatitude", scrubbed)
        self.assertNotIn(b"GPSLongitude", scrubbed)
        self.assertNotIn(b"TestApp", scrubbed)

    def test_mime_mismatch_rejected(self) -> None:
        png_bytes = _png_with_metadata()
        with self.assertRaises(ImageScrubError):
            scrub_image_bytes(png_bytes, "image/jpeg")

    def test_unsupported_mime_rejected(self) -> None:
        with self.assertRaises(ImageScrubError):
            scrub_image_bytes(b"whatever", "image/gif")

    def test_garbage_bytes_rejected(self) -> None:
        with self.assertRaises(ImageScrubError):
            scrub_image_bytes(b"not an image", "image/png")

    def test_pixel_bomb_rejected_before_decode(self) -> None:
        # Craft a PNG header that advertises 60_000 x 60_000 (3.6 gigapixels)
        # but do not deliver any IDAT bytes: the check must reject on shape
        # alone rather than allocating pixel memory.
        from zlib import crc32

        def chunk(kind: bytes, payload: bytes) -> bytes:
            return (
                len(payload).to_bytes(4, "big") + kind + payload + crc32(kind + payload).to_bytes(4, "big")
            )

        header = (
            (60_000).to_bytes(4, "big")
            + (60_000).to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00"  # bit depth 8, colour type 2 (RGB)
        )
        bomb = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IEND", b"")
        with self.assertRaisesRegex(ImageScrubError, "pixel limit|side limit"):
            scrub_image_bytes(bomb, "image/png")

    def test_metadata_only_strip_preserves_bytes_when_no_metadata(self) -> None:
        # A PNG built without eXIf/tEXt/iTXt/zTXt/tIME should be byte-identical
        # after the scrub: no lossy re-encode, no chunk rewrite.
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), color=(255, 128, 0)).save(buffer, format="PNG")
        original = buffer.getvalue()
        self.assertEqual(scrub_image_bytes(original, "image/png"), original)

    def test_scrub_returns_dimensions_and_preview(self) -> None:
        from backend.app.image_scrub import scrub_image

        buffer = io.BytesIO()
        Image.new("RGB", (200, 100), color=(0, 128, 255)).save(buffer, format="PNG")
        result = scrub_image(buffer.getvalue(), "image/png")
        self.assertEqual((result.width, result.height), (200, 100))
        self.assertIsNotNone(result.preview_base64)
        self.assertTrue(result.preview_base64.startswith("data:image/jpeg;base64,"))

    def test_scrub_omits_preview_for_tiny_images(self) -> None:
        from backend.app.image_scrub import scrub_image

        buffer = io.BytesIO()
        Image.new("RGB", (16, 16), color=(0, 0, 0)).save(buffer, format="PNG")
        result = scrub_image(buffer.getvalue(), "image/png")
        self.assertIsNone(result.preview_base64)

    def test_orientation_1_jpeg_skips_reencode(self) -> None:
        # An orientation-1 JPEG must NOT be re-encoded — only its metadata is
        # rebuilt. The stripped output should be within a few bytes of the
        # source and the pixel data must be byte-identical.
        buffer = io.BytesIO()
        image = Image.new("RGB", (32, 24), color=(10, 200, 50))
        image.save(buffer, format="JPEG", quality=90)
        original = buffer.getvalue()
        scrubbed = scrub_image_bytes(original, "image/jpeg")
        # No metadata was present, so the strip should return the exact bytes.
        self.assertEqual(scrubbed, original)

    def test_extended_xmp_and_gpano_app1_dropped(self) -> None:
        # Extended-XMP APP1 uses http://ns.adobe.com/xmp/extension/ as its
        # signature — the round-2 strip only dropped the plain XMP signature
        # and let this survive. Any APP1 flavour must go.
        buffer = io.BytesIO()
        Image.new("RGB", (16, 16), color=(30, 30, 30)).save(buffer, format="JPEG", quality=90)
        base_jpeg = buffer.getvalue()
        # Insert a fake APP1 segment right after the SOI marker.
        signature = b"http://ns.adobe.com/xmp/extension/\x00"
        marker_payload = signature + b"<xmp><gps>SECRET-GPS-COORDS</gps></xmp>"
        length = (len(marker_payload) + 2).to_bytes(2, "big")
        smuggled = base_jpeg[:2] + b"\xff\xe1" + length + marker_payload + base_jpeg[2:]
        self.assertIn(b"SECRET-GPS-COORDS", smuggled)
        scrubbed = scrub_image_bytes(smuggled, "image/jpeg")
        self.assertNotIn(b"SECRET-GPS-COORDS", scrubbed)
        self.assertNotIn(b"ns.adobe.com/xmp/extension", scrubbed)

    def test_png_orientation_baked_into_pixels(self) -> None:
        from zlib import crc32

        from PIL.ExifTags import Base as ExifBase

        image = Image.new("RGB", (16, 24), color=(255, 255, 255))
        for x in range(4):
            for y in range(4):
                image.putpixel((x, y), (0, 0, 0))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
        # Pillow doesn't expose an API for arbitrary PNG chunks, so splice
        # an eXIf chunk in by hand right after the IHDR.
        exif = image.getexif()
        exif[ExifBase.Orientation.value] = 6
        tiff = exif.tobytes(offset=0)
        crc = crc32(b"eXIf" + tiff).to_bytes(4, "big")
        exif_chunk = len(tiff).to_bytes(4, "big") + b"eXIf" + tiff + crc
        # Splice the chunk after IHDR (bytes 8..33 hold IHDR).
        ihdr_end = 8 + 4 + 4 + 13 + 4  # length + type + payload + crc
        data_with_exif = data[:ihdr_end] + exif_chunk + data[ihdr_end:]
        scrubbed = scrub_image_bytes(data_with_exif, "image/png")
        with Image.open(io.BytesIO(scrubbed)) as reopened:
            reopened.load()
            self.assertEqual(reopened.size, (24, 16))  # axes swapped
            top_right = reopened.getpixel((reopened.width - 1, 0))
            top_left = reopened.getpixel((0, 0))
            self.assertLess(sum(top_right[:3]), 60)
            self.assertGreater(sum(top_left[:3]), 600)
        # Any lingering eXIf chunk in the output must be gone.
        self.assertNotIn(b"eXIf", scrubbed)

    def test_webp_orientation_baked_into_pixels(self) -> None:
        from PIL.ExifTags import Base as ExifBase

        image = Image.new("RGB", (32, 48), color=(255, 255, 255))
        for x in range(6):
            for y in range(6):
                image.putpixel((x, y), (0, 0, 0))
        exif = image.getexif()
        exif[ExifBase.Orientation.value] = 6
        buffer = io.BytesIO()
        image.save(buffer, format="WEBP", quality=95, exif=exif.tobytes(offset=0))
        scrubbed = scrub_image_bytes(buffer.getvalue(), "image/webp")
        with Image.open(io.BytesIO(scrubbed)) as reopened:
            reopened.load()
            self.assertEqual(reopened.size, (48, 32))
            top_right = reopened.getpixel((reopened.width - 2, 1))
            top_left = reopened.getpixel((1, 1))
            self.assertLess(sum(top_right[:3]), 90)
            self.assertGreater(sum(top_left[:3]), 600)
        # Every EXIF chunk in the output must be gone.
        self.assertNotIn(b"EXIF", scrubbed)

    def test_resize_rejects_pixel_bomb_before_decode(self) -> None:
        from zlib import crc32

        from backend.app.image_scrub import ImageScrubError, resize_image_bytes

        def chunk(kind: bytes, payload: bytes) -> bytes:
            return (
                len(payload).to_bytes(4, "big")
                + kind
                + payload
                + crc32(kind + payload).to_bytes(4, "big")
            )

        header = (
            (60_000).to_bytes(4, "big")
            + (60_000).to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00"
        )
        bomb = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IEND", b"")
        with self.assertRaisesRegex(ImageScrubError, "pixel limit|side limit"):
            resize_image_bytes(bomb, "image/png", 320)

    def test_resize_smaller_than_source_shrinks(self) -> None:
        from backend.app.image_scrub import resize_image_bytes

        buffer = io.BytesIO()
        Image.new("RGB", (800, 600), color=(10, 200, 50)).save(buffer, format="PNG")
        resized, out_mime = resize_image_bytes(buffer.getvalue(), "image/png", 320)
        self.assertEqual(out_mime, "image/png")
        with Image.open(io.BytesIO(resized)) as reopened:
            reopened.load()
            self.assertEqual(reopened.size, (320, 240))

    def test_resize_leaves_undersized_source_untouched(self) -> None:
        from backend.app.image_scrub import resize_image_bytes

        buffer = io.BytesIO()
        Image.new("RGB", (100, 100), color=(10, 200, 50)).save(buffer, format="PNG")
        original = buffer.getvalue()
        resized, out_mime = resize_image_bytes(original, "image/png", 320)
        self.assertEqual(resized, original)
        self.assertEqual(out_mime, "image/png")

    def test_resize_bakes_orientation_before_computing_target_height(self) -> None:
        # Source: 400x240 tagged Orientation=6 — canonical upright is 240x400.
        # A 160-wide thumbnail must be 160x266 (240:400 aspect), NOT 160x96
        # (400:240 aspect) which would look distorted.
        from PIL.ExifTags import Base as ExifBase

        from backend.app.image_scrub import resize_image_bytes

        image = Image.new("RGB", (400, 240), color=(10, 200, 50))
        exif = image.getexif()
        exif[ExifBase.Orientation.value] = 6
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90, exif=exif.tobytes())
        resized, out_mime = resize_image_bytes(buffer.getvalue(), "image/jpeg", 160)
        self.assertEqual(out_mime, "image/jpeg")
        with Image.open(io.BytesIO(resized)) as reopened:
            reopened.load()
            self.assertEqual(reopened.size, (160, 267))

    def test_resize_passthrough_still_bakes_orientation(self) -> None:
        # If the source is already narrow enough that no rescale is needed,
        # we must STILL rotate the pixels so served bytes match the metadata
        # dimensions the frontend was told about.
        from PIL.ExifTags import Base as ExifBase

        from backend.app.image_scrub import resize_image_bytes

        image = Image.new("RGB", (80, 60), color=(10, 200, 50))
        exif = image.getexif()
        exif[ExifBase.Orientation.value] = 6
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90, exif=exif.tobytes())
        resized, _ = resize_image_bytes(buffer.getvalue(), "image/jpeg", 320)
        with Image.open(io.BytesIO(resized)) as reopened:
            reopened.load()
            self.assertEqual(reopened.size, (60, 80))

    def test_scrub_returns_scrubbed_dimensions_after_orientation_swap(self) -> None:
        from backend.app.image_scrub import scrub_image

        # Portrait source with orientation=6 should report the upright
        # dimensions, not the raw pre-rotation ones.
        image = Image.new("RGB", (24, 40), color=(10, 200, 50))
        exif = image.getexif()
        from PIL.ExifTags import Base as ExifBase

        exif[ExifBase.Orientation.value] = 6
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90, exif=exif.tobytes())
        result = scrub_image(buffer.getvalue(), "image/jpeg")
        self.assertEqual((result.width, result.height), (40, 24))


if __name__ == "__main__":
    unittest.main()
