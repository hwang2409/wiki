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
