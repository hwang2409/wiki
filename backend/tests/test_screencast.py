"""WIKI-176: bounded tail extractor + fleet screencast endpoint."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from backend.app import main, screencast


def _claude_assistant(text: str, *, ts: str = "2026-07-30T00:00:00Z") -> dict:
    return {
        "provider": "claude",
        "received_at": ts,
        "payload": {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        },
    }


def _claude_tool_use(name: str) -> dict:
    return {
        "provider": "claude",
        "received_at": "2026-07-30T00:00:01Z",
        "payload": {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": name, "input": {}}],
            },
        },
    }


def _claude_tool_result(text: str) -> dict:
    return {
        "provider": "claude",
        "received_at": "2026-07-30T00:00:02Z",
        "payload": {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": text, "tool_use_id": "toolu_x"}
                ],
            },
        },
    }


def _codex_message(text: str, role: str = "assistant") -> dict:
    return {
        "provider": "codex",
        "received_at": "2026-07-30T00:00:03Z",
        "payload": {
            "method": "rawResponseItem/completed",
            "params": {
                "item": {
                    "type": "message",
                    "role": role,
                    "content": [{"type": "output_text", "text": text}],
                }
            },
        },
    }


def _codex_shell_output(text: str) -> dict:
    return {
        "provider": "codex",
        "received_at": "2026-07-30T00:00:04Z",
        "payload": {
            "method": "rawResponseItem/completed",
            "params": {
                "item": {"type": "custom_tool_call_output", "output": text}
            },
        },
    }


def _write_raw(path: Path, envelopes: list[dict], *, trailing_newline: bool = True) -> None:
    lines = [json.dumps(env) for env in envelopes]
    body = "\n".join(lines)
    if trailing_newline:
        body += "\n"
    path.write_text(body, encoding="utf-8")


class TailExtractorTests(unittest.TestCase):
    def test_extracts_claude_assistant_text(self) -> None:
        frames = screencast.frames_from_lines(
            [json.dumps(_claude_assistant("hello world")).encode("utf-8")]
        )
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].kind, "assistant")
        self.assertEqual(frames[0].text, "hello world")

    def test_falls_back_to_tool_hint_when_assistant_has_only_tool_use(self) -> None:
        frames = screencast.frames_from_lines(
            [json.dumps(_claude_tool_use("Bash")).encode("utf-8")]
        )
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].kind, "tool")
        self.assertEqual(frames[0].text, "→ Bash")

    def test_extracts_claude_tool_result_output(self) -> None:
        frames = screencast.frames_from_lines(
            [json.dumps(_claude_tool_result("compiled ok")).encode("utf-8")]
        )
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].kind, "tool")
        self.assertEqual(frames[0].text, "compiled ok")

    def test_extracts_codex_assistant_and_shell_output(self) -> None:
        frames = screencast.frames_from_lines(
            [
                json.dumps(_codex_message("thinking done")).encode("utf-8"),
                json.dumps(_codex_shell_output("build passed")).encode("utf-8"),
            ]
        )
        kinds = [(frame.kind, frame.text) for frame in frames]
        self.assertEqual(
            kinds,
            [("assistant", "thinking done"), ("tool", "build passed")],
        )

    def test_ignores_non_message_envelopes(self) -> None:
        envelope = {
            "provider": "codex",
            "payload": {
                "method": "thread/tokenUsage/updated",
                "params": {"tokenUsage": {}},
            },
        }
        frames = screencast.frames_from_lines(
            [json.dumps(envelope).encode("utf-8")]
        )
        self.assertEqual(frames, [])

    def test_skips_unparseable_lines(self) -> None:
        frames = screencast.frames_from_lines(
            [b"not-json", json.dumps(_claude_assistant("ok")).encode("utf-8")]
        )
        self.assertEqual(len(frames), 1)

    def test_keeps_only_last_max_frames(self) -> None:
        many = [
            json.dumps(_claude_assistant(f"line {n}")).encode("utf-8") for n in range(30)
        ]
        frames = screencast.frames_from_lines(many, max_frames=5)
        self.assertEqual(len(frames), 5)
        self.assertEqual(frames[0].text, "line 25")
        self.assertEqual(frames[-1].text, "line 29")

    def test_clips_long_text(self) -> None:
        big = "x" * (screencast.MAX_FRAME_TEXT + 500)
        frames = screencast.frames_from_lines(
            [json.dumps(_claude_assistant(big)).encode("utf-8")]
        )
        self.assertEqual(len(frames), 1)
        self.assertLessEqual(len(frames[0].text), screencast.MAX_FRAME_TEXT)
        self.assertTrue(frames[0].text.endswith("…"))

    def test_default_max_frames_is_twenty(self) -> None:
        """Spec: strip shows ~20 lines. Not 6, not 500 — twenty."""
        self.assertEqual(screencast.DEFAULT_MAX_FRAMES, 20)


class SanitizeTextTests(unittest.TestCase):
    def test_strips_ansi_colour_and_cursor_escapes(self) -> None:
        payload = "\x1b[31mred\x1b[0m\x1b[2Kclear-line\x1b]0;title\x07after"
        frames = screencast.frames_from_lines(
            [json.dumps(_claude_assistant(payload)).encode("utf-8")]
        )
        self.assertEqual(frames[0].text, "redclear-lineafter")

    def test_strips_bidi_overrides_that_could_spoof_tool_labels(self) -> None:
        """LRO/RLO/PDI/BOM must never survive to the client — they would
        let a worker rewrite the visual reading order of the strip."""

        # A tool name that pretends to be "→ ls" but reverses to "→ sl" in RTL
        payload = "‮rm -rf /‬ legit-looking"
        frames = screencast.frames_from_lines(
            [json.dumps(_claude_assistant(payload)).encode("utf-8")]
        )
        for ch in ("‮", "‬", "‭", "⁦", "⁩"):
            self.assertNotIn(ch, frames[0].text)

    def test_strips_nul_and_c0_c1_controls(self) -> None:
        payload = "hello\x00\x07\x1b\x9bworld"
        frames = screencast.frames_from_lines(
            [json.dumps(_claude_assistant(payload)).encode("utf-8")]
        )
        for ch in ("\x00", "\x07", "\x1b", "\x9b"):
            self.assertNotIn(ch, frames[0].text)
        self.assertIn("hello", frames[0].text)
        self.assertIn("world", frames[0].text)

    def test_strips_zero_width_and_soft_hyphen(self) -> None:
        payload = "safe​word‌‍﻿­"
        frames = screencast.frames_from_lines(
            [json.dumps(_claude_assistant(payload)).encode("utf-8")]
        )
        self.assertEqual(frames[0].text, "safeword")


class TornLineTests(unittest.TestCase):
    def test_iter_drops_torn_head_when_seeked(self) -> None:
        chunk = b"partial-record\n" + b"{\"provider\":\"claude\"}\n"
        parts = list(screencast._iter_newline_terminated(chunk, drop_head=True))
        self.assertEqual(parts, [b'{"provider":"claude"}'])

    def test_iter_keeps_first_when_not_seeked(self) -> None:
        chunk = b"a\nb\n"
        parts = list(screencast._iter_newline_terminated(chunk, drop_head=False))
        self.assertEqual(parts, [b"a", b"b"])

    def test_iter_drops_torn_tail_without_newline(self) -> None:
        chunk = b"complete\nhalf-written-record"
        parts = list(screencast._iter_newline_terminated(chunk, drop_head=False))
        self.assertEqual(parts, [b"complete"])

    def test_iter_handles_empty(self) -> None:
        self.assertEqual(
            list(screencast._iter_newline_terminated(b"", drop_head=False)), []
        )
        self.assertEqual(
            list(screencast._iter_newline_terminated(b"\n", drop_head=False)), []
        )


class BoundedTailFileTests(unittest.TestCase):
    def test_tail_reads_only_end_window_of_multi_gib_sparse_file(self) -> None:
        """Prove the tail read is bounded: a sparse 4 GiB file with data only
        at the tail. If the extractor scanned the whole file, the test would
        allocate multi-gigabyte buffers and either OOM or take minutes. Also
        instrument ``os.pread`` — the exact (fd, size, offset) call — and
        assert we read exactly one window ending at EOF, from a stable
        offset equal to ``file_size - window_bytes``.

        This is the assertion R1 finding #6 asked for: pread SIZE **and**
        OFFSET, not just the extracted output — a whole-file implementation
        would call read()/pread(fd, size, 0) and this test would fail.
        """

        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            run_dir = root / "abc"
            run_dir.mkdir()
            raw_path = run_dir / "raw.jsonl"

            # Sparse-hole up to 4 GiB, then append the tail records at EOF.
            tail_records = (
                json.dumps(_claude_assistant("first")).encode("utf-8")
                + b"\n"
                + json.dumps(_claude_assistant("tail-marker")).encode("utf-8")
                + b"\n"
            )
            sparse_size = 4 * 1024**3
            with raw_path.open("wb") as fp:
                fp.truncate(sparse_size)
                fp.seek(sparse_size)
                fp.write(tail_records)

            expected_size = sparse_size + len(tail_records)
            expected_offset = expected_size - screencast.DEFAULT_WINDOW_BYTES
            observed: list[tuple[int, int]] = []

            real_pread = os.pread

            def spy_pread(fd: int, size: int, offset: int) -> bytes:
                observed.append((size, offset))
                return real_pread(fd, size, offset)

            root_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
            try:
                with mock.patch.object(screencast.os, "pread", spy_pread):
                    frames = screencast.tail_frames(root_fd, "abc")
            finally:
                os.close(root_fd)

        # Exactly one pread — a whole-file implementation would call read()
        # or pread(..., 0), or make many chunked calls. Neither is allowed.
        self.assertEqual(len(observed), 1, f"expected 1 pread, got {observed}")
        observed_size, observed_offset = observed[0]
        self.assertEqual(observed_size, screencast.DEFAULT_WINDOW_BYTES)
        self.assertEqual(observed_offset, expected_offset)

        # And the tail must have been extracted correctly.
        texts = [frame.text for frame in frames]
        self.assertIn("tail-marker", texts)

    def test_rapid_append_yields_the_newer_tail(self) -> None:
        """Two successive tail reads must reflect newly-appended records.

        This proves the reader always seeks to the current EOF instead of
        caching a stale ``st_size``. A worker appending in a hot loop must
        be visible to the strip within one poll.
        """

        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            run_dir = root / "run"
            run_dir.mkdir()
            raw_path = run_dir / "raw.jsonl"

            _write_raw(raw_path, [_claude_assistant("first")])
            root_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
            try:
                first = screencast.tail_frames(root_fd, "run")
                with raw_path.open("ab") as fp:
                    fp.write(
                        json.dumps(_claude_assistant("second")).encode("utf-8")
                        + b"\n"
                    )
                second = screencast.tail_frames(root_fd, "run")
            finally:
                os.close(root_fd)

        self.assertEqual([f.text for f in first], ["first"])
        self.assertEqual([f.text for f in second], ["first", "second"])

    def test_tail_skips_torn_last_line(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            run_dir = root / "def"
            run_dir.mkdir()
            raw_path = run_dir / "raw.jsonl"
            envelopes = [
                _claude_assistant("stable"),
                _claude_assistant("also-stable"),
            ]
            body = "\n".join(json.dumps(env) for env in envelopes) + "\n"
            partial = json.dumps(_claude_assistant("torn"))[:-5]
            raw_path.write_text(body + partial, encoding="utf-8")

            root_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
            try:
                frames = screencast.tail_frames(root_fd, "def")
            finally:
                os.close(root_fd)

        texts = [frame.text for frame in frames]
        self.assertEqual(texts, ["stable", "also-stable"])

    def test_tail_returns_empty_for_missing_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root_fd = os.open(raw_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                frames = screencast.tail_frames(root_fd, "no-such-run")
            finally:
                os.close(root_fd)
        self.assertEqual(frames, [])

    def test_tail_rejects_symlink_run_dir(self) -> None:
        """Path safety: a symlink swapped in for the run dir must NOT be
        followed — pathwalk uses O_NOFOLLOW per component so tail_frames
        returns []."""

        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            outside = root / "outside"
            outside.mkdir()
            raw_path = outside / "raw.jsonl"
            _write_raw(raw_path, [_claude_assistant("secret")])
            link = root / "fake-run"
            link.symlink_to(outside)

            root_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
            try:
                frames = screencast.tail_frames(root_fd, "fake-run")
            finally:
                os.close(root_fd)

        self.assertEqual(frames, [])

    def test_tail_rejects_symlink_raw_file(self) -> None:
        """Final-component symlink for raw.jsonl itself must be refused."""

        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            run_dir = root / "run"
            run_dir.mkdir()
            outside = root / "outside.jsonl"
            _write_raw(outside, [_claude_assistant("secret")])
            (run_dir / "raw.jsonl").symlink_to(outside)

            root_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
            try:
                frames = screencast.tail_frames(root_fd, "run")
            finally:
                os.close(root_fd)

        self.assertEqual(frames, [])

    def test_tail_rejects_traversal_run_id(self) -> None:
        """A run_id containing ``..`` would escape the anchor via
        os.open(dir_fd=...) despite O_NOFOLLOW. Both the helper and
        tail_frames must refuse — the reviewer flagged the previous
        version as a false positive because only the endpoint-level UUID
        check was holding the line."""

        with tempfile.TemporaryDirectory() as parent_dir:
            parent = Path(parent_dir)
            root = parent / "root"
            root.mkdir()
            outside = parent / "outside"
            outside.mkdir()
            secret = _claude_assistant("outside-secret")
            _write_raw(outside / "raw.jsonl", [secret])

            root_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
            try:
                frames = screencast.tail_frames(root_fd, "..")
            finally:
                os.close(root_fd)

        # No frame from the outside file may leak in.
        self.assertEqual(frames, [])

    def test_pathwalk_helper_refuses_traversal_component(self) -> None:
        """Assert refusal at the helper level, not at the endpoint. If
        the helper accepts ``..``, the fixture file at ``../outside``
        gets returned — that was the R2 false-positive scenario."""

        from backend.app.pathwalk import open_relative_file

        with tempfile.TemporaryDirectory() as parent_dir:
            parent = Path(parent_dir)
            root = parent / "root"
            root.mkdir()
            outside = parent / "outside"
            outside.mkdir()
            outside_file = outside / "raw.jsonl"
            outside_file.write_text("nope\n", encoding="utf-8")

            root_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(OSError) as ctx:
                    open_relative_file(root_fd, ("..", "outside", "raw.jsonl"))
                # Must be a validation refusal (EINVAL), not "file not
                # found" — otherwise a real ``..outside`` file could
                # succeed and the helper is still unsafe.
                import errno as errno_mod

                self.assertEqual(ctx.exception.errno, errno_mod.EINVAL)

                # Single-component ``..`` and ``.`` both refused.
                for bad in ("..", ".", "", "a/b", "a\x00b", "a\\b"):
                    with self.assertRaises(OSError):
                        open_relative_file(root_fd, (bad,))
            finally:
                os.close(root_fd)


class OpenRootDirectoryTests(unittest.TestCase):
    def test_refuses_symlinked_root(self) -> None:
        """R1 finding #4: runs ROOT itself must not follow a symlink."""

        from backend.app.pathwalk import open_root_directory

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real-runs"
            real.mkdir()
            link = root / "runs-link"
            link.symlink_to(real)

            with self.assertRaises(OSError):
                open_root_directory(link)

    def test_opens_real_directory(self) -> None:
        from backend.app.pathwalk import open_root_directory

        with tempfile.TemporaryDirectory() as tmp:
            fd = open_root_directory(tmp)
            try:
                self.assertIsInstance(fd, int)
            finally:
                os.close(fd)

    def test_refuses_regular_file(self) -> None:
        from backend.app.pathwalk import open_root_directory

        with tempfile.TemporaryDirectory() as tmp:
            regular = Path(tmp) / "not-a-dir"
            regular.write_text("hi", encoding="utf-8")
            with self.assertRaises(OSError):
                open_root_directory(regular)


