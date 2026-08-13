"""Tests for the wiki fleet TUI (WIKI-290).

The interactive shell (curses) stays untested by design — everything
worth verifying is in pure sibling modules: dashboard payload parsing,
row/header formatting, filter matching, and the keymap navigation
reducer.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from wiki_cli.tui import format as fmt, keymap
from wiki_cli.tui.snapshot import (
    FleetSnapshot,
    WorkerRow,
    session_from_payload,
    snapshot_from_payload,
)


def _worker(**over) -> WorkerRow:
    base = dict(
        ticket="WIKI-100",
        base_ticket="WIKI-100",
        orch="wiki-dev",
        role="implement",
        kind="cc",
        model="opus-4.7",
        state="working",
        step="doing a thing",
        blocker=None,
        pr=None,
        worktree=None,
        status_age_s=5.0,
        alarms=(),
        review_round=None,
    )
    base.update(over)
    return WorkerRow(**base)


class SnapshotParseTests(unittest.TestCase):
    def test_empty_payload_returns_empty_snapshot(self) -> None:
        s = snapshot_from_payload({})
        self.assertEqual(s.groups, ())
        self.assertEqual(s.total_workers(), 0)
        self.assertFalse(s.stale)

    def test_orchestrator_group_and_worker_alarms_preserved(self) -> None:
        payload = {
            "workers": [
                {
                    "ticket": "PHO-1",
                    "base_ticket": "PHO-1",
                    "orch": "phoebe-dev",
                    "role": "review",
                    "kind": "cdx",
                    "state": "merge-ready",
                    "step": "verified at abc",
                    "alarms": ["merge-ready"],
                    "status_age_s": 33.0,
                    "pr": "https://github.com/x/y/pull/1",
                    "worktree": "/tmp/x",
                    "model": "gpt-5.6",
                    "review_round": 3,
                },
                {
                    "ticket": "WIKI-9",
                    "base_ticket": "WIKI-9",
                    "orch": "wiki-dev",
                    "role": "implement",
                    "kind": "cc",
                    "state": "working",
                    "step": "step1",
                    "alarms": [],
                    "status_age_s": 4.0,
                },
            ],
            "orchestrators": [
                {"orch": "wiki-dev", "total": 1, "working": 1, "idle": 0,
                 "merge_ready": 0, "blocked": 0, "stalled_or_failed": 0,
                 "waiting_approval": 0},
                {"orch": "phoebe-dev", "total": 1, "working": 0, "idle": 0,
                 "merge_ready": 1, "blocked": 0, "stalled_or_failed": 0,
                 "waiting_approval": 0},
            ],
            "generated_at": "2026-08-13T19:30:00+00:00",
        }
        s = snapshot_from_payload(payload)
        self.assertEqual([g.orch for g in s.groups], ["phoebe-dev", "wiki-dev"])
        self.assertEqual(s.total_workers(), 2)
        pho = s.groups[0]
        self.assertEqual(pho.rollup.merge_ready, 1)
        self.assertEqual(pho.workers[0].ticket, "PHO-1")
        self.assertIn("merge-ready", pho.workers[0].alarms)
        self.assertEqual(pho.workers[0].review_round, 3)
        wiki = s.groups[1]
        self.assertEqual(wiki.rollup.working, 1)
        self.assertEqual(s.find("WIKI-9").state, "working")

    def test_zero_worker_orch_still_renders_group(self) -> None:
        payload = {
            "workers": [],
            "orchestrators": [
                {"orch": "wiki-dev", "total": 0, "working": 0, "idle": 0,
                 "merge_ready": 0, "blocked": 0, "stalled_or_failed": 0,
                 "waiting_approval": 0},
            ],
        }
        s = snapshot_from_payload(payload)
        self.assertEqual(len(s.groups), 1)
        self.assertEqual(s.groups[0].workers, ())

    def test_unassigned_bucket_for_missing_orch(self) -> None:
        payload = {"workers": [{"ticket": "X-1", "orch": None, "state": "working"}]}
        s = snapshot_from_payload(payload)
        self.assertEqual(s.groups[0].orch, "(unassigned)")

    def test_alarm_urgent_sorts_ahead(self) -> None:
        payload = {"workers": [
            {"ticket": "T-1", "orch": "o", "state": "working", "alarms": []},
            {"ticket": "T-2", "orch": "o", "state": "blocked", "alarms": ["blocked"]},
            {"ticket": "T-3", "orch": "o", "state": "merge-ready", "alarms": ["merge-ready"]},
        ]}
        s = snapshot_from_payload(payload)
        tickets = [w.ticket for w in s.groups[0].workers]
        self.assertEqual(tickets, ["T-2", "T-3", "T-1"])

    def test_fetch_error_carried_through(self) -> None:
        s = snapshot_from_payload({}, stale=True, fetch_error="connection refused")
        self.assertTrue(s.stale)
        self.assertEqual(s.fetch_error, "connection refused")


class SessionParseTests(unittest.TestCase):
    def test_events_flattened_with_labels(self) -> None:
        payload = {
            "pr": "https://github.com/x/y/pull/9",
            "session_meta": {"state": "working", "step": "s", "blocker": None},
            "events": [
                {"kind": "user", "text": "hello", "ts": "2026-08-13T19:00:00Z"},
                {"kind": "tool", "tool": {"name": "Bash", "summary": "ls"}, "ts": "2026-08-13T19:00:01Z"},
                {"kind": "assistant", "text": "reply MERGE-READY: url", "ts": "2026-08-13T19:00:02Z"},
            ],
        }
        s = session_from_payload("PHO-1", payload)
        self.assertEqual(s.pr, "https://github.com/x/y/pull/9")
        self.assertEqual([e.label for e in s.events], ["user", "tool.Bash", "assistant"])
        self.assertEqual(s.events[1].text, "ls")
        self.assertIsNotNone(s.latest_verdict)
        self.assertIn("MERGE-READY", s.latest_verdict)


class FormatterTests(unittest.TestCase):
    def test_truncate_pad(self) -> None:
        self.assertEqual(fmt.truncate("hello world", 5), "hell…")
        self.assertEqual(fmt.truncate("hi", 5), "hi")
        self.assertEqual(fmt.pad("hi", 5), "hi   ")
        self.assertEqual(fmt.pad("very long", 5), "very…")

    def test_format_age(self) -> None:
        self.assertEqual(fmt.format_age(None), "—")
        self.assertEqual(fmt.format_age(45), "45s")
        self.assertEqual(fmt.format_age(90), "1m")
        self.assertEqual(fmt.format_age(3600), "1h")
        self.assertEqual(fmt.format_age(3720), "1h 02m")
        self.assertEqual(fmt.format_age(86400 * 2), "2d")

    def test_state_marker(self) -> None:
        self.assertEqual(fmt.state_marker("working", ()), ">>")
        self.assertEqual(fmt.state_marker("merge-ready", ("merge-ready",)), "MR")
        self.assertEqual(fmt.state_marker("blocked", ("blocked",)), "!!")
        self.assertEqual(fmt.state_marker("working", ("stale",)), "**")
        self.assertEqual(fmt.state_marker("working", ("waiting-approval",)), "??")
        self.assertEqual(fmt.state_marker("idle", ()), "..")

    def test_format_worker_row_includes_marker_ticket_step(self) -> None:
        w = _worker(ticket="WIKI-10", step="doing thing", state="working")
        line = fmt.format_worker_row(w, 100)
        self.assertIn("WIKI-10", line)
        self.assertIn(">>", line)
        self.assertIn("doing thing", line)
        self.assertIn("working", line)

    def test_format_worker_row_truncates_step(self) -> None:
        long = "s" * 500
        w = _worker(step=long)
        line = fmt.format_worker_row(w, 100)
        self.assertLessEqual(len(line), 100)
        self.assertTrue(line.rstrip().endswith("…"))

    def test_generated_at_age_from_iso(self) -> None:
        now = datetime(2026, 8, 13, 19, 30, 30, tzinfo=timezone.utc)
        self.assertEqual(
            fmt.format_generated_at("2026-08-13T19:30:25+00:00", now=now),
            "5s",
        )
        self.assertEqual(
            fmt.format_generated_at("2026-08-13T19:20:30+00:00", now=now),
            "10m",
        )

    def test_flatten_navigable(self) -> None:
        s = snapshot_from_payload({"workers": [
            {"ticket": "A-1", "orch": "o1", "state": "working"},
            {"ticket": "B-1", "orch": "o2", "state": "working"},
            {"ticket": "B-2", "orch": "o2", "state": "working"},
        ]})
        flat = fmt.flatten_navigable(s.groups)
        kinds = [k for k, _ in flat]
        self.assertEqual(kinds, ["group", "worker", "group", "worker", "worker"])


class KeymapTests(unittest.TestCase):
    def _snapshot(self, tickets: list[tuple[str, str]]) -> FleetSnapshot:
        # tickets = [(orch, ticket)]
        return snapshot_from_payload({"workers": [
            {"ticket": t, "orch": o, "state": "working"} for o, t in tickets
        ]})

    def test_filter_snapshot_narrows_and_drops_empty_groups(self) -> None:
        snap = self._snapshot([("o1", "A-1"), ("o2", "B-1"), ("o2", "B-2")])
        filtered = keymap.filter_snapshot(snap, "B-")
        self.assertEqual(len(filtered.groups), 1)
        self.assertEqual(filtered.groups[0].orch, "o2")
        self.assertEqual(len(filtered.groups[0].workers), 2)

    def test_matches_filter_case_insensitive(self) -> None:
        w = _worker(ticket="WIKI-10", step="build TUI")
        self.assertTrue(keymap.matches_filter(w, "wiki"))
        self.assertTrue(keymap.matches_filter(w, "tui"))
        self.assertFalse(keymap.matches_filter(w, "xxx"))

    def test_move_jumps_over_group_headers(self) -> None:
        snap = self._snapshot([("o1", "A-1"), ("o2", "B-1"), ("o2", "B-2")])
        entries = fmt.flatten_navigable(snap.groups)
        # entries: group,o1 / worker,A-1 / group,o2 / worker,B-1 / worker,B-2
        self.assertEqual([k for k, _ in entries], ["group", "worker", "group", "worker", "worker"])
        sel = keymap.Selection(index=1)  # on A-1
        sel = keymap.move(sel, entries, 1)
        self.assertEqual(sel.index, 3)  # skipped group at 2, landed on B-1
        sel = keymap.move(sel, entries, 1)
        self.assertEqual(sel.index, 4)  # B-2
        sel = keymap.move(sel, entries, 1)  # no more
        self.assertEqual(sel.index, 4)

    def test_move_up_skips_group_header(self) -> None:
        snap = self._snapshot([("o1", "A-1"), ("o2", "B-1")])
        entries = fmt.flatten_navigable(snap.groups)
        sel = keymap.Selection(index=3)  # on B-1
        sel = keymap.move(sel, entries, -1)
        self.assertEqual(sel.index, 1)  # A-1

    def test_jump_top_end_land_on_workers(self) -> None:
        snap = self._snapshot([("o1", "A-1"), ("o2", "B-1")])
        entries = fmt.flatten_navigable(snap.groups)
        self.assertEqual(keymap.jump_top(entries), 1)
        self.assertEqual(keymap.jump_end(entries), 3)

    def test_selected_worker_returns_none_on_group(self) -> None:
        snap = self._snapshot([("o1", "A-1")])
        sel = keymap.Selection(index=0)  # on group header
        self.assertIsNone(keymap.selected_worker(sel, snap))
        sel = keymap.Selection(index=1)
        w = keymap.selected_worker(sel, snap)
        self.assertIsNotNone(w)
        self.assertEqual(w.ticket, "A-1")


class CLIWiringTests(unittest.TestCase):
    """`wiki tui --help` succeeds and the subcommand is registered."""

    def test_cli_tui_help(self) -> None:
        import subprocess
        import sys
        from pathlib import Path

        repo = Path(__file__).resolve().parents[2]
        proc = subprocess.run(
            [sys.executable, str(repo / "wiki"), "tui", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("usage: wiki tui", proc.stdout)
        self.assertIn("--backend", proc.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
