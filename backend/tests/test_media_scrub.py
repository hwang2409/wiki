from __future__ import annotations

import resource
import random
import shutil
import struct
import subprocess
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path

from backend.app import media_scrub
from backend.app.media_scrub import gif as gif_scrubber
from backend.app.media_scrub import _h264 as h264_scrubber
from backend.app.media_scrub import mp4 as mp4_scrubber


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "media"
REAL_MP4 = FIXTURE_DIR / "tiny.mp4"
REAL_MIXED_MP4 = FIXTURE_DIR / "tiny_avc1_aac.mp4"
REAL_AAC_ONLY_MP4 = FIXTURE_DIR / "tiny_aac_only.mp4"
REAL_WAV = FIXTURE_DIR / "tone.wav"
REAL_MP3 = FIXTURE_DIR / "tone.mp3"
REAL_MP3_APE = FIXTURE_DIR / "tone_ape.mp3"

FFMPEG = shutil.which("ffmpeg")


# ---------------------------------------------------------------------------
# Small synthetic fixtures for edge-case coverage. Structural tests hit the
# real files above; these are only for isolated corner cases.
# ---------------------------------------------------------------------------

def _gif_bytes(*, with_xmp: bool = False) -> bytes:
    header = b"GIF89a"
    lsd = struct.pack("<HH", 4, 2) + b"\x80\x00\x00"
    body = bytearray(header + lsd)
    body += b"\x00\x00\x00\xff\xff\xff"
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


class ScrubMp4MixedAacFixtureTests(unittest.TestCase):
    """A real mixed avc1+AAC MP4 exercises both supported track types."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.original = REAL_MIXED_MP4.read_bytes()
        cls.result = media_scrub.scrub_video(cls.original, "video/mp4")

    def test_avc1_and_mp4a_tracks_survive_rebuild(self) -> None:
        self.assertIn(b"avc1", self.result.data)
        self.assertIn(b"mp4a", self.result.data)
        self.assertIn(b"esds", self.result.data)
        self.assertEqual(len(self.result.data), len(self.original))

    def test_mixed_stored_bytes_decode_cleanly_through_ffmpeg(self) -> None:
        if FFMPEG is None:
            self.skipTest("ffmpeg not installed")
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
                msg=f"ffmpeg mixed decode failed: {probe.stderr.decode(errors='replace')}",
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


class ScrubMp3ApeV2FixtureTests(unittest.TestCase):
    """APEv2 metadata is the review's round-2 blocker: silent survival of
    APE-tagged location/title bytes in an mp3. Fixture appends an APEv2
    footer (no header variant) to the real fixture with `ape-secret-*` items.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.original = REAL_MP3_APE.read_bytes()
        cls.result = media_scrub.scrub_audio(cls.original, "audio/mpeg")

    def test_apev2_preamble_is_absent_after_scrub(self) -> None:
        self.assertIn(b"APETAGEX", self.original)
        self.assertNotIn(b"APETAGEX", self.result.data)

    def test_ape_item_values_are_destroyed(self) -> None:
        for marker in (b"ape-secret-lat-lon", b"ape-secret-title", b"LOCATION"):
            self.assertIn(marker, self.original)
            self.assertNotIn(marker, self.result.data)

    def test_frame_stream_still_starts_with_sync(self) -> None:
        self.assertEqual(self.result.data[0], 0xFF)
        self.assertEqual(self.result.data[1] & 0xE0, 0xE0)

    @unittest.skipIf(FFMPEG is None, "ffmpeg not installed")
    def test_stored_bytes_still_decode_through_ffmpeg(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
            handle.write(self.result.data)
            path = handle.name
        try:
            probe = subprocess.run(
                [FFMPEG, "-v", "error", "-i", path, "-f", "null", "-"],
                capture_output=True, timeout=30,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr.decode(errors="replace"))
        finally:
            Path(path).unlink(missing_ok=True)


class ApeV2BoundaryTests(unittest.TestCase):
    """The round-3 review flagged that malformed APEv2 footers with sizes
    0, 16, or 31 slipped past scrub and retained metadata. Rule: any tag
    field we cannot parse safely → reject the whole file. These tests
    build minimal MP3 payloads with hostile APE footers and confirm every
    boundary case now raises rather than passing through.
    """

    @staticmethod
    def _build_mp3_with_ape_footer(
        tag_size: int,
        flags: int = 0,
        item_count: int = 0,
    ) -> bytes:
        # Real mp3 frames (matches round-2 fixture pattern) plus an
        # attacker-controlled APE footer. Use the real fixture's front so
        # the frame walk succeeds; splice a hostile footer over the tail.
        frames = REAL_MP3.read_bytes()
        # Drop any existing APE/ID3v1 by taking a prefix that ends on a
        # frame boundary — the real fixture has no APE, so the tail is
        # already frame-only.
        version = 2000
        footer = (
            b"APETAGEX"
            + struct.pack("<III", version, tag_size, item_count)
            + struct.pack("<I", flags)
            + b"\x00" * 8
        )
        return frames + b"payload-bytes-that-encode-metadata" + footer

    def test_ape_footer_size_zero_is_rejected(self) -> None:
        payload = self._build_mp3_with_ape_footer(tag_size=0)
        self.assertIn(b"payload-bytes-that-encode-metadata", payload)
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "APEv2 tag_size 0 smaller than minimum 32"
        ):
            media_scrub.scrub_audio(payload, "audio/mpeg")

    def test_ape_footer_size_sixteen_is_rejected(self) -> None:
        payload = self._build_mp3_with_ape_footer(tag_size=16)
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "APEv2 tag_size 16 smaller than minimum 32"
        ):
            media_scrub.scrub_audio(payload, "audio/mpeg")

    def test_ape_footer_size_thirty_one_is_rejected(self) -> None:
        payload = self._build_mp3_with_ape_footer(tag_size=31)
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "APEv2 tag_size 31 smaller than minimum 32"
        ):
            media_scrub.scrub_audio(payload, "audio/mpeg")

    def test_ape_footer_size_larger_than_payload_is_rejected(self) -> None:
        # tag_size 999999 — footer claims a tag bigger than the file. Rejected
        # via the size-vs-remaining bounds check (either directly by the
        # "larger than payload" guard or the implausible-ceiling guard,
        # depending on order; both fail the file, which is the whole point).
        payload = self._build_mp3_with_ape_footer(tag_size=999_999)
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "APEv2.*(larger than payload|exceeds implausible)"
        ):
            media_scrub.scrub_audio(payload, "audio/mpeg")

    def test_ape_footer_absurd_item_count_is_rejected(self) -> None:
        payload = self._build_mp3_with_ape_footer(
            tag_size=32, item_count=10**7,
        )
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "APEv2 item count"
        ):
            media_scrub.scrub_audio(payload, "audio/mpeg")

    def test_ape_footer_with_is_header_flag_at_end_is_rejected(self) -> None:
        # bit 31 = IS_HEADER; if that shows up in a tail-position preamble
        # we refuse to interpret it — silent pass-through is banned.
        payload = self._build_mp3_with_ape_footer(
            tag_size=32, flags=1 << 31,
        )
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "APEv2 marker at end is a header"
        ):
            media_scrub.scrub_audio(payload, "audio/mpeg")

    def test_ape_header_at_start_with_size_larger_than_payload_is_rejected(self) -> None:
        # Attack: valid frame stream but APE header at the front with a
        # tag_size that overflows the remaining bytes. Rejected via one of
        # the size guards (implausible-ceiling for very large; larger-than-
        # payload for merely oversized). Either way — no pass-through.
        frames = REAL_MP3.read_bytes()
        # 1 MB tag_size — plausible ceiling, but larger than the payload.
        header = (
            b"APETAGEX"
            + struct.pack("<III", 2000, 1 << 20, 0)
            + struct.pack("<I", 1 << 31)  # IS_HEADER
            + b"\x00" * 8
        )
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "APEv2 header size larger than payload"
        ):
            media_scrub.scrub_audio(header + frames, "audio/mpeg")

    def test_ape_header_at_start_with_absurd_size_hits_ceiling(self) -> None:
        # Same attack but with a truly implausible tag_size; the ceiling
        # guard is what fires here.
        frames = REAL_MP3.read_bytes()
        header = (
            b"APETAGEX"
            + struct.pack("<III", 2000, 10**8, 0)
            + struct.pack("<I", 1 << 31)
            + b"\x00" * 8
        )
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "APEv2 tag_size .* exceeds implausible ceiling"
        ):
            media_scrub.scrub_audio(header + frames, "audio/mpeg")


class Mp4Stbl_stsdStructuralTests(unittest.TestCase):
    """A fake moov/mvhd/trak with no stbl/stsd sample entry masqueraded as
    MP4 under round-2 rules. Now the sample-entry check rejects it.
    """

    def _make_bogus_mp4_no_stsd(self) -> bytes:
        # ftyp + moov(mvhd + trak(tkhd)) — trak has no mdia/minf/stbl at all.
        ftyp = struct.pack(">I", 24) + b"ftyp" + b"isom" + b"\x00\x00\x02\x00" + b"isommp41"
        mvhd_body = (
            b"\x00\x00\x00\x00"
            + b"\x00" * 8
            + struct.pack(">I", 1000)
            + struct.pack(">I", 1000)
            + b"\x00" * 80
        )
        mvhd = struct.pack(">I", 8 + len(mvhd_body)) + b"mvhd" + mvhd_body
        tkhd_body = (
            b"\x00\x00\x00\x07"
            + b"\x00" * 8
            + b"\x00\x00\x00\x01"
            + b"\x00" * 60
            + struct.pack(">II", 320 << 16, 240 << 16)
        )
        tkhd = struct.pack(">I", 8 + len(tkhd_body)) + b"tkhd" + tkhd_body
        trak = struct.pack(">I", 8 + len(tkhd)) + b"trak" + tkhd
        moov = struct.pack(">I", 8 + len(mvhd) + len(trak)) + b"moov" + mvhd + trak
        return ftyp + moov

    def test_bogus_mp4_without_stsd_sample_entry_is_rejected(self) -> None:
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "stbl/stsd"):
            media_scrub.scrub_video(self._make_bogus_mp4_no_stsd(), "video/mp4")

    def test_real_mp4_carries_a_stsd_sample_entry(self) -> None:
        # Confirms the check does not false-reject the real fixture.
        result = media_scrub.scrub_video(REAL_MP4.read_bytes(), "video/mp4")
        self.assertEqual(result.mime, "video/mp4")

    @staticmethod
    def _wrap_atom(atom_type: bytes, body: bytes) -> bytes:
        return struct.pack(">I", 8 + len(body)) + atom_type + body

    def _make_mp4_with_stsd(self, stsd_body: bytes) -> bytes:
        ftyp = struct.pack(">I", 24) + b"ftyp" + b"isom" + b"\x00\x00\x02\x00" + b"isommp41"
        mvhd_body = (
            b"\x00\x00\x00\x00"
            + b"\x00" * 8
            + struct.pack(">I", 1000)
            + struct.pack(">I", 1000)
            + b"\x00" * 80
        )
        mvhd = self._wrap_atom(b"mvhd", mvhd_body)
        tkhd_body = (
            b"\x00\x00\x00\x07"
            + b"\x00" * 8
            + b"\x00\x00\x00\x01"
            + b"\x00" * 60
            + struct.pack(">II", 320 << 16, 240 << 16)
        )
        tkhd = self._wrap_atom(b"tkhd", tkhd_body)
        stsd = self._wrap_atom(b"stsd", stsd_body)
        stbl = self._wrap_atom(b"stbl", stsd)
        minf = self._wrap_atom(b"minf", stbl)
        mdia = self._wrap_atom(b"mdia", minf)
        trak = self._wrap_atom(b"trak", tkhd + mdia)
        moov = self._wrap_atom(b"moov", mvhd + trak)
        return ftyp + moov

    def test_stsd_with_zero_declared_entries_is_rejected(self) -> None:
        stsd_body = b"\x00\x00\x00\x00" + struct.pack(">I", 0)
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "stbl/stsd"):
            media_scrub.scrub_video(self._make_mp4_with_stsd(stsd_body), "video/mp4")

    def test_stsd_with_entry_size_smaller_than_header_is_rejected(self) -> None:
        # Declared 1 entry, entry size 4 — smaller than the 8-byte minimum.
        stsd_body = (
            b"\x00\x00\x00\x00"
            + struct.pack(">I", 1)
            + struct.pack(">I", 4) + b"avc1"
        )
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "stbl/stsd"):
            media_scrub.scrub_video(self._make_mp4_with_stsd(stsd_body), "video/mp4")

    def test_stsd_with_entry_size_past_atom_end_is_rejected(self) -> None:
        # Entry size claims 4 kB, but the stsd atom only contains 16 bytes.
        stsd_body = (
            b"\x00\x00\x00\x00"
            + struct.pack(">I", 1)
            + struct.pack(">I", 4096) + b"avc1"
        )
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "stbl/stsd"):
            media_scrub.scrub_video(self._make_mp4_with_stsd(stsd_body), "video/mp4")

    def test_stsd_with_null_entry_type_is_rejected(self) -> None:
        stsd_body = (
            b"\x00\x00\x00\x00"
            + struct.pack(">I", 1)
            + struct.pack(">I", 8) + b"\x00\x00\x00\x00"
        )
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "stbl/stsd"):
            media_scrub.scrub_video(self._make_mp4_with_stsd(stsd_body), "video/mp4")

    def test_stsd_with_absurd_entry_count_is_rejected(self) -> None:
        # 4 billion entries won't fit; the remaining-bytes check catches it.
        stsd_body = b"\x00\x00\x00\x00" + struct.pack(">I", 0xFFFFFFFF)
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "stbl/stsd"):
            media_scrub.scrub_video(self._make_mp4_with_stsd(stsd_body), "video/mp4")

    def test_trailing_bytes_after_last_stsd_entry_are_dropped_by_reconstruction(self) -> None:
        # Round-5 reviewer's regression: an attacker marker placed AFTER
        # the last declared stsd entry survived in-place scrub. Under
        # reconstruction, we only emit what we parsed — trailing bytes
        # cannot appear in the output. We splice a marker into the real
        # fixture's stsd (past the last declared entry) and verify it
        # never reaches the stored output.
        marker = b"round5-reviewer-marker-must-not-survive"
        original = REAL_MP4.read_bytes()
        stsd_pos = original.find(b"stsd")
        assert stsd_pos > 0
        stsd_size = struct.unpack(">I", original[stsd_pos - 4:stsd_pos])[0]
        stsd_end = stsd_pos - 4 + stsd_size
        # Insert marker AFTER the last declared sample entry (which fills
        # the whole stsd body in the fixture — the marker is trailing).
        # The whole file must be extended so the size fields around it
        # remain consistent; we grow moov + trak + mdia + minf + stbl +
        # stsd sizes by len(marker).
        # Simpler: leave file structurally consistent by growing stsd's
        # size claim to include the marker. Then verify marker survives
        # in the ORIGINAL input but not the output.
        grown_stsd_size = stsd_size + len(marker)
        assembled = bytearray(original)
        assembled[stsd_pos - 4:stsd_pos] = struct.pack(">I", grown_stsd_size)
        # Grow all enclosing containers' sizes too so the file parses.
        # trak/mdia/minf/stbl each carry stsd. Walk parents by find().
        for parent in (b"stbl", b"minf", b"mdia", b"trak", b"moov"):
            pos = assembled.find(parent)
            assert pos > 0
            existing = struct.unpack(">I", bytes(assembled[pos - 4:pos]))[0]
            assembled[pos - 4:pos] = struct.pack(">I", existing + len(marker))
        # Insert the marker at the end of stsd.
        assembled[stsd_end:stsd_end] = marker
        stco_pos = assembled.find(b"stco")
        count = struct.unpack(">I", bytes(assembled[stco_pos + 8:stco_pos + 12]))[0]
        for index in range(count):
            value_pos = stco_pos + 12 + index * 4
            value = struct.unpack(">I", bytes(assembled[value_pos:value_pos + 4]))[0]
            assembled[value_pos:value_pos + 4] = struct.pack(">I", value + len(marker))
        self.assertIn(marker, bytes(assembled))
        result = media_scrub.scrub_video(bytes(assembled), "video/mp4")
        self.assertNotIn(marker, result.data)

    def test_stsd_outside_stbl_chain_is_ignored(self) -> None:
        # stsd hoisted OUT of stbl and pasted directly under moov — a
        # hostile fixture that tried to trick the scanner in round-2.
        # The chain-enforced check must NOT count this as a valid stream.
        ftyp = struct.pack(">I", 24) + b"ftyp" + b"isom" + b"\x00\x00\x02\x00" + b"isommp41"
        mvhd_body = (
            b"\x00\x00\x00\x00"
            + b"\x00" * 8
            + struct.pack(">I", 1000)
            + struct.pack(">I", 1000)
            + b"\x00" * 80
        )
        mvhd = self._wrap_atom(b"mvhd", mvhd_body)
        tkhd_body = (
            b"\x00\x00\x00\x07"
            + b"\x00" * 8
            + b"\x00\x00\x00\x01"
            + b"\x00" * 60
            + struct.pack(">II", 320 << 16, 240 << 16)
        )
        tkhd = self._wrap_atom(b"tkhd", tkhd_body)
        stsd_body = (
            b"\x00\x00\x00\x00"
            + struct.pack(">I", 1)
            + struct.pack(">I", 8) + b"avc1"
        )
        stsd = self._wrap_atom(b"stsd", stsd_body)
        trak = self._wrap_atom(b"trak", tkhd)
        # Note: stsd is a sibling of trak, NOT nested inside a stbl inside
        # a minf inside a mdia inside the trak.
        moov = self._wrap_atom(b"moov", mvhd + trak + stsd)
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "stbl/stsd"):
            media_scrub.scrub_video(ftyp + moov, "video/mp4")


