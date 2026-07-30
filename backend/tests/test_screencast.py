"""WIKI-176: bounded tail extractor + fleet screencast endpoint."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
    def test_tail_reads_only_end_window_of_large_file(self) -> None:
        """Prove the tail read is bounded: pad the head with megabytes of
        garbage and assert the extractor still returns the true tail frame.

        Files in production reach multi-GB. The tail read MUST NOT scan
        the head. This test proves that by making the head so large that
        parsing it would either fail (invalid JSON garbage) or wildly
        exceed the max-frames cap — either way, if the reader read past
        the window, the output would not equal the expected tail.
        """

        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            run_dir = root / "abc"
            run_dir.mkdir()
            raw_path = run_dir / "raw.jsonl"

            garbage_line = b"x" * (screencast.DEFAULT_WINDOW_BYTES * 4) + b"\n"
            tail_env = _claude_assistant("tail-marker")
            with raw_path.open("wb") as fp:
                fp.write(garbage_line)
                fp.write(garbage_line)
                fp.write(json.dumps(tail_env).encode("utf-8") + b"\n")

            root_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
            try:
                frames = screencast.tail_frames(root_fd, "abc")
            finally:
                os.close(root_fd)

        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].text, "tail-marker")

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
            # Create the real run outside the runs-root anchor.
            outside = root / "outside"
            outside.mkdir()
            raw_path = outside / "raw.jsonl"
            _write_raw(raw_path, [_claude_assistant("secret")])
            # Symlink pretends to be a legitimate run dir under the anchor.
            link = root / "fake-run"
            link.symlink_to(outside)

            root_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
            try:
                frames = screencast.tail_frames(root_fd, "fake-run")
            finally:
                os.close(root_fd)

        self.assertEqual(frames, [])


class FleetScreencastEndpointTests(unittest.TestCase):
    def test_returns_empty_frames_for_ticket_without_registry_entry(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            with mock.patch.object(
                main.SUPERVISOR_CLIENT.paths.__class__,
                "runs_dir",
                new=property(lambda self: Path(raw_dir)),
            ), mock.patch.object(main, "_read_agent_registry", return_value={}):
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
            with mock.patch.object(
                main.SUPERVISOR_CLIENT.paths.__class__,
                "runs_dir",
                new=property(lambda self: root),
            ), mock.patch.object(main, "_read_agent_registry", return_value=registry):
                payload = main._screencast_payload(["WIKI-1", "WIKI-1", "WIKI-2"])

        tickets_seen = [worker["ticket"] for worker in payload["workers"]]
        self.assertEqual(tickets_seen, ["WIKI-1", "WIKI-2"])
        self.assertEqual(payload["workers"][0]["run_id"], run_id)
        self.assertEqual(payload["workers"][0]["frames"][0]["text"], "live tail")
        self.assertEqual(payload["workers"][1]["frames"], [])

    def test_rejects_invalid_ticket_ids_without_reading_disk(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            with mock.patch.object(
                main.SUPERVISOR_CLIENT.paths.__class__,
                "runs_dir",
                new=property(lambda self: Path(raw_dir)),
            ), mock.patch.object(main, "_read_agent_registry", return_value={}):
                payload = main._screencast_payload(["../etc/passwd"])
        self.assertIsNone(payload["workers"][0]["run_id"])

    def test_caps_batch_at_max_tickets(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            with mock.patch.object(
                main.SUPERVISOR_CLIENT.paths.__class__,
                "runs_dir",
                new=property(lambda self: Path(raw_dir)),
            ), mock.patch.object(main, "_read_agent_registry", return_value={}):
                many = [f"WIKI-{n}" for n in range(main.MAX_SCREENCAST_TICKETS + 10)]
                payload = main._screencast_payload(many)
        self.assertEqual(len(payload["workers"]), main.MAX_SCREENCAST_TICKETS)


if __name__ == "__main__":
    unittest.main()
