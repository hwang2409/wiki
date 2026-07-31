from __future__ import annotations

import base64
import asyncio
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from backend.app import main, media_scrub, wiki_agent_tools, wiki_artifacts
from backend.app.agent_runtime import next_review as next_review_runtime
from backend.app.agent_runtime.autopilot import AutopilotController, AutopilotStore
from backend.app.agent_runtime.diversity_orchestration import collect_diversity_verdict
from backend.app.next_review_schema import NextReviewIn, mcp_input_schema


RUN_ID = "00000000-0000-4000-8000-000000000085"


def _fixture_png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color=(255, 128, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


FIXTURE_PNG_BYTES = _fixture_png_bytes()


from backend.tests.test_media_scrub import REAL_MP4, REAL_WAV

FIXTURE_MP4_BYTES = REAL_MP4.read_bytes()
FIXTURE_WAV_BYTES = REAL_WAV.read_bytes()


def _payload(kind: str) -> dict:
    return {
        "mermaid": {"source": "graph TD; A-->B"},
        "svg": {"source": '<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'},
        "image": {
            "data_base64": base64.b64encode(FIXTURE_PNG_BYTES).decode(),
            "mime": "image/png",
        },
        "table": {
            "columns": [
                {"key": "id", "label": "ID", "type": "number"},
                {"key": "name", "label": "Name", "type": "string"},
            ],
            "rows": [[1, "one"], [2, "two"]],
        },
        "plot": {
            "spec_vega_lite": {
                "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                "mark": "bar",
                "data": {"values": [{"x": "A", "y": 1}]},
                "encoding": {"x": {"field": "x"}, "y": {"field": "y"}},
            }
        },
        "code": {
            "language": "python",
            "filename": "hello.py",
            "source": "print('new')\n",
            "diff_from": "print('old')\n",
        },
        "diff": {
            "source": (
                "diff --git a/hello.py b/hello.py\n"
                "--- a/hello.py\n"
                "+++ b/hello.py\n"
                "@@ -1 +1 @@\n"
                "-print('old')\n"
                "+print('new')\n"
            ),
        },
        "file-list": {
            "files": [
                {"path": "hello.py", "label": "hello.py", "status": "modified"},
                {"path": "goodbye.py", "status": "added", "size": 42},
            ],
        },
        "json": {
            "json_data": {
                "ticket": "WIKI-143",
                "counts": {"added": 2, "modified": 1},
                "flags": [True, False, True],
            },
        },
        "pdf": {
            "data_base64": base64.b64encode(b"%PDF-1.4\n%fixture bytes\n").decode(),
        },
        "video": {
            "data_base64": base64.b64encode(FIXTURE_MP4_BYTES).decode(),
            "mime": "video/mp4",
        },
        "audio": {
            "data_base64": base64.b64encode(FIXTURE_WAV_BYTES).decode(),
            "mime": "audio/wav",
        },
        "visual-diff": {
            "before": {
                "data_base64": base64.b64encode(FIXTURE_PNG_BYTES).decode(),
                "mime": "image/png",
            },
            "after": {
                "data_base64": base64.b64encode(FIXTURE_PNG_BYTES).decode(),
                "mime": "image/png",
            },
        },
    }[kind]


class WikiArtifactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(
            os.environ,
            {
                "WIKI_AGENT_RUNTIME_DIR": str(self.root / "runtime"),
                "WIKI_RUN_ID": RUN_ID,
                "WIKI_AGENT_ROLE": "worker",
                "WIKI_AGENT_ID": "test-worker",
            },
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_each_kind_is_valid_and_normalized(self) -> None:
        for kind in sorted(wiki_artifacts.ARTIFACT_KINDS):
            with self.subTest(kind=kind):
                event = wiki_artifacts.render_artifact(
                    {
                        "kind": kind,
                        "title": f"Fixture {kind}",
                        "caption": "Artifact fixture",
                        "payload": _payload(kind),
                    }
                )
                self.assertEqual(event["kind"], "artifact")
                self.assertEqual(event["artifact"]["kind"], kind)
                self.assertEqual(
                    wiki_artifacts.artifact_from_text(wiki_artifacts.sentinel_text(event)),
                    event,
                )
                if kind == "image":
                    self.assertNotIn("data_base64", event["artifact"])
                    image = (
                        self.root
                        / "runtime"
                        / "runs"
                        / RUN_ID
                        / "artifacts"
                        / f"{event['id']}.png"
                    )
                    stored = image.read_bytes()
                    self.assertTrue(stored.startswith(b"\x89PNG\r\n\x1a\n"))
                    with Image.open(io.BytesIO(stored)) as reopened:
                        reopened.load()
                        self.assertEqual(reopened.size, (2, 2))
                    self.assertEqual(image.stat().st_mode & 0o777, 0o600)
                elif kind == "pdf":
                    self.assertNotIn("data_base64", event["artifact"])
                    self.assertNotIn("path", event["artifact"])
                    self.assertEqual(event["artifact"]["mime"], wiki_artifacts.PDF_MIME)
                    pdf = (
                        self.root
                        / "runtime"
                        / "runs"
                        / RUN_ID
                        / "artifacts"
                        / f"{event['id']}.pdf"
                    )
                    self.assertTrue(pdf.read_bytes().startswith(b"%PDF-"))
                    self.assertEqual(pdf.stat().st_mode & 0o777, 0o600)
                elif kind == "video":
                    self.assertNotIn("data_base64", event["artifact"])
                    self.assertEqual(event["artifact"]["mime"], "video/mp4")
                    # Real fixture: 160x120, ~0.5s runtime.
                    self.assertEqual(event["artifact"]["width"], 160)
                    self.assertEqual(event["artifact"]["height"], 120)
                    self.assertEqual(event["artifact"]["duration_ms"], 533)
                    video = (
                        self.root
                        / "runtime"
                        / "runs"
                        / RUN_ID
                        / "artifacts"
                        / f"{event['id']}.mp4"
                    )
                    self.assertTrue(video.read_bytes()[4:8] == b"ftyp")
                    self.assertEqual(video.stat().st_mode & 0o777, 0o600)
                elif kind == "audio":
                    self.assertNotIn("data_base64", event["artifact"])
                    self.assertEqual(event["artifact"]["mime"], "audio/wav")
                    audio = (
                        self.root
                        / "runtime"
                        / "runs"
                        / RUN_ID
                        / "artifacts"
                        / f"{event['id']}.wav"
                    )
                    self.assertTrue(audio.read_bytes().startswith(b"RIFF"))
                    self.assertEqual(audio.stat().st_mode & 0o777, 0o600)
                elif kind == "visual-diff":
                    self.assertEqual(event["artifact"]["before"]["width"], 2)
                    self.assertEqual(event["artifact"]["after"]["height"], 2)
                else:
                    for key, value in _payload(kind).items():
                        self.assertEqual(event["artifact"][key], value)

    def test_malformed_payloads_are_rejected_per_kind(self) -> None:
        malformed = {
            "mermaid": {},
            "svg": {"source": "<script>alert(1)</script>"},
            "image": {"data_base64": "not-base64", "mime": "image/png"},
            "table": {
                "columns": [{"key": "id", "label": "ID", "type": "number"}],
                "rows": [[1, 2]],
            },
            "plot": {"spec_vega_lite": []},
            "code": {"language": 1, "source": "x"},
            "diff": {},
            "file-list": {"files": [{"label": "missing path"}]},
            "json": {},
            "pdf": {"data_base64": base64.b64encode(b"not a pdf").decode()},
            "video": {
                "data_base64": base64.b64encode(b"not a video").decode(),
                "mime": "video/mp4",
            },
            "audio": {
                "data_base64": base64.b64encode(b"not audio").decode(),
                "mime": "audio/wav",
            },
        }
        for kind, payload in malformed.items():
            with self.subTest(kind=kind), self.assertRaises(
                wiki_artifacts.ArtifactValidationError
            ):
                wiki_artifacts.render_artifact({"kind": kind, "payload": payload})

    def test_text_and_image_caps_are_enforced(self) -> None:
        oversized_text = "x" * (wiki_artifacts.TEXT_LIMIT + 1)
        text_payloads = {
            "mermaid": {"source": oversized_text},
            "svg": {"source": f"<svg>{oversized_text}</svg>"},
            "table": {
                "columns": [{"key": "x", "label": "X", "type": "string"}],
                "rows": [[oversized_text]],
            },
            "plot": {"spec_vega_lite": {"description": oversized_text}},
            "code": {"language": "text", "source": oversized_text},
            "diff": {"source": oversized_text},
            "file-list": {
                "files": [{"path": oversized_text, "label": oversized_text}],
            },
            "json": {"json_data": {"body": oversized_text}},
        }
        for kind, payload in text_payloads.items():
            with self.subTest(kind=kind), self.assertRaisesRegex(
                wiki_artifacts.ArtifactValidationError, "100KB text limit"
            ):
                wiki_artifacts.render_artifact({"kind": kind, "payload": payload})

        image = base64.b64encode(b"x" * (wiki_artifacts.IMAGE_LIMIT + 1)).decode()
        with self.assertRaisesRegex(
            wiki_artifacts.ArtifactValidationError, "5MB image limit"
        ):
            wiki_artifacts.render_artifact(
                {
                    "kind": "image",
                    "payload": {"data_base64": image, "mime": "image/png"},
                }
            )

        video = base64.b64encode(b"x" * (wiki_artifacts.VIDEO_LIMIT + 1)).decode()
        with self.assertRaisesRegex(
            wiki_artifacts.ArtifactValidationError, "40MB video limit"
        ):
            wiki_artifacts.render_artifact(
                {
                    "kind": "video",
                    "payload": {"data_base64": video, "mime": "video/mp4"},
                }
            )

        audio = base64.b64encode(b"x" * (wiki_artifacts.AUDIO_LIMIT + 1)).decode()
        with self.assertRaisesRegex(
            wiki_artifacts.ArtifactValidationError, "20MB audio limit"
        ):
            wiki_artifacts.render_artifact(
                {
                    "kind": "audio",
                    "payload": {"data_base64": audio, "mime": "audio/wav"},
                }
            )

    def test_video_rejects_wrong_mime(self) -> None:
        payload = {
            "data_base64": base64.b64encode(FIXTURE_MP4_BYTES).decode(),
            "mime": "application/octet-stream",
        }
        with self.assertRaisesRegex(
            wiki_artifacts.ArtifactValidationError, "payload.mime must be one of"
        ):
            wiki_artifacts.render_artifact({"kind": "video", "payload": payload})

    def test_audio_transcript_length_is_capped(self) -> None:
        payload = {
            "data_base64": base64.b64encode(FIXTURE_WAV_BYTES).decode(),
            "mime": "audio/wav",
            "transcript": "x" * (wiki_artifacts.TEXT_LIMIT + 1),
        }
        with self.assertRaisesRegex(
            wiki_artifacts.ArtifactValidationError, "audio transcript exceeds"
        ):
            wiki_artifacts.render_artifact({"kind": "audio", "payload": payload})

    def test_audio_multibyte_transcript_length_is_capped_by_utf8_bytes(self) -> None:
        payload = {
            "data_base64": base64.b64encode(FIXTURE_WAV_BYTES).decode(),
            "mime": "audio/wav",
            "transcript": "𐍈" * (wiki_artifacts.TEXT_LIMIT // 4 + 1),
        }
        with self.assertRaisesRegex(
            wiki_artifacts.ArtifactValidationError, "audio transcript exceeds"
        ):
            wiki_artifacts.render_artifact({"kind": "audio", "payload": payload})

    def test_audio_rejected_transcript_does_not_orphan_media_file(self) -> None:
        # Round-2 review flagged: an oversized transcript fires AFTER
        # _write_binary, leaving a scrubbed .wav resident on disk while
        # the caller sees a failure. Validation must run before the write.
        payload = {
            "data_base64": base64.b64encode(FIXTURE_WAV_BYTES).decode(),
            "mime": "audio/wav",
            "transcript": "x" * (wiki_artifacts.TEXT_LIMIT + 1),
        }
        artifact_dir = self.root / "runtime" / "runs" / RUN_ID / "artifacts"
        with self.assertRaises(wiki_artifacts.ArtifactValidationError):
            wiki_artifacts.render_artifact({"kind": "audio", "payload": payload})
        # No .wav should exist in the artifact dir.
        if artifact_dir.exists():
            leftover = list(artifact_dir.glob("*.wav"))
            self.assertEqual(
                leftover, [], msg=f"orphaned media files: {leftover}",
            )

    def test_audio_non_string_transcript_does_not_orphan_media_file(self) -> None:
        payload = {
            "data_base64": base64.b64encode(FIXTURE_WAV_BYTES).decode(),
            "mime": "audio/wav",
            "transcript": ["not", "a", "string"],
        }
        artifact_dir = self.root / "runtime" / "runs" / RUN_ID / "artifacts"
        with self.assertRaisesRegex(
            wiki_artifacts.ArtifactValidationError, "transcript must be a string"
        ):
            wiki_artifacts.render_artifact({"kind": "audio", "payload": payload})
        if artifact_dir.exists():
            self.assertEqual(list(artifact_dir.glob("*.wav")), [])

    def test_video_over_limit_duration_does_not_orphan_media_file(self) -> None:
        payload = bytearray(FIXTURE_MP4_BYTES)
        mvhd_pos = payload.find(b"mvhd")
        self.assertGreater(mvhd_pos, 0)
        # v0 mvhd: version+flags, creation, modification, timescale, duration.
        payload[mvhd_pos + 20:mvhd_pos + 24] = struct.pack(">I", 0xFFFFFFFF)
        artifact_dir = self.root / "runtime" / "runs" / RUN_ID / "artifacts"
        with self.assertRaisesRegex(
            wiki_artifacts.ArtifactValidationError,
            "duration_ms is out of bounds|mvhd and tkhd durations differ",
        ):
            wiki_artifacts.render_artifact(
                {
                    "kind": "video",
                    "payload": {
                        "data_base64": base64.b64encode(payload).decode(),
                        "mime": "video/mp4",
                    },
                }
            )
        if artifact_dir.exists():
            self.assertEqual(list(artifact_dir.iterdir()), [])

    def test_audio_over_limit_duration_does_not_orphan_media_file(self) -> None:
        scrubbed = media_scrub.MediaScrubResult(
            data=FIXTURE_WAV_BYTES,
            mime="audio/wav",
            width=None,
            height=None,
            duration_ms=wiki_artifacts._BINARY_ARTIFACT_MAX_DURATION_MS + 1,
            peaks=[1],
        )
        artifact_dir = self.root / "runtime" / "runs" / RUN_ID / "artifacts"
        with mock.patch.object(wiki_artifacts, "scrub_audio", return_value=scrubbed):
            with self.assertRaisesRegex(
                wiki_artifacts.ArtifactValidationError, "duration_ms is out of bounds"
            ):
                wiki_artifacts.render_artifact(
                    {
                        "kind": "audio",
                        "payload": {
                            "data_base64": base64.b64encode(FIXTURE_WAV_BYTES).decode(),
                            "mime": "audio/wav",
                        },
                    }
                )
        if artifact_dir.exists():
            self.assertEqual(list(artifact_dir.iterdir()), [])

    def test_video_accepts_path_payload_alongside_data_base64(self) -> None:
        # Reject "both" and "neither".
        with self.assertRaisesRegex(
            wiki_artifacts.ArtifactValidationError, "exactly one"
        ):
            wiki_artifacts.render_artifact(
                {"kind": "video", "payload": {"mime": "video/mp4"}}
            )
        with self.assertRaisesRegex(
            wiki_artifacts.ArtifactValidationError, "exactly one"
        ):
            wiki_artifacts.render_artifact(
                {
                    "kind": "video",
                    "payload": {
                        "mime": "video/mp4",
                        "data_base64": base64.b64encode(FIXTURE_MP4_BYTES).decode(),
                        "path": str(self.root),
                    },
                }
            )
        # Accept a path inside the allowed roots.
        runtime_root = self.root / "runtime"
        runtime_root.mkdir(exist_ok=True)
        mp4_path = runtime_root / "path-fixture.mp4"
        mp4_path.write_bytes(FIXTURE_MP4_BYTES)
        event = wiki_artifacts.render_artifact(
            {
                "kind": "video",
                "payload": {"mime": "video/mp4", "path": str(mp4_path)},
            }
        )
        self.assertEqual(event["artifact"]["mime"], "video/mp4")

    def test_audio_normalizes_bounded_peaks_array(self) -> None:
        payload = {
            "data_base64": base64.b64encode(FIXTURE_WAV_BYTES).decode(),
            "mime": "audio/wav",
        }
        event = wiki_artifacts.render_artifact({"kind": "audio", "payload": payload})
        peaks = event["artifact"].get("peaks")
        self.assertIsInstance(peaks, list)
        self.assertLessEqual(len(peaks), 512)
        self.assertTrue(any(peak > 0 for peak in peaks))

    def test_video_accepts_scrubbed_poster_frame(self) -> None:
        import io as pil_io
        buffer = pil_io.BytesIO()
        Image.new("RGB", (160, 120), color=(10, 20, 30)).save(buffer, format="PNG")
        poster_bytes = buffer.getvalue()
        payload = {
            "data_base64": base64.b64encode(FIXTURE_MP4_BYTES).decode(),
            "mime": "video/mp4",
            "poster_base64": base64.b64encode(poster_bytes).decode(),
            "poster_mime": "image/png",
        }
        event = wiki_artifacts.render_artifact({"kind": "video", "payload": payload})
        self.assertIn("poster_base64", event["artifact"])
        poster_url = event["artifact"]["poster_base64"]
        self.assertTrue(poster_url.startswith("data:image/"))
        _header, _separator, encoded = poster_url.partition(",")
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as poster:
            poster.load()
            self.assertEqual(poster.size, (160, 120))

    def test_transport_cap_covers_video_with_poster(self) -> None:
        request = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "render_artifact",
                    "arguments": {
                        "kind": "video",
                        "payload": {
                            "mime": "video/mp4",
                            "data_base64": "",
                            "poster_base64": "",
                            "poster_mime": "image/png",
                        },
                    },
                },
            },
            separators=(",", ":"),
        ).encode()
        video_encoded_limit = ((wiki_artifacts.VIDEO_LIMIT + 2) // 3) * 4 + 4
        poster_encoded_limit = ((wiki_artifacts.IMAGE_LIMIT + 2) // 3) * 4 + 4
        required = len(request) + video_encoded_limit + poster_encoded_limit + 1
        self.assertGreaterEqual(wiki_artifacts.MAX_REQUEST_BYTES, required)

    def test_video_rejects_ogg_and_webm(self) -> None:
        for mime in ("video/webm", "audio/ogg", "audio/webm"):
            with self.subTest(mime=mime):
                payload = {
                    "data_base64": base64.b64encode(b"\x00" * 32).decode(),
                    "mime": mime,
                }
                kind = "audio" if mime.startswith("audio/") else "video"
                with self.assertRaises(wiki_artifacts.ArtifactValidationError):
                    wiki_artifacts.render_artifact({"kind": kind, "payload": payload})

    def test_audio_transcript_flows_through(self) -> None:
        payload = {
            "data_base64": base64.b64encode(FIXTURE_WAV_BYTES).decode(),
            "mime": "audio/wav",
            "transcript": "hello from the fixture",
        }
        event = wiki_artifacts.render_artifact(
            {"kind": "audio", "payload": payload}
        )
        self.assertEqual(event["artifact"]["transcript"], "hello from the fixture")

    def test_sentinel_parser_revalidates_text_payload_caps(self) -> None:
        event = {
            "kind": "artifact",
            "id": RUN_ID,
            "artifact": {
                "kind": "mermaid",
                "source": "x" * (wiki_artifacts.TEXT_LIMIT + 1),
            },
        }

        self.assertIsNone(wiki_artifacts.artifact_from_text(wiki_artifacts.sentinel_text(event)))

    def test_sentinel_parser_revalidates_binary_artifact_fields(self) -> None:
        artifact_id = RUN_ID
        preview = "data:image/png;base64," + base64.b64encode(FIXTURE_PNG_BYTES).decode()

        def event(kind: str, **fields: object) -> dict:
            artifact = {
                "kind": kind,
                "ref": f"artifact://{artifact_id}",
                "mime": "audio/wav" if kind == "audio" else "video/mp4",
                **fields,
            }
            return {"kind": "artifact", "id": artifact_id, "artifact": artifact}

        invalid_events = (
            event("audio", peaks="not-an-array"),
            event("audio", peaks=[256]),
            event("audio", peaks=[0] * 513),
            event("audio", transcript="é" * (wiki_artifacts.TEXT_LIMIT // 2 + 1)),
            event("video", mime="text/html"),
            event("video", poster_base64="https://attacker.invalid/poster.png"),
            event("video", width=True),
            event("audio", ref="artifact://00000000-0000-4000-8000-000000000086"),
            event("audio", unexpected="marker"),
        )
        for invalid in invalid_events:
            with self.subTest(artifact=invalid["artifact"]):
                self.assertIsNone(
                    wiki_artifacts.artifact_from_text(
                        wiki_artifacts.sentinel_text(invalid)
                    )
                )

        valid = event("video", poster_base64=preview, width=2, height=2)
        self.assertIsNotNone(
            wiki_artifacts.artifact_from_text(wiki_artifacts.sentinel_text(valid))
        )

    def test_sentinel_parser_rejects_wrong_typed_table_column_types(self) -> None:
        for column_type in ([], {}, None, 42):
            with self.subTest(column_type=column_type):
                event = {
                    "kind": "artifact",
                    "id": RUN_ID,
                    "artifact": {
                        "kind": "table",
                        "columns": [
                            {"key": "id", "label": "ID", "type": column_type}
                        ],
                        "rows": [[1]],
                    },
                }

                self.assertIsNone(
                    wiki_artifacts.artifact_from_text(
                        wiki_artifacts.sentinel_text(event)
                    )
                )

    def test_stdio_server_lists_tool_and_returns_sentinel_result(self) -> None:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "render_artifact",
                    "arguments": {"kind": "mermaid", "payload": _payload("mermaid")},
                },
            },
        ]
        env = os.environ.copy()
        process = subprocess.run(
            [sys.executable, "-m", "backend.app.wiki_artifacts"],
            input="".join(json.dumps(request) + "\n" for request in requests),
            text=True,
            capture_output=True,
            env=env,
            timeout=5,
            check=True,
        )
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(responses[1]["result"]["tools"][0]["name"], "render_artifact")
        result = responses[2]["result"]
        event = wiki_artifacts.artifact_from_text(result["content"][0]["text"])
        self.assertIsNotNone(event)
        self.assertEqual(result["structuredContent"]["ok"], True)
        self.assertEqual(
            result["structuredContent"]["artifact"],
            event["artifact"],
        )
        self.assertEqual(event["artifact"]["kind"], "mermaid")

    def test_orchestrator_lists_native_ops_but_worker_does_not(self) -> None:
        worker = wiki_artifacts._response(  # noqa: SLF001 - MCP contract test
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        assert worker is not None
        worker_names = {tool["name"] for tool in worker["result"]["tools"]}
        self.assertEqual(worker_names, {"render_artifact", "search_knowledge"})

        with mock.patch.dict(os.environ, {"WIKI_AGENT_ROLE": "orchestrator"}):
            orchestrator = wiki_artifacts._response(  # noqa: SLF001
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            )
        assert orchestrator is not None
        names = {tool["name"] for tool in orchestrator["result"]["tools"]}
        self.assertTrue(
            {
                "spawn_agent",
                "steer_agent",
                "replace_agent",
                "archive_agent",
                "list_agents",
                "read_agent",
                "read_agent_events",
                "read_agent_pr",
                "next_review",
            }.issubset(names)
        )

    def test_next_review_uses_runtime_orchestrator_grouping(self) -> None:
        calls: list[tuple[str, str, dict | None]] = []

        def backend(method: str, path: str, payload: dict | None = None) -> dict:
            calls.append((method, path, payload))
            return {"status": "spawned", "run_id": "review-run"}

        with (
            mock.patch.dict(
                os.environ,
                {"WIKI_AGENT_ROLE": "orchestrator", "WIKI_AGENT_ID": "wiki"},
            ),
            mock.patch.object(wiki_agent_tools, "_backend_api", side_effect=backend),
        ):
            result = wiki_agent_tools.next_review(
                {
                    "ticket": "WIKI-171",
                    "pr_number": 171,
                    "expected_sha": "a" * 40,
                    "request_id": "mcp-next-review-1",
                    "diversity": ["correctness", "security"],
                }
            )

        self.assertEqual(result["run_id"], "review-run")
        self.assertEqual(calls[0][0:2], ("POST", "/api/agents/next-review"))
        assert calls[0][2] is not None
        self.assertEqual(calls[0][2]["orch"], "wiki")
        self.assertEqual(calls[0][2]["request_id"], "mcp-next-review-1")
        self.assertEqual(calls[0][2]["diversity"], ["correctness", "security"])

    def test_next_review_canonical_handler_routes_combined_verdict(self) -> None:
        runtime_dir = self.root / "runtime"
        status_dir = self.root / "status"
        status_dir.mkdir()
        sha = "b" * 40
        spawned: list[str] = []
        recorded: list[dict] = []
        merged: list[str] = []

        def backend(method: str, path: str, payload: dict | None = None) -> dict:
            self.assertEqual((method, path), ("POST", "/api/agents/next-review"))
            assert payload is not None
            return next_review_runtime.next_review(
                **payload,
                gate=lambda pr, _sha: {"verdict": "pass", "pr": pr},
                resolve_root=lambda _orch: Path("/repo"),
                worktree=lambda **kwargs: Path(f"/repo/{kwargs['lens']}"),
                spawn=lambda request: spawned.append(request.ticket) or {"run_id": request.ticket},
                archived=lambda: [],
                registry=lambda: {},
            )

        graph = {
            "ticket": "WIKI-181",
            "orch": "wiki",
            "iteration_cap": 8,
            "nodes": [],
            "edges": [
                {"kind": "spawn", "to": "WIKI-181", "payload": {"role": "implement"}},
                *[
                    {"kind": "spawn", "to": f"WIKI-181-REVIEW1-{lens}", "payload": {"role": "review"}}
                    for lens in ("correctness", "security")
                ],
                *[
                    {
                        "kind": "verdict",
                        "from": f"WIKI-181-REVIEW1-{lens}",
                        "payload": {
                            "worker": f"WIKI-181-REVIEW1-{lens}",
                            "state": "MERGE-READY",
                            "sha": sha,
                            "findings": [],
                        },
                    }
                    for lens in ("correctness", "security")
                ],
            ],
        }
        with (
            mock.patch.dict(
                os.environ,
                {"WIKI_AGENT_ROLE": "orchestrator", "WIKI_AGENT_ID": "wiki"},
            ),
            mock.patch.object(wiki_agent_tools, "_backend_api", side_effect=backend),
            mock.patch.object(main, "AGENT_RUNTIME_DIR", runtime_dir),
            mock.patch.object(main, "AGENT_STATUS_DIR", status_dir),
        ):
            result = wiki_agent_tools.next_review(
                {
                    "ticket": "WIKI-181",
                    "pr_number": 181,
                    "expected_sha": sha,
                    "request_id": "canonical-diversity-e2e",
                    "diversity": ["correctness", "security"],
                }
            )
            self.assertEqual(result["status"], "spawned")

            controller = AutopilotController(
                store=AutopilotStore(self.root / "autopilot"),
                status_reader=lambda _ticket: {"pr": "https://github.com/hwang2409/wiki/pull/181", "sha": sha},
                registry_reader=lambda: {"WIKI-181": {"current": {"orch": "wiki"}}},
                graph_loader=lambda _ticket: graph,
                collect_diversity=lambda **kwargs: collect_diversity_verdict(
                    runtime_dir=runtime_dir,
                    record_verdict=lambda **record: recorded.append(record),
                    **kwargs,
                ),
                gate=lambda pr, _sha: {"verdict": "pass", "pr": pr},
                merge=lambda ticket, _sha: merged.append(ticket),
            )
            controller.enable("WIKI-181")
            asyncio.run(
                controller.on_transition(
                    {"agent_id": "WIKI-181-REVIEW1-correctness", "run_id": "r1", "status_state": "merge-ready"}
                )
            )
            asyncio.run(
                controller.on_transition(
                    {"agent_id": "WIKI-181-REVIEW1-security", "run_id": "r2", "status_state": "merge-ready"}
                )
            )

        self.assertEqual(set(spawned), {"WIKI-181-REVIEW1-correctness", "WIKI-181-REVIEW1-security"})
        self.assertEqual(merged, ["https://github.com/hwang2409/wiki/pull/181"])
        self.assertEqual(recorded[-1]["reviewer"], "WIKI-181-REVIEW1-synthesis")
        self.assertEqual(recorded[-1]["payload"]["state"], "MERGE-READY")

    def test_next_review_canonical_handler_dirty_path_steers_and_archives_lenses(self) -> None:
        runtime_dir = self.root / "runtime"
        status_dir = self.root / "status"
        status_dir.mkdir()
        sha = "c" * 40
        spawned: list[str] = []
        steers: list[str] = []
        archived: list[str] = []

        def backend(method: str, path: str, payload: dict | None = None) -> dict:
            self.assertEqual((method, path), ("POST", "/api/agents/next-review"))
            assert payload is not None
            return next_review_runtime.next_review(
                **payload,
                gate=lambda _pr, _sha: {"verdict": "pass"},
                resolve_root=lambda _orch: Path("/repo"),
                worktree=lambda **kwargs: Path(f"/repo/{kwargs['lens']}"),
                spawn=lambda request: spawned.append(request.ticket) or {"run_id": request.ticket},
                archived=lambda: [],
                registry=lambda: {},
            )

        graph = {
            "ticket": "WIKI-181",
            "orch": "wiki",
            "iteration_cap": 8,
            "nodes": [],
            "edges": [
                {"kind": "spawn", "to": "WIKI-181", "payload": {"role": "implement"}},
                *[
                    {"kind": "spawn", "to": f"WIKI-181-REVIEW1-{lens}", "payload": {"role": "review"}}
                    for lens in ("correctness", "security")
                ],
                {
                    "kind": "verdict",
                    "from": "WIKI-181-REVIEW1-correctness",
                    "payload": {
                        "worker": "WIKI-181-REVIEW1-correctness",
                        "state": "NOT-MERGE-READY",
                        "sha": sha,
                        "findings": [{
                            "id": "F-dirty1",
                            "severity": "HIGH",
                            "file": "backend/app/main.py",
                            "line": 10,
                            "line_end": 12,
                            "problem": "unsafe input reaches the merge path",
                            "fix": "validate input before merge",
                        }],
                    },
                },
                {
                    "kind": "verdict",
                    "from": "WIKI-181-REVIEW1-security",
                    "payload": {
                        "worker": "WIKI-181-REVIEW1-security",
                        "state": "MERGE-READY",
                        "sha": sha,
                        "findings": [],
                    },
                },
            ],
        }
        with (
            mock.patch.dict(
                os.environ,
                {"WIKI_AGENT_ROLE": "orchestrator", "WIKI_AGENT_ID": "wiki"},
            ),
            mock.patch.object(wiki_agent_tools, "_backend_api", side_effect=backend),
            mock.patch.object(main, "AGENT_RUNTIME_DIR", runtime_dir),
            mock.patch.object(main, "AGENT_STATUS_DIR", status_dir),
        ):
            result = wiki_agent_tools.next_review(
                {
                    "ticket": "WIKI-181",
                    "pr_number": 181,
                    "expected_sha": sha,
                    "request_id": "canonical-diversity-dirty-e2e",
                    "diversity": ["correctness", "security"],
                }
            )
            self.assertEqual(result["status"], "spawned")
            controller = AutopilotController(
                store=AutopilotStore(self.root / "autopilot"),
                status_reader=lambda _ticket: {"pr": "https://github.com/hwang2409/wiki/pull/181", "sha": sha},
                registry_reader=lambda: {"WIKI-181": {"current": {"orch": "wiki"}}},
                graph_loader=lambda _ticket: graph,
                collect_diversity=lambda **kwargs: collect_diversity_verdict(
                    runtime_dir=runtime_dir,
                    record_verdict=lambda **_record: None,
                    **kwargs,
                ),
                steer=lambda _ticket, message: steers.append(message),
                archive=lambda reviewer: archived.append(reviewer),
            )
            controller.enable("WIKI-181")
            first = asyncio.run(
                controller.on_transition(
                    {"agent_id": "WIKI-181-REVIEW1-correctness", "run_id": "r1", "status_state": "merge-ready"}
                )
            )
            second = asyncio.run(
                controller.on_transition(
                    {"agent_id": "WIKI-181-REVIEW1-security", "run_id": "r2", "status_state": "merge-ready"}
                )
            )
            retry = asyncio.run(
                controller.on_transition(
                    {"agent_id": "WIKI-181-REVIEW1-security", "run_id": "r3", "status_state": "merge-ready"}
                )
            )

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertTrue(retry)
        self.assertEqual(set(spawned), {"WIKI-181-REVIEW1-correctness", "WIKI-181-REVIEW1-security"})
        self.assertEqual(archived, ["WIKI-181-REVIEW1-correctness", "WIKI-181-REVIEW1-security"])
        self.assertEqual(len(steers), 1)
        self.assertIn("source lenses: correctness", steers[0])
        self.assertIn("backend/app/main.py:10-12", steers[0])

    def test_next_review_mcp_constraints_match_endpoint_model(self) -> None:
        endpoint = NextReviewIn.model_json_schema()["properties"]
        mcp = mcp_input_schema()["properties"]
        for field in ("ticket", "expected_sha", "reviewer_model"):
            self.assertEqual(mcp[field], endpoint[field])
        self.assertNotIn("orch", mcp)

    def test_orchestrator_operations_route_through_the_live_backend(self) -> None:
        calls: list[tuple[str, str, dict | None]] = []

        def backend(method: str, path: str, payload: dict | None = None) -> dict:
            calls.append((method, path, payload))
            return {"ok": True, "path": path}

        with (
            mock.patch.dict(os.environ, {"WIKI_AGENT_ROLE": "orchestrator"}),
            mock.patch.object(wiki_agent_tools, "_backend_api", side_effect=backend),
        ):
            spawned = wiki_agent_tools.spawn_agent(
                {
                    "ticket": "WIKI-200",
                    "kind": "cdx",
                    "role": "implement",
                    "model": "gpt-5.4",
                    "effort": "high",
                    "workdir": "/tmp/worktree",
                    "prompt": "Implement ticket WIKI-200",
                    "orch": "wiki",
                    "request_id": "mcp-spawn-1",
                }
            )
            wiki_agent_tools.steer_agent(
                {
                    "id": "WIKI-200",
                    "message": "Run tests",
                    "mode": "on-idle",
                    "request_id": "mcp-steer-1",
                }
            )
            wiki_agent_tools.steer_agent(
                {
                    "id": "WIKI-200",
                    "message": "prepare for merge",
                    "request_id": "mcp-steer-mastermind",
                    "source": "mastermind",
                }
            )
            wiki_agent_tools.archive_agent(
                {"id": "WIKI-200", "outcome": "merged"}
            )
        self.assertTrue(spawned["ok"])
        self.assertEqual(
            calls,
            [
                (
                    "POST",
                    "/api/agents/spawn",
                    {
                        "ticket": "WIKI-200",
                        "kind": "cdx",
                        "role": "implement",
                        "model": "gpt-5.4",
                        "effort": "high",
                        "workdir": "/tmp/worktree",
                        "prompt": "Implement ticket WIKI-200",
                        "orch": "wiki",
                        "request_id": "mcp-spawn-1",
                    },
                ),
                (
                    "POST",
                    "/api/agents/WIKI-200/message",
                    {
                        "text": "Run tests",
                        "mode": "on-idle",
                        "request_id": "mcp-steer-1",
                        # Default supervisor-steer source so orchestrator turns
                        # render as system markers rather than Henry bubbles.
                        "source": "supervisor-steer",
                    },
                ),
                (
                    "POST",
                    "/api/agents/WIKI-200/message",
                    {
                        "text": "prepare for merge",
                        "mode": "now",
                        "request_id": "mcp-steer-mastermind",
                        "source": "mastermind",
                    },
                ),
                (
                    "POST",
                    "/api/agents/WIKI-200/archive",
                    {"outcome": "merged"},
                ),
            ],
        )

    def test_image_scrub_strips_metadata_end_to_end(self) -> None:
        # Mutation-proof: bytes that reach disk must not contain the GPS or
        # camera-model strings the source payload embedded. If the scrub is
        # ever disabled or bypassed this assertion trips.
        from PIL import ExifTags, TiffImagePlugin

        image = Image.new("RGB", (48, 32), color=(200, 50, 50))
        exif = image.getexif()
        exif[ExifTags.Base.Orientation.value] = 1
        exif[ExifTags.Base.Make.value] = "GhostCam"
        exif[ExifTags.Base.Model.value] = "SecretModel-42"
        gps = exif.get_ifd(ExifTags.Base.GPSInfo.value)
        gps[ExifTags.GPS.GPSLatitudeRef] = "N"
        gps[ExifTags.GPS.GPSLatitude] = (
            TiffImagePlugin.IFDRational(37, 1),
            TiffImagePlugin.IFDRational(46, 1),
            TiffImagePlugin.IFDRational(3060, 100),
        )
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", exif=exif.tobytes(), quality=90)
        payload = {"data_base64": base64.b64encode(buffer.getvalue()).decode(), "mime": "image/jpeg"}
        event = wiki_artifacts.render_artifact({"kind": "image", "payload": payload})
        target = (
            self.root
            / "runtime"
            / "runs"
            / RUN_ID
            / "artifacts"
            / f"{event['id']}.jpg"
        )
        stored = target.read_bytes()
        self.assertNotIn(b"SecretModel-42", stored)
        self.assertNotIn(b"GhostCam", stored)
        self.assertNotIn(b"GPSLatitude", stored)
        self.assertNotIn(b"Exif\x00\x00", stored)
        # Metadata is stripped, but dimensions are preserved on the event.
        self.assertEqual(event["artifact"]["width"], 48)
        self.assertEqual(event["artifact"]["height"], 32)

    def test_orchestrator_render_artifact_uses_its_run_directory(self) -> None:
        with mock.patch.dict(os.environ, {"WIKI_AGENT_ROLE": "orchestrator"}):
            event = wiki_artifacts.render_artifact(
                {"kind": "image", "payload": _payload("image")}
            )
        target = (
            self.root
            / "runtime"
            / "runs"
            / RUN_ID
            / "artifacts"
            / f"{event['id']}.png"
        )
        stored_bytes = target.read_bytes()
        self.assertTrue(stored_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(io.BytesIO(stored_bytes)) as reopened:
            reopened.load()
            self.assertEqual(reopened.size, (2, 2))

    def test_pdf_accepts_base64_and_path_payload_variants(self) -> None:
        pdf_bytes = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"

        base_event = wiki_artifacts.render_artifact(
            {
                "kind": "pdf",
                "title": "Base64 PDF",
                "payload": {"data_base64": base64.b64encode(pdf_bytes).decode()},
            }
        )
        self.assertEqual(base_event["artifact"]["mime"], wiki_artifacts.PDF_MIME)
        self.assertEqual(base_event["artifact"]["byte_size"], len(pdf_bytes))

        # Path variant: source must live under an allowed root (runtime dir here).
        (self.root / "runtime").mkdir(exist_ok=True)
        source = self.root / "runtime" / "source.pdf"
        source.write_bytes(pdf_bytes)
        path_event = wiki_artifacts.render_artifact(
            {"kind": "pdf", "payload": {"path": str(source)}}
        )
        stored = (
            self.root
            / "runtime"
            / "runs"
            / RUN_ID
            / "artifacts"
            / f"{path_event['id']}.pdf"
        )
        self.assertEqual(stored.read_bytes(), pdf_bytes)

    def test_pdf_path_outside_allowed_roots_is_rejected(self) -> None:
        pdf_bytes = b"%PDF-1.4\ncontent\n"
        outside_root = self.root.parent / "outside-tree"
        outside_root.mkdir(exist_ok=True)
        outside = outside_root / "leaked.pdf"
        outside.write_bytes(pdf_bytes)
        try:
            with self.assertRaisesRegex(
                wiki_artifacts.ArtifactValidationError, "outside the allowed roots"
            ):
                wiki_artifacts.render_artifact(
                    {"kind": "pdf", "payload": {"path": str(outside)}}
                )
        finally:
            outside.unlink(missing_ok=True)
            try:
                outside_root.rmdir()
            except OSError:
                pass

    def test_pdf_path_refused_when_no_allowed_roots_configured(self) -> None:
        pdf_bytes = b"%PDF-1.4\nsource\n"
        source = self.root / "runtime" / "keep.pdf"
        source.parent.mkdir(exist_ok=True)
        source.write_bytes(pdf_bytes)
        with mock.patch.dict(
            os.environ,
            {
                "WIKI_VAULT_DIR": "",
                "WIKI_AGENT_RUNTIME_DIR": "",
                "WIKI_AGENT_ARCHIVE_DIR": "",
                "WIKI_RUN_ID": RUN_ID,
            },
            clear=False,
        ):
            os.environ.pop("WIKI_VAULT_DIR", None)
            os.environ.pop("WIKI_AGENT_RUNTIME_DIR", None)
            os.environ.pop("WIKI_AGENT_ARCHIVE_DIR", None)
            with self.assertRaisesRegex(
                wiki_artifacts.ArtifactValidationError, "no allowed roots configured"
            ):
                wiki_artifacts._read_pdf_path(str(source))

    def test_pdf_rejects_ambiguous_and_unsafe_payloads(self) -> None:
        pdf_bytes = b"%PDF-1.4\nx\n"
        with self.assertRaises(wiki_artifacts.ArtifactValidationError):
            wiki_artifacts.render_artifact(
                {
                    "kind": "pdf",
                    "payload": {
                        "data_base64": base64.b64encode(pdf_bytes).decode(),
                        "path": "/etc/passwd",
                    },
                }
            )
        with self.assertRaises(wiki_artifacts.ArtifactValidationError):
            wiki_artifacts.render_artifact({"kind": "pdf", "payload": {}})
        with self.assertRaises(wiki_artifacts.ArtifactValidationError):
            wiki_artifacts.render_artifact(
                {"kind": "pdf", "payload": {"path": "relative/path.pdf"}}
            )
        symlink_target = self.root / "symlinked.pdf"
        real = self.root / "real.pdf"
        real.write_bytes(pdf_bytes)
        try:
            symlink_target.symlink_to(real)
        except OSError:  # symlinks unsupported in this environment
            return
        with self.assertRaises(wiki_artifacts.ArtifactValidationError):
            wiki_artifacts.render_artifact(
                {"kind": "pdf", "payload": {"path": str(symlink_target)}}
            )

    def test_read_fd_bounded_rejects_files_that_exceed_the_cap(self) -> None:
        payload = b"%PDF-1.4\n" + b"z" * (wiki_artifacts.PDF_LIMIT + 1)
        source = self.root / "runtime" / "grown.pdf"
        source.parent.mkdir(exist_ok=True)
        source.write_bytes(payload)
        fd = os.open(source, os.O_RDONLY)
        try:
            with self.assertRaisesRegex(
                wiki_artifacts.ArtifactValidationError, "MB pdf limit"
            ):
                wiki_artifacts._read_fd_bounded(
                    fd, wiki_artifacts.PDF_LIMIT, "pdf",
                    f"{wiki_artifacts.PDF_LIMIT // (1024 * 1024)}MB pdf limit",
                )
        finally:
            os.close(fd)

    def test_pdf_path_refuses_intermediate_directory_symlink(self) -> None:
        # Race-real TOCTOU: prime a legitimate path, then swap an
        # intermediate directory for a symlink pointing outside the root
        # AFTER validation (resolve + allow-root check) has already
        # accepted the path but BEFORE the walker opens it. The walker's
        # dir_fd + O_NOFOLLOW per-component open MUST refuse; a single
        # O_NOFOLLOW on the leaf would happily follow the intermediate hop.
        pdf_bytes = b"%PDF-1.4\nallowed\n"
        outside_bytes = b"%PDF-1.4\nSECRET-OUTSIDE\n"
        runtime = self.root / "runtime"
        runtime.mkdir(exist_ok=True)
        legit_parent = runtime / "reports"
        legit_parent.mkdir(exist_ok=True)
        legit_file = legit_parent / "doc.pdf"
        legit_file.write_bytes(pdf_bytes)
        # Sanity: happy path still reads through the walker.
        self.assertEqual(wiki_artifacts._read_pdf_path(str(legit_file)), pdf_bytes)

        outside_root = self.root.parent / "outside-tree-intermediate"
        outside_root.mkdir(exist_ok=True)
        outside_file = outside_root / "doc.pdf"
        outside_file.write_bytes(outside_bytes)

        # Intercept _open_root_fd so the swap happens AFTER validation but
        # BEFORE the walker opens any component under the root. This is the
        # real race window the walker must close — without patching a
        # barrier here the swap would land before resolve() and be caught
        # by symlink-check on the parent, never exercising the walker.
        real_open_root = wiki_artifacts._open_root_fd

        def swap_then_open_root(root):
            os.rename(legit_parent, runtime / "reports.tmp")
            try:
                os.symlink(outside_root, legit_parent)
            except OSError:
                # If symlink creation fails, undo the rename and skip.
                os.rename(runtime / "reports.tmp", legit_parent)
                raise
            return real_open_root(root)

        try:
            with mock.patch.object(
                wiki_artifacts, "_open_root_fd", side_effect=swap_then_open_root
            ):
                with self.assertRaises(wiki_artifacts.ArtifactValidationError):
                    wiki_artifacts._read_pdf_path(str(legit_file))
        finally:
            try:
                legit_parent.unlink()
            except OSError:
                pass
            try:
                os.rename(runtime / "reports.tmp", legit_parent)
            except OSError:
                pass
            outside_file.unlink(missing_ok=True)
            try:
                outside_root.rmdir()
            except OSError:
                pass

    def test_pdf_path_rejects_non_regular_files(self) -> None:
        fifo = self.root / "runtime" / "pipe.pdf"
        fifo.parent.mkdir(exist_ok=True)
        try:
            os.mkfifo(fifo)
        except (AttributeError, OSError):  # not all filesystems support fifos
            return
        try:
            with self.assertRaisesRegex(
                wiki_artifacts.ArtifactValidationError, "regular file"
            ):
                wiki_artifacts._read_pdf_path(str(fifo))
        finally:
            fifo.unlink(missing_ok=True)

    def test_stdio_server_rejects_oversized_transport_line(self) -> None:
        # Craft a line larger than MAX_REQUEST_BYTES, followed by a well-formed
        # request. The server should drain the giant line, respond with a
        # transport error, and still process the trailing request.
        garbage = b"x" * (wiki_artifacts.MAX_REQUEST_BYTES + 4096)
        good = json.dumps(
            {"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {}}
        ).encode() + b"\n"
        env = os.environ.copy()
        process = subprocess.run(
            [sys.executable, "-m", "backend.app.wiki_artifacts"],
            input=garbage + b"\n" + good,
            capture_output=True,
            env=env,
            timeout=10,
            check=True,
        )
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["id"], None)
        self.assertIn("transport limit", responses[0]["error"]["message"])
        self.assertEqual(responses[1]["id"], 99)
        self.assertIn("tools", responses[1]["result"])

    def test_storage_failure_returns_a_tool_error_without_crashing_server(self) -> None:
        with mock.patch.object(wiki_artifacts, "render_artifact", side_effect=OSError("disk full")):
            response = wiki_artifacts._tool_result(7, {"kind": "image", "payload": {}})
        self.assertTrue(response["result"]["isError"])
        self.assertIn("disk full", response["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