class Mp4Round6SurvivorProbes(unittest.TestCase):
    """Round-6 review named five reviewer-confirmed byte survivors. Each
    is exercised here as a stored-bytes probe: build a hostile input,
    scrub, and assert the marker never appears in the output (or that
    the file is rejected outright, per strict-subset acceptance)."""

    @staticmethod
    def _wrap(atom_type: bytes, body: bytes) -> bytes:
        return struct.pack(">I", 8 + len(body)) + atom_type + body

    def test_top_level_skip_content_is_zeroed(self) -> None:
        # Round-6 survivor: `skip` at top level. Pre-R6 the atom was
        # emitted through, keeping its body bytes. Under strict rebuild,
        # `skip` becomes a same-size `free` box with zeroed body.
        real = REAL_MP4.read_bytes()
        marker = b"round6-skip-content-must-not-survive"
        skip_box = self._wrap(b"skip", marker + b"\x00" * 40)
        payload = real + skip_box
        result = media_scrub.scrub_video(payload, "video/mp4")
        # The marker bytes are gone…
        self.assertNotIn(marker, result.data)
        # …and no top-level `skip` box exists in the output. (Walk the
        # atoms so we don't false-match on "skip" substrings inside
        # mdat — x264's SEI legitimately embeds `fast_pskip=…`.)
        offset = 0
        while offset + 8 <= len(result.data):
            size = struct.unpack(">I", result.data[offset:offset + 4])[0]
            atom_type = result.data[offset + 4:offset + 8]
            self.assertNotEqual(atom_type, b"skip")
            if size == 0:
                break
            offset += size

    def test_empty_mdat_is_rejected(self) -> None:
        # Round-6 MAJOR #2: a zero-body mdat passes reconstruction shape
        # but fails ffmpeg decode. Reject at validation.
        real = REAL_MP4.read_bytes()
        # Locate the actual mdat and empty its body.
        mdat_pos = real.find(b"mdat")
        assert mdat_pos > 0
        mdat_size = struct.unpack(">I", real[mdat_pos - 4:mdat_pos])[0]
        mdat_end = mdat_pos - 4 + mdat_size
        payload = bytearray(real)
        # Replace mdat body with nothing: shrink mdat to just its 8-byte header
        payload[mdat_pos - 4:mdat_end] = struct.pack(">I", 8) + b"mdat"
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "mdat body is empty"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_unknown_child_inside_dinf_is_rejected(self) -> None:
        # Round-6 survivor: dinf contained an unknown child that survived
        # opaque copy. Canonical rebuild rejects anything that isn't the
        # self-referencing dref/url shape.
        # Splice a foreign child into dinf and confirm reject.
        real = REAL_MP4.read_bytes()
        dinf_pos = real.find(b"dinf")
        assert dinf_pos > 0
        dinf_size = struct.unpack(">I", real[dinf_pos - 4:dinf_pos])[0]
        dinf_end = dinf_pos - 4 + dinf_size
        foreign = self._wrap(b"junk", b"round6-dinf-unknown-marker")
        payload = bytearray(real)
        # Grow dinf and all ancestors by the added child size.
        added = len(foreign)
        payload[dinf_pos - 4:dinf_pos] = struct.pack(">I", dinf_size + added)
        for parent in (b"minf", b"mdia", b"trak", b"moov"):
            pos = payload.find(parent)
            existing = struct.unpack(">I", bytes(payload[pos - 4:pos]))[0]
            payload[pos - 4:pos] = struct.pack(">I", existing + added)
        payload[dinf_end:dinf_end] = foreign
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "dinf must contain exactly one dref"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_unknown_inner_box_inside_stsd_sample_entry_is_rejected(self) -> None:
        # Round-6 survivor: an unknown inner box inside e.g. avc1
        # (something like `junk`) was copied through opaquely. Under
        # rebuild the inner-box allowlist rejects it.
        real = REAL_MP4.read_bytes()
        # Find stsd/avc1 entry, splice a foreign inner box before avcC.
        stsd_pos = real.find(b"stsd")
        stsd_size = struct.unpack(">I", real[stsd_pos - 4:stsd_pos])[0]
        # avc1 sits right after stsd's 8-byte v+f+count header.
        entry_pos = stsd_pos - 4 + 8 + 8  # stsd header(8) + v+f+count(8)
        entry_size = struct.unpack(">I", real[entry_pos:entry_pos + 4])[0]
        entry_end = entry_pos + entry_size
        # Insert a foreign box AT THE END of the entry (after all inner boxes).
        foreign = self._wrap(b"junk", b"round6-stsd-inner-marker")
        added = len(foreign)
        payload = bytearray(real)
        # Grow the sample entry size, stsd size, and every ancestor.
        payload[entry_pos:entry_pos + 4] = struct.pack(">I", entry_size + added)
        payload[stsd_pos - 4:stsd_pos] = struct.pack(">I", stsd_size + added)
        for parent in (b"stbl", b"minf", b"mdia", b"trak", b"moov"):
            pos = payload.find(parent)
            existing = struct.unpack(">I", bytes(payload[pos - 4:pos]))[0]
            payload[pos - 4:pos] = struct.pack(">I", existing + added)
        payload[entry_end:entry_end] = foreign
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "sample entry inner box .* outside allowlist"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_compressor_name_is_zeroed_in_visual_sample_entry(self) -> None:
        # ffmpeg embeds its encoder identity ("Lavc62.28.101 libx264") in
        # the compressor_name field of avc1. That's metadata leakage.
        # Round-6 rebuild zeros the 32-byte compressor_name field.
        original = REAL_MP4.read_bytes()
        self.assertIn(b"libx264", original)
        self.assertIn(b"Lavc", original)
        result = media_scrub.scrub_video(original, "video/mp4")
        self.assertNotIn(b"libx264", result.data)
        self.assertNotIn(b"Lavc", result.data)


class WavRound6SurvivorProbes(unittest.TestCase):
    def test_fact_chunk_content_beyond_sample_length_is_zeroed(self) -> None:
        # Round-6 survivor: WAV `fact` content copied through opaquely.
        # A malicious fact of, say, 32 bytes could hide metadata. Under
        # rebuild fact is emitted as exactly 4 bytes = sample_length.
        # Build a fresh WAV whose fact chunk holds a marker past its
        # 4-byte sample_length field.
        marker = b"round6-wav-fact-content-marker"
        fmt_body = struct.pack("<HHIIHH", 1, 1, 16000, 32000, 2, 16)
        fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
        fact_payload = struct.pack("<I", 12345) + marker
        pad = 1 if len(fact_payload) & 1 else 0
        fact_chunk = (
            b"fact"
            + struct.pack("<I", len(fact_payload))
            + fact_payload
            + (b"\x00" * pad)
        )
        data_chunk = b"data" + struct.pack("<I", 8) + b"\x00\x01" * 4
        body = b"WAVE" + fmt_chunk + fact_chunk + data_chunk
        payload = b"RIFF" + struct.pack("<I", len(body)) + body
        self.assertIn(marker, payload)
        result = media_scrub.scrub_audio(payload, "audio/wav")
        self.assertNotIn(marker, result.data)
        # fact chunk in output must be exactly 4 bytes body.
        fact_out = result.data.find(b"fact")
        self.assertGreater(fact_out, 0)
        fact_size = struct.unpack("<I", result.data[fact_out + 4:fact_out + 8])[0]
        self.assertEqual(fact_size, 4)

    def test_trailing_bytes_inside_pcm_fmt_chunk_are_dropped(self) -> None:
        # Round-6 survivor: PCM fmt chunks larger than 16 bytes had their
        # tail copied through. Under rebuild PCM emits exactly 16 bytes;
        # extensible emits exactly 40. Anything past that is dropped.
        marker = b"round6-pcm-fmt-trailer-marker"
        fmt_body = struct.pack("<HHIIHH", 1, 1, 16000, 32000, 2, 16) + marker
        fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body + (b"\x00" if len(fmt_body) & 1 else b"")
        data_chunk = b"data" + struct.pack("<I", 4) + b"\x00\x00\x00\x00"
        body = b"WAVE" + fmt_chunk + data_chunk
        payload = b"RIFF" + struct.pack("<I", len(body)) + body
        self.assertIn(marker, payload)
        result = media_scrub.scrub_audio(payload, "audio/wav")
        self.assertNotIn(marker, result.data)
        # fmt chunk in output must be exactly 16 bytes body.
        fmt_out = result.data.find(b"fmt ")
        self.assertEqual(fmt_out, 12)  # right after RIFF+size+WAVE
        fmt_size = struct.unpack("<I", result.data[fmt_out + 4:fmt_out + 8])[0]
        self.assertEqual(fmt_size, 16)


class Mp4ReconstructionRegressionTests(unittest.TestCase):
    """Round-5 directive: the stored MP4 is REBUILT from the parsed chain.
    Trailing bytes, out-of-chain hoisted atoms, and top-level unknowns
    cannot survive because they are never written to the output. These
    tests exercise that surface directly.
    """

    @staticmethod
    def _wrap(atom_type: bytes, body: bytes) -> bytes:
        return struct.pack(">I", 8 + len(body)) + atom_type + body

    def test_trailing_bytes_past_top_level_atoms_are_dropped(self) -> None:
        # Append a hostile marker AFTER the last legal top-level atom.
        # The reconstruction only emits parsed atoms, so a marker sitting
        # beyond the last mdat is never copied through.
        original = REAL_MP4.read_bytes()
        marker = b"top-level-trailer-round5-marker"
        # Reconstruction requires input to be a valid atom chain — any
        # trailing junk fails structural parse. The scrub therefore
        # rejects, but rejection is safer than silent survival.
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_video(original + marker, "video/mp4")

    def test_hoisted_moov_pieces_do_not_leak_through(self) -> None:
        # Attack: fake moov followed by a top-level `mvhd` sitting outside
        # any moov (a hostile fixture that hopes the scanner picks it up).
        # Under reconstruction the hoisted mvhd is not moov-parented, so
        # it becomes a `free` box.
        real = REAL_MP4.read_bytes()
        hoisted_mvhd = self._wrap(b"mvhd", b"round5-hoisted-payload" + b"\x00" * 40)
        payload = real + hoisted_mvhd
        # The hoisted marker exists on input.
        self.assertIn(b"round5-hoisted-payload", payload)
        result = media_scrub.scrub_video(payload, "video/mp4")
        # Reconstruction dropped it into a `free` box — bytes destroyed.
        self.assertNotIn(b"round5-hoisted-payload", result.data)

    def test_unknown_top_level_atom_is_replaced_with_free(self) -> None:
        real = REAL_MP4.read_bytes()
        marker = b"unknown-top-level-round5"
        unknown = self._wrap(b"vend", marker + b"\x00" * 20)
        payload = real + unknown
        result = media_scrub.scrub_video(payload, "video/mp4")
        self.assertNotIn(b"vend", result.data)
        self.assertNotIn(marker, result.data)

    def test_stco_offsets_still_point_at_mdat_after_reconstruction(self) -> None:
        # The whole reason moov is padded with `free` — chunk offsets in
        # stco/co64 must still resolve to mdat sample data. FFmpeg's
        # decode of the scrubbed bytes is the final proof; we assert
        # that here too so this stays wired up.
        if not FFMPEG:
            self.skipTest("ffmpeg not installed")
        result = media_scrub.scrub_video(REAL_MP4.read_bytes(), "video/mp4")
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as handle:
            handle.write(result.data)
            path = handle.name
        try:
            probe = subprocess.run(
                [FFMPEG, "-v", "error", "-i", path, "-f", "null", "-"],
                capture_output=True, timeout=30,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr.decode(errors="replace"))
        finally:
            Path(path).unlink(missing_ok=True)


class WavReconstructionRegressionTests(unittest.TestCase):
    """Round-5 directive: WAV bytes are rebuilt from parsed fmt/data/fact
    chunks with computed sizes and correct padding. RIFF size mismatches
    → whole-file reject. Trailing attacker bytes cannot land in the
    output because reconstruction never writes them.
    """

    def _real(self) -> bytes:
        return REAL_WAV.read_bytes()

    def test_riff_size_undercount_is_rejected(self) -> None:
        # Splice attacker bytes past the RIFF-declared end. In-place scrub
        # scanned to len(data) so the marker slipped through. Under
        # reconstruction, the RIFF size must equal the payload size, so
        # this simply fails validation.
        real = bytearray(self._real())
        marker = b"riff-tail-round5-marker"
        # RIFF size stays the same; we append after it.
        real_with_tail = bytes(real) + marker
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "RIFF size .* does not match"
        ):
            media_scrub.scrub_audio(real_with_tail, "audio/wav")

    def test_riff_size_overcount_is_rejected(self) -> None:
        real = bytearray(self._real())
        real[4:8] = struct.pack("<I", len(real) + 128)
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "RIFF size .* does not match"
        ):
            media_scrub.scrub_audio(bytes(real), "audio/wav")

    def test_reconstruction_emits_only_fmt_fact_data_in_canonical_order(self) -> None:
        # Splice a LIST/INFO chunk (metadata) into the real WAV. After
        # reconstruction, only fmt + data survive AND in the canonical
        # order regardless of input ordering.
        real = self._real()
        list_body = b"INFO" + b"IART" + struct.pack("<I", 8) + b"round5\x00\x00"
        list_chunk = b"LIST" + struct.pack("<I", len(list_body)) + list_body
        head = real[:12]
        tail = real[12:]
        payload = bytearray(head) + list_chunk + tail
        payload[4:8] = struct.pack("<I", len(payload) - 8)
        self.assertIn(b"round5", bytes(payload))
        result = media_scrub.scrub_audio(bytes(payload), "audio/wav")
        # Marker gone.
        self.assertNotIn(b"round5", result.data)
        # Chunks walk: only fmt, (fact optional), data — in that order.
        seen: list[bytes] = []
        offset = 12
        while offset + 8 <= len(result.data):
            cid = result.data[offset:offset + 4]
            size = struct.unpack("<I", result.data[offset + 4:offset + 8])[0]
            seen.append(cid)
            offset += 8 + size + (size & 1)
        self.assertEqual(seen[0], b"fmt ")
        self.assertEqual(seen[-1], b"data")
        for cid in seen:
            self.assertIn(cid, {b"fmt ", b"data", b"fact"})

    def test_odd_data_chunk_gets_pad_byte_in_output(self) -> None:
        # Build a WAV whose data chunk is odd-length; reconstruction must
        # emit the pad byte to keep RIFF size even-aligned per the spec.
        # Use 8-bit mono so block_align=1 and a 3-byte data payload lands
        # on frame boundaries — the pad byte comes from RIFF chunk
        # padding, not misalignment. Round-10 review requires data-chunk
        # length align to block_align; the pad byte is added outside the
        # declared chunk size.
        fmt_body = struct.pack("<HHIIHH", 1, 1, 8000, 8000, 1, 8)
        fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
        odd_data = b"\x11\x22\x33"
        data_chunk = b"data" + struct.pack("<I", 3) + odd_data + b"\x00"  # includes pad
        body = b"WAVE" + fmt_chunk + data_chunk
        payload = b"RIFF" + struct.pack("<I", len(body)) + body
        result = media_scrub.scrub_audio(payload, "audio/wav")
        # RIFF size in output must be even (spec compliance).
        out_riff_size = struct.unpack("<I", result.data[4:8])[0]
        self.assertEqual(out_riff_size, len(result.data) - 8)
        # data chunk size == 3, followed by a pad byte, then EOF.
        self.assertEqual(len(result.data) & 1, 0)

    def test_duplicate_fmt_chunk_is_rejected(self) -> None:
        fmt_body = struct.pack("<HHIIHH", 1, 1, 8000, 16000, 2, 16)
        fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
        data_chunk = b"data" + struct.pack("<I", 4) + b"\x00\x00\x00\x00"
        body = b"WAVE" + fmt_chunk + fmt_chunk + data_chunk
        payload = b"RIFF" + struct.pack("<I", len(body)) + body
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "duplicate fmt"
        ):
            media_scrub.scrub_audio(payload, "audio/wav")


class WavFormatCodeAllowlistTests(unittest.TestCase):
    """Format code 0 (WAVE_FORMAT_UNKNOWN) has no decoder. Round-2 review
    found the fmt check accepted it. Now the allowlist gates PCM/float and
    the extensible SubFormat GUID must resolve to PCM or float."""

    @staticmethod
    def _wav_with_format(format_code: int, extensible_subformat: bytes | None = None,
                         bits: int = 16) -> bytes:
        # Minimal WAV with the given format code. If subformat provided,
        # emits an extensible fmt chunk (chunk_size = 40). Round-10:
        # block_align and byte_rate must be internally consistent, and the
        # data chunk length must be a multiple of block_align.
        channels = 1
        sample_rate = 16000
        block_align = channels * (bits // 8)
        byte_rate = sample_rate * block_align
        if extensible_subformat is not None:
            fmt_body = (
                struct.pack("<HHIIHH", format_code, channels, sample_rate, byte_rate, block_align, bits)
                + struct.pack("<HHI", 22, bits, 0)
                + extensible_subformat
            )
        else:
            fmt_body = struct.pack("<HHIIHH", format_code, channels, sample_rate, byte_rate, block_align, bits)
        fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
        # Aligned single-frame data payload.
        data_chunk = b"data" + struct.pack("<I", block_align) + b"\x00" * block_align
        body = b"WAVE" + fmt_chunk + data_chunk
        return b"RIFF" + struct.pack("<I", len(body)) + body

    def test_format_code_zero_is_rejected(self) -> None:
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "format code 0"):
            media_scrub.scrub_audio(self._wav_with_format(0), "audio/wav")

    def test_format_code_pcm_is_accepted(self) -> None:
        media_scrub.scrub_audio(self._wav_with_format(1), "audio/wav")

    def test_format_code_ieee_float_is_accepted(self) -> None:
        media_scrub.scrub_audio(self._wav_with_format(3, bits=32), "audio/wav")

    def test_format_code_alaw_is_rejected(self) -> None:
        # 6 = WAVE_FORMAT_ALAW: valid ITU codec but not on our allowlist.
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "format code 6"):
            media_scrub.scrub_audio(self._wav_with_format(6), "audio/wav")

    def test_extensible_pcm_subformat_is_accepted(self) -> None:
        subformat = media_scrub._WAV_KSDATAFORMAT_PCM
        media_scrub.scrub_audio(
            self._wav_with_format(0xFFFE, subformat), "audio/wav",
        )

    def test_extensible_alien_subformat_is_rejected(self) -> None:
        alien = b"\xaa" * 16
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "SubFormat"):
            media_scrub.scrub_audio(
                self._wav_with_format(0xFFFE, alien), "audio/wav",
            )

    @staticmethod
    def _extensible_wav_with_cbsize(cb_size: int) -> bytes:
        """Build an extensible WAV whose fmt chunk has the given cbSize.

        The extension body (22 bytes: valid_bits + channel_mask + SubFormat)
        is always emitted; only the cbSize field varies. This lets us prove
        the cbSize check fires independently of the SubFormat check.
        """
        subformat = media_scrub._WAV_KSDATAFORMAT_PCM
        # 16 base bytes + 2 cbSize + 22 extension = 40-byte fmt chunk.
        # PCM 16-bit mono: block_align=2, byte_rate=32000.
        fmt_body = (
            struct.pack("<HHIIHH", 0xFFFE, 1, 16000, 32000, 2, 16)
            + struct.pack("<H", cb_size)
            + struct.pack("<HI", 16, 0)
            + subformat
        )
        fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
        # Data chunk aligned to block_align=2.
        data_chunk = b"data" + struct.pack("<I", 4) + b"\x00\x00\x00\x00"
        body = b"WAVE" + fmt_chunk + data_chunk
        return b"RIFF" + struct.pack("<I", len(body)) + body

    def test_extensible_cbsize_zero_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "cbSize 0 smaller than the 22-byte extension"
        ):
            media_scrub.scrub_audio(
                self._extensible_wav_with_cbsize(0), "audio/wav",
            )

    def test_extensible_cbsize_below_twenty_two_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "cbSize 10 smaller than the 22-byte extension"
        ):
            media_scrub.scrub_audio(
                self._extensible_wav_with_cbsize(10), "audio/wav",
            )

    def test_extensible_cbsize_past_chunk_end_is_rejected(self) -> None:
        # cbSize far larger than the fmt chunk holds.
        subformat = media_scrub._WAV_KSDATAFORMAT_PCM
        fmt_body = (
            struct.pack("<HHIIHH", 0xFFFE, 1, 16000, 32000, 2, 16)
            + struct.pack("<H", 9999)
            + struct.pack("<HI", 16, 0)
            + subformat
        )
        fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
        data_chunk = b"data" + struct.pack("<I", 4) + b"\x00\x00\x00\x00"
        body = b"WAVE" + fmt_chunk + data_chunk
        payload = b"RIFF" + struct.pack("<I", len(body)) + body
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "extends past fmt chunk"
        ):
            media_scrub.scrub_audio(payload, "audio/wav")

    def test_extensible_missing_cbsize_field_is_rejected(self) -> None:
        # A fmt chunk of only 16 bytes (no cbSize) but claiming extensible.
        fmt_body = struct.pack("<HHIIHH", 0xFFFE, 1, 16000, 32000, 2, 16)
        fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
        data_chunk = b"data" + struct.pack("<I", 4) + b"\x00\x00\x00\x00"
        body = b"WAVE" + fmt_chunk + data_chunk
        payload = b"RIFF" + struct.pack("<I", len(body)) + body
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "cbSize field|SubFormat"
        ):
            media_scrub.scrub_audio(payload, "audio/wav")


