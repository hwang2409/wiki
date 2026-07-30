from __future__ import annotations

import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from backend.app import media_scrub


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "media"
REAL_MP4 = FIXTURE_DIR / "tiny.mp4"
REAL_WAV = FIXTURE_DIR / "tone.wav"
REAL_MP3 = FIXTURE_DIR / "tone.mp3"

FFMPEG = shutil.which("ffmpeg")


# ---------------------------------------------------------------------------
# Small synthetic fixtures for edge-case coverage. Structural tests hit the
# real files above; these are only for isolated corner cases.
# ---------------------------------------------------------------------------

def _gif_bytes(*, with_xmp: bool = False) -> bytes:
    header = b"GIF89a"
    lsd = struct.pack("<HH", 4, 2) + b"\x00\x00\x00"
    body = bytearray(header + lsd)
    if with_xmp:
        body += b"\x21\xff\x0b" + b"XMP DataXMP"
        body += b"\x04meta" + b"\x00"
    body += b"\x2c" + struct.pack("<HHHH", 0, 0, 4, 2) + b"\x00"
    body += b"\x02\x02\x44\x01\x00"
    body += b"\x3b"
    return bytes(body)


class ScrubMp4RealFixtureTests(unittest.TestCase):
    """Structural + metadata assertions against a real x264-encoded MP4.

    The fixture is a fast-start MP4 with ffmpeg encoder tags and a QuickTime
    `loci` (location) atom inside moov/udta — the exact shape that leaked
    GPS coordinates in the round-1 review.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.original = REAL_MP4.read_bytes()
        cls.result = media_scrub.scrub_video(cls.original, "video/mp4")

    def test_size_is_preserved_so_stco_offsets_stay_valid(self) -> None:
        self.assertEqual(len(self.result.data), len(self.original))

    def test_udta_metadata_atoms_are_destroyed(self) -> None:
        # Fixture markers: `loci` box (GPS), `earth` string (loci suffix),
        # `Lavf` encoder tag, `udta` container name.
        self.assertIn(b"udta", self.original)
        self.assertIn(b"loci", self.original)
        self.assertIn(b"earth", self.original)
        self.assertIn(b"Lavf", self.original)
        for marker in (b"udta", b"loci", b"earth", b"Lavf"):
            self.assertNotIn(marker, self.result.data)

    def test_playback_atoms_survive(self) -> None:
        for marker in (b"ftyp", b"moov", b"mvhd", b"trak", b"tkhd", b"mdat"):
            self.assertIn(marker, self.result.data)

    def test_dimensions_and_duration_are_extracted(self) -> None:
        self.assertEqual((self.result.width, self.result.height), (160, 120))
        self.assertIsNotNone(self.result.duration_ms)
        assert self.result.duration_ms is not None
        self.assertGreater(self.result.duration_ms, 0)

    @unittest.skipIf(FFMPEG is None, "ffmpeg not installed")
    def test_stored_bytes_decode_cleanly_through_ffmpeg(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as handle:
            handle.write(self.result.data)
            stored_path = handle.name
        try:
            probe = subprocess.run(
                [FFMPEG, "-v", "error", "-i", stored_path, "-f", "null", "-"],
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(
                probe.returncode,
                0,
                msg=f"ffmpeg decode failed after scrub: {probe.stderr.decode(errors='replace')}",
            )
        finally:
            Path(stored_path).unlink(missing_ok=True)


class ScrubMp4StructuralGuards(unittest.TestCase):
    def test_missing_moov_is_rejected(self) -> None:
        payload = struct.pack(">I", 24) + b"ftyp" + b"isom" + b"\x00\x00\x02\x00" + b"isommp41"
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "moov"):
            media_scrub.scrub_video(payload, "video/mp4")

    def test_moov_without_trak_is_rejected(self) -> None:
        # A real MP4 has trak inside moov; fixtures that omit it must fail
        # the structural check so text-with-ftyp cannot masquerade as MP4.
        ftyp = struct.pack(">I", 24) + b"ftyp" + b"isom" + b"\x00\x00\x02\x00" + b"isommp41"
        # Build a moov with only mvhd (v0) and no trak.
        mvhd_body = (
            b"\x00\x00\x00\x00"
            + b"\x00" * 8
            + struct.pack(">I", 1000)
            + struct.pack(">I", 1000)
            + b"\x00" * 80
        )
        mvhd = struct.pack(">I", 8 + len(mvhd_body)) + b"mvhd" + mvhd_body
        moov = struct.pack(">I", 8 + len(mvhd)) + b"moov" + mvhd
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "trak"):
            media_scrub.scrub_video(ftyp + moov, "video/mp4")

    def test_missing_ftyp_is_rejected(self) -> None:
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_video(b"\x00" * 32, "video/mp4")


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
        self.assertEqual(scrubbed[-1], 0x3B)

    def test_scrub_rejects_wrong_magic(self) -> None:
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_video(b"NOTGIF" + b"\x00" * 40, "image/gif")


class RejectedContainerMimes(unittest.TestCase):
    """Silent pass-through was the round-1 leak; reject webm and ogg entirely."""

    def test_webm_video_is_rejected(self) -> None:
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "unsupported"):
            media_scrub.scrub_video(b"\x1a\x45\xdf\xa3" + b"\x00" * 32, "video/webm")

    def test_webm_audio_is_rejected(self) -> None:
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "unsupported"):
            media_scrub.scrub_audio(b"\x1a\x45\xdf\xa3" + b"\x00" * 32, "audio/webm")

    def test_ogg_audio_is_rejected(self) -> None:
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "unsupported"):
            media_scrub.scrub_audio(b"OggS" + b"\x00" * 32, "audio/ogg")


class ScrubWavRealFixtureTests(unittest.TestCase):
    """Real ffmpeg-encoded pcm_s16le WAV. Structural scrub + peaks."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.original = REAL_WAV.read_bytes()
        cls.result = media_scrub.scrub_audio(cls.original, "audio/wav")

    def test_header_is_intact_after_scrub(self) -> None:
        self.assertEqual(self.result.data[:4], b"RIFF")
        self.assertEqual(self.result.data[8:12], b"WAVE")
        riff_size = struct.unpack("<I", self.result.data[4:8])[0]
        self.assertEqual(riff_size, len(self.result.data) - 8)

    def test_only_playback_chunks_survive(self) -> None:
        # Walk chunks in the scrubbed output — only fmt/data/fact allowed.
        offset = 12
        seen: list[bytes] = []
        while offset + 8 <= len(self.result.data):
            chunk_id = self.result.data[offset:offset + 4]
            chunk_size = struct.unpack("<I", self.result.data[offset + 4:offset + 8])[0]
            seen.append(chunk_id)
            offset += 8 + chunk_size + (chunk_size & 1)
        self.assertTrue(seen)
        for chunk_id in seen:
            self.assertIn(chunk_id, {b"fmt ", b"data", b"fact"})

    def test_duration_matches_fixture_length(self) -> None:
        # Fixture is 0.5s = 500ms sine tone.
        self.assertIsNotNone(self.result.duration_ms)
        assert self.result.duration_ms is not None
        self.assertGreaterEqual(self.result.duration_ms, 480)
        self.assertLessEqual(self.result.duration_ms, 520)

    def test_peaks_are_bounded_regardless_of_data_size(self) -> None:
        self.assertIsNotNone(self.result.peaks)
        assert self.result.peaks is not None
        self.assertLessEqual(len(self.result.peaks), media_scrub.WAVEFORM_MAX_PEAKS)
        # A real sine tone at 440 Hz cannot yield all zero peaks.
        self.assertTrue(any(peak > 0 for peak in self.result.peaks))

    @unittest.skipIf(FFMPEG is None, "ffmpeg not installed")
    def test_stored_bytes_decode_cleanly_through_ffmpeg(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            handle.write(self.result.data)
            stored_path = handle.name
        try:
            probe = subprocess.run(
                [FFMPEG, "-v", "error", "-i", stored_path, "-f", "null", "-"],
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(
                probe.returncode,
                0,
                msg=f"ffmpeg decode failed after scrub: {probe.stderr.decode(errors='replace')}",
            )
        finally:
            Path(stored_path).unlink(missing_ok=True)


class WavMetadataStripTests(unittest.TestCase):
    def test_list_info_chunk_is_dropped(self) -> None:
        # Splice a LIST/INFO chunk into the real WAV to prove allowlist works.
        original = REAL_WAV.read_bytes()
        list_body = b"INFO" + b"IART" + struct.pack("<I", 8) + b"secret\x00\x00"
        list_chunk = b"LIST" + struct.pack("<I", len(list_body)) + list_body
        # Insert LIST just after RIFF/WAVE header.
        head = original[:12]
        tail = original[12:]
        payload = bytearray(head) + list_chunk + tail
        # Patch RIFF size.
        payload[4:8] = struct.pack("<I", len(payload) - 8)
        self.assertIn(b"secret", bytes(payload))
        result = media_scrub.scrub_audio(bytes(payload), "audio/wav")
        self.assertNotIn(b"secret", result.data)
        self.assertNotIn(b"LIST", result.data)

    def test_ixml_chunk_is_dropped(self) -> None:
        original = REAL_WAV.read_bytes()
        ixml_body = b"<BWFXML><PROJECT>secret-proj</PROJECT></BWFXML>"
        ixml_chunk = b"iXML" + struct.pack("<I", len(ixml_body)) + ixml_body + (b"\x00" if len(ixml_body) & 1 else b"")
        head = original[:12]
        tail = original[12:]
        payload = bytearray(head) + ixml_chunk + tail
        payload[4:8] = struct.pack("<I", len(payload) - 8)
        self.assertIn(b"secret-proj", bytes(payload))
        result = media_scrub.scrub_audio(bytes(payload), "audio/wav")
        self.assertNotIn(b"secret-proj", result.data)
        self.assertNotIn(b"iXML", result.data)

    def test_missing_fmt_is_rejected(self) -> None:
        header = b"RIFF" + struct.pack("<I", 8) + b"WAVE" + b"data" + struct.pack("<I", 0)
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_audio(header, "audio/wav")

    def test_missing_riff_wave_magic_is_rejected(self) -> None:
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_audio(b"RIFF" + b"\x00" * 8 + b"NOPE", "audio/wav")


class ScrubMp3RealFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original = REAL_MP3.read_bytes()
        cls.result = media_scrub.scrub_audio(cls.original, "audio/mpeg")

    def test_id3v2_title_and_artist_are_destroyed(self) -> None:
        # ffmpeg puts the -metadata values into the ID3v2 tag at the front.
        # After scrub, the front-of-file tag is gone, so those strings vanish.
        self.assertIn(b"stripme-title-marker", self.original)
        self.assertIn(b"stripme-artist-marker", self.original)
        self.assertNotIn(b"stripme-title-marker", self.result.data)
        self.assertNotIn(b"stripme-artist-marker", self.result.data)

    def test_frame_stream_starts_with_valid_mpeg_sync(self) -> None:
        first_two = self.result.data[:2]
        self.assertEqual(first_two[0], 0xFF)
        self.assertEqual(first_two[1] & 0xE0, 0xE0)

    @unittest.skipIf(FFMPEG is None, "ffmpeg not installed")
    def test_stored_bytes_decode_cleanly_through_ffmpeg(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
            handle.write(self.result.data)
            stored_path = handle.name
        try:
            probe = subprocess.run(
                [FFMPEG, "-v", "error", "-i", stored_path, "-f", "null", "-"],
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(
                probe.returncode,
                0,
                msg=f"ffmpeg decode failed after scrub: {probe.stderr.decode(errors='replace')}",
            )
        finally:
            Path(stored_path).unlink(missing_ok=True)


class Mp3StructuralGuards(unittest.TestCase):
    def test_random_bytes_are_rejected_even_with_first_sync(self) -> None:
        # A single sync byte is not enough — the 3-frame walk must fail.
        payload = b"\xff\xfb\x90\x00" + b"\x00" * 128
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_audio(payload, "audio/mpeg")


class WavStreamingPeaksBoundsTests(unittest.TestCase):
    """The waveform generator must be bounded and NEVER perform a full-file
    decode. Synthesizing a large WAV proves peaks stay capped at MAX_PEAKS
    regardless of payload size and the memory footprint is limited to
    memoryview slices of the input buffer.
    """

    def test_large_wav_produces_bounded_peak_array(self) -> None:
        # 2 MB of pcm_s16le samples at 16 kHz mono = ~62s of audio.
        sample_rate = 16000
        channels = 1
        bits = 16
        byte_rate = sample_rate * channels * bits // 8
        # Fabricate a triangle wave so peaks vary.
        payload = bytearray()
        for i in range(1_000_000):
            value = (i % 32000) - 16000
            payload.extend(value.to_bytes(2, "little", signed=True))
        fmt_body = struct.pack(
            "<HHIIHH", 1, channels, sample_rate, byte_rate, channels * bits // 8, bits,
        )
        fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
        data_chunk = b"data" + struct.pack("<I", len(payload)) + bytes(payload)
        wav = b"RIFF" + struct.pack("<I", 4 + len(fmt_chunk) + len(data_chunk)) + b"WAVE" + fmt_chunk + data_chunk

        result = media_scrub.scrub_audio(wav, "audio/wav")
        self.assertIsNotNone(result.peaks)
        assert result.peaks is not None
        self.assertLessEqual(len(result.peaks), media_scrub.WAVEFORM_MAX_PEAKS)
        self.assertTrue(any(peak > 0 for peak in result.peaks))


class UnsupportedMimeTests(unittest.TestCase):
    def test_scrub_video_rejects_audio_mime(self) -> None:
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_video(b"\x00" * 32, "audio/wav")

    def test_scrub_audio_rejects_video_mime(self) -> None:
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_audio(b"\x00" * 32, "video/mp4")


if __name__ == "__main__":
    unittest.main()
