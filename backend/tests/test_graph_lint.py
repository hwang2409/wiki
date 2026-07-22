"""Tests for graph artifact schemas and the standalone graph linter."""

from __future__ import annotations

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


class GraphLintTests(unittest.TestCase):
    def test_valid_fixture_for_each_schema(self) -> None:
        for schema in graph_lint.SCHEMA_NAMES:
            with self.subTest(schema=schema):
                violations = graph_lint.lint_path(FIXTURES / f"{schema}.valid.json")
                self.assertEqual(violations, [])

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

    def test_each_edge_kind_rejects_empty_payload(self) -> None:
        kinds = {
            "spawn",
            "steer",
            "verdict",
            "archive",
            "handoff",
            "monitor_alarm",
            "capability_grant",
            "escalation",
        }
        for kind in kinds:
            with self.subTest(kind=kind):
                document = {
                    "kind": kind,
                    "from": "N-1",
                    "to": "N-2",
                    "payload": {},
                    "created_at": "2026-07-22T18:00:00Z",
                }
                with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as handle:
                    json.dump(document, handle)
                    handle.flush()
                    violations = graph_lint.lint_path(handle.name, "edge")
                self.assertTrue(violations)

    def test_verdict_finding_sha_mismatch_is_a_violation(self) -> None:
        verdict = json.loads((FIXTURES / "verdict.valid.json").read_text())
        verdict["findings"][0]["source_sha"] = "deadbee"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as handle:
            json.dump(verdict, handle)
            handle.flush()
            violations = graph_lint.lint_path(handle.name)
        self.assertEqual(len(violations), 1)
        self.assertIn("/findings/0/source_sha", violations[0])
        self.assertIn("must match verdict sha", violations[0])

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