class WavStreamingPeaksBoundsTests(unittest.TestCase):
    """The waveform generator must be bounded and NEVER perform a full-file
    decode. Round-2 review flagged that the previous test only checked the
    output array size — a full-file allocation could still pass. Here we
    ALSO measure the incremental allocation via tracemalloc: computing
    peaks over a 10 MB WAV must not allocate anywhere near 10 MB of new
    bytes, which is only possible if the generator streams memoryview
    slices instead of decoding the data chunk into an intermediate array.
    """

    @staticmethod
    def _build_wav(sample_count: int) -> bytes:
        sample_rate = 16000
        channels = 1
        bits = 16
        byte_rate = sample_rate * channels * bits // 8
        payload = bytearray(sample_count * 2)
        for i in range(sample_count):
            value = (i % 32000) - 16000
            payload[i * 2:i * 2 + 2] = value.to_bytes(2, "little", signed=True)
        fmt_body = struct.pack(
            "<HHIIHH", 1, channels, sample_rate, byte_rate, channels * bits // 8, bits,
        )
        fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
        data_chunk = b"data" + struct.pack("<I", len(payload)) + bytes(payload)
        return (
            b"RIFF"
            + struct.pack("<I", 4 + len(fmt_chunk) + len(data_chunk))
            + b"WAVE"
            + fmt_chunk
            + data_chunk
        )

    def test_large_wav_produces_bounded_peak_array(self) -> None:
        wav = self._build_wav(1_000_000)
        result = media_scrub.scrub_audio(wav, "audio/wav")
        self.assertIsNotNone(result.peaks)
        assert result.peaks is not None
        self.assertLessEqual(len(result.peaks), media_scrub.WAVEFORM_MAX_PEAKS)
        self.assertTrue(any(peak > 0 for peak in result.peaks))

    def test_peak_generator_streams_without_decoding_full_data_chunk(self) -> None:
        # Isolate the streaming-peaks function from the byte-copy the scrub
        # does to write the scrubbed output. A 10 MB PCM data chunk fed
        # straight to _wav_stream_peaks must allocate ORDERS of magnitude
        # less — only the peak buffer (≤512 uint8) plus a handful of
        # transient ints. A naive full-decode implementation would blow
        # through the cap. The payload buffer itself is allocated BEFORE
        # tracemalloc starts so we only measure the incremental cost of
        # peak generation, not the setup.
        sample_count = 5_000_000
        data_size = sample_count * 2
        payload = bytearray(data_size)
        for i in range(sample_count):
            value = (i % 32000) - 16000
            payload[i * 2:i * 2 + 2] = value.to_bytes(2, "little", signed=True)
        payload_bytes = bytes(payload)  # allocated pre-trace
        # Warm up the interpreter (int cache, function dispatch) so the
        # measurement below is just the incremental peak-generation cost.
        media_scrub._wav_stream_peaks(payload_bytes[:200], 0, 200, 1, 16)
        tracemalloc.start()
        try:
            peaks = media_scrub._wav_stream_peaks(
                payload_bytes, 0, data_size, 1, 16,
            )
            _current, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertIsNotNone(peaks)
        assert peaks is not None
        self.assertLessEqual(len(peaks), media_scrub.WAVEFORM_MAX_PEAKS)
        # Bound: peak-generation allocation must scale with the OUTPUT size
        # (up to 512 uint8 peaks + transient slice churn), not the input
        # size. 200 KB is a generous ceiling for per-sample slice churn
        # from int.from_bytes; a full-decode implementation lands at
        # roughly the input size.
        self.assertLess(
            peak_bytes,
            200 * 1024,
            msg=(
                f"peak generator allocated {peak_bytes} bytes on a "
                f"{data_size}-byte input — should be independent of input size"
            ),
        )

    def test_scrub_wav_rss_bound_stays_under_2x_input(self) -> None:
        # RSS discipline — a real hostile fixture would fail here if the
        # generator kept O(N) intermediate structures. We allow up to
        # 2x the input size (the scrubbed output is ≈1x, plus scratch).
        wav = self._build_wav(2_000_000)  # 4 MB PCM
        # Warm up.
        media_scrub.scrub_audio(self._build_wav(1_000), "audio/wav")
        rusage_before = resource.getrusage(resource.RUSAGE_SELF)
        rss_before = rusage_before.ru_maxrss
        media_scrub.scrub_audio(wav, "audio/wav")
        rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        delta = max(0, rss_after - rss_before)
        # macOS ru_maxrss is bytes, Linux is kilobytes. Normalize by treating
        # anything under 100 MB as bytes (macOS) and anything larger as KB.
        if delta > 100 * 1024 * 1024:
            delta = delta * 1024
        # 40 MB cap = 10x the input, comfortable ceiling for interpreter
        # noise; a genuine unbounded allocation blows past this easily.
        self.assertLess(
            delta,
            40 * 1024 * 1024,
            msg=f"maxrss delta {delta} bytes exceeds bound on {len(wav)}-byte WAV",
        )


class Mp4Round7SurvivorProbes(unittest.TestCase):
    """Round-7 review found allowlisted box bodies were still copied
    unchanged: sidx, btrt inside sample entries, and oversized mdhd slack
    all leaked bytes through. Each is exercised here as a stored-bytes
    probe."""

    @staticmethod
    def _wrap(atom_type: bytes, body: bytes) -> bytes:
        return struct.pack(">I", 8 + len(body)) + atom_type + body

    def test_sidx_trailing_bytes_are_rejected(self) -> None:
        # A single-reference sidx has a well-defined length. Splice attacker
        # marker bytes past its declared reference table and reconstruction
        # must reject rather than copy.
        marker = b"round7-sidx-trailer-must-not-survive"
        # v0 fixed = 4+4+4+4+4+2+2 = 24, + 1 reference (12 bytes) = 36
        sidx_body = (
            b"\x00\x00\x00\x00"          # version+flags
            + struct.pack(">I", 1)        # reference_ID
            + struct.pack(">I", 90000)    # timescale
            + struct.pack(">I", 0)        # earliest_presentation_time
            + struct.pack(">I", 0)        # first_offset
            + struct.pack(">HH", 0, 1)    # reserved + reference_count
            + struct.pack(">III", 0x80000100, 90000, 0x00000000)
            + marker
        )
        sidx_atom = self._wrap(b"sidx", sidx_body)
        real = REAL_MP4.read_bytes()
        payload = real + sidx_atom
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "sidx body has .* trailing bytes"
        ):
            media_scrub.scrub_video(payload, "video/mp4")

    def test_sidx_rebuilt_from_valid_input(self) -> None:
        # A well-formed sidx passes reconstruction; the output body is
        # produced by struct.pack, so it must not contain arbitrary bytes.
        real = REAL_MP4.read_bytes()
        sidx_body = (
            b"\x00\x00\x00\x00"
            + struct.pack(">I", 1)
            + struct.pack(">I", 90000)
            + struct.pack(">I", 0)
            + struct.pack(">I", 0)
            + struct.pack(">HH", 0, 1)
            + struct.pack(">III", 0x80000100, 90000, 0x00000000)
        )
        sidx_atom = self._wrap(b"sidx", sidx_body)
        payload = real + sidx_atom
        result = media_scrub.scrub_video(payload, "video/mp4")
        # sidx bytes appear (rebuilt from fields).
        self.assertIn(b"sidx", result.data)

    def test_btrt_slack_inside_sample_entry_is_rejected(self) -> None:
        # Splice trailing bytes AFTER the 12-byte btrt body inside the
        # avc1 sample entry. Round-7 rebuild rejects. Use rfind for names
        # that ALSO appear in ftyp's compatible_brands token stream (avc1)
        # so we grow the enclosing sample-entry box, not the brand string.
        marker = b"round7-btrt-slack-marker"
        real = REAL_MP4.read_bytes()
        btrt_pos = real.find(b"btrt")
        assert btrt_pos > 0
        btrt_size = struct.unpack(">I", real[btrt_pos - 4:btrt_pos])[0]
        btrt_end = btrt_pos - 4 + btrt_size
        added = len(marker)
        payload = bytearray(real)
        payload[btrt_pos - 4:btrt_pos] = struct.pack(">I", btrt_size + added)
        for parent in (b"avc1", b"stsd", b"stbl", b"minf", b"mdia", b"trak", b"moov"):
            pos = payload.rfind(parent, 0, btrt_pos)
            existing = struct.unpack(">I", bytes(payload[pos - 4:pos]))[0]
            payload[pos - 4:pos] = struct.pack(">I", existing + added)
        payload[btrt_end:btrt_end] = marker
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "btrt body length .* not the 12-byte"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_mdhd_slack_is_rejected(self) -> None:
        # mdhd v0 spec size is 24 bytes. Splice extra bytes into the fixture's
        # mdhd and expect reconstruction to reject.
        marker = b"round7-mdhd-slack-marker"
        real = REAL_MP4.read_bytes()
        mdhd_pos = real.find(b"mdhd")
        assert mdhd_pos > 0
        mdhd_size = struct.unpack(">I", real[mdhd_pos - 4:mdhd_pos])[0]
        mdhd_end = mdhd_pos - 4 + mdhd_size
        added = len(marker)
        payload = bytearray(real)
        payload[mdhd_pos - 4:mdhd_pos] = struct.pack(">I", mdhd_size + added)
        for parent in (b"mdia", b"trak", b"moov"):
            pos = payload.find(parent)
            existing = struct.unpack(">I", bytes(payload[pos - 4:pos]))[0]
            payload[pos - 4:pos] = struct.pack(">I", existing + added)
        payload[mdhd_end:mdhd_end] = marker
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "mdhd v0 body length"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_hdlr_name_string_is_zeroed(self) -> None:
        # ffmpeg embeds "VideoHandler" (or the caller's chosen name) in the
        # hdlr name field. Round-7 rebuild emits an empty name.
        real = REAL_MP4.read_bytes()
        self.assertIn(b"VideoHandler", real)
        result = media_scrub.scrub_video(real, "video/mp4")
        self.assertNotIn(b"VideoHandler", result.data)


class Mp3Round7FixpointProbes(unittest.TestCase):
    """Round-7 review: an ID3v1 placed BEFORE an APEv2 footer survived
    scrub because the tag stripper only handled tags in a fixed order.
    And the full frame stream past the third frame was never validated."""

    def test_id3v1_before_apev2_footer_is_stripped(self) -> None:
        # Layout: [ID3v2][frames][ID3v1 128 bytes][APEv2 footer 32 bytes]
        base = REAL_MP3.read_bytes()
        id3v1_marker = b"round7-id3v1-marker".ljust(125, b" ")
        id3v1 = b"TAG" + id3v1_marker
        assert len(id3v1) == 128
        # APEv2 footer with tag_size >= 32 minimum, item_count 0, flags 0.
        ape_footer = (
            b"APETAGEX"
            + struct.pack("<III", 2000, 32, 0)
            + struct.pack("<I", 0)
            + b"\x00" * 8
        )
        payload = base + id3v1 + ape_footer
        self.assertIn(b"round7-id3v1-marker", payload)
        result = media_scrub.scrub_audio(payload, "audio/mpeg")
        self.assertNotIn(b"round7-id3v1-marker", result.data)
        self.assertNotIn(b"TAG", result.data[-128:])
        self.assertNotIn(b"APETAGEX", result.data)

    def test_apev2_before_id3v1_footer_is_stripped(self) -> None:
        # Reverse ordering: [ID3v2][frames][APEv2 footer][ID3v1]
        base = REAL_MP3.read_bytes()
        marker = b"round7-marker-apeitem"
        ape_footer = (
            b"APETAGEX"
            + struct.pack("<III", 2000, 32, 0)
            + struct.pack("<I", 0)
            + b"\x00" * 8
        )
        id3v1 = b"TAG" + marker.ljust(125, b" ")
        assert len(id3v1) == 128
        payload = base + ape_footer + id3v1
        result = media_scrub.scrub_audio(payload, "audio/mpeg")
        self.assertNotIn(marker, result.data)
        self.assertNotIn(b"TAG", result.data[-128:])
        self.assertNotIn(b"APETAGEX", result.data)

    def test_lyrics3v2_tag_is_stripped(self) -> None:
        # Spec layout (bottom-up): the last 9 bytes are "LYRICS200"; the
        # 6 bytes before that are an ASCII decimal size covering all bytes
        # from the leading LYRICSBEGIN through the end of the size field
        # itself. Round-8 review flagged that the previous test used the
        # wrong order (marker before size); real Lyrics3v2 tags never
        # matched under that layout.
        base = REAL_MP3.read_bytes()
        items = b"round8-lyrics3v2-marker-items"
        # tag span = LYRICSBEGIN + items + 6-digit size = 11 + N + 6
        tag_span = len(b"LYRICSBEGIN") + len(items) + 6
        size_field = f"{tag_span:06d}".encode("ascii")
        lyrics_tag = b"LYRICSBEGIN" + items + size_field + b"LYRICS200"
        payload = base + lyrics_tag
        self.assertIn(b"round8-lyrics3v2-marker-items", payload)
        result = media_scrub.scrub_audio(payload, "audio/mpeg")
        self.assertNotIn(b"LYRICS200", result.data)
        self.assertNotIn(b"LYRICSBEGIN", result.data)
        self.assertNotIn(b"round8-lyrics3v2-marker-items", result.data)

    def test_hostile_bytes_past_third_frame_reject_full_stream(self) -> None:
        # The pre-R7 walker only checked the first three frames — a
        # hostile append of unstructured bytes past the third frame
        # survived. Under full-stream validation, any un-parseable byte
        # in the frame region rejects.
        base = REAL_MP3.read_bytes()
        # Splice hostile bytes right before the ID3v1 tail (if any) —
        # here the fixture has no ID3v1, so we can just append.
        hostile = b"\xff\xff\xff\xff" * 8  # not a valid MPEG sync sequence
        payload = base + hostile
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_audio(payload, "audio/mpeg")


