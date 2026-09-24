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
            worker_metadata={
                "WIKI-170": {"state": "working", "role": "implement", "kind": "cdx"},
                "WIKI-170-REVIEW1": {"state": "merge-ready", "role": "review", "kind": "cc"},
            },
            allowed_tickets={"WIKI-170", "WIKI-170-REVIEW1"},
        )

        self.assertEqual({ticket["ticket"] for ticket in tickets}, {"WIKI-170", "WIKI-170-REVIEW1"})
        edges = [edge for ticket in tickets for edge in ticket["edges"]]
        live_edge = next(edge for edge in edges if edge["to"] == "WIKI-170")
        self.assertEqual(live_edge["from"], "orch:wiki")
        self.assertTrue(live_edge["active"])
        archived_edge = next(edge for edge in edges if edge["kind"] == "archive")
        self.assertFalse(archived_edge["active"])

    def test_payload_groups_live_workers_without_archive_read(self) -> None:
        registry = {
            "WIKI-170": {
                "current": {"orch": "wiki", "role": "implement", "kind": "cdx"}
            },
            "PHO-8": {
                "current": {"orch": "phoebe", "role": "review", "kind": "cc"}
            },
        }
        with (
            mock.patch.object(main, "_read_agent_registry", return_value=registry),
            mock.patch.object(main, "read_agent_status", return_value={"state": "working"}),
            mock.patch.object(main, "list_archived", side_effect=AssertionError("fleet read archive")),
            mock.patch.object(main.graph_health, "load_validated_graph", return_value=(None, None)),
        ):
            payload = main._fleet_graph_payload()  # noqa: SLF001

        self.assertEqual([group["orch"] for group in payload["groups"]], ["phoebe", "wiki"])
        self.assertEqual(payload["groups"][0]["tickets"][0]["ticket"], "PHO-8")
        self.assertEqual(payload["groups"][1]["tickets"][0]["state"], "working")
        self.assertIsInstance(payload["updated_at_ns"], int)

    def test_historical_graph_nodes_and_edges_stay_out_of_live_fleet(self) -> None:
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
        with (
            mock.patch.object(main, "_read_agent_registry", return_value=registry),
            mock.patch.object(main, "read_agent_status", return_value={"state": "working"}),
            mock.patch.object(main, "list_archived", side_effect=AssertionError("fleet read archive")),
            mock.patch.object(main.graph_health, "load_validated_graph", return_value=(graph, "snapshot")),
        ):
            payload = main._fleet_graph_payload()  # noqa: SLF001

        tickets = payload["groups"][0]["tickets"]
        self.assertEqual({ticket["ticket"] for ticket in tickets}, {"WIKI-170"})
        edge_text = repr([ticket["edges"] for ticket in tickets])
        self.assertNotIn("WIKI-170-REVIEW3", edge_text)
        self.assertNotIn("WIKI-170-REVIEW4", edge_text)
        self.assertNotIn("WIKI-170-REVIEW5", edge_text)

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
            mock.patch.object(main, "list_archived", side_effect=AssertionError("fleet read archive")),
            mock.patch.object(main.graph_health, "load_validated_graph", return_value=(None, None)),
        ):
            payload = main._fleet_graph_payload()  # noqa: SLF001

        group = next(group for group in payload["groups"] if group["orch"] == "wiki")
        self.assertEqual(
            {ticket["ticket"] for ticket in group["tickets"]},
            {"WIKI-172", "WIKI-172-REVIEW1"},
        )
        self.assertTrue(all(ticket["edges"] == [] for ticket in group["tickets"]))


if __name__ == "__main__":
    unittest.main()
