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


if __name__ == "__main__":
    unittest.main()