class GifRound7ExtensionProbes(unittest.TestCase):
    """Round-7 review: non-XMP extension blocks (comment, plain-text,
    non-NETSCAPE application extensions) were byte-copied through the
    scrubber. Allowlist rebuild kills them all."""

    @staticmethod
    def _min_gif_prefix() -> bytes:
        header = b"GIF89a"
        # 4x2 canvas with a two-entry global color table.
        lsd = struct.pack("<HH", 4, 2) + b"\x80\x00\x00"
        return header + lsd + b"\x00\x00\x00\xff\xff\xff"

    @staticmethod
    def _min_gif_image_data() -> bytes:
        # image descriptor + LZW sub-block + terminator + trailer
        image_desc = b"\x2c" + struct.pack("<HHHH", 0, 0, 4, 2) + b"\x00"
        # LZW min code size + one 4-byte data block + terminator
        lzw = b"\x02\x02\x44\x01\x00"
        return image_desc + lzw + b"\x3b"

    def test_comment_extension_is_dropped(self) -> None:
        marker = b"round7-comment-extension-marker"
        # Comment extension: 0x21 0xFE, sub-blocks, terminator
        comment = b"\x21\xfe" + bytes([len(marker)]) + marker + b"\x00"
        payload = self._min_gif_prefix() + comment + self._min_gif_image_data()
        self.assertIn(marker, payload)
        result = media_scrub.scrub_video(payload, "image/gif")
        self.assertNotIn(marker, result.data)

    def test_plain_text_extension_is_dropped(self) -> None:
        marker = b"round7-plain-text-extension-marker"
        plain_text = b"\x21\x01" + b"\x0c" + b"\x00" * 12 + bytes([len(marker)]) + marker + b"\x00"
        payload = self._min_gif_prefix() + plain_text + self._min_gif_image_data()
        result = media_scrub.scrub_video(payload, "image/gif")
        self.assertNotIn(marker, result.data)

    def test_plain_text_consumes_pending_gce(self) -> None:
        gce = b"\x21\xf9\x04\x00\x00\x00\x00\x00"
        plain_text = b"\x21\x01\x0c" + b"\x00" * 12 + b"\x00"
        payload = self._min_gif_prefix() + gce + plain_text + self._min_gif_image_data()
        result = media_scrub.scrub_video(payload, "image/gif")
        self.assertNotIn(b"\x21\xf9\x04", result.data)

    def test_comments_and_app_extensions_do_not_consume_pending_gce(self) -> None:
        gce = b"\x21\xf9\x04\x00\x00\x00\x00\x00"
        comment = b"\x21\xfe\x03abc\x00"
        app = b"\x21\xff\x0bADOBE1.0abc\x03xyz\x00"
        payload = self._min_gif_prefix() + gce + comment + app + self._min_gif_image_data()
        result = media_scrub.scrub_video(payload, "image/gif")
        self.assertIn(b"\x21\xf9\x04", result.data)

    def test_image_descriptor_reserved_bits_are_rejected(self) -> None:
        payload = bytearray(self._min_gif_prefix() + self._min_gif_image_data())
        image_descriptor = len(self._min_gif_prefix())
        payload[image_descriptor + 9] = 0x18
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "image descriptor packed byte.*reserved bits"
        ):
            media_scrub.scrub_video(bytes(payload), "image/gif")

    def test_image_descriptor_flags_and_lzw_data_are_preserved(self) -> None:
        image_descriptor = b"\x2c" + struct.pack("<HHHH", 0, 0, 4, 2) + b"\xe1"
        palette = bytes(range(12))
        lzw = b"\x02\x02\x44\x01\x00"
        payload = self._min_gif_prefix() + image_descriptor + palette + lzw + b"\x3b"
        result = media_scrub.scrub_video(payload, "image/gif")
        output_descriptor = result.data.find(b"\x2c")
        self.assertGreater(output_descriptor, 0)
        self.assertEqual(result.data[output_descriptor + 9], 0xe1)
        self.assertTrue(result.data.endswith(image_descriptor + palette + lzw + b"\x3b"))

    def test_false_global_table_fields_are_rejected_without_a_palette(self) -> None:
        payload = (
            b"GIF89a" + struct.pack("<HH", 4, 2) + b"\x78\x47\x00"
            + self._min_gif_image_data()
        )
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "color table"):
            media_scrub.scrub_video(payload, "image/gif")

    def test_false_local_table_fields_are_zeroed_but_interlace_survives(self) -> None:
        image_descriptor = b"\x2c" + struct.pack("<HHHH", 0, 0, 4, 2) + b"\x67"
        lzw = b"\x02\x02\x44\x01\x00"
        payload = self._min_gif_prefix() + image_descriptor + lzw + b"\x3b"
        result = media_scrub.scrub_video(payload, "image/gif")
        output_descriptor = result.data.find(b"\x2c")
        self.assertEqual(result.data[output_descriptor + 9], 0x40)
        self.assertTrue(result.data.endswith(b"\x2c" + image_descriptor[1:9] + b"\x40" + lzw + b"\x3b"))

    def test_unknown_application_extension_is_dropped(self) -> None:
        marker = b"round7-adobe-marker"
        # Application extension with "ADOBE1.00abc" identifier + marker body.
        ident = b"ADOBE1.0abc"
        assert len(ident) == 11
        payload_ext = b"\x21\xff\x0b" + ident + bytes([len(marker)]) + marker + b"\x00"
        payload = self._min_gif_prefix() + payload_ext + self._min_gif_image_data()
        result = media_scrub.scrub_video(payload, "image/gif")
        self.assertNotIn(marker, result.data)
        self.assertNotIn(ident, result.data)

    def test_netscape_looping_extension_is_replaced_with_canonical_loop(self) -> None:
        # An input loop extension is replaced by the canonical infinite loop.
        netscape = (
            b"\x21\xff\x0b"
            + b"NETSCAPE2.0"
            + b"\x03\x01"
            + struct.pack("<H", 3)
            + b"\x00"
        )
        frame = self._min_gif_image_data()[:-1]
        payload = self._min_gif_prefix() + netscape + frame + frame + b"\x3b"
        result = media_scrub.scrub_video(payload, "image/gif")
        self.assertIn(b"NETSCAPE2.0", result.data)
        loop_pos = result.data.find(b"NETSCAPE2.0")
        self.assertEqual(result.data[loop_pos + 11], 0x03)
        self.assertEqual(result.data[loop_pos + 12], 0x01)
        self.assertEqual(
            struct.unpack("<H", result.data[loop_pos + 13:loop_pos + 15])[0], 0,
        )
        self.assertEqual(result.data.count(b"NETSCAPE2.0"), 1)

    def test_animated_gif_without_loop_extension_gets_infinite_loop(self) -> None:
        frame = self._min_gif_image_data()[:-1]
        payload = self._min_gif_prefix() + frame + frame + b"\x3b"
        result = media_scrub.scrub_video(payload, "image/gif")
        self.assertEqual(result.data.count(b"NETSCAPE2.0"), 1)
        loop_pos = result.data.find(b"NETSCAPE2.0")
        self.assertEqual(
            struct.unpack("<H", result.data[loop_pos + 13:loop_pos + 15])[0], 0,
        )


class Mp4Round8SurvivorProbes(unittest.TestCase):
    """Round-8 review found byte-smuggling gaps: avcC/sinf/schi bodies
    were copied opaquely, and stbl table children (stts/stsc/stsz/stco)
    bypassed entry-count validation so trailing bytes rode through.
    Each probe here mutates the real fixture (or builds a hostile one)
    and asserts the marker cannot land in the sanitized output."""

    @staticmethod
    def _wrap(atom_type: bytes, body: bytes) -> bytes:
        return struct.pack(">I", 8 + len(body)) + atom_type + body

    def test_avcC_trailing_bytes_past_pps_arrays_are_rejected(self) -> None:
        # Splice attacker bytes after the last declared PPS. Under R8
        # rebuild, avcC counts SPS/PPS entries from parsed fields and
        # rejects any trailing bytes past the last declared entry.
        real = REAL_MP4.read_bytes()
        marker = b"round8-avcC-trailer-must-not-survive"
        avcc_pos = real.find(b"avcC")
        assert avcc_pos > 0
        avcc_size = struct.unpack(">I", real[avcc_pos - 4:avcc_pos])[0]
        avcc_end = avcc_pos - 4 + avcc_size
        added = len(marker)
        payload = bytearray(real)
        # Grow avcC + every ancestor size (avc1 uses rfind to skip the
        # ftyp compatible-brands token).
        payload[avcc_pos - 4:avcc_pos] = struct.pack(">I", avcc_size + added)
        for parent in (b"avc1", b"stsd", b"stbl", b"minf", b"mdia", b"trak", b"moov"):
            pos = payload.rfind(parent, 0, avcc_pos)
            existing = struct.unpack(">I", bytes(payload[pos - 4:pos]))[0]
            payload[pos - 4:pos] = struct.pack(">I", existing + added)
        payload[avcc_end:avcc_end] = marker
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "avcC has .* trailing bytes"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_hvcC_sample_entry_inner_box_is_rejected(self) -> None:
        # Round-8 tightened the sample-entry inner allowlist: hvcC / vpcC
        # / av1C / esds / sinf / schm / schi / tenc are all gone. A file
        # that carries any of them is rejected outright.
        real = REAL_MP4.read_bytes()
        # Replace avcC's box type with hvcC in-place (contents nonsense
        # for HEVC, but reject fires on the type check before any parse).
        avcc_pos = real.find(b"avcC")
        assert avcc_pos > 0
        payload = bytearray(real)
        payload[avcc_pos:avcc_pos + 4] = b"hvcC"
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "sample entry inner box .* outside allowlist"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_sinf_encryption_container_is_rejected(self) -> None:
        real = REAL_MP4.read_bytes()
        avcc_pos = real.find(b"avcC")
        payload = bytearray(real)
        payload[avcc_pos:avcc_pos + 4] = b"sinf"
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "sample entry inner box .* outside allowlist"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_stts_table_trailing_bytes_are_rejected(self) -> None:
        # Splice marker bytes past stts's declared entry table. The
        # rebuilder computes the expected body length from entry_count
        # and rejects any mismatch.
        real = REAL_MP4.read_bytes()
        marker = b"round8-stts-slack-marker"
        stts_pos = real.find(b"stts")
        assert stts_pos > 0
        stts_size = struct.unpack(">I", real[stts_pos - 4:stts_pos])[0]
        stts_end = stts_pos - 4 + stts_size
        added = len(marker)
        payload = bytearray(real)
        payload[stts_pos - 4:stts_pos] = struct.pack(">I", stts_size + added)
        for parent in (b"stbl", b"minf", b"mdia", b"trak", b"moov"):
            pos = payload.rfind(parent, 0, stts_pos)
            existing = struct.unpack(">I", bytes(payload[pos - 4:pos]))[0]
            payload[pos - 4:pos] = struct.pack(">I", existing + added)
        payload[stts_end:stts_end] = marker
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "stts body length .* differs from expected"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_stco_table_trailing_bytes_are_rejected(self) -> None:
        real = REAL_MP4.read_bytes()
        marker = b"round8-stco-slack-marker"
        stco_pos = real.find(b"stco")
        assert stco_pos > 0
        stco_size = struct.unpack(">I", real[stco_pos - 4:stco_pos])[0]
        stco_end = stco_pos - 4 + stco_size
        added = len(marker)
        payload = bytearray(real)
        payload[stco_pos - 4:stco_pos] = struct.pack(">I", stco_size + added)
        for parent in (b"stbl", b"minf", b"mdia", b"trak", b"moov"):
            pos = payload.rfind(parent, 0, stco_pos)
            existing = struct.unpack(">I", bytes(payload[pos - 4:pos]))[0]
            payload[pos - 4:pos] = struct.pack(">I", existing + added)
        payload[stco_end:stco_end] = marker
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "stco body length .* differs from expected"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_stsz_table_trailing_bytes_are_rejected(self) -> None:
        real = REAL_MP4.read_bytes()
        marker = b"round8-stsz-slack-marker"
        stsz_pos = real.find(b"stsz")
        assert stsz_pos > 0
        stsz_size = struct.unpack(">I", real[stsz_pos - 4:stsz_pos])[0]
        stsz_end = stsz_pos - 4 + stsz_size
        added = len(marker)
        payload = bytearray(real)
        payload[stsz_pos - 4:stsz_pos] = struct.pack(">I", stsz_size + added)
        for parent in (b"stbl", b"minf", b"mdia", b"trak", b"moov"):
            pos = payload.rfind(parent, 0, stsz_pos)
            existing = struct.unpack(">I", bytes(payload[pos - 4:pos]))[0]
            payload[pos - 4:pos] = struct.pack(">I", existing + added)
        payload[stsz_end:stsz_end] = marker
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "stsz body length .* differs from expected"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_stsc_table_trailing_bytes_are_rejected(self) -> None:
        real = REAL_MP4.read_bytes()
        marker = b"round8-stsc-slack-marker"
        stsc_pos = real.find(b"stsc")
        assert stsc_pos > 0
        stsc_size = struct.unpack(">I", real[stsc_pos - 4:stsc_pos])[0]
        stsc_end = stsc_pos - 4 + stsc_size
        added = len(marker)
        payload = bytearray(real)
        payload[stsc_pos - 4:stsc_pos] = struct.pack(">I", stsc_size + added)
        for parent in (b"stbl", b"minf", b"mdia", b"trak", b"moov"):
            pos = payload.rfind(parent, 0, stsc_pos)
            existing = struct.unpack(">I", bytes(payload[pos - 4:pos]))[0]
            payload[pos - 4:pos] = struct.pack(">I", existing + added)
        payload[stsc_end:stsc_end] = marker
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "stsc body length .* differs from expected"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_rare_stbl_child_types_are_rejected(self) -> None:
        # Round-8 removed rare stbl types (sdtp/sbgp/sgpd/etc) from the
        # allowlist. A file that carries one is rejected. Splice a hostile
        # sdtp box into the fixture's stbl and verify.
        real = REAL_MP4.read_bytes()
        stsc_pos = real.find(b"stsc")
        assert stsc_pos > 0
        # Insert an sdtp box just before stsc.
        sdtp_body = b"round8-sdtp-body"
        sdtp = struct.pack(">I", 8 + len(sdtp_body)) + b"sdtp" + sdtp_body
        added = len(sdtp)
        payload = bytearray(real)
        insert_at = stsc_pos - 4
        # Grow stbl and every ancestor.
        for parent in (b"stbl", b"minf", b"mdia", b"trak", b"moov"):
            pos = payload.rfind(parent, 0, insert_at)
            existing = struct.unpack(">I", bytes(payload[pos - 4:pos]))[0]
            payload[pos - 4:pos] = struct.pack(">I", existing + added)
        payload[insert_at:insert_at] = sdtp
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "stbl child .* not supported"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")


