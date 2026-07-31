from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from backend.app import main


RUN_ID = "00000000-0000-4000-8000-000000000085"
ARTIFACT_ID = "00000000-0000-4000-8000-000000000086"


class ArtifactServeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        self.archive = self.root / "archive"
        self.registry = self.root / "registry.json"
        self.runtime.mkdir()
        self.archive.mkdir()
        self.registry.write_text("{}", encoding="utf-8")
        self.patches = (
            mock.patch.object(main, "AGENT_REGISTRY_PATH", self.registry),
            mock.patch.object(main, "AGENT_ARCHIVE_DIR", self.archive),
            mock.patch.object(
                main,
                "SUPERVISOR_CLIENT",
                SimpleNamespace(paths=SimpleNamespace(runtime_dir=self.runtime)),
            ),
        )
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tmp.cleanup()

    def _write_live(self, ticket: str, artifact_id: str = ARTIFACT_ID) -> Path:
        self.registry.write_text(
            json.dumps(
                {
                    ticket: {
                        "current": {"run_id": RUN_ID, "kind": "cdx"},
                        "history": [],
                    }
                }
            ),
            encoding="utf-8",
        )
        target = self.runtime / "runs" / RUN_ID / "artifacts" / f"{artifact_id}.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"live-png")
        return target

    def test_live_artifact_is_served_with_headers(self) -> None:
        target = self._write_live("WIKI-85")
        response = main.get_agent_artifact("WIKI-85", ARTIFACT_ID)
        self.assertEqual(Path(response.path), target)
        self.assertEqual(response.media_type, "image/png")
        self.assertEqual(response.headers["cache-control"], "private, max-age=3600")

    def test_newest_archived_artifact_is_served(self) -> None:
        older = self.archive / "WIKI-85" / "20260712-120000" / "artifacts"
        newest = self.archive / "WIKI-85" / "20260713-120000" / "artifacts"
        older.mkdir(parents=True)
        newest.mkdir(parents=True)
        (older / f"{ARTIFACT_ID}.png").write_bytes(b"old")
        target = newest / f"{ARTIFACT_ID}.webp"
        target.write_bytes(b"new")

        response = main.get_agent_artifact("WIKI-85", ARTIFACT_ID)
        self.assertEqual(Path(response.path), target)
        self.assertEqual(response.media_type, "image/webp")

    def test_visual_diff_variant_serves_paired_files(self) -> None:
        self.registry.write_text(
            json.dumps(
                {
                    "WIKI-85": {
                        "current": {"run_id": RUN_ID, "kind": "cdx"},
                        "history": [],
                    }
                }
            ),
            encoding="utf-8",
        )
        artifacts_dir = self.runtime / "runs" / RUN_ID / "artifacts"
        artifacts_dir.mkdir(parents=True)
        before = artifacts_dir / f"{ARTIFACT_ID}.before.png"
        after = artifacts_dir / f"{ARTIFACT_ID}.after.webp"
        before.write_bytes(b"before-bytes")
        after.write_bytes(b"after-bytes")

        before_response = main.get_agent_artifact("WIKI-85", ARTIFACT_ID, variant="before")
        self.assertEqual(Path(before_response.path), before)
        self.assertEqual(before_response.media_type, "image/png")

        after_response = main.get_agent_artifact("WIKI-85", ARTIFACT_ID, variant="after")
        self.assertEqual(Path(after_response.path), after)
        self.assertEqual(after_response.media_type, "image/webp")

    def test_unknown_variant_returns_404(self) -> None:
        self._write_live("WIKI-85")
        with self.assertRaises(HTTPException) as raised:
            main.get_agent_artifact("WIKI-85", ARTIFACT_ID, variant="sideways")
        self.assertEqual(raised.exception.status_code, 404)

    def test_ticket_scope_and_unknown_artifact_return_404(self) -> None:
        self._write_live("WIKI-85")
        for ticket, artifact_id in (
            ("WIKI-86", ARTIFACT_ID),
            ("WIKI-85", "00000000-0000-4000-8000-000000000099"),
            ("../WIKI-85", ARTIFACT_ID),
        ):
            with self.subTest(ticket=ticket, artifact_id=artifact_id), self.assertRaises(
                HTTPException
            ) as raised:
                main.get_agent_artifact(ticket, artifact_id)
            self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
