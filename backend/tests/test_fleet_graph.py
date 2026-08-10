"""WIKI-170: bounded fleet graph roll-up and edge classification."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from backend.app import main
from backend.app.agent_runtime.archive_protocol import commit_archive


GRAPH = {
    "ticket": "WIKI-170",
    "orch": "wiki",
    "nodes": [
        {"id": "orch:wiki", "kind": "orchestrator", "label": "wiki orch"},
        {"id": "N-1", "kind": "implement", "label": "WIKI-170", "worker_id": "WIKI-170"},
        {
            "id": "N-2",
            "kind": "review",
            "label": "WIKI-170-REVIEW1",
            "worker_id": "WIKI-170-REVIEW1",
        },
    ],
    "edges": [
        {"kind": "spawn", "from": "orch:wiki", "to": "N-1", "created_at": "2026-07-29T12:00:00Z"},
        {"kind": "spawn", "from": "orch:wiki", "to": "N-2", "created_at": "2026-07-29T12:01:00Z"},
        {"kind": "archive", "from": "orch:wiki", "to": "N-2", "created_at": "2026-07-29T12:02:00Z"},
    ],
}


def _commit_archive_fixture(session: Path, run_id: str) -> None:
    (session / "run.json").write_text(
        f'{{"run_id":"{run_id}","provider":"codex"}}', encoding="utf-8"
    )
    (session / "raw.jsonl").write_text("raw\n", encoding="utf-8")
    (session / "events.jsonl").write_text("events\n", encoding="utf-8")
    commit_archive(session, run_id=run_id, completed_at="2026-08-01T00:00:00Z")


class FleetGraphTests(unittest.TestCase):
    def test_normalizes_group_and_active_historical_edges(self) -> None:
        tickets = main._fleet_graph_ticket(  # noqa: SLF001
            graph=GRAPH,
            source="live",
            worker_metadata={"WIKI-170": {"state": "working", "role": "implement", "kind": "cdx"}},
            archived_metadata={"WIKI-170-REVIEW1": {"state": "merge-ready", "role": "review", "kind": "cc"}},
            allowed_tickets={"WIKI-170", "WIKI-170-REVIEW1"},
        )

        self.assertEqual({ticket["ticket"] for ticket in tickets}, {"WIKI-170", "WIKI-170-REVIEW1"})
        edges = [edge for ticket in tickets for edge in ticket["edges"]]
        live_edge = next(edge for edge in edges if edge["to"] == "WIKI-170")
        self.assertEqual(live_edge["from"], "orch:wiki")
        self.assertTrue(live_edge["active"])
        archived_edge = next(edge for edge in edges if edge["kind"] == "archive")
        self.assertFalse(archived_edge["active"])

    def test_payload_groups_live_workers_and_uses_bounded_archive_read(self) -> None:
        registry = {
            "WIKI-170": {
                "current": {"orch": "wiki", "role": "implement", "kind": "cdx"}
            },
            "PHO-8": {
                "current": {"orch": "phoebe", "role": "review", "kind": "cc"}
            },
        }
        archive = [
            {
                "ticket": "PHO-8",
                "orch": "phoebe",
                "state": "merge-ready",
                "role": "review",
                "kind": "cc",
            }
        ]
        with (
            mock.patch.object(main, "_read_agent_registry", return_value=registry),
            mock.patch.object(main, "read_agent_status", return_value={"state": "working"}),
            mock.patch.object(main, "list_archived", return_value=archive) as list_archived,
            mock.patch.object(main.graph_health, "load_validated_graph", return_value=(None, None)),
        ):
            payload = main._fleet_graph_payload()  # noqa: SLF001

        list_archived.assert_called_once_with(limit=10, latest_per_ticket=True, limit_per_orch=10)
        self.assertEqual([group["orch"] for group in payload["groups"]], ["phoebe", "wiki"])
        self.assertEqual(payload["groups"][0]["tickets"][0]["ticket"], "PHO-8")
        self.assertEqual(payload["groups"][1]["tickets"][0]["state"], "working")
        self.assertIsInstance(payload["updated_at_ns"], int)

    def test_bounded_archive_read_loads_only_selected_bodies(self) -> None:
        with TemporaryDirectory() as temp_dir:
            archive_root = Path(temp_dir)
            for prefix in ("WIKI", "PHO"):
                for index in range(200):
                    session = archive_root / f"{prefix}-{index}" / (
                        f"20260729-{index // 60:02d}{index % 60:02d}00"
                    )
                    session.mkdir(parents=True)
                    (session / "meta.json").write_text(
                        '{"worker":{"orch":"wiki"}}' if prefix == "WIKI" else '{"worker":{"orch":"phoebe"}}',
                        encoding="utf-8",
                    )
                    (session / "final-status.json").write_text(
                        '{"state":"closed"}', encoding="utf-8"
                    )
                    _commit_archive_fixture(session, f"{prefix.lower()}-{index}")

            loaded_paths: list[Path] = []
            original_read = main._read_json_object  # noqa: SLF001

            def counted_read(path: Path) -> dict:
                loaded_paths.append(path)
                return original_read(path)

            with (
                mock.patch.object(main, "AGENT_ARCHIVE_DIR", archive_root),
                mock.patch.object(
                    main,
                    "_read_agent_registry",
                    return_value={"_orchestrators": {"wiki": {}, "phoebe": {}}},
                ),
                mock.patch.object(main, "_read_json_object", side_effect=counted_read),
            ):
                entries = main.list_archived(
                    limit=5,
                    latest_per_ticket=True,
                    limit_per_orch=5,
                )

            loaded_sessions = {path.parent for path in loaded_paths}
            self.assertEqual(len(entries), 10)
            self.assertEqual(len(loaded_sessions), 10)
            self.assertEqual(len(loaded_paths), 20)
            self.assertFalse(any("-000000" in str(path) for path in loaded_paths))

    def test_headless_orchestrators_each_get_archive_window(self) -> None:
        with TemporaryDirectory() as temp_dir:
            archive_root = Path(temp_dir)
            registry = {
                "wiki": {"current": {"role": "orchestrator", "control_attached": True}},
                "phoebe": {"current": {"role": "orchestrator", "control_attached": True}},
            }
            for prefix, orch in (("WIKI", "wiki"), ("PHO", "phoebe")):
                for index in range(5):
                    session = archive_root / f"{prefix}-{index}" / f"20260729-00{index:04d}"
                    session.mkdir(parents=True)
                    (session / "meta.json").write_text(
                        f'{{"worker":{{"orch":"{orch}","role":"review","kind":"cdx"}}}}',
                        encoding="utf-8",
                    )
                    (session / "final-status.json").write_text(
                        '{"state":"closed"}', encoding="utf-8"
                    )
                    _commit_archive_fixture(session, f"{prefix.lower()}-{index}")

            with (
                mock.patch.object(main, "AGENT_ARCHIVE_DIR", archive_root),
                mock.patch.object(main, "_read_agent_registry", return_value=registry),
                mock.patch.object(main.graph_health, "load_validated_graph", return_value=(None, None)),
            ):
                payload = main.fleet_graph(limit=5)

        counts = {group["orch"]: len(group["tickets"]) for group in payload["groups"]}
        self.assertEqual(counts, {"phoebe": 5, "wiki": 5})
        self.assertEqual(sum(counts.values()), 10)

    def test_selected_archives_bound_graph_nodes_and_edges(self) -> None:
        registry = {
            "WIKI-170": {
                "current": {"orch": "wiki", "role": "implement", "kind": "cdx"}
            }
        }
        graph = {
            "ticket": "WIKI-170",
            "orch": "wiki",
            "nodes": [
                {"id": "orch:wiki", "kind": "orchestrator"},
                {"id": "live", "kind": "implement", "worker_id": "WIKI-170"},
                *[
                    {"id": f"review{index}", "kind": "review", "worker_id": f"WIKI-170-REVIEW{index}"}
                    for index in range(1, 6)
                ],
            ],
            "edges": [
                {"kind": "spawn", "from": "orch:wiki", "to": "live", "created_at": ""},
                *[
                    {
                        "kind": "verdict",
                        "from": f"review{index}",
                        "to": f"review{index + 1}",
                        "created_at": "",
                    }
                    for index in range(1, 5)
                ],
            ],
        }
        archive = [
            {
                "ticket": "WIKI-170-REVIEW1",
                "orch": "wiki",
                "state": "closed",
                "role": "review",
                "kind": "cdx",
            },
            {
                "ticket": "WIKI-170-REVIEW2",
                "orch": "wiki",
                "state": "closed",
                "role": "review",
                "kind": "cdx",
            },
        ]
        with (
            mock.patch.object(main, "_read_agent_registry", return_value=registry),
            mock.patch.object(main, "read_agent_status", return_value={"state": "working"}),
            mock.patch.object(main, "list_archived", return_value=archive),
            mock.patch.object(main.graph_health, "load_validated_graph", return_value=(graph, "snapshot")),
        ):
            payload = main._fleet_graph_payload(limit=2)  # noqa: SLF001
            payload_without_archives = main._fleet_graph_payload(limit=0)  # noqa: SLF001

        tickets = payload["groups"][0]["tickets"]
        self.assertEqual(
            {ticket["ticket"] for ticket in tickets},
            {"WIKI-170", "WIKI-170-REVIEW1", "WIKI-170-REVIEW2"},
        )
        edge_text = repr([ticket["edges"] for ticket in tickets])
        self.assertNotIn("WIKI-170-REVIEW3", edge_text)
        self.assertNotIn("WIKI-170-REVIEW4", edge_text)
        self.assertNotIn("WIKI-170-REVIEW5", edge_text)
        self.assertEqual(
            {ticket["ticket"] for ticket in payload_without_archives["groups"][0]["tickets"]},
            {"WIKI-170"},
        )

    def test_legacy_n_orchestrator_node_keeps_live_edge_active(self) -> None:
        # This is the N-* node shape emitted by the existing workgraph writer
        # fixture, before the fleet view normalizes the orchestrator endpoint.
        legacy_graph = {
            "ticket": "TST-1",
            "orch": "wiki",
            "nodes": [
                {"id": "N-1", "kind": "orchestrator", "label": "wiki orch"},
                {"id": "N-2", "kind": "implement", "label": "TST-1", "worker_id": "TST-1"},
            ],
            "edges": [
                {
                    "kind": "spawn",
                    "from": "N-1",
                    "to": "N-2",
                    "created_at": "2026-07-29T12:00:00Z",
                }
            ],
        }
        tickets = main._fleet_graph_ticket(  # noqa: SLF001
            graph=legacy_graph,
            source="live",
            worker_metadata={"TST-1": {"state": "working", "role": "implement", "kind": "cdx"}},
            archived_metadata={},
            allowed_tickets={"TST-1"},
        )

        self.assertEqual(tickets[0]["edges"][0]["from"], "orch:wiki")
        self.assertTrue(tickets[0]["edges"][0]["active"])

    def test_live_worker_metadata_survives_missing_graph(self) -> None:
        registry = {
            "WIKI-172": {
                "current": {"orch": "wiki", "role": "implement", "kind": "cdx"}
            },
            "WIKI-172-REVIEW1": {
                "current": {"orch": "wiki", "role": "review", "kind": "cdx"}
            },
        }
        with (
            mock.patch.object(main, "_read_agent_registry", return_value=registry),
            mock.patch.object(main, "read_agent_status", return_value={"state": "working"}),
            mock.patch.object(main, "list_archived", return_value=[]),
            mock.patch.object(main.graph_health, "load_validated_graph", return_value=(None, None)),
        ):
            payload = main._fleet_graph_payload(limit=10)  # noqa: SLF001

        group = next(group for group in payload["groups"] if group["orch"] == "wiki")
        self.assertEqual(
            {ticket["ticket"] for ticket in group["tickets"]},
            {"WIKI-172", "WIKI-172-REVIEW1"},
        )
        self.assertTrue(all(ticket["edges"] == [] for ticket in group["tickets"]))


if __name__ == "__main__":
    unittest.main()