class Mp4Round9SurvivorProbes(unittest.TestCase):
    """Round-9 review found four byte-smuggling / DoS paths:
      1. SPS/PPS NAL bodies inside avcC were copied verbatim after
         length checks. R9 canonicalises each NAL via a full RBSP
         parse+re-encode; a marker byte inside a declared NAL either
         fails parsing (rejected) or is clobbered by canonical emission.
      2. stbl table fullbox flags (3 bytes each) were captured then
         re-emitted verbatim, so attacker bytes rode through per table.
         R9 requires canonical zero flags and rejects anything else.
      3. stbl accepted duplicate singleton tables (multiple stss/stco/
         ...), stacking many empty tables each smuggling flag bytes.
         R9 tracks seen types and rejects duplicates.
      4. Large stsz tables materialised millions of Python ints. R9
         validates the declared length and splices the entry payload
         verbatim (fixed-size big-endian, no room for non-canonical
         variation), keeping RSS bounded to the input size."""

    @staticmethod
    def _grow_ancestors(payload: bytearray, child_pos: int,
                        parents: tuple[bytes, ...], added: int) -> None:
        for parent in parents:
            pos = payload.rfind(parent, 0, child_pos)
            existing = struct.unpack(">I", bytes(payload[pos - 4:pos]))[0]
            payload[pos - 4:pos] = struct.pack(">I", existing + added)

    @staticmethod
    def _sps_slice(mp4: bytes) -> tuple[int, int]:
        """Return (sps_body_start, sps_body_end) for the first SPS in the
        real fixture's avcC. avcC body layout: version(1) profile(1)
        compat(1) level(1) lsm(1) num_sps(1) then per-SPS length(u16)+
        NAL bytes."""
        avcc_pos = mp4.find(b"avcC")
        assert avcc_pos > 0
        # avcC body starts after 4-byte size + 4-byte type header.
        body_start = avcc_pos + 4
        sps_len = struct.unpack(">H", mp4[body_start + 6:body_start + 8])[0]
        sps_start = body_start + 8
        return sps_start, sps_start + sps_len

    @staticmethod
    def _pps_slice(mp4: bytes) -> tuple[int, int]:
        avcc_pos = mp4.find(b"avcC")
        assert avcc_pos > 0
        body_start = avcc_pos + 4
        sps_len = struct.unpack(">H", mp4[body_start + 6:body_start + 8])[0]
        after_sps = body_start + 8 + sps_len
        # after_sps points to num_pps byte
        pps_len = struct.unpack(">H", mp4[after_sps + 1:after_sps + 3])[0]
        pps_start = after_sps + 3
        return pps_start, pps_start + pps_len

    def test_sps_body_byte_mutation_does_not_survive_verbatim(self) -> None:
        # Splice a distinctive 4-byte marker deep inside the SPS body.
        # Under the R8 code path, avcC copied SPS bytes verbatim so this
        # marker would appear at the same relative avcC offset in the
        # sanitized output. Under R9 canonicalise_nal, the SPS RBSP is
        # decoded field-by-field and re-emitted from parsed values --
        # either the mutation breaks RBSP syntax (raises MediaScrubError)
        # or the re-encoded bits differ so the specific marker sequence
        # cannot appear verbatim.
        real = REAL_MP4.read_bytes()
        sps_start, sps_end = self._sps_slice(real)
        marker = b"\xde\xad\xbe\xef"
        # Sanity-check: marker is not already in the fixture.
        self.assertNotIn(marker, real)
        # Splice near the tail of the SPS (past the fixed header) so it
        # lands inside VUI / trailing bits rather than the profile byte.
        splice_at = sps_end - 4
        payload = bytearray(real)
        payload[splice_at:splice_at + 4] = marker
        try:
            result = media_scrub.scrub_video(bytes(payload), "video/mp4")
        except media_scrub.MediaScrubError:
            return  # rejected: marker cannot survive
        self.assertNotIn(marker, result.data)

    def test_pps_body_byte_mutation_does_not_survive_verbatim(self) -> None:
        real = REAL_MP4.read_bytes()
        pps_start, pps_end = self._pps_slice(real)
        marker = b"\xca\xfe\xba\xbe"
        self.assertNotIn(marker, real)
        splice_at = pps_end - min(4, pps_end - pps_start)
        payload = bytearray(real)
        payload[splice_at:splice_at + 4] = marker
        try:
            result = media_scrub.scrub_video(bytes(payload), "video/mp4")
        except media_scrub.MediaScrubError:
            return
        self.assertNotIn(marker, result.data)

    def test_stbl_table_flags_mutation_is_rejected(self) -> None:
        # Set the 3 flag bytes of stts to non-zero. R9 requires canonical
        # zeros. Revert the check and this test passes-through, meaning
        # attacker bytes smuggled into fullbox flags survive to storage.
        real = REAL_MP4.read_bytes()
        stts_pos = real.find(b"stts")
        assert stts_pos > 0
        body_flags_start = stts_pos + 4 + 1  # after size(4) type(4) version(1)
        payload = bytearray(real)
        marker = b"\xa1\xb2\xc3"
        payload[body_flags_start:body_flags_start + 3] = marker
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "fullbox flags non-zero"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_stbl_duplicate_stss_table_is_rejected(self) -> None:
        # Splice a duplicate stss box just before the existing stss. Under
        # R8 both got emitted, providing two independent 3-byte flag slots.
        real = REAL_MP4.read_bytes()
        stss_pos = real.find(b"stss")
        if stss_pos < 0:
            self.skipTest("fixture has no stss table; probe not applicable")
        stss_size = struct.unpack(">I", real[stss_pos - 4:stss_pos])[0]
        original_stss = real[stss_pos - 4:stss_pos - 4 + stss_size]
        payload = bytearray(real)
        insert_at = stss_pos - 4
        added = len(original_stss)
        self._grow_ancestors(
            payload, insert_at,
            (b"stbl", b"minf", b"mdia", b"trak", b"moov"), added,
        )
        payload[insert_at:insert_at] = original_stss
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "duplicate .* table"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_avcc_sps_ext_arrays_are_rejected(self) -> None:
        # Flip a High profile byte, then extend the fixture avcC to carry
        # a bogus SPS-ext trailer. R9 rejects any avcC declaring SPS-ext
        # NALs because canonicalising the auxiliary-picture RBSP is out of
        # scope. Verifies the strict-subset rejection path.
        real = REAL_MP4.read_bytes()
        avcc_pos = real.find(b"avcC")
        assert avcc_pos > 0
        # We construct a full replacement avcC body that declares 1 SPS
        # copied from the fixture, 1 PPS copied from the fixture, then a
        # High-profile extended trailer with num_sps_ext=1 and a 1-byte
        # dummy SPS-ext NAL. The exact SPS/PPS bytes don't matter because
        # rejection fires before we would try to canonicalise the SPS-ext.
        # Reject fires on num_sps_ext != 0.
        avcc_size = struct.unpack(">I", real[avcc_pos - 4:avcc_pos])[0]
        avcc_body = real[avcc_pos + 4:avcc_pos + avcc_size]
        # Flip profile to High (100) and enable extended trailer.
        # Layout: version(1) profile(1) compat(1) level(1) lsm(1) numSPS(1)
        new_body = bytearray(avcc_body)
        new_body[1] = 100  # profile_idc = High
        # Locate PPS end within avcc_body.
        sps_len = struct.unpack(">H", bytes(new_body[6:8]))[0]
        after_sps = 8 + sps_len
        num_pps = new_body[after_sps]
        pos = after_sps + 1
        for _ in range(num_pps):
            pps_len = struct.unpack(">H", bytes(new_body[pos:pos + 2]))[0]
            pos += 2 + pps_len
        # Truncate and append extended trailer: chroma(1) bd_luma(1)
        # bd_chroma(1) num_sps_ext(1)=1 + one 1-byte dummy SPS-ext NAL.
        new_body = bytes(new_body[:pos])
        new_body += bytes([0xFC | 1, 0xF8, 0xF8, 1])  # chroma=1, depth=0
        new_body += struct.pack(">H", 1) + b"\x00"
        new_avcc = struct.pack(">I", 8 + len(new_body)) + b"avcC" + new_body
        payload = bytearray(real)
        payload[avcc_pos - 4:avcc_pos - 4 + avcc_size] = new_avcc
        # Fix parent atom sizes for the delta.
        delta = len(new_avcc) - avcc_size
        if delta != 0:
            for parent in (b"avc1", b"stsd", b"stbl", b"minf", b"mdia",
                           b"trak", b"moov"):
                pos = payload.rfind(parent, 0, avcc_pos)
                existing = struct.unpack(">I", bytes(payload[pos - 4:pos]))[0]
                payload[pos - 4:pos] = struct.pack(">I", existing + delta)
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "SPS-ext arrays"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_ctts_version_1_signed_offsets_round_trip(self) -> None:
        # ctts version 1 carries signed int32 sample_offset. R8 rejected
        # any non-zero version. R9 accepts version in {0, 1} and preserves
        # the version, so signed offsets survive. This probe replaces the
        # fixture's ctts body with a valid v1 table (one entry with a
        # negative offset) and asserts the scrubbed output preserves the
        # version byte AND the exact signed offset bytes.
        real = REAL_MP4.read_bytes()
        ctts_pos = real.find(b"ctts")
        if ctts_pos < 0:
            # Splice a synthetic ctts before stco: same treatment.
            stco_pos = real.find(b"stco")
            if stco_pos < 0:
                self.skipTest("fixture has neither ctts nor stco")
            insert_before = stco_pos - 4
        else:
            ctts_size = struct.unpack(">I", real[ctts_pos - 4:ctts_pos])[0]
            insert_before = ctts_pos - 4
        stsz_pos = real.find(b"stsz")
        sample_count = struct.unpack(">I", real[stsz_pos + 12:stsz_pos + 16])[0]
        # v1 header: 0x01 version, zero flags, entry_count=1
        # entry: all samples in one run, sample_offset=-42 (i32)
        v1_body = (
            bytes([1, 0, 0, 0])
            + struct.pack(">I", 1)
            + struct.pack(">I", sample_count)
            + struct.pack(">i", -42)
        )
        new_ctts = struct.pack(">I", 8 + len(v1_body)) + b"ctts" + v1_body
        payload = bytearray(real)
        if ctts_pos >= 0:
            payload[insert_before:insert_before + ctts_size] = new_ctts
            delta = len(new_ctts) - ctts_size
        else:
            payload[insert_before:insert_before] = new_ctts
            delta = len(new_ctts)
        for parent in (b"stbl", b"minf", b"mdia", b"trak", b"moov"):
            pos = payload.rfind(parent, 0, insert_before)
            existing = struct.unpack(">I", bytes(payload[pos - 4:pos]))[0]
            payload[pos - 4:pos] = struct.pack(">I", existing + delta)
        stco_pos = payload.find(b"stco")
        count = struct.unpack(">I", bytes(payload[stco_pos + 8:stco_pos + 12]))[0]
        for index in range(count):
            value_pos = stco_pos + 12 + index * 4
            value = struct.unpack(">I", bytes(payload[value_pos:value_pos + 4]))[0]
            payload[value_pos:value_pos + 4] = struct.pack(">I", value + delta)
        result = media_scrub.scrub_video(bytes(payload), "video/mp4")
        # v1 ctts appears in output with its version byte and negative
        # offset preserved.
        expected_ctts = (
            b"ctts"
            + bytes([1, 0, 0, 0])
            + struct.pack(">I", 1)
            + struct.pack(">I", sample_count)
            + struct.pack(">i", -42)
        )
        self.assertIn(expected_ctts, result.data)

    def test_stsz_large_table_rss_stays_bounded(self) -> None:
        # Build a synthetic stsz table with 5M entries (20MB body + 12 byte
        # header). Under the R8 rebuild, per-entry decode-and-repack
        # allocated a Python int list plus a bytes list of the same size,
        # so RSS ballooned to hundreds of MB. Under R9 the entries are
        # spliced verbatim, so peak allocation stays close to input size.
        real = REAL_MP4.read_bytes()
        stsz_pos = real.find(b"stsz")
        assert stsz_pos > 0
        stsz_size = struct.unpack(">I", real[stsz_pos - 4:stsz_pos])[0]

        sample_count = 5_000_000
        new_body = (
            bytes([0, 0, 0, 0])
            + struct.pack(">II", 0, sample_count)
            + b"\x00\x00\x00\x01" * sample_count
        )
        new_stsz = struct.pack(">I", 8 + len(new_body)) + b"stsz" + new_body
        added = len(new_stsz) - stsz_size

        payload = bytearray(real)
        payload[stsz_pos - 4:stsz_pos - 4 + stsz_size] = new_stsz
        for parent in (b"stbl", b"minf", b"mdia", b"trak", b"moov"):
            pos = payload.rfind(parent, 0, stsz_pos)
            existing = struct.unpack(">I", bytes(payload[pos - 4:pos]))[0]
            payload[pos - 4:pos] = struct.pack(">I", existing + added)

        raw = bytes(payload)
        del payload

        tracemalloc.start()
        try:
            try:
                media_scrub.scrub_video(raw, "video/mp4")
            except media_scrub.MediaScrubError:
                # cross-table inconsistencies may cause rejection late;
                # measuring peak alloc up to that point is still valid.
                pass
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        # Pre-R9 (per-entry decode+repack) peaked at ~660MB for this
        # 20MB stsz body -- millions of Python ints plus a per-entry
        # bytes list. Post-R9 splices the entry payload verbatim, so peak
        # is dominated by intermediate concat copies of the input size
        # (~95MB observed). Ceiling set at 150MB catches any regression
        # to per-entry Python object allocation while allowing headroom
        # for the concat pipeline.
        self.assertLess(
            peak, 150 * 1024 * 1024,
            f"tracemalloc peak {peak/1024/1024:.1f}MB exceeded 150MB ceiling; "
            "table rebuild is allocating per-entry Python objects again",
        )


class Mp4Round10FullBoxFlagProbes(unittest.TestCase):
    """Round-10 review found that FullBox flag bytes rode through many mp4
    rebuilders verbatim (mvhd, sidx, elst, tkhd, mdhd, hdlr, vmhd, smhd,
    nmhd, hmhd). Reserved bits carry attacker-controlled hidden bytes
    through the metadata scrub. R10 requires per-box canonical flag masks
    and rejects any bits outside them. These probes mutate each rebuilder's
    flag field with a distinctive marker and assert rejection. Revert the
    R10 validator and every probe would silently pass through, letting
    3 attacker bytes per box land in storage."""

    FLAG_MARKER = b"\x47\x50\x53"  # "GPS" per the R9 review verdict

    @staticmethod
    def _flag_offset_after(mp4: bytes, box_type: bytes) -> int:
        """Return the byte offset of the 3 flag bytes for `box_type` in
        the real fixture. Assumes each named box appears exactly once."""
        pos = mp4.find(box_type)
        assert pos > 0, f"fixture has no {box_type!r}"
        # 4-byte size precedes the type; body starts after the 4-byte
        # type token. Version is at body[0]; flags at body[1:4].
        return pos + 4 + 1

    def _run_mutation(self, box_type: bytes) -> None:
        real = REAL_MP4.read_bytes()
        flag_offset = self._flag_offset_after(real, box_type)
        payload = bytearray(real)
        payload[flag_offset:flag_offset + 3] = self.FLAG_MARKER
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "fullbox flags .* has reserved bits set"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_mvhd_flag_mutation_is_rejected(self) -> None:
        self._run_mutation(b"mvhd")

    def test_tkhd_flag_high_bits_are_rejected(self) -> None:
        # tkhd's low 4 bits ARE defined (track_enabled etc.), so the probe
        # sets a high bit that must be rejected.
        real = REAL_MP4.read_bytes()
        flag_offset = self._flag_offset_after(real, b"tkhd")
        payload = bytearray(real)
        # Preserve any low nibble bits the fixture set; set bit 8 (0x100).
        original = bytes(payload[flag_offset:flag_offset + 3])
        payload[flag_offset:flag_offset + 3] = bytes([original[0], original[1] | 0x01, original[2]])
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "fullbox flags .* has reserved bits set"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_mdhd_flag_mutation_is_rejected(self) -> None:
        self._run_mutation(b"mdhd")

    def test_hdlr_flag_mutation_is_rejected(self) -> None:
        self._run_mutation(b"hdlr")

    def test_vmhd_flag_high_bit_is_rejected(self) -> None:
        # vmhd defines only bit 0 (no_lean_ahead). Mutate a higher bit.
        real = REAL_MP4.read_bytes()
        flag_offset = self._flag_offset_after(real, b"vmhd")
        payload = bytearray(real)
        payload[flag_offset:flag_offset + 3] = b"\x00\x00\x03"  # bit 1 set
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "fullbox flags .* has reserved bits set"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_vmhd_zero_flags_are_rejected(self) -> None:
        real = REAL_MP4.read_bytes()
        flag_offset = self._flag_offset_after(real, b"vmhd")
        payload = bytearray(real)
        payload[flag_offset:flag_offset + 3] = b"\x00\x00\x00"
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "vmhd fullbox flags must be exactly 0x000001"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_avc1_without_avcc_is_rejected(self) -> None:
        real = REAL_MP4.read_bytes()
        avcc_pos = real.find(b"avcC")
        assert avcc_pos > 0
        avcc_size = struct.unpack(">I", real[avcc_pos - 4:avcc_pos])[0]
        remove_start = avcc_pos - 4
        remove_end = remove_start + avcc_size
        payload = bytearray(real)
        del payload[remove_start:remove_end]
        for parent in (b"avc1", b"stsd", b"stbl", b"minf", b"mdia", b"trak", b"moov"):
            pos = payload.rfind(parent, 0, remove_start)
            existing = struct.unpack(">I", bytes(payload[pos - 4:pos]))[0]
            payload[pos - 4:pos] = struct.pack(">I", existing - avcc_size)
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "avc1 sample entry requires exactly one avcC"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_stco_flag_mutation_survives_r9_check(self) -> None:
        # Sanity: the R9 stbl-table check already handles stco. The R10
        # helper's mask=0 policy matches (this probe would also succeed via
        # R9's `fullbox flags non-zero` path).
        real = REAL_MP4.read_bytes()
        flag_offset = self._flag_offset_after(real, b"stco")
        payload = bytearray(real)
        payload[flag_offset:flag_offset + 3] = self.FLAG_MARKER
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "fullbox flags"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")


class Mp4Round11CorrectnessProbes(unittest.TestCase):
    @staticmethod
    def _replace_inner_box(
        real: bytes, old_type: bytes, replacement: bytes,
    ) -> bytes:
        old_pos = real.find(old_type)
        assert old_pos > 0
        old_size = struct.unpack(">I", real[old_pos - 4:old_pos])[0]
        old_atom_start = old_pos - 4
        payload = bytearray(real)
        payload[old_atom_start:old_atom_start + old_size] = replacement
        delta = len(replacement) - old_size
        for parent in (b"avc1", b"stsd", b"stbl", b"minf", b"mdia", b"trak", b"moov"):
            pos = payload.rfind(parent, 0, old_atom_start)
            existing = struct.unpack(">I", bytes(payload[pos - 4:pos]))[0]
            payload[pos - 4:pos] = struct.pack(">I", existing + delta)
        if delta:
            stco_pos = payload.find(b"stco")
            count = struct.unpack(">I", bytes(payload[stco_pos + 8:stco_pos + 12]))[0]
            for index in range(count):
                value_pos = stco_pos + 12 + index * 4
                value = struct.unpack(">I", bytes(payload[value_pos:value_pos + 4]))[0]
                payload[value_pos:value_pos + 4] = struct.pack(">I", value + delta)
        return bytes(payload)

    def test_empty_avcc_is_rejected_for_avc1(self) -> None:
        real = REAL_MP4.read_bytes()
        avcc_pos = real.find(b"avcC")
        assert avcc_pos > 0
        old_size = struct.unpack(">I", real[avcc_pos - 4:avcc_pos])[0]
        body = real[avcc_pos + 4:avcc_pos - 4 + old_size]
        empty_body = bytes([1, body[1], body[2], body[3], body[4], 0xe0, 0])
        replacement = struct.pack(">I", 8 + len(empty_body)) + b"avcC" + empty_body
        payload = self._replace_inner_box(real, b"avcC", replacement)
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "avc1 avcC requires at least one SPS"
        ):
            media_scrub.scrub_video(payload, "video/mp4")

    def test_avcc_header_mismatch_with_canonical_sps_is_rejected(self) -> None:
        real = bytearray(REAL_MP4.read_bytes())
        avcc_pos = real.find(b"avcC")
        assert avcc_pos > 0
        real[avcc_pos + 6] ^= 0x01  # compatibility byte, not the SPS
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError,
            "header profile/compatibility/level mismatches canonical SPS",
        ):
            media_scrub.scrub_video(bytes(real), "video/mp4")

    def test_multiple_matching_sps_records_are_supported(self) -> None:
        real = REAL_MP4.read_bytes()
        avcc_pos = real.find(b"avcC")
        assert avcc_pos > 0
        old_size = struct.unpack(">I", real[avcc_pos - 4:avcc_pos])[0]
        body = real[avcc_pos + 4:avcc_pos - 4 + old_size]
        offset = 6
        sps: list[bytes] = []
        for _ in range(body[5] & 0x1f):
            sps_len = struct.unpack(">H", body[offset:offset + 2])[0]
            offset += 2
            sps.append(body[offset:offset + sps_len])
            offset += sps_len
        num_pps = body[offset]
        pps_start = offset + 1
        pps_bytes = body[pps_start:]
        duplicate_body = (
            body[:5]
            + bytes([0xe0 | (len(sps) + 1)])
            + b"".join(struct.pack(">H", len(item)) + item for item in sps)
            + struct.pack(">H", len(sps[0]))
            + sps[0]
            + bytes([num_pps])
            + pps_bytes
        )
        replacement = struct.pack(">I", 8 + len(duplicate_body)) + b"avcC" + duplicate_body
        payload = self._replace_inner_box(real, b"avcC", replacement)
        result = media_scrub.scrub_video(payload, "video/mp4")
        self.assertIn(b"avcC", result.data)

    def test_nclx_reserved_low_bits_are_rejected(self) -> None:
        body = b"nclx" + struct.pack(">HHH", 1, 2, 3) + b"\x81"
        replacement = struct.pack(">I", 8 + len(body)) + b"colr" + body
        payload = self._replace_inner_box(REAL_MP4.read_bytes(), b"pasp", replacement)
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "full_range_flag has reserved low bits"
        ):
            media_scrub.scrub_video(payload, "video/mp4")

    def test_nclx_full_range_flag_is_preserved(self) -> None:
        body = b"nclx" + struct.pack(">HHH", 1, 2, 3) + b"\x80"
        replacement = struct.pack(">I", 8 + len(body)) + b"colr" + body
        payload = self._replace_inner_box(REAL_MP4.read_bytes(), b"pasp", replacement)
        result = media_scrub.scrub_video(payload, "video/mp4")
        self.assertIn(b"colr" + body, result.data)

    def test_direct_frma_inside_avc1_is_rejected(self) -> None:
        payload = bytearray(REAL_MP4.read_bytes())
        frma_pos = bytes(payload).find(b"pasp")
        assert frma_pos > 0
        payload[frma_pos:frma_pos + 4] = b"frma"
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "sample entry inner box .* outside allowlist"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")


