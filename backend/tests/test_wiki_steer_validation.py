"""Regression tests for the typed Steer gate on legacy message sends."""

from __future__ import annotations

import importlib.util
import sys
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


if __name__ == "__main__":
    unittest.main()
