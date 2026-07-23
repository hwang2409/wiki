"""Tests for prefix-based work-graph template selection."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WIKI_CLI = REPO_ROOT / "wiki"
sys.path.insert(0, str(REPO_ROOT))

from wiki_cli import workgraph_templates  # noqa: E402


class WorkgraphTemplateSelectionTests(unittest.TestCase):
    def test_ticket_prefix_matrix(self) -> None:
        expected = {
            "PHO-1234": "phoebe.implement",
            "WIKI-99": "wiki.implement",
            "MITMWEB-42": "tooling.implement",
            "TIX-5": "tooling.implement",
            "GAU-7": "tooling.implement",
            "PUF-8": "tooling.implement",
        }
        for ticket, template_id in expected.items():
            with self.subTest(ticket=ticket):
                selection = workgraph_templates.select_template(ticket)
                self.assertEqual(selection["template_id"], template_id)
                self.assertEqual(selection["ticket_id"], ticket)

    def test_optional_labels_select_available_specialized_template(self) -> None:
        self.assertEqual(
            workgraph_templates.select_template("PHO-1", labels=["frontend"])["template_id"],
            "phoebe.frontend",
        )
        self.assertEqual(
            workgraph_templates.select_template("PHO-1", labels=["migration"])["template_id"],
            "phoebe.migration",
        )
        self.assertEqual(
            workgraph_templates.select_template("PHO-1", labels=["plan"])["template_id"],
            "phoebe.plan",
        )

    def test_missing_or_unknown_prefix_is_a_nonzero_input_error(self) -> None:
        for ticket in ("XYZ-1", "PHO", ""):
            with self.subTest(ticket=ticket):
                with self.assertRaises(workgraph_templates.TemplateSelectionError):
                    workgraph_templates.select_template(ticket)

    def test_cli_human_and_json_outputs_include_template_and_roles(self) -> None:
        human = subprocess.run(
            [sys.executable, str(WIKI_CLI), "graph", "select-template", "PHO-1234"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("template: phoebe.implement", human.stdout)
        self.assertIn("implement: runtime=cdx model=gpt-5.6-luna effort=high", human.stdout)
        self.assertIn("review: runtime=cdx model=gpt-5.6-sol effort=high", human.stdout)

        machine = subprocess.run(
            [sys.executable, str(WIKI_CLI), "graph", "select-template", "WIKI-99", "--json"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(machine.returncode, 0, machine.stderr)
        payload = json.loads(machine.stdout)
        self.assertEqual(payload["template_id"], "wiki.implement")
        self.assertEqual(payload["roles"][0]["model"], "gpt-5.6-luna")

    def test_cli_unknown_and_malformed_ticket_ids_return_input_error(self) -> None:
        cases = (
            ("XYZ-1", "unknown ticket prefix", "WIKI"),
            ("PHO", "invalid ticket id", "PREFIX-number"),
            ("", "invalid ticket id", "PREFIX-number"),
        )
        for ticket, expected_error, expected_detail in cases:
            with self.subTest(ticket=ticket):
                result = subprocess.run(
                    [sys.executable, str(WIKI_CLI), "graph", "select-template", ticket],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(expected_error, result.stderr)
                self.assertIn(expected_detail, result.stderr)


if __name__ == "__main__":
    unittest.main()
