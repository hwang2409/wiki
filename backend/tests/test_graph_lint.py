"""Tests for graph artifact schemas and the standalone graph linter."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "graph"
WIKI_CLI = REPO_ROOT / "wiki"
sys.path.insert(0, str(REPO_ROOT))

from wiki_cli import graph_lint  # noqa: E402


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _edge_document(kind: str, payload: dict) -> dict:
    return {
        "kind": kind,
        "from": "N-1",
        "to": "N-2",
        "payload": payload,
        "created_at": "2026-07-22T18:00:00Z",
    }


def _valid_edge_documents() -> dict[str, dict]:
    return {
        "spawn": _edge_document("spawn", {
            "ticket": "WIKI-162",
            "role": "implement",
            "model": "gpt-5.4",
            "worktree": "/tmp/wiki-162",
            "request_id": "spawn-1",
        }),
        "steer": _edge_document("steer", _fixture("steer.valid.json")),
        "verdict": _edge_document("verdict", _fixture("verdict.valid.json")),
        "archive": _edge_document("archive", {
            "outcome": "merged",
            "ended_at": "2026-07-22T18:00:00Z",
        }),
        "handoff": _edge_document("handoff", {
            "from_session": "session-a",
            "to_session": "session-b",
            "worktree": "/tmp/wiki-162",
            "pr_url": None,
            "remaining_summary": "run the final gate",
        }),
        "monitor_alarm": _edge_document("monitor_alarm", {
            "alarm_kind": "stale",
            "message": "worker is stale",
            "watchlist_row": "WIKI-162",
        }),
        "capability_grant": _edge_document("capability_grant", {
            "capability": "merge",
            "granted_by": "wiki",
        }),
        "escalation": _edge_document("escalation", {
            "reason": "blocked",
            "prior_findings": [_fixture("finding.valid.json")],
            "target": "orchestrator",
        }),
    }


EDGE_REQUIRED_FIELDS = {
    "spawn": ("ticket", "role", "model", "worktree", "request_id"),
    "steer": ("target_worker", "mode", "findings", "created_at"),
    "verdict": ("worker", "sha", "state", "findings", "created_at"),
    "archive": ("outcome", "ended_at"),
    "handoff": ("from_session", "to_session", "worktree", "pr_url", "remaining_summary"),
    "monitor_alarm": ("alarm_kind", "message", "watchlist_row"),
    "capability_grant": ("capability", "granted_by"),
    "escalation": ("reason", "prior_findings", "target"),
}


class GraphLintTests(unittest.TestCase):
    def test_valid_fixture_for_each_schema(self) -> None:
        for schema in graph_lint.SCHEMA_NAMES:
            with self.subTest(schema=schema):
                self.assertEqual(
                    graph_lint.lint_path(FIXTURES / f"{schema}.valid.json"), []
                )

    def test_invalid_fixture_for_each_schema(self) -> None:
        for schema in graph_lint.SCHEMA_NAMES:
            with self.subTest(schema=schema):
                violations = graph_lint.lint_path(FIXTURES / f"{schema}.invalid.json")
                self.assertTrue(violations)
                self.assertTrue(all(f"{schema}.invalid.json: /" in line for line in violations))

    def test_explicit_schema_override_validates_shape(self) -> None:
        violations = graph_lint.lint_path(
            FIXTURES / "finding.valid.json", schema_override="verdict"
        )
        self.assertTrue(violations)
        self.assertIn("'worker' is a required property", "\n".join(violations))

    def test_each_edge_kind_rejects_each_missing_required_payload_field(self) -> None:
        for kind, required in EDGE_REQUIRED_FIELDS.items():
            for field in required:
                with self.subTest(kind=kind, field=field):
                    document = copy.deepcopy(_valid_edge_documents()[kind])
                    del document["payload"][field]
                    violations = graph_lint.validate_document(document, "edge")
                    self.assertTrue(violations)
                    self.assertTrue(any(field in message for _, message in violations))

    def test_each_edge_kind_rejects_unexpected_payload_property(self) -> None:
        for kind in EDGE_REQUIRED_FIELDS:
            with self.subTest(kind=kind):
                document = copy.deepcopy(_valid_edge_documents()[kind])
                document["payload"]["unexpected"] = True
                violations = graph_lint.validate_document(document, "edge")
                self.assertTrue(violations)
                self.assertTrue(any("additional properties" in message.lower() for _, message in violations))

    def test_verdict_sha_binding_accepts_and_rejects_7_and_40_char_values(self) -> None:
        for sha in ("0123456", "0" * 40):
            for mismatch in (False, True):
                with self.subTest(sha_length=len(sha), mismatch=mismatch):
                    verdict = _fixture("verdict.valid.json")
                    verdict["sha"] = sha
                    verdict["findings"][0]["source_sha"] = (
                        ("1" * len(sha)) if mismatch else sha
                    )
                    violations = graph_lint.validate_document(verdict, "verdict")
                    if mismatch:
                        self.assertTrue(any(pointer == "/findings/0/source_sha" for pointer, _ in violations))
                    else:
                        self.assertEqual(violations, [])

    def test_nested_verdict_sha_binding_accepts_and_rejects_7_and_40_char_values(self) -> None:
        for sha in ("0123456", "a" * 40):
            for mismatch in (False, True):
                with self.subTest(sha_length=len(sha), mismatch=mismatch):
                    verdict = _fixture("verdict.valid.json")
                    verdict["sha"] = sha
                    verdict["findings"][0]["source_sha"] = (
                        ("b" * len(sha)) if mismatch else sha
                    )
                    edge = _edge_document("verdict", verdict)
                    edge_violations = graph_lint.validate_document(edge, "edge")
                    workgraph = _fixture("workgraph.valid.json")
                    workgraph["edges"] = [edge]
                    workgraph_violations = graph_lint.validate_document(workgraph, "workgraph")
                    if mismatch:
                        self.assertTrue(
                            any(
                                pointer == "/payload/findings/0/source_sha"
                                for pointer, _ in edge_violations
                            )
                        )
                        self.assertTrue(
                            any(
                                pointer == "/edges/0/payload/findings/0/source_sha"
                                for pointer, _ in workgraph_violations
                            )
                        )
                    else:
                        self.assertEqual(edge_violations, [])
                        self.assertEqual(workgraph_violations, [])

    def test_nested_verdict_sha_binding_rejects_missing_fields_with_rooted_pointers(self) -> None:
        for field, edge_pointer, workgraph_pointer in (
            ("sha", "/payload/sha", "/edges/0/payload/sha"),
            (
                "source_sha",
                "/payload/findings/0/source_sha",
                "/edges/0/payload/findings/0/source_sha",
            ),
        ):
            with self.subTest(field=field):
                verdict = _fixture("verdict.valid.json")
                if field == "sha":
                    del verdict["sha"]
                else:
                    del verdict["findings"][0][field]

                edge = _edge_document("verdict", verdict)
                edge_violations = graph_lint.validate_document(edge, "edge")
                self.assertTrue(
                    any(pointer == edge_pointer for pointer, _ in edge_violations)
                )

                workgraph = _fixture("workgraph.valid.json")
                workgraph["edges"] = [edge]
                workgraph_violations = graph_lint.validate_document(workgraph, "workgraph")
                self.assertTrue(
                    any(
                        pointer == workgraph_pointer
                        for pointer, _ in workgraph_violations
                    )
                )

    def test_cli_reports_malformed_json(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as handle:
            handle.write("{not json")
            handle.flush()
            result = subprocess.run(
                [sys.executable, str(WIKI_CLI), "graph", "lint", handle.name],
                capture_output=True,
                text=True,
                cwd=REPO_ROOT,
                check=False,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid JSON", result.stdout)

    def test_cli_reports_no_matching_schema(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as handle:
            json.dump({"not": "a graph artifact"}, handle)
            handle.flush()
            result = subprocess.run(
                [sys.executable, str(WIKI_CLI), "graph", "lint", handle.name],
                capture_output=True,
                text=True,
                cwd=REPO_ROOT,
                check=False,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("unable to auto-detect schema", result.stdout)

    def test_cli_rejects_ambiguous_shape(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as handle:
            json.dump(
                {
                    "target_worker": "WIKI-162",
                    "mode": "now",
                    "findings": [],
                    "worker": "WIKI-162",
                    "state": "MERGE-READY",
                },
                handle,
            )
            handle.flush()
            result = subprocess.run(
                [sys.executable, str(WIKI_CLI), "graph", "lint", handle.name],
                capture_output=True,
                text=True,
                cwd=REPO_ROOT,
                check=False,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("ambiguous schema shape", result.stdout)
        self.assertIn("steer", result.stdout)
        self.assertIn("verdict", result.stdout)

    def test_cli_returns_one_and_prints_one_violation_per_line(self) -> None:
        result = subprocess.run(
            [sys.executable, str(WIKI_CLI), "graph", "lint", str(FIXTURES / "edge.invalid.json")],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        self.assertEqual(result.returncode, 1, msg=result.stderr)
        self.assertTrue(result.stdout.strip())
        self.assertTrue(all("/" in line for line in result.stdout.splitlines()))


if __name__ == "__main__":
    unittest.main()
