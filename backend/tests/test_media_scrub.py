from __future__ import annotations

import struct
import unittest
import zlib

from backend.app import media_scrub


# ---------------------------------------------------------------------------
# Fixture builders — every media fixture is minimal but structurally valid so
# it exercises the same scrub paths a real payload would.
# ---------------------------------------------------------------------------

def _mp4_atom(atom_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + atom_type + payload


def _mp4_mvhd(timescale: int, duration: int) -> bytes:
    # v0 mvhd: flags(4) creation(4) modification(4) timescale(4) duration(4)
    # rate(4) volume(2) reserved(10) matrix(36) pre_defined(24) next_track(4)
    body = (
        b"\x00\x00\x00\x00"
        + b"\x00" * 4
        + b"\x00" * 4
        + struct.pack(">I", timescale)
        + struct.pack(">I", duration)
        + b"\x00\x01\x00\x00"  # rate
        + b"\x01\x00"  # volume
        + b"\x00" * 10
        + b"\x00" * 36
        + b"\x00" * 24
        + b"\x00" * 4
    )
    return _mp4_atom(b"mvhd", body)


def _mp4_tkhd(width: int, height: int) -> bytes:
    # v0 tkhd: flags(4) creation(4) modification(4) trackID(4) reserved(4)
    # duration(4) reserved(8) layer(2) alt_group(2) volume(2) reserved(2)
    # matrix(36) width(4 fixed) height(4 fixed)
    body = (
        b"\x00\x00\x00\x07"
        + b"\x00" * 4
        + b"\x00" * 4
        + b"\x00\x00\x00\x01"
        + b"\x00" * 4
        + b"\x00" * 4
        + b"\x00" * 8
        + b"\x00" * 2
        + b"\x00" * 2
        + b"\x00" * 2
        + b"\x00" * 2
        + b"\x00" * 36
        + struct.pack(">I", width << 16)
        + struct.pack(">I", height << 16)
    )
    return _mp4_atom(b"tkhd", body)


def _mp4_udta_gps() -> bytes:
    # ©xyz atom carrying a fake ISO 6709 GPS string.
    xyz_payload = struct.pack(">HH", 16, 0x15C7) + b"+40.7128-074.0060/"
    inner = _mp4_atom(b"\xa9xyz", xyz_payload)
    return _mp4_atom(b"udta", inner)


def _minimal_mp4(*, with_gps: bool = True) -> bytes:
    ftyp = _mp4_atom(b"ftyp", b"isom" + b"\x00\x00\x02\x00" + b"isom" + b"mp41")
    trak_children = _mp4_tkhd(320, 240)
    trak = _mp4_atom(b"trak", trak_children)
    moov_children = _mp4_mvhd(timescale=1000, duration=2500) + trak
    if with_gps:
        moov_children += _mp4_udta_gps()
    moov = _mp4_atom(b"moov", moov_children)
    mdat = _mp4_atom(b"mdat", b"pretend-frame-bytes-here")
    return ftyp + moov + mdat


def _gif_bytes(*, with_xmp: bool = False) -> bytes:
    header = b"GIF89a"
    # 4x2 logical screen, no global color table, background 0, aspect 0
    lsd = struct.pack("<HH", 4, 2) + b"\x00\x00\x00"
    body = bytearray(header + lsd)
    if with_xmp:
        # Application Extension: 0x21 0xFF <length=11> "XMP DataXMP" <sub-blocks>
        body += b"\x21\xff\x0b" + b"XMP DataXMP"
        # A single 4-byte payload sub-block then terminator.
        body += b"\x04metadata\x00"[:5] + b"\x00"
    # Image Descriptor: 0x2C left(2) top(2) width(2) height(2) packed(1)
    body += b"\x2c" + struct.pack("<HHHH", 0, 0, 4, 2) + b"\x00"
    # LZW min code size + a single sub-block with dummy bytes + terminator.
    body += b"\x02\x02\x44\x01\x00"
    body += b"\x3b"
    return bytes(body)


def _wav_bytes(*, with_list: bool = False) -> bytes:
    fmt_chunk = b"fmt " + struct.pack(
        "<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16
    )
    data_chunk = b"data" + struct.pack("<I", 8) + b"\x00\x01" * 4
    body = b"WAVE" + fmt_chunk
    if with_list:
        list_payload = b"INFO" + b"IART" + struct.pack("<I", 8) + b"secret\x00\x00"
        body += b"LIST" + struct.pack("<I", len(list_payload)) + list_payload
    body += data_chunk
    header = b"RIFF" + struct.pack("<I", len(body)) + body[:4]
    return header + body[4:]


def _mp3_bytes(*, with_id3v2: bool = False, with_id3v1: bool = False) -> bytes:
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 24  # minimal MPEG-1 layer3 frame
    body = frame * 4
    if with_id3v2:
        payload = b"\x00" * 20
        header = b"ID3\x04\x00\x00" + bytes(
            [
                (len(payload) >> 21) & 0x7F,
                (len(payload) >> 14) & 0x7F,
                (len(payload) >> 7) & 0x7F,
                len(payload) & 0x7F,
            ]
        )
        body = header + payload + body
    if with_id3v1:
        body = body + b"TAG" + b"secret".ljust(125, b"\x00")
    return body


class ScrubMp4Tests(unittest.TestCase):
    def test_scrub_extracts_duration_and_dims(self) -> None:
        result = media_scrub.scrub_video(_minimal_mp4(), "video/mp4")
        self.assertEqual(result.mime, "video/mp4")
        self.assertEqual(result.duration_ms, 2500)
        self.assertEqual((result.width, result.height), (320, 240))

    def test_scrub_removes_top_level_udta_gps(self) -> None:
        original = _minimal_mp4(with_gps=True)
        self.assertIn(b"+40.7128-074.0060", original)
        result = media_scrub.scrub_video(original, "video/mp4")
        self.assertNotIn(b"+40.7128-074.0060", result.data)
        self.assertNotIn(b"udta", result.data)

    def test_scrub_removes_gps_inside_moov(self) -> None:
        # udta nested inside moov must also be stripped.
        ftyp = _mp4_atom(
            b"ftyp", b"isom" + b"\x00\x00\x02\x00" + b"isom" + b"mp41"
        )
        moov = _mp4_atom(
            b"moov",
            _mp4_mvhd(1000, 1000) + _mp4_udta_gps() + _mp4_tkhd(10, 10),
        )
        mdat = _mp4_atom(b"mdat", b"x")
        result = media_scrub.scrub_video(ftyp + moov + mdat, "video/mp4")
        self.assertNotIn(b"+40.7128-074.0060", result.data)
        # The rewritten moov should still contain mvhd and tkhd (so playback works).
        self.assertIn(b"mvhd", result.data)
        self.assertIn(b"tkhd", result.data)

    def test_scrub_rejects_missing_ftyp(self) -> None:
        payload = _mp4_atom(b"moov", _mp4_mvhd(1000, 1000))
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_video(payload, "video/mp4")

    def test_scrub_rejects_truncated_atom(self) -> None:
        ftyp = _mp4_atom(b"ftyp", b"isom" + b"\x00\x00\x02\x00")
        truncated = ftyp + b"\x00\x00\x00\x40moov" + b"only-a-few-bytes"
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_video(truncated, "video/mp4")


class ScrubGifTests(unittest.TestCase):
    def test_scrub_gif_returns_dims(self) -> None:
        result = media_scrub.scrub_video(_gif_bytes(), "image/gif")
        self.assertEqual(result.mime, "image/gif")
        self.assertEqual((result.width, result.height), (4, 2))

    def test_scrub_drops_xmp_application_extension(self) -> None:
        payload = _gif_bytes(with_xmp=True)
        self.assertIn(b"XMP DataXMP", payload)
        scrubbed = media_scrub.scrub_video(payload, "image/gif").data
        self.assertNotIn(b"XMP DataXMP", scrubbed)
        # Trailer should still terminate the stream.
        self.assertEqual(scrubbed[-1], 0x3B)

    def test_scrub_rejects_wrong_magic(self) -> None:
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_video(b"NOTGIF" + b"\x00" * 40, "image/gif")


class ScrubMatroskaTests(unittest.TestCase):
    def test_scrub_validates_ebml_header(self) -> None:
        payload = b"\x1a\x45\xdf\xa3" + b"\x00" * 40
        result = media_scrub.scrub_video(payload, "video/webm")
        self.assertEqual(result.mime, "video/webm")
        self.assertEqual(result.data, payload)

    def test_scrub_rejects_missing_ebml(self) -> None:
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_video(b"not-webm-header", "video/webm")


class ScrubWavTests(unittest.TestCase):
    def test_scrub_computes_duration(self) -> None:
        result = media_scrub.scrub_audio(_wav_bytes(), "audio/wav")
        self.assertEqual(result.mime, "audio/wav")
        self.assertIsNotNone(result.duration_ms)
        # 8 bytes data at 16000 byte_rate = 0.5 ms.
        assert result.duration_ms is not None
        self.assertEqual(result.duration_ms, 0)

    def test_scrub_drops_list_info_chunk(self) -> None:
        payload = _wav_bytes(with_list=True)
        self.assertIn(b"secret", payload)
        result = media_scrub.scrub_audio(payload, "audio/wav")
        self.assertNotIn(b"secret", result.data)
        self.assertNotIn(b"LIST", result.data)
        self.assertEqual(result.data[:4], b"RIFF")
        self.assertEqual(result.data[8:12], b"WAVE")
        riff_size = struct.unpack("<I", result.data[4:8])[0]
        self.assertEqual(riff_size, len(result.data) - 8)

    def test_scrub_rejects_missing_fmt(self) -> None:
        header = b"RIFF" + struct.pack("<I", 8) + b"WAVE" + b"data" + struct.pack("<I", 0)
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_audio(header, "audio/wav")

    def test_scrub_rejects_missing_magic(self) -> None:
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_audio(b"RIFF" + b"\x00" * 8 + b"NOPE", "audio/wav")


class ScrubMp3Tests(unittest.TestCase):
    def test_scrub_passes_frames(self) -> None:
        payload = _mp3_bytes()
        result = media_scrub.scrub_audio(payload, "audio/mpeg")
        self.assertEqual(result.mime, "audio/mpeg")
        self.assertEqual(result.data, payload)

    def test_scrub_strips_id3v2_prefix(self) -> None:
        payload = _mp3_bytes(with_id3v2=True)
        result = media_scrub.scrub_audio(payload, "audio/mpeg")
        self.assertFalse(result.data.startswith(b"ID3"))
        self.assertEqual(result.data[:2], b"\xff\xfb")

    def test_scrub_strips_id3v1_suffix(self) -> None:
        payload = _mp3_bytes(with_id3v1=True)
        self.assertIn(b"secret", payload)
        result = media_scrub.scrub_audio(payload, "audio/mpeg")
        self.assertNotIn(b"secret", result.data)
        self.assertNotIn(b"TAG", result.data[-128:])

    def test_scrub_rejects_missing_sync(self) -> None:
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_audio(b"\x00" * 128, "audio/mpeg")


class ScrubOggTests(unittest.TestCase):
    def test_scrub_validates_ogg_header(self) -> None:
        payload = b"OggS" + b"\x00" * 40
        result = media_scrub.scrub_audio(payload, "audio/ogg")
        self.assertEqual(result.mime, "audio/ogg")

    def test_scrub_rejects_missing_ogg(self) -> None:
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_audio(b"NOTAOGG", "audio/ogg")


class UnsupportedMimeTests(unittest.TestCase):
    def test_scrub_video_rejects_audio_mime(self) -> None:
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_video(b"\x00" * 32, "audio/wav")

    def test_scrub_audio_rejects_video_mime(self) -> None:
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_audio(b"\x00" * 32, "video/mp4")


# Silence unused-import lints from earlier fixture drafts that referenced zlib.
del zlib


if __name__ == "__main__":
    unittest.main()