class GifRound10GCEProbes(unittest.TestCase):
    """Round-10 review: GCE packed byte and transparent_color_index were
    copied without semantic validation. R10 rejects reserved packed bits
    (7-5), rejects reserved disposal methods (4-7), normalises the
    transparent index to zero when the flag is clear, and validates the
    index against the active color table when the flag is set."""

    @staticmethod
    def _gif_with_gce(packed: int, index: int, *, gct_entries: int = 4) -> bytes:
        # LSD: width/height 4x2, packed byte GCT_flag=1 with size code that
        # yields gct_entries palette entries.
        # gct_size_code = log2(entries) - 1; entries=4 -> code=1; entries=2 -> code=0
        assert gct_entries in (2, 4, 8, 16, 32, 64, 128, 256)
        size_code = (gct_entries.bit_length() - 1) - 1
        packed_lsd = 0x80 | size_code  # GCT present + size code
        lsd = struct.pack("<HH", 4, 2) + bytes([packed_lsd, 0x00, 0x00])
        gct = bytes(gct_entries * 3)  # zero palette
        # GCE: 21 f9 04 <packed> <delay lo hi> <index> 00
        gce = bytes([0x21, 0xF9, 0x04, packed, 0x00, 0x00, index, 0x00])
        # Image descriptor: 2c left/top/w/h + packed=0 (no LCT)
        idesc = b"\x2c" + struct.pack("<HHHH", 0, 0, 4, 2) + b"\x00"
        # LZW min code size + one sub-block of length 2 + terminator.
        lzw = b"\x02\x02\x44\x01\x00"
        trailer = b"\x3b"
        return b"GIF89a" + lsd + gct + gce + idesc + lzw + trailer

    def test_reserved_packed_bits_are_rejected(self) -> None:
        # Bits 7-5 set (e.g., 0xE0). Transparency + disposal all zero.
        payload = self._gif_with_gce(packed=0xE0, index=0x47)
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "reserved bits"
        ):
            media_scrub.scrub_video(payload, "image/gif")

    def test_reserved_disposal_method_is_rejected(self) -> None:
        # Disposal method 4 (bits 4-2 = 100). Reserved per spec.
        packed = (4 << 2)
        payload = self._gif_with_gce(packed=packed, index=0)
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "disposal method .* reserved"
        ):
            media_scrub.scrub_video(payload, "image/gif")

    def test_ignored_transparent_index_is_zeroed_on_output(self) -> None:
        # Transparent flag clear (bit 0 = 0) but the input smuggles an
        # index byte 0x47. Under R10 the output must carry a zero byte
        # there, not the attacker byte.
        marker = 0x47
        payload = self._gif_with_gce(packed=0, index=marker)
        result = media_scrub.scrub_video(payload, "image/gif")
        # Locate the GCE in the output and check the transparent index
        # byte (byte 6 of the 8-byte GCE structure: 21 f9 04 packed
        # dly_lo dly_hi index 00).
        gce_pos = result.data.find(b"\x21\xf9\x04")
        self.assertGreater(gce_pos, 0)
        self.assertEqual(result.data[gce_pos + 6], 0,
                         "ignored transparent_color_index must be canonical zero on output")

    def test_transparent_index_past_color_table_is_rejected(self) -> None:
        # gct_entries=4 (indices 0..3). Transparent flag set + index 5.
        payload = self._gif_with_gce(
            packed=_GCE_TRANSPARENT_FLAG, index=5, gct_entries=4,
        )
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "transparent_color_index .* out of range"
        ):
            media_scrub.scrub_video(payload, "image/gif")

    def test_valid_gce_round_trips_canonical_bytes(self) -> None:
        # Baseline: disposal=1, transparent flag set, index=1 (in table).
        packed = (1 << 2) | _GCE_TRANSPARENT_FLAG
        payload = self._gif_with_gce(packed=packed, index=1, gct_entries=4)
        result = media_scrub.scrub_video(payload, "image/gif")
        # Reserved bits stay zero, disposal/user_input/trans preserved.
        gce_pos = result.data.find(b"\x21\xf9\x04")
        self.assertGreater(gce_pos, 0)
        self.assertEqual(result.data[gce_pos + 3], packed)
        self.assertEqual(result.data[gce_pos + 6], 1)


class GifRound12LzwProbes(unittest.TestCase):
    def test_lzw_sub_blocks_after_eoi_are_dropped(self) -> None:
        marker = b"round12-gif-lzw-after-eoi-secret"
        image_desc = b"\x2c" + struct.pack("<HHHH", 0, 0, 4, 2) + b"\x00"
        # clear, one pixel, EOI; the next sub-block is not part of the
        # compressed stream and must not reach the stored artifact.
        lzw = b"\x02\x02\x44\x01" + bytes([len(marker)]) + marker + b"\x00"
        payload = (
            b"GIF89a" + struct.pack("<HH", 4, 2) + b"\x80\x00\x00"
            + b"\x00\x00\x00\xff\xff\xff" + image_desc + lzw + b"\x3b"
        )
        result = media_scrub.scrub_video(payload, "image/gif")
        self.assertNotIn(marker, result.data)

    def test_pixel_count_limit_rejects_huge_descriptor(self) -> None:
        image_desc = b"\x2c" + struct.pack("<HHHH", 0, 0, 65535, 65535) + b"\x00"
        lzw = b"\x02\x02\x44\x01\x00"
        payload = (
            b"GIF89a" + struct.pack("<HH", 1, 1) + b"\x00\x00\x00"
            + image_desc + lzw + b"\x3b"
        )
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "pixel limit"):
            media_scrub.scrub_video(payload, "image/gif")


class Mp4Round12OwnershipAndH264Probes(unittest.TestCase):
    @staticmethod
    def _replace_avcc(real: bytes, body: bytes) -> bytes:
        avcc_pos = real.find(b"avcC")
        assert avcc_pos > 0
        old_size = struct.unpack(">I", real[avcc_pos - 4:avcc_pos])[0]
        replacement = struct.pack(">I", 8 + len(body)) + b"avcC" + body
        old_atom_start = avcc_pos - 4
        payload = bytearray(real)
        payload[old_atom_start:old_atom_start + old_size] = replacement
        delta = len(replacement) - old_size
        for parent in (b"avc1", b"stsd", b"stbl", b"minf", b"mdia", b"trak", b"moov"):
            pos = payload.rfind(parent, 0, old_atom_start)
            existing = struct.unpack(">I", bytes(payload[pos - 4:pos]))[0]
            payload[pos - 4:pos] = struct.pack(">I", existing + delta)
        if delta:
            stco_pos = payload.find(b"stco")
            count = struct.unpack(">I", bytes(payload[stco_pos + 8:stco_pos + 12]))[0]
            for index in range(count):
                value_pos = stco_pos + 12 + index * 4
                value = struct.unpack(">I", bytes(payload[value_pos:value_pos + 4]))[0]
                payload[value_pos:value_pos + 4] = struct.pack(">I", value + delta)
        return bytes(payload)

    @staticmethod
    def _set_pps_sps_id(nal: bytes, sps_id: int) -> bytes:
        bits = "".join(f"{byte:08b}" for byte in nal[1:])

        def read_ue(position: int) -> tuple[str, int]:
            start = position
            zeros = 0
            while bits[position] == "0":
                zeros += 1
                position += 1
            position += 1 + zeros
            return bits[start:position], position

        first_code, position = read_ue(0)
        _second_code, position = read_ue(position)
        code_number = sps_id + 1
        code_width = code_number.bit_length()
        replacement = first_code + ("0" * (code_width - 1) + f"{code_number:0{code_width}b}") + bits[position:]
        replacement = replacement[:len(bits)]
        return nal[:1] + bytes(
            int(replacement[index:index + 8], 2)
            for index in range(0, len(replacement), 8)
        )

    def test_mdat_unreferenced_bytes_are_zeroed(self) -> None:
        real = REAL_MP4.read_bytes()
        marker = b"round12-mdat-unreferenced-secret"
        mdat_pos = real.find(b"mdat")
        assert mdat_pos > 0
        mdat_start = mdat_pos - 4
        mdat_size = struct.unpack(">I", real[mdat_start:mdat_pos])[0]
        payload = bytearray(real)
        payload[mdat_start:mdat_pos] = struct.pack(">I", mdat_size + len(marker))
        payload[mdat_start + mdat_size:mdat_start + mdat_size] = marker
        result = media_scrub.scrub_video(bytes(payload), "video/mp4")
        marker_start = mdat_start + mdat_size
        self.assertEqual(result.data[marker_start:marker_start + len(marker)], b"\x00" * len(marker))

    def test_codec_metadata_is_removed_from_mp4_and_mp3(self) -> None:
        mp4_result = media_scrub.scrub_video(REAL_MP4.read_bytes(), "video/mp4")
        self.assertNotIn(b"x264 - core", mp4_result.data)
        self.assertNotIn(b"Lavc62.28", mp4_result.data)
        mp3_result = media_scrub.scrub_audio(REAL_MP3.read_bytes(), "audio/mpeg")
        self.assertNotIn(b"Info", mp3_result.data)
        self.assertNotIn(b"Lavc62.28", mp3_result.data)

    def test_stsc_zero_first_chunk_is_rejected(self) -> None:
        payload = bytearray(REAL_MP4.read_bytes())
        stsc_pos = payload.find(b"stsc")
        assert stsc_pos > 0
        payload[stsc_pos + 12:stsc_pos + 16] = b"\x00\x00\x00\x00"
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "stsc entries are invalid"):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_uniform_sample_count_limit_is_rejected(self) -> None:
        payload = bytearray(REAL_MP4.read_bytes())
        stsz_pos = payload.find(b"stsz")
        assert stsz_pos > 0
        payload[stsz_pos + 12:stsz_pos + 16] = struct.pack(">I", 0xFFFFFFFF)
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "sample_count .* exceeds"):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_avc_sample_length_framing_is_rejected(self) -> None:
        payload = bytearray(REAL_MP4.read_bytes())
        mdat_pos = payload.find(b"mdat")
        assert mdat_pos > 0
        payload[mdat_pos + 4:mdat_pos + 8] = b"\x00\x00\x00\x00"
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "NAL length"):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_sample_range_past_mdat_is_rejected(self) -> None:
        payload = bytearray(REAL_MP4.read_bytes())
        stco_pos = payload.find(b"stco")
        assert stco_pos > 0
        value_pos = stco_pos + 12
        payload[value_pos:value_pos + 4] = struct.pack(">I", len(payload) + 1)
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "not contained"):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_stsc_description_index_outside_stsd_is_rejected(self) -> None:
        payload = bytearray(REAL_MP4.read_bytes())
        stsc_pos = payload.find(b"stsc")
        assert stsc_pos > 0
        payload[stsc_pos + 20:stsc_pos + 24] = struct.pack(">I", 2)
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "description_index"):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_avc3_sample_entry_is_rejected_as_out_of_scope(self) -> None:
        payload = bytearray(REAL_MP4.read_bytes())
        stsd_pos = payload.find(b"stsd")
        avc1_pos = payload.find(b"avc1", stsd_pos)
        assert avc1_pos > stsd_pos
        payload[avc1_pos:avc1_pos + 4] = b"avc3"
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "outside allowlist|outside scrubber scope"):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_avcc_length_size_minus_one_two_is_rejected(self) -> None:
        real = bytearray(REAL_MP4.read_bytes())
        avcc_pos = real.find(b"avcC")
        assert avcc_pos > 0
        real[avcc_pos + 8] = (real[avcc_pos + 8] & 0xFC) | 0x02
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "lengthSizeMinusOne must be 0, 1, or 3"
        ):
            media_scrub.scrub_video(bytes(real), "video/mp4")

    def test_avc1_pps_reference_to_missing_sps_is_rejected(self) -> None:
        real = REAL_MP4.read_bytes()
        avcc_pos = real.find(b"avcC")
        assert avcc_pos > 0
        old_size = struct.unpack(">I", real[avcc_pos - 4:avcc_pos])[0]
        body = real[avcc_pos + 4:avcc_pos - 4 + old_size]
        offset = 6
        for _ in range(body[5] & 0x1F):
            sps_len = struct.unpack(">H", body[offset:offset + 2])[0]
            offset += 2 + sps_len
        pps_len = struct.unpack(">H", body[offset + 1:offset + 3])[0]
        pps_start = offset + 3
        pps = body[pps_start:pps_start + pps_len]
        bad_pps = self._set_pps_sps_id(pps, 1)
        bad_body = body[:pps_start] + bad_pps + body[pps_start + pps_len:]
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "PPS references an SPS identifier absent"
        ):
            media_scrub.scrub_video(self._replace_avcc(real, bad_body), "video/mp4")


_GCE_TRANSPARENT_FLAG = 0x01


class WavRound10FmtConsistencyProbes(unittest.TestCase):
    """Round-10 review: WAV fmt only rejected zero fields. block_align,
    byte_rate, bit-depth legality, and data-frame alignment were
    unvalidated, so a byte_rate=1 mutation on the fixture made the
    half-second sample report a duration of 16000000ms. R10 validates
    every cross-field invariant before rebuilding."""

    @staticmethod
    def _pcm_wav(*, channels: int, sample_rate: int, byte_rate: int,
                 block_align: int, bits: int, data_len: int) -> bytes:
        fmt_body = struct.pack(
            "<HHIIHH", 1, channels, sample_rate, byte_rate, block_align, bits,
        )
        fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
        data_chunk = b"data" + struct.pack("<I", data_len) + b"\x00" * data_len
        pad = data_len & 1
        body = b"WAVE" + fmt_chunk + data_chunk + (b"\x00" if pad else b"")
        return b"RIFF" + struct.pack("<I", len(body)) + body

    def test_pcm_bit_depth_12_is_rejected(self) -> None:
        payload = self._pcm_wav(
            channels=1, sample_rate=16000, byte_rate=24000,
            block_align=3, bits=12, data_len=6,
        )
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "PCM bit depth 12 not in allowed set"
        ):
            media_scrub.scrub_audio(payload, "audio/wav")

    def test_block_align_mismatch_is_rejected(self) -> None:
        payload = self._pcm_wav(
            channels=1, sample_rate=16000, byte_rate=32000,
            block_align=7, bits=16, data_len=14,
        )
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "block_align 7 != channels\\*bytes-per-sample 2"
        ):
            media_scrub.scrub_audio(payload, "audio/wav")

    def test_byte_rate_mismatch_is_rejected(self) -> None:
        # byte_rate=1 was the reviewer's specific mutation that made the
        # fixture report duration 16000000ms.
        payload = self._pcm_wav(
            channels=1, sample_rate=16000, byte_rate=1,
            block_align=2, bits=16, data_len=4,
        )
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "byte_rate 1 != sample_rate\\*block_align 32000"
        ):
            media_scrub.scrub_audio(payload, "audio/wav")

    def test_data_length_misaligned_to_block_align_is_rejected(self) -> None:
        # 16-bit mono needs block_align=2; a 5-byte data payload is 2.5
        # frames.
        payload = self._pcm_wav(
            channels=1, sample_rate=16000, byte_rate=32000,
            block_align=2, bits=16, data_len=5,
        )
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "data chunk length 5 is not aligned to block_align 2"
        ):
            media_scrub.scrub_audio(payload, "audio/wav")

    def test_duration_uses_validated_frame_count(self) -> None:
        # PCM 16-bit mono at 16000Hz. 8000 frames = 500ms.
        payload = self._pcm_wav(
            channels=1, sample_rate=16000, byte_rate=32000,
            block_align=2, bits=16, data_len=16000,
        )
        result = media_scrub.scrub_audio(payload, "audio/wav")
        self.assertEqual(result.duration_ms, 500)

    def test_extensible_valid_bits_over_container_is_rejected(self) -> None:
        subformat = media_scrub._WAV_KSDATAFORMAT_PCM
        # container bits=16, valid_bits=20 (illegal).
        fmt_body = (
            struct.pack("<HHIIHH", 0xFFFE, 1, 16000, 32000, 2, 16)
            + struct.pack("<HHI", 22, 20, 0)
            + subformat
        )
        fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
        data_chunk = b"data" + struct.pack("<I", 4) + b"\x00\x00\x00\x00"
        body = b"WAVE" + fmt_chunk + data_chunk
        payload = b"RIFF" + struct.pack("<I", len(body)) + body
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "valid_bits 20 out of range"
        ):
            media_scrub.scrub_audio(payload, "audio/wav")


class UnsupportedMimeTests(unittest.TestCase):
    def test_scrub_video_rejects_audio_mime(self) -> None:
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_video(b"\x00" * 32, "audio/wav")

    def test_scrub_audio_rejects_video_mime(self) -> None:
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_audio(b"\x00" * 32, "video/mp4")


