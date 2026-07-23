"""Regression tests for the typed Steer gate on legacy message sends."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from argparse import Namespace
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
WIKI_CLI = REPO_ROOT / "wiki"
sys.path.insert(0, str(REPO_ROOT))


def _load_cli_module():
    loader = SourceFileLoader("wiki_cli_entry_for_test", str(WIKI_CLI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class SteerValidationTests(unittest.TestCase):
    def test_invalid_steer_aborts_before_backend_request(self) -> None:
        cli = _load_cli_module()
        args = Namespace(
            id="WIKI-162",
            message="continue the implementation",
            file=None,
            mode="now",
            request_id="steer-regression-1",
            source=None,
        )
        with (
            mock.patch.object(cli.graph_lint, "compose_steer_document", return_value={}),
            mock.patch.object(cli, "_print_backend_call") as backend_call,
        ):
            with self.assertRaises(SystemExit):
                cli.cmd_agent_steer(args)
        backend_call.assert_not_called()

    def test_composed_steer_is_schema_valid(self) -> None:
        cli = _load_cli_module()
        document = cli.graph_lint.compose_steer_document(
            "WIKI-162",
            "on-idle",
            "continue the implementation",
            request_id="steer-regression-2",
            created_at="2026-07-22T18:00:00Z",
        )
        self.assertEqual(cli.graph_lint.validate_document(document, "steer"), [])

    def test_packaging_spec_keeps_graph_lint_and_all_schema_datas(self) -> None:
        spec_path = REPO_ROOT / "packaging" / "wiki-backend.spec"
        spec = spec_path.read_text(encoding="utf-8")
        schema_names = sorted(
            path.name for path in (REPO_ROOT / "schemas").glob("*.schema.json")
        )

        self.assertIn(
            'SCHEMA_FILES = sorted((ROOT / "schemas").glob("*.schema.json"))',
            spec,
        )
        self.assertIn(
            '*((str(path), "schemas") for path in SCHEMA_FILES),',
            spec,
        )
        self.assertIn("datas=datas,", spec)
        self.assertIn('"wiki_cli.graph_lint",', spec)
        self.assertEqual(
            schema_names,
            [
                "edge.schema.json",
                "finding.schema.json",
                "steer.schema.json",
                "verdict.schema.json",
                "workgraph-template.schema.json",
                "workgraph.schema.json",
            ],
        )
        self.assertIn(
            'WORKGRAPH_TEMPLATE_FILES = sorted(',
            spec,
        )
        self.assertIn(
            '*((str(path), "templates/workgraphs") for path in WORKGRAPH_TEMPLATE_FILES),',
            spec,
        )

    def test_frozen_native_steer_uses_bundled_schemas_and_sends_once(self) -> None:
        from backend.app import wiki_agent_tools
        from wiki_cli import graph_lint

        with tempfile.TemporaryDirectory() as bundle_dir:
            bundled_schemas = Path(bundle_dir) / "schemas"
            bundled_schemas.mkdir()
            for schema_path in (REPO_ROOT / "schemas").glob("*.schema.json"):
                shutil.copy2(schema_path, bundled_schemas / schema_path.name)

            calls: list[tuple[str, str, dict[str, str]]] = []

            def backend_call(method: str, path: str, payload: dict[str, str]) -> dict:
                calls.append((method, path, payload))
                return {"ok": True}

            with (
                mock.patch.object(sys, "frozen", True, create=True),
                mock.patch.object(sys, "_MEIPASS", bundle_dir, create=True),
                mock.patch.dict(os.environ, {"WIKI_AGENT_ROLE": "orchestrator"}),
                mock.patch.object(wiki_agent_tools, "_backend_api", side_effect=backend_call),
            ):
                importlib.reload(graph_lint)
                response = wiki_agent_tools.steer_agent(
                    {
                        "id": "WIKI-162",
                        "message": "preserve this exact native steer body",
                        "mode": "on-idle",
                        "request_id": "native-steer-1",
                        "source": "mastermind",
                    }
                )

            importlib.reload(graph_lint)

        self.assertEqual(response, {"ok": True})
        self.assertEqual(
            calls,
            [
                (
                    "POST",
                    "/api/agents/WIKI-162/message",
                    {
                        "text": "preserve this exact native steer body",
                        "mode": "on-idle",
                        "request_id": "native-steer-1",
                        "source": "mastermind",
                    },
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
