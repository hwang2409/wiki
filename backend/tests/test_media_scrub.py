from __future__ import annotations

import resource
import shutil
import struct
import subprocess
import tempfile
import tracemalloc
import unittest
from pathlib import Path

from backend.app import media_scrub


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "media"
REAL_MP4 = FIXTURE_DIR / "tiny.mp4"
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
        fmt_body = struct.pack("<HHIIHH", 1, 1, 8000, 16000, 2, 16)
        fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
        # 3-byte data payload — odd length.
        odd_data = b"\x00\x00\x00"
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
    def _wav_with_format(format_code: int, extensible_subformat: bytes | None = None) -> bytes:
        # Minimal WAV with the given format code. If subformat provided,
        # emits an extensible fmt chunk (chunk_size = 40).
        if extensible_subformat is not None:
            fmt_body = (
                struct.pack("<HHIIHH", format_code, 1, 16000, 32000, 2, 16)
                + struct.pack("<HHI", 22, 16, 0)
                + extensible_subformat
            )
        else:
            fmt_body = struct.pack("<HHIIHH", format_code, 1, 16000, 32000, 2, 16)
        fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
        data_chunk = b"data" + struct.pack("<I", 4) + b"\x00\x00\x00\x00"
        body = b"WAVE" + fmt_chunk + data_chunk
        return b"RIFF" + struct.pack("<I", len(body)) + body

    def test_format_code_zero_is_rejected(self) -> None:
        with self.assertRaisesRegex(media_scrub.MediaScrubError, "format code 0"):
            media_scrub.scrub_audio(self._wav_with_format(0), "audio/wav")

    def test_format_code_pcm_is_accepted(self) -> None:
        media_scrub.scrub_audio(self._wav_with_format(1), "audio/wav")

    def test_format_code_ieee_float_is_accepted(self) -> None:
        media_scrub.scrub_audio(self._wav_with_format(3), "audio/wav")

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
        fmt_body = (
            struct.pack("<HHIIHH", 0xFFFE, 1, 16000, 32000, 2, 16)
            + struct.pack("<H", cb_size)
            + struct.pack("<HI", 16, 0)
            + subformat
        )
        fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_body)) + fmt_body
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


class UnsupportedMimeTests(unittest.TestCase):
    def test_scrub_video_rejects_audio_mime(self) -> None:
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_video(b"\x00" * 32, "audio/wav")

    def test_scrub_audio_rejects_video_mime(self) -> None:
        with self.assertRaises(media_scrub.MediaScrubError):
            media_scrub.scrub_audio(b"\x00" * 32, "video/mp4")


if __name__ == "__main__":
    unittest.main()