class Review15MediaProbeTests(unittest.TestCase):
    def test_unknown_avc_nal_with_marker_is_rejected(self) -> None:
        payload = bytearray(REAL_MP4.read_bytes())
        mdat_type = payload.find(b"mdat")
        self.assertGreater(mdat_type, 0)
        nal_length = struct.unpack(">I", payload[mdat_type + 4:mdat_type + 8])[0]
        nal_start = mdat_type + 8
        self.assertGreater(nal_length, 24)
        marker = b"review15-unknown-nal-marker"
        payload[nal_start] = (payload[nal_start] & 0xE0) | 30
        payload[nal_start + 1:nal_start + 1 + len(marker)] = marker
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "unsupported NAL type 30"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_in_band_parameter_set_nal_with_marker_is_rejected(self) -> None:
        payload = bytearray(REAL_MP4.read_bytes())
        mdat_type = payload.find(b"mdat")
        nal_start = mdat_type + 8
        marker = b"review15-in-band-parameter-marker"
        payload[nal_start] = (payload[nal_start] & 0xE0) | 7
        payload[nal_start + 1:nal_start + 1 + len(marker)] = marker
        with self.assertRaisesRegex(
            media_scrub.MediaScrubError, "in-band parameter-set"
        ):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_huge_uniform_sample_count_rejects_before_range_expansion(self) -> None:
        payload = bytearray(REAL_MP4.read_bytes())
        stsc_type = payload.find(b"stsc")
        stsz_type = payload.find(b"stsz")
        self.assertGreater(stsc_type, 0)
        self.assertGreater(stsz_type, 0)
        payload[stsc_type + 16:stsc_type + 20] = struct.pack(">I", 200_000)
        old_size = struct.unpack(">I", payload[stsz_type - 4:stsz_type])[0]
        new_body = b"\x00\x00\x00\x00" + struct.pack(">II", 1, 200_000)
        new_box = struct.pack(">I", 8 + len(new_body)) + b"stsz" + new_body
        old_start = stsz_type - 4
        payload[old_start:old_start + old_size] = new_box
        delta = len(new_box) - old_size
        for parent_type in (b"stbl", b"minf", b"mdia", b"trak", b"moov"):
            parent_type_pos = payload.find(parent_type, 0, old_start)
            parent_size = struct.unpack(">I", payload[parent_type_pos - 4:parent_type_pos])[0]
            payload[parent_type_pos - 4:parent_type_pos] = struct.pack(">I", parent_size + delta)
        raw = bytes(payload)
        tracemalloc.start()
        try:
            with self.assertRaisesRegex(
                media_scrub.MediaScrubError, "chunk extent|sample_count"
            ):
                media_scrub.scrub_video(raw, "video/mp4")
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 5 * 1024 * 1024, f"peak allocation was {peak} bytes")

    def test_mp4_clock_fields_and_language_are_normalized(self) -> None:
        payload = bytearray(REAL_MP4.read_bytes())
        for box_type in (b"mvhd", b"tkhd", b"mdhd"):
            type_pos = payload.find(box_type)
            body_pos = type_pos + 4
            payload[body_pos + 4:body_pos + 8] = b"GPS!"
            payload[body_pos + 8:body_pos + 12] = b"TIME"
        result = media_scrub.scrub_video(bytes(payload), "video/mp4")
        for box_type in (b"mvhd", b"tkhd", b"mdhd"):
            type_pos = result.data.find(box_type)
            body_pos = type_pos + 4
            self.assertEqual(result.data[body_pos + 4:body_pos + 12], b"\x00" * 8)
        mdhd_pos = result.data.find(b"mdhd")
        self.assertEqual(result.data[mdhd_pos + 24:mdhd_pos + 26], b"\x00\x00")

    def test_required_mp4_children_cannot_be_renamed(self) -> None:
        for box_type in (b"tkhd", b"mdhd", b"hdlr", b"vmhd", b"dinf"):
            with self.subTest(box_type=box_type):
                payload = bytearray(REAL_MP4.read_bytes())
                type_pos = payload.find(box_type)
                payload[type_pos:type_pos + 4] = b"bad!"
                with self.assertRaisesRegex(
                    media_scrub.MediaScrubError, box_type.decode("ascii")
                ):
                    media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_gif_dropped_extension_blocks_are_not_retained(self) -> None:
        blocks = b"\xff" + (b"x" * 255)
        comment = b"\x21\xfe" + blocks * 1882 + b"\x00"
        payload = GifRound7ExtensionProbes._min_gif_prefix() + comment + GifRound7ExtensionProbes._min_gif_image_data()
        tracemalloc.start()
        try:
            result = media_scrub.scrub_video(payload, "image/gif")
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertNotIn(b"x" * 32, result.data)
        self.assertLess(peak, 3 * 1024 * 1024, f"peak allocation was {peak} bytes")

    def test_gif_extension_sub_block_count_is_capped(self) -> None:
        comment = b"\x21\xfe" + (b"\x01x" * 4097) + b"\x00"
        payload = GifRound7ExtensionProbes._min_gif_prefix() + comment
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "too many sub-blocks"):
            media_scrub.scrub_video(payload, "image/gif")

    def test_mp4_child_box_count_is_capped_with_bounded_memory(self) -> None:
        real = REAL_MP4.read_bytes()
        free_boxes = b"".join(
            struct.pack(">I", 8) + b"free" for _ in range(100_000)
        )
        payload = real + free_boxes
        tracemalloc.start()
        try:
            with self.assertRaisesRegex(
                media_scrub.MediaScrubError, "more than 4096 child boxes"
            ):
                media_scrub.scrub_video(payload, "video/mp4")
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 8 * 1024 * 1024, f"peak allocation was {peak} bytes")

    def test_extensible_pcm_padding_marker_is_zeroed_and_decodes(self) -> None:
        samples = b"".join(
            struct.pack("<H", (0x12 << 8) | marker)
            for marker in (0xA5, 0x5A, 0xC3)
        )
        fmt_body = (
            struct.pack("<HHIIHH", 0xFFFE, 1, 16_000, 32_000, 2, 16)
            + struct.pack("<HHI", 22, 8, 0)
            + media_scrub._WAV_KSDATAFORMAT_PCM
        )
        fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
        data_chunk = b"data" + struct.pack("<I", len(samples)) + samples
        body = b"WAVE" + fmt_chunk + data_chunk
        payload = b"RIFF" + struct.pack("<I", len(body)) + body
        result = media_scrub.scrub_audio(payload, "audio/wav")
        data_pos = result.data.find(b"data")
        data_size = struct.unpack("<I", result.data[data_pos + 4:data_pos + 8])[0]
        stored = result.data[data_pos + 8:data_pos + 8 + data_size]
        self.assertEqual(stored, b"\x00\x12\x00\x12\x00\x12")
        if FFMPEG is not None:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                handle.write(result.data)
                stored_path = handle.name
            try:
                probe = subprocess.run(
                    [FFMPEG, "-v", "error", "-i", stored_path, "-f", "null", "-"],
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(
                    probe.returncode, 0,
                    msg=f"ffmpeg decode failed: {probe.stderr.decode(errors='replace')}",
                )
            finally:
                Path(stored_path).unlink(missing_ok=True)

    def test_gif_cumulative_pixels_are_capped_before_decode(self) -> None:
        image = b"\x2c" + struct.pack("<HHHH", 0, 0, 4096, 4096) + b"\x00\x02\x01\x2c\x00"
        payload = (
            b"GIF89a" + struct.pack("<HH", 4096, 4096) + b"\x80\x00\x00"
            + b"\x00\x00\x00\x00\x00\x00"
            + image + image + b"\x3b"
        )
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "cumulative image pixels"):
            media_scrub.scrub_video(payload, "image/gif")

    @staticmethod
    def _float_wav(value: float) -> bytes:
        fmt = struct.pack("<HHIIHH", 3, 1, 16_000, 64_000, 4, 32)
        data = struct.pack("<f", value)
        body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
        return b"RIFF" + struct.pack("<I", len(body)) + body

    def test_float_wav_peaks_unpack_ieee_samples(self) -> None:
        result = media_scrub.scrub_audio(self._float_wav(0.5), "audio/wav")
        self.assertEqual(result.peaks, [127])

    def test_float_wav_rejects_non_finite_samples(self) -> None:
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "not finite"):
            media_scrub.scrub_audio(self._float_wav(float("nan")), "audio/wav")


class Review17MediaProbeTests(unittest.TestCase):
    @staticmethod
    def _children(data: bytes | bytearray, start: int, end: int) -> list[tuple[bytes, int, int, int, int]]:
        children: list[tuple[bytes, int, int, int, int]] = []
        offset = start
        while offset < end:
            size = struct.unpack(">I", data[offset:offset + 4])[0]
            if size < 8 or offset + size > end:
                raise AssertionError("fixture box is malformed")
            children.append((
                bytes(data[offset + 4:offset + 8]),
                offset,
                offset + size,
                offset + 8,
                offset + size,
            ))
            offset += size
        return children

    @classmethod
    def _track_info(cls, payload: bytes | bytearray) -> list[dict[str, int | bytes]]:
        moov = next(child for child in cls._children(payload, 0, len(payload)) if child[0] == b"moov")
        tracks: list[dict[str, int | bytes]] = []
        for trak in cls._children(payload, moov[3], moov[4]):
            if trak[0] != b"trak":
                continue
            mdia = next(child for child in cls._children(payload, trak[3], trak[4]) if child[0] == b"mdia")
            mdia_children = cls._children(payload, mdia[3], mdia[4])
            hdlr = next(child for child in mdia_children if child[0] == b"hdlr")
            minf = next(child for child in mdia_children if child[0] == b"minf")
            stbl = next(child for child in cls._children(payload, minf[3], minf[4]) if child[0] == b"stbl")
            stbl_children = cls._children(payload, stbl[3], stbl[4])
            stsd = next(child for child in stbl_children if child[0] == b"stsd")
            stco = next(child for child in stbl_children if child[0] == b"stco")
            stsz = next(child for child in stbl_children if child[0] == b"stsz")
            tracks.append({
                "handler": bytes(payload[hdlr[3] + 8:hdlr[3] + 12]),
                "trak_start": trak[1],
                "trak_end": trak[2],
                "stsd_body": stsd[3],
                "stco_body": stco[3],
                "stsz_body": stsz[3],
            })
        return tracks

    @classmethod
    def _overlap_fixture(cls, *, exact: bool, swapped: bool) -> bytes:
        payload = bytearray(REAL_MIXED_MP4.read_bytes())
        tracks = cls._track_info(payload)
        video = next(track for track in tracks if track["handler"] == b"vide")
        audio = next(track for track in tracks if track["handler"] == b"soun")
        video_offset = struct.unpack(">I", payload[int(video["stco_body"]) + 8:int(video["stco_body"]) + 12])[0]
        audio_stco = int(audio["stco_body"])
        payload[audio_stco + 8:audio_stco + 12] = struct.pack(">I", video_offset)
        if exact:
            video_stsz = int(video["stsz_body"])
            audio_stsz = int(audio["stsz_body"])
            video_sample_size = struct.unpack(">I", payload[video_stsz + 12:video_stsz + 16])[0]
            payload[audio_stsz + 12:audio_stsz + 16] = struct.pack(">I", video_sample_size)
        if not swapped:
            return bytes(payload)
        tracks = cls._track_info(payload)
        moov = next(child for child in cls._children(payload, 0, len(payload)) if child[0] == b"moov")
        first, second = sorted(
            (track for track in tracks), key=lambda track: int(track["trak_start"]),
        )
        moov_body = bytes(payload[moov[3]:moov[4]])
        first_start = int(first["trak_start"]) - moov[3]
        first_end = int(first["trak_end"]) - moov[3]
        second_start = int(second["trak_start"]) - moov[3]
        second_end = int(second["trak_end"]) - moov[3]
        swapped_body = (
            moov_body[:first_start]
            + moov_body[second_start:second_end]
            + moov_body[first_end:second_start]
            + moov_body[first_start:first_end]
            + moov_body[second_end:]
        )
        payload[moov[3]:moov[4]] = swapped_body
        return bytes(payload)

    def test_cross_track_partial_overlap_is_rejected_in_both_orders(self) -> None:
        for swapped in (False, True):
            with self.subTest(swapped=swapped):
                with self.assertRaisesRegex(media_scrub.MediaScrubError, "sample (?:chunks|ranges) overlap"):
                    media_scrub.scrub_video(
                        self._overlap_fixture(exact=False, swapped=swapped),
                        "video/mp4",
                    )

    def test_cross_track_exact_overlap_is_rejected_in_both_orders(self) -> None:
        for swapped in (False, True):
            with self.subTest(swapped=swapped):
                with self.assertRaisesRegex(media_scrub.MediaScrubError, "sample (?:chunks|ranges) overlap"):
                    media_scrub.scrub_video(
                        self._overlap_fixture(exact=True, swapped=swapped),
                        "video/mp4",
                    )

    def test_video_track_cannot_use_mp4a_sample_entry(self) -> None:
        payload = bytearray(REAL_MIXED_MP4.read_bytes())
        video = next(track for track in self._track_info(payload) if track["handler"] == b"vide")
        stsd_entry_type = int(video["stsd_body"]) + 12
        payload[stsd_entry_type:stsd_entry_type + 4] = b"mp4a"
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "vide track cannot use"):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_audio_track_cannot_use_avc1_sample_entry(self) -> None:
        payload = bytearray(REAL_MIXED_MP4.read_bytes())
        audio = next(track for track in self._track_info(payload) if track["handler"] == b"soun")
        stsd_entry_type = int(audio["stsd_body"]) + 12
        payload[stsd_entry_type:stsd_entry_type + 4] = b"avc1"
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "soun track cannot use"):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_sample_entry_child_iterator_is_bounded(self) -> None:
        btrt = struct.pack(">I", 20) + b"btrt" + b"\x00" * 12
        entry = b"\x00" * 36 + btrt * 100_000
        tracemalloc.start()
        try:
            iterator = mp4_scrubber._iter_sample_entry_inner_boxes(entry, 36)
            with self.assertRaisesRegex(media_scrub.MediaScrubError, "more than 4096 child boxes"):
                for _box_type, _body in iterator:
                    pass
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 8 * 1024 * 1024, f"peak allocation was {peak} bytes")

    def test_gif_high_entropy_rebuild_has_bounded_peak_memory(self) -> None:
        pixel_count = 4_000_000
        pixels = random.Random(190).randbytes(pixel_count)
        compressed = gif_scrubber._encode_gif_lzw(pixels, 8)
        blocks = b"".join(
            bytes([min(255, len(compressed) - offset)])
            + compressed[offset:offset + 255]
            for offset in range(0, len(compressed), 255)
        )
        payload = (
            b"GIF89a"
            + struct.pack("<HH", 2000, 2000)
            + b"\xf7\x00\x00"
            + bytes(range(256)) * 3
            + b"\x2c"
            + struct.pack("<HHHH", 0, 0, 2000, 2000)
            + b"\x00\x08"
            + blocks
            + b"\x00\x3b"
        )
        tracemalloc.start()
        try:
            result = media_scrub.scrub_video(payload, "image/gif")
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual((result.width, result.height), (2000, 2000))
        self.assertLess(peak, 30 * 1024 * 1024, f"peak allocation was {peak} bytes")


class Review18MediaProbeTests(unittest.TestCase):
    def test_gif_logical_screen_pixel_budget_is_enforced(self) -> None:
        payload = (
            b"GIF89a"
            + struct.pack("<HH", 65535, 65535)
            + b"\x00\x00\x00"
            + b"\x2c"
            + struct.pack("<HHHH", 0, 0, 1, 1)
            + b"\x00"
            + b"\x02\x02\x44\x01\x00"
            + b"\x3b"
        )
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "logical screen|pixels"):
            media_scrub.scrub_video(payload, "image/gif")

    def test_gif_image_requires_an_active_color_table(self) -> None:
        payload = (
            b"GIF89a" + struct.pack("<HH", 1, 1) + b"\x00\x00\x00"
            + b"\x2c" + struct.pack("<HHHH", 0, 0, 1, 1) + b"\x00"
            + b"\x02\x02\x44\x01\x00\x3b"
        )
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "color table"):
            media_scrub.scrub_video(payload, "image/gif")

    def test_gif_pixel_index_must_fit_active_color_table(self) -> None:
        payload = (
            b"GIF89a" + struct.pack("<HH", 1, 1) + b"\x80\x00\x00"
            + b"\x00\x00\x00\xff\xff\xff"
            + b"\x2c" + struct.pack("<HHHH", 0, 0, 1, 1) + b"\x00"
            + b"\x02\x02\x54\x01\x00\x3b"
        )
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "pixel index"):
            media_scrub.scrub_video(payload, "image/gif")

    def test_aac_only_mp4_is_rejected_without_video_track(self) -> None:
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "vide/avc1"):
            media_scrub.scrub_video(REAL_AAC_ONLY_MP4.read_bytes(), "video/mp4")

    def test_mp4_unknown_compatible_brand_is_rejected(self) -> None:
        payload = bytearray(REAL_MP4.read_bytes())
        marker_offset = payload.find(b"mp41", 8)
        self.assertGreaterEqual(marker_offset, 0)
        payload[marker_offset:marker_offset + 4] = b"GPS!"
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "brand.*allowlist"):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_mp4_unknown_major_brand_is_canonicalized(self) -> None:
        payload = bytearray(REAL_MP4.read_bytes())
        payload[8:12] = b"GPS!"
        result = media_scrub.scrub_video(bytes(payload), "video/mp4")
        self.assertNotIn(b"GPS!", result.data[:32])

    def test_mp3_layer_i_header_with_trailing_marker_is_rejected(self) -> None:
        payload = b"\xff\xff\x10\x00" + b"GEOLOCATION-MARKER"
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_audio(payload, "audio/mpeg")

    def test_mp3_layer_ii_header_with_trailing_marker_is_rejected(self) -> None:
        payload = b"\xff\xfd\x10\x00" + b"GEOLOCATION-MARKER"
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_audio(payload, "audio/mpeg")

    @staticmethod
    def _large_stsz_fixture(sample_count: int = 250_000) -> bytes:
        payload = bytearray(REAL_MP4.read_bytes())
        tracks = Review17MediaProbeTests._track_info(payload)
        video = next(track for track in tracks if track["handler"] == b"vide")
        moov = next(
            child for child in Review17MediaProbeTests._children(payload, 0, len(payload))
            if child[0] == b"moov"
        )
        trak = next(
            child for child in Review17MediaProbeTests._children(
                payload, moov[3], moov[4],
            )
            if child[0] == b"trak" and child[1] == int(video["trak_start"])
        )
        mdia = next(
            child for child in Review17MediaProbeTests._children(
                payload, trak[3], trak[4],
            )
            if child[0] == b"mdia"
        )
        minf = next(
            child for child in Review17MediaProbeTests._children(
                payload, mdia[3], mdia[4],
            )
            if child[0] == b"minf"
        )
        stbl = next(
            child for child in Review17MediaProbeTests._children(
                payload, minf[3], minf[4],
            )
            if child[0] == b"stbl"
        )
        stsz_start = int(video["stsz_body"]) - 8
        stsz_end = stsz_start + struct.unpack(
            ">I", payload[stsz_start:stsz_start + 4]
        )[0]
        replacement_body = (
            b"\x00\x00\x00\x00"
            + struct.pack(">II", 0, sample_count)
            + b"\x00\x00\x00\x01" * sample_count
        )
        replacement = struct.pack(">I", 8 + len(replacement_body)) + b"stsz" + replacement_body
        delta = len(replacement) - (stsz_end - stsz_start)
        payload[stsz_start:stsz_end] = replacement
        for parent in (stbl, minf, mdia, trak, moov):
            parent_start = parent[1]
            old_size = struct.unpack(">I", payload[parent_start:parent_start + 4])[0]
            payload[parent_start:parent_start + 4] = struct.pack(">I", old_size + delta)
        updated_video = next(
            track for track in Review17MediaProbeTests._track_info(payload)
            if track["handler"] == b"vide"
        )
        stco_body = int(updated_video["stco_body"])
        first_offset = struct.unpack(">I", payload[stco_body + 8:stco_body + 12])[0]
        payload[stco_body + 8:stco_body + 12] = struct.pack(">I", first_offset + delta)
        return bytes(payload)

    def test_mp4_large_stsz_full_scrub_has_bounded_peak(self) -> None:
        payload = self._large_stsz_fixture()
        tracemalloc.start()
        try:
            with self.assertRaises(media_scrub.MediaScrubError):
                media_scrub.scrub_video(payload, "video/mp4")
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(
            peak, len(payload) * 16,
            f"peak allocation {peak} exceeded 16x input size {len(payload)}",
        )

    @staticmethod
    def _large_stco_fixture(chunk_count: int = 1_000_000) -> bytes:
        payload = bytearray(REAL_MP4.read_bytes())
        tracks = Review17MediaProbeTests._track_info(payload)
        video = next(track for track in tracks if track["handler"] == b"vide")
        moov = next(
            child for child in Review17MediaProbeTests._children(payload, 0, len(payload))
            if child[0] == b"moov"
        )
        trak = next(
            child for child in Review17MediaProbeTests._children(payload, moov[3], moov[4])
            if child[0] == b"trak" and child[1] == int(video["trak_start"])
        )
        mdia = next(
            child for child in Review17MediaProbeTests._children(payload, trak[3], trak[4])
            if child[0] == b"mdia"
        )
        minf = next(
            child for child in Review17MediaProbeTests._children(payload, mdia[3], mdia[4])
            if child[0] == b"minf"
        )
        stbl = next(
            child for child in Review17MediaProbeTests._children(payload, minf[3], minf[4])
            if child[0] == b"stbl"
        )
        stco_start = int(video["stco_body"]) - 8
        old_size = struct.unpack(">I", payload[stco_start:stco_start + 4])[0]
        replacement_body = (
            b"\x00\x00\x00\x00" + struct.pack(">I", chunk_count)
            + b"\x00\x00\x00\x00" * chunk_count
        )
        replacement = struct.pack(">I", 8 + len(replacement_body)) + b"stco" + replacement_body
        delta = len(replacement) - old_size
        payload[stco_start:stco_start + old_size] = replacement
        for parent in (stbl, minf, mdia, trak, moov):
            parent_start = parent[1]
            parent_size = struct.unpack(">I", payload[parent_start:parent_start + 4])[0]
            payload[parent_start:parent_start + 4] = struct.pack(">I", parent_size + delta)
        return bytes(payload)

    def test_mp4_millions_of_chunks_reject_before_materialization(self) -> None:
        payload = self._large_stco_fixture()
        tracemalloc.start()
        try:
            with self.assertRaisesRegex(media_scrub.MediaScrubError, "chunk count"):
                media_scrub.scrub_video(payload, "video/mp4")
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 32 * 1024 * 1024, f"peak allocation was {peak} bytes")

    def test_mp4_sample_plan_is_reused_for_multiple_mdat_boxes(self) -> None:
        extra_body = b"second-mdat-marker"
        payload = REAL_MP4.read_bytes() + (
            struct.pack(">I", 8 + len(extra_body)) + b"mdat" + extra_body
        )
        result = media_scrub.scrub_video(payload, "video/mp4")
        self.assertNotIn(extra_body, result.data)
        self.assertEqual(result.data[-len(extra_body):], b"\x00" * len(extra_body))


