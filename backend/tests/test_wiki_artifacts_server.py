from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.app import wiki_agent_tools, wiki_artifacts


RUN_ID = "00000000-0000-4000-8000-000000000085"


def _payload(kind: str) -> dict:
    return {
        "mermaid": {"source": "graph TD; A-->B"},
        "svg": {"source": '<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'},
        "image": {
            "data_base64": base64.b64encode(b"fixture-png").decode(),
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
                    self.assertEqual(image.read_bytes(), b"fixture-png")
                    self.assertEqual(image.stat().st_mode & 0o777, 0o600)
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
        self.assertNotIn("structuredContent", result)
        event = wiki_artifacts.artifact_from_text(result["content"][0]["text"])
        self.assertIsNotNone(event)
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
            }.issubset(names)
        )

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
                    },
                ),
                (
                    "POST",
                    "/api/agents/WIKI-200/archive",
                    {"outcome": "merged"},
                ),
            ],
        )

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
        self.assertEqual(target.read_bytes(), b"fixture-png")

    def test_storage_failure_returns_a_tool_error_without_crashing_server(self) -> None:
        with mock.patch.object(wiki_artifacts, "render_artifact", side_effect=OSError("disk full")):
            response = wiki_artifacts._tool_result(7, {"kind": "image", "payload": {}})
        self.assertTrue(response["result"]["isError"])
        self.assertIn("disk full", response["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
