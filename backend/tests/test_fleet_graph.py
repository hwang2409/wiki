"""WIKI-170: bounded fleet graph roll-up and edge classification."""

from __future__ import annotations

import unittest
from unittest import mock

from backend.app import main


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


class FleetGraphTests(unittest.TestCase):
    def test_normalizes_group_and_active_historical_edges(self) -> None:
        tickets = main._fleet_graph_ticket(  # noqa: SLF001
            graph=GRAPH,
            source="live",
            worker_metadata={"WIKI-170": {"state": "working", "role": "implement", "kind": "cdx"}},
            archived_metadata={"WIKI-170-REVIEW1": {"state": "merge-ready", "role": "review", "kind": "cc"}},
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


if __name__ == "__main__":
    unittest.main()