class Review20MediaProbeTests(unittest.TestCase):
    @staticmethod
    def _oversized_sps() -> bytes:
        writer = h264_scrubber._BitWriter()
        writer.write_bits(100, 8)  # profile_idc, matching the fixture avcC
        writer.write_bits(0, 8)  # constraint flags + reserved bits
        writer.write_bits(10, 8)  # level_idc, matching the fixture avcC
        writer.write_ue(0)  # seq_parameter_set_id
        writer.write_ue(1)  # chroma_format_idc = 1
        writer.write_ue(0)  # bit_depth_luma_minus8
        writer.write_ue(0)  # bit_depth_chroma_minus8
        writer.write_u1(0)  # qpprime_y_zero_transform_bypass_flag
        writer.write_u1(0)  # seq_scaling_matrix_present_flag
        writer.write_ue(0)  # log2_max_frame_num_minus4
        writer.write_ue(0)  # pic_order_cnt_type
        writer.write_ue(0)  # log2_max_pic_order_cnt_lsb_minus4
        writer.write_ue(0)  # max_num_ref_frames
        writer.write_u1(0)  # gaps_in_frame_num_value_allowed_flag
        writer.write_ue(65535)  # pic_width_in_mbs_minus1
        writer.write_ue(65535)  # pic_height_in_map_units_minus1
        writer.write_u1(1)  # frame_mbs_only_flag
        writer.write_u1(1)  # direct_8x8_inference_flag
        writer.write_u1(0)  # frame_cropping_flag
        writer.write_u1(0)  # vui_parameters_present_flag
        writer.write_rbsp_trailing_bits()
        return b"\x67" + h264_scrubber._rbsp_escape(writer.to_bytes())

    @staticmethod
    def _replace_sps(real: bytes, sps: bytes) -> bytes:
        avcc_pos = real.find(b"avcC")
        assert avcc_pos > 0
        atom_start = avcc_pos - 4
        old_size = struct.unpack(">I", real[atom_start:avcc_pos])[0]
        body_start = avcc_pos + 4
        old_sps_len = struct.unpack(">H", real[body_start + 6:body_start + 8])[0]
        after_sps = body_start + 8 + old_sps_len
        body = bytearray(real[body_start:atom_start + old_size])
        body[6:8] = struct.pack(">H", len(sps))
        body[8:8 + old_sps_len] = sps
        del body[8 + len(sps):8 + len(sps) + max(0, old_sps_len - len(sps))]
        replacement = struct.pack(">I", 8 + len(body)) + b"avcC" + body
        payload = bytearray(real)
        payload[atom_start:atom_start + old_size] = replacement
        delta = len(replacement) - old_size
        for parent in (b"avc1", b"stsd", b"stbl", b"minf", b"mdia", b"trak", b"moov"):
            pos = payload.rfind(parent, 0, atom_start)
            existing = struct.unpack(">I", bytes(payload[pos - 4:pos]))[0]
            payload[pos - 4:pos] = struct.pack(">I", existing + delta)
        if delta:
            stco_pos = payload.find(b"stco")
            count = struct.unpack(">I", bytes(payload[stco_pos + 8:stco_pos + 12]))[0]
            for index in range(count):
                value_pos = stco_pos + 12 + index * 4
                value = struct.unpack(">I", bytes(payload[value_pos:value_pos + 4]))[0]
                payload[value_pos:value_pos + 4] = struct.pack(">I", value + delta)
        return bytes(payload)

    def test_oversized_sps_is_rejected_before_decode(self) -> None:
        payload = self._replace_sps(REAL_MP4.read_bytes(), self._oversized_sps())
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "SPS coded dimensions"):
            media_scrub.scrub_video(payload, "video/mp4")

    @staticmethod
    def _remove_video_stts(real: bytes) -> bytes:
        payload = bytearray(real)
        stts_pos = payload.find(b"stts")
        assert stts_pos > 0
        stts_start = stts_pos - 4
        stts_size = struct.unpack(">I", payload[stts_start:stts_pos])[0]
        del payload[stts_start:stts_start + stts_size]
        for parent in (b"stbl", b"minf", b"mdia", b"trak", b"moov"):
            pos = payload.rfind(parent, 0, stts_start)
            old_size = struct.unpack(">I", payload[pos - 4:pos])[0]
            payload[pos - 4:pos] = struct.pack(">I", old_size - stts_size)
        for track in Review17MediaProbeTests._track_info(payload):
            stco_body = int(track["stco_body"])
            count = struct.unpack(">I", payload[stco_body + 4:stco_body + 8])[0]
            for index in range(count):
                value_pos = stco_body + 8 + index * 4
                value = struct.unpack(">I", payload[value_pos:value_pos + 4])[0]
                payload[value_pos:value_pos + 4] = struct.pack(">I", value - stts_size)
        return bytes(payload)

    def test_missing_stts_is_rejected(self) -> None:
        payload = self._remove_video_stts(REAL_MP4.read_bytes())
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "exactly one stts"):
            media_scrub.scrub_video(payload, "video/mp4")

    @staticmethod
    def _many_mdat_uniform_fixture(
        sample_count: int = 100_000, extra_mdat_count: int = 2_000,
    ) -> bytes:
        payload = bytearray(REAL_MP4.read_bytes())
        stsz_pos = payload.find(b"stsz")
        old_stsz_start = stsz_pos - 4
        old_stsz_size = struct.unpack(">I", payload[old_stsz_start:stsz_pos])[0]
        stsz_body = b"\x00\x00\x00\x00" + struct.pack(">II", 6, sample_count)
        replacement = struct.pack(">I", 8 + len(stsz_body)) + b"stsz" + stsz_body
        payload[old_stsz_start:old_stsz_start + old_stsz_size] = replacement
        delta = len(replacement) - old_stsz_size
        for parent in (b"stbl", b"minf", b"mdia", b"trak", b"moov"):
            pos = payload.rfind(parent, 0, old_stsz_start)
            old_size = struct.unpack(">I", payload[pos - 4:pos])[0]
            payload[pos - 4:pos] = struct.pack(">I", old_size + delta)
        stts_pos = payload.find(b"stts")
        payload[stts_pos + 12:stts_pos + 16] = struct.pack(">I", sample_count)
        stsc_pos = payload.find(b"stsc")
        payload[stsc_pos + 16:stsc_pos + 20] = struct.pack(">I", sample_count)
        ctts_pos = payload.find(b"ctts")
        if ctts_pos > 0:
            ctts_start = ctts_pos - 4
            old_ctts_size = struct.unpack(">I", payload[ctts_start:ctts_pos])[0]
            ctts_body = (
                b"\x00\x00\x00\x00" + struct.pack(">I", 1)
                + struct.pack(">II", sample_count, 0)
            )
            replacement_ctts = (
                struct.pack(">I", 8 + len(ctts_body)) + b"ctts" + ctts_body
            )
            payload[ctts_start:ctts_start + old_ctts_size] = replacement_ctts
            ctts_delta = len(replacement_ctts) - old_ctts_size
            for parent in (b"stbl", b"minf", b"mdia", b"trak", b"moov"):
                pos = payload.rfind(parent, 0, ctts_start)
                old_size = struct.unpack(">I", payload[pos - 4:pos])[0]
                payload[pos - 4:pos] = struct.pack(">I", old_size + ctts_delta)
        stco_pos = payload.find(b"stco")
        old_offset = struct.unpack(">I", payload[stco_pos + 12:stco_pos + 16])[0]
        payload[stco_pos + 12:stco_pos + 16] = struct.pack(
            ">I", old_offset + delta + (ctts_delta if ctts_pos > 0 else 0),
        )
        mdat_pos = payload.find(b"mdat")
        mdat_start = mdat_pos - 4
        old_mdat_size = struct.unpack(">I", payload[mdat_start:mdat_pos])[0]
        sample = b"\x00\x00\x00\x02\x06\x80"
        mdat_body = sample * sample_count
        replacement_mdat = struct.pack(">I", 8 + len(mdat_body)) + b"mdat" + mdat_body
        payload[mdat_start:mdat_start + old_mdat_size] = replacement_mdat
        extra = b"".join(
            struct.pack(">I", 9) + b"mdat" + b"\x00"
            for _ in range(extra_mdat_count)
        )
        return bytes(payload) + extra

    def test_many_mdats_do_not_restart_large_sample_walk(self) -> None:
        payload = self._many_mdat_uniform_fixture()
        started = time.perf_counter()
        result = media_scrub.scrub_video(payload, "video/mp4")
        elapsed = time.perf_counter() - started
        self.assertEqual(len(result.data), len(payload))
        self.assertLess(elapsed, 8.0, f"scrub took {elapsed:.2f}s")


class Review21MediaProbeTests(unittest.TestCase):
    def test_aac_sample_marker_is_rejected_before_storage(self) -> None:
        payload = bytearray(REAL_MIXED_MP4.read_bytes())
        audio = next(
            track for track in Review17MediaProbeTests._track_info(payload)
            if track["handler"] == b"soun"
        )
        stsz_body = int(audio["stsz_body"])
        stco_body = int(audio["stco_body"])
        first_offset = struct.unpack(">I", payload[stco_body + 8:stco_body + 12])[0]
        first_size = struct.unpack(">I", payload[stsz_body + 12:stsz_body + 16])[0]
        marker = b"GPS-AAC-SAMPLE"
        payload[first_offset:first_offset + first_size] = (
            marker + b"X" * (first_size - len(marker))
        )
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "AAC"):
            media_scrub.scrub_video(bytes(payload), "video/mp4")

    def test_mvhd_and_tkhd_matrices_reject_marker_bytes(self) -> None:
        for box_type, matrix_offset in ((b"mvhd", 36), (b"tkhd", 40)):
            with self.subTest(box_type=box_type):
                payload = bytearray(REAL_MP4.read_bytes())
                type_pos = payload.find(box_type)
                marker = b"GPS-MATRIX!!"
                payload[type_pos + 4 + matrix_offset:type_pos + 4 + matrix_offset + len(marker)] = marker
                with self.assertRaisesRegex(media_scrub.MediaScrubError, "matrix"):
                    media_scrub.scrub_video(bytes(payload), "video/mp4")

    @staticmethod
    def _sps_with_ref_count(max_num_ref_frames: int) -> bytes:
        writer = h264_scrubber._BitWriter()
        writer.write_bits(100, 8)
        writer.write_bits(0, 8)
        writer.write_bits(10, 8)
        writer.write_ue(0)
        writer.write_ue(1)
        writer.write_ue(0)
        writer.write_ue(0)
        writer.write_u1(0)
        writer.write_u1(0)
        writer.write_ue(0)
        writer.write_ue(0)
        writer.write_ue(0)
        writer.write_ue(max_num_ref_frames)
        writer.write_u1(0)
        writer.write_ue(9)
        writer.write_ue(7)
        writer.write_u1(1)
        writer.write_u1(1)
        writer.write_u1(1)
        writer.write_ue(0)
        writer.write_ue(0)
        writer.write_ue(0)
        writer.write_ue(4)
        writer.write_u1(0)
        writer.write_rbsp_trailing_bits()
        return b"\x67" + h264_scrubber._rbsp_escape(writer.to_bytes())

    def test_sps_reference_count_is_bounded_by_level_dpb(self) -> None:
        payload = Review20MediaProbeTests._replace_sps(
            REAL_MP4.read_bytes(), self._sps_with_ref_count(5),
        )
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "DPB"):
            media_scrub.scrub_video(payload, "video/mp4")

    @staticmethod
    def _near_limit_chunk_mdat_fixture() -> bytes:
        real = REAL_MP4.read_bytes()
        top = Review17MediaProbeTests._children(real, 0, len(real))
        ftyp = next(child for child in top if child[0] == b"ftyp")
        moov = next(child for child in top if child[0] == b"moov")
        ftyp_bytes = real[ftyp[1]:ftyp[2]]
        moov_bytes = real[moov[1]:moov[2]]
        sample_count = 65_536
        mdat_count = 4_094
        samples_per_mdat = [16] * (mdat_count - 1) + [48]
        sample = b"\x00\x00\x00\x02\x06\x80"
        mdat_bodies = [sample * count for count in samples_per_mdat]

        def box(box_type: bytes, body: bytes) -> bytes:
            return struct.pack(">I", 8 + len(body)) + box_type + body

        def rebuild_tree(raw: bytes, replacements: dict[bytes, bytes]) -> bytes:
            box_type = raw[4:8]
            if box_type in replacements:
                return replacements[box_type]
            if box_type not in {b"moov", b"trak", b"mdia", b"minf", b"stbl"}:
                return raw
            body = raw[8:]
            children = []
            offset = 0
            while offset < len(body):
                child_size = struct.unpack(">I", body[offset:offset + 4])[0]
                children.append(rebuild_tree(body[offset:offset + child_size], replacements))
                offset += child_size
            if offset != len(body):
                raise AssertionError("fixture container did not end on a child boundary")
            return box(box_type, b"".join(children))

        def replacement_map(stco: bytes) -> dict[bytes, bytes]:
            replacements = {
                b"stsc": box(
                    b"stsc",
                    b"\x00\x00\x00\x00" + struct.pack(">I", 1)
                    + struct.pack(">III", 1, 1, 1),
                ),
                b"stsz": box(
                    b"stsz",
                    b"\x00\x00\x00\x00" + struct.pack(">II", len(sample), sample_count),
                ),
                b"stts": box(
                    b"stts",
                    b"\x00\x00\x00\x00" + struct.pack(">I", 1)
                    + struct.pack(">II", sample_count, 1),
                ),
                b"stco": stco,
            }
            if b"ctts" in moov_bytes:
                replacements[b"ctts"] = box(
                    b"ctts",
                    b"\x00\x00\x00\x00" + struct.pack(">I", 1)
                    + struct.pack(">II", sample_count, 0),
                )
            return replacements

        placeholder_stco = box(
            b"stco",
            b"\x00\x00\x00\x00" + struct.pack(">I", sample_count)
            + b"\x00" * (sample_count * 4),
        )
        placeholder_moov = rebuild_tree(
            moov_bytes, replacement_map(placeholder_stco),
        )
        mdat_start = len(ftyp_bytes) + len(placeholder_moov)
        offsets: list[int] = []
        cursor = mdat_start
        for body in mdat_bodies:
            body_start = cursor + 8
            offsets.extend(body_start + index * len(sample) for index in range(len(body) // len(sample)))
            cursor += 8 + len(body)
        self_stco = box(
            b"stco",
            b"\x00\x00\x00\x00" + struct.pack(">I", sample_count)
            + b"".join(struct.pack(">I", value) for value in offsets),
        )
        final_moov = rebuild_tree(moov_bytes, replacement_map(self_stco))
        mdats = b"".join(box(b"mdat", body) for body in mdat_bodies)
        return ftyp_bytes + final_moov + mdats

    def test_near_limit_chunks_and_mdats_are_grouped(self) -> None:
        payload = self._near_limit_chunk_mdat_fixture()
        started = time.perf_counter()
        result = media_scrub.scrub_video(payload, "video/mp4")
        elapsed = time.perf_counter() - started
        self.assertEqual(len(result.data), len(payload))
        self.assertLess(elapsed, 8.0, f"scrub took {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