class FleetScreencastEndpointTests(unittest.TestCase):
    def _patch_runs_root(self, root: Path):
        return mock.patch.object(
            main.SUPERVISOR_CLIENT.paths.__class__,
            "runs_dir",
            new=property(lambda self: root),
        )

    def test_returns_empty_frames_for_ticket_without_registry_entry(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            with self._patch_runs_root(Path(raw_dir)), mock.patch.object(
                main, "_read_agent_registry", return_value={}
            ):
                payload = main._screencast_payload(["WIKI-176"])

        self.assertEqual(payload["workers"][0]["ticket"], "WIKI-176")
        self.assertIsNone(payload["workers"][0]["run_id"])
        self.assertEqual(payload["workers"][0]["frames"], [])

    def test_batches_multiple_tickets_and_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            run_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            run_dir = root / run_id
            run_dir.mkdir()
            _write_raw(run_dir / "raw.jsonl", [_claude_assistant("live tail")])
            registry = {
                "WIKI-1": {"current": {"run_id": run_id}},
                "WIKI-2": {"current": {"run_id": None}},
            }
            with self._patch_runs_root(root), mock.patch.object(
                main, "_read_agent_registry", return_value=registry
            ):
                payload = main._screencast_payload(["WIKI-1", "WIKI-1", "WIKI-2"])

        tickets_seen = [worker["ticket"] for worker in payload["workers"]]
        self.assertEqual(tickets_seen, ["WIKI-1", "WIKI-2"])
        self.assertEqual(payload["workers"][0]["run_id"], run_id)
        self.assertEqual(payload["workers"][0]["frames"][0]["text"], "live tail")
        self.assertEqual(payload["workers"][1]["frames"], [])

    def test_rejects_invalid_ticket_ids_without_reading_disk(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            with self._patch_runs_root(Path(raw_dir)), mock.patch.object(
                main, "_read_agent_registry", return_value={}
            ):
                payload = main._screencast_payload(["../etc/passwd"])
        self.assertIsNone(payload["workers"][0]["run_id"])

    def test_caps_batch_at_max_tickets(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            with self._patch_runs_root(Path(raw_dir)), mock.patch.object(
                main, "_read_agent_registry", return_value={}
            ):
                many = [f"WIKI-{n}" for n in range(main.MAX_SCREENCAST_TICKETS + 10)]
                payload = main._screencast_payload(many)
        self.assertEqual(len(payload["workers"]), main.MAX_SCREENCAST_TICKETS)


class ScreencastEtagTests(unittest.TestCase):
    def test_etag_excludes_updated_at_ns(self) -> None:
        """R1 finding #3: ETag must not churn — same tails → same ETag."""

        workers = [
            {
                "ticket": "WIKI-1",
                "run_id": "abc",
                "frames": [{"kind": "assistant", "text": "hello", "ts": None}],
            }
        ]
        first = main._screencast_etag(workers)
        second = main._screencast_etag(workers)
        self.assertEqual(first, second)

    def test_etag_changes_when_frames_change(self) -> None:
        workers = [
            {
                "ticket": "WIKI-1",
                "run_id": "abc",
                "frames": [{"kind": "assistant", "text": "hello", "ts": None}],
            }
        ]
        first = main._screencast_etag(workers)
        workers[0]["frames"].append({"kind": "assistant", "text": "world", "ts": None})
        self.assertNotEqual(first, main._screencast_etag(workers))

    def test_endpoint_returns_304_on_matching_if_none_match(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            with mock.patch.object(
                main.SUPERVISOR_CLIENT.paths.__class__,
                "runs_dir",
                new=property(lambda self: Path(raw_dir)),
            ), mock.patch.object(main, "_read_agent_registry", return_value={}):
                client = TestClient(main.app, base_url="http://127.0.0.1")
                first = client.get(
                    "/api/fleet/screencast",
                    params=[("ticket", "WIKI-176")],
                )
                self.assertEqual(first.status_code, 200)
                etag = first.headers.get("etag")
                self.assertIsNotNone(etag)

                second = client.get(
                    "/api/fleet/screencast",
                    params=[("ticket", "WIKI-176")],
                    headers={"If-None-Match": etag},
                )
                self.assertEqual(second.status_code, 304)
                self.assertEqual(second.content, b"")


if __name__ == "__main__":
    unittest.main()
