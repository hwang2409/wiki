"""Durable store of unresolved provider-health notices.

Account events (usage limits, rotation failures, dead auth, failed worker
revivals) arrive over SSE only while the app is open. This store keeps the
unresolved ones on disk so the action guidance survives an app reload, and
clears a notice only when a matching recovery event proves resolution.

Worker-scoped failures (dead-auth exhaustion, failed revivals) are tracked
per ticket inside one aggregated notice per failure kind: new events merge
their tickets in, and a recovery event removes only the tickets it proves
revived. Fleet-wide conditions (usage-limit deadlock, rotation failure)
stay single-keyed.

Two lifecycle gaps remain, both tracked as WIKI-228 (durable provider-health
lifecycle in the supervisor): (a) the headless supervisor never emits
``claude_limit_cleared``, so a Claude usage-limit notice only clears when
this store reconciles it away against the live registry (replaced/archived
ticket) — automatic reset detection lives in WIKI-228; (b) failure events
that fire while the native backend is offline never reach the SSE bridge,
so events during that window are lost. WIKI-228 will own state in the
supervisor and replay from a cursor on reconnect. Reconciliation here is
the near-term truthful behavior: a notice for a ticket that no longer
matches a live run is dropped on the next ``/api/agents`` refresh.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path


def _default_path() -> Path:
    override = os.environ.get("WIKI_ACCOUNT_NOTICES_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".wiki" / "account-notices.json"


def _ticket_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, str) and entry]


def _reason_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        ticket: reason
        for ticket, reason in value.items()
        if isinstance(ticket, str) and isinstance(reason, str)
    }


_EXHAUSTED_KEY = "codex:auth-exhausted"
_REVIVE_FAILED_KEYS = {
    "codex_rotation": "codex:rotation-revive-failed",
    "codex_auth_dead_revival": "codex:auth-revive-failed",
}


# Required shape check per notice type. AccountEventsBanner has an unchecked
# switch on ``type``, so an unknown or malformed payload leaked from the
# on-disk store would crash the Runs view. On load we drop anything that
# doesn't match these shapes.
def _valid_notice(kind: str, payload: dict) -> bool:
    if kind == "codex_limit_no_eligible":
        return isinstance(payload.get("tickets"), list)
    if kind == "codex_rotation_failed":
        return isinstance(payload.get("error"), str)
    if kind == "codex_rotation":
        return (
            isinstance(payload.get("revived"), list)
            and isinstance(payload.get("failed"), list)
        )
    if kind == "codex_auth_dead_revival":
        return (
            isinstance(payload.get("revived"), list)
            and isinstance(payload.get("failed"), list)
        )
    if kind == "codex_auth_dead_exhausted":
        return isinstance(payload.get("tickets"), list)
    if kind == "claude_limit_hit":
        return isinstance(payload.get("ticket"), str) and bool(payload.get("ticket"))
    return False


class AccountNoticeStore:
    """Keyed unresolved notices with recovery-event resolution."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_path()
        self._lock = threading.Lock()
        self._notices: dict[str, dict] = self._load()
        # Monotonic counter that bumps on every state-changing event. The
        # /api/agents flow captures this revision BEFORE reading the
        # registry, then passes it to reconcile_with_live so that a notice
        # published between the registry read and the reconcile is not
        # silently deleted. See main.py agents().
        self._revision: int = 0

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        loaded: dict[str, dict] = {}
        for key, value in data.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            kind = value.get("type")
            if not isinstance(kind, str):
                continue
            # An unknown or shape-invalid payload would reach the frontend
            # AccountEventsBanner switch, which is exhaustive on the known
            # event types and would crash the Runs view for anything else.
            if not _valid_notice(kind, value):
                continue
            loaded[key] = value
        return loaded

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self._notices, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            # Persistence is best-effort; in-memory state stays authoritative.
            pass

    @property
    def revision(self) -> int:
        """Monotonic snapshot marker of the notice store (see __init__)."""

        with self._lock:
            return self._revision

    def apply_event(self, event: dict) -> bool:
        """Set or resolve notices from one SSE event. True when state changed."""

        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return False
        with self._lock:
            changed = self._apply_locked(event)
            if changed:
                self._revision += 1
                self._persist()
            return changed

    def _apply_locked(self, event: dict) -> bool:
        kind = event["type"]
        changed = False

        def set_notice(key: str, payload: dict) -> None:
            nonlocal changed
            self._notices[key] = payload
            changed = True

        def clear(*keys: str) -> None:
            nonlocal changed
            for key in keys:
                if self._notices.pop(key, None) is not None:
                    changed = True

        def merge_exhausted(tickets: list[str]) -> None:
            existing = self._notices.get(_EXHAUSTED_KEY)
            merged = sorted(
                set(_ticket_list(existing.get("tickets")) if existing else [])
                | set(tickets)
            )
            run_ids = dict(_reason_map(existing.get("run_ids")) if existing else {})
            run_ids.update(_reason_map(event.get("run_ids")))
            run_ids = {ticket: rid for ticket, rid in run_ids.items() if ticket in set(merged)}
            payload = dict(event)
            payload["tickets"] = merged
            payload["run_ids"] = run_ids
            set_notice(_EXHAUSTED_KEY, payload)

        def remove_revived(revived: list[str]) -> None:
            """A revived worker is proven running: drop it from every
            worker-scoped failure notice; keep tickets not yet proven."""

            if not revived:
                return
            revived_set = set(revived)
            existing = self._notices.get(_EXHAUSTED_KEY)
            if existing is not None:
                remaining = [
                    ticket
                    for ticket in _ticket_list(existing.get("tickets"))
                    if ticket not in revived_set
                ]
                if remaining != _ticket_list(existing.get("tickets")):
                    if remaining:
                        updated = dict(existing)
                        updated["tickets"] = remaining
                        updated["run_ids"] = {
                            ticket: rid
                            for ticket, rid in _reason_map(existing.get("run_ids")).items()
                            if ticket in remaining
                        }
                        set_notice(_EXHAUSTED_KEY, updated)
                    else:
                        clear(_EXHAUSTED_KEY)
            for key in _REVIVE_FAILED_KEYS.values():
                notice = self._notices.get(key)
                if notice is None:
                    continue
                remaining = [
                    ticket
                    for ticket in _ticket_list(notice.get("failed"))
                    if ticket not in revived_set
                ]
                if remaining == _ticket_list(notice.get("failed")):
                    continue
                if remaining:
                    updated = dict(notice)
                    updated["failed"] = remaining
                    updated["failed_reasons"] = {
                        ticket: reason
                        for ticket, reason in _reason_map(notice.get("failed_reasons")).items()
                        if ticket in remaining
                    }
                    updated["failed_run_ids"] = {
                        ticket: rid
                        for ticket, rid in _reason_map(notice.get("failed_run_ids")).items()
                        if ticket in remaining
                    }
                    set_notice(key, updated)
                else:
                    clear(key)

        def merge_revive_failures(key: str) -> None:
            failed = _ticket_list(event.get("failed"))
            if not failed:
                return
            existing = self._notices.get(key)
            prior_failed = _ticket_list(existing.get("failed")) if existing else []
            prior_reasons = _reason_map(existing.get("failed_reasons")) if existing else {}
            prior_run_ids = _reason_map(existing.get("failed_run_ids")) if existing else {}
            merged_failed = sorted(set(prior_failed) | set(failed))
            merged_reasons = {**prior_reasons, **_reason_map(event.get("failed_reasons"))}
            merged_run_ids = {**prior_run_ids, **_reason_map(event.get("failed_run_ids"))}
            payload = dict(event)
            payload["failed"] = merged_failed
            payload["failed_reasons"] = {
                ticket: reason
                for ticket, reason in merged_reasons.items()
                if ticket in set(merged_failed)
            }
            payload["failed_run_ids"] = {
                ticket: rid
                for ticket, rid in merged_run_ids.items()
                if ticket in set(merged_failed)
            }
            set_notice(key, payload)

        if kind == "codex_limit_no_eligible":
            set_notice("codex:limit", dict(event))
        elif kind == "codex_rotation_failed":
            set_notice("codex:rotation-failed", dict(event))
        elif kind == "codex_auth_dead_exhausted":
            merge_exhausted(_ticket_list(event.get("tickets")))
        elif kind in ("codex_rotation", "codex_auth_dead_revival"):
            if kind == "codex_rotation":
                # A completed rotation moved onto an eligible account: the
                # limit deadlock and any prior rotation failure are resolved.
                clear("codex:limit", "codex:rotation-failed")
            remove_revived(_ticket_list(event.get("revived")))
            merge_revive_failures(_REVIVE_FAILED_KEYS[kind])
        elif kind == "codex_auth_verified":
            # Per-ticket recovery only: a successful Codex turn proves the run
            # that emitted it can authenticate, not that every exhausted or
            # failed worker recovered. A missing ticket (legacy events) is
            # ignored so we never clear another worker's unresolved failure.
            if event.get("success") is True and event.get("credential_source") == "current":
                ticket = event.get("ticket")
                if isinstance(ticket, str) and ticket:
                    remove_revived([ticket])
        elif kind == "claude_limit_hit":
            ticket = event.get("ticket")
            if isinstance(ticket, str) and ticket:
                set_notice(f"claude:limit:{ticket}", dict(event))
        elif kind == "claude_limit_cleared":
            # The only Claude recovery proof: the watchdog observed the limit
            # banner gone from a previously limited worker. Generic session
            # events (queue edits, model changes) must never resolve limits.
            ticket = event.get("ticket")
            if isinstance(ticket, str) and ticket:
                clear(f"claude:limit:{ticket}")

        return changed

    def reconcile_with_live(
        self,
        live_runs: dict[str, str | None],
        *,
        expected_revision: int | None = None,
    ) -> bool:
        """Drop worker-scoped notice tickets that no longer match a live run.

        ``expected_revision`` guards against a TOCTOU race between the
        registry snapshot and this call: publish_agent_event may apply a new
        failure notice for a run that started or was replaced during that
        window. That fresh notice carries the new run_id, but the caller's
        ``live_runs`` was built before it landed and would treat the ticket
        as absent (or under the wrong run_id) and drop the notice. When the
        caller captures ``self.revision`` before the registry read and passes
        it here, a mismatch means at least one event landed after the
        snapshot — bail and let the next /api/agents refresh reconcile from
        a consistent pair.

        ``live_runs`` maps every live ticket to its current run_id (or None
        for legacy tmux entries without a run_id). A notice ticket clears
        when the ticket is absent from ``live_runs`` (archived) or when the
        notice recorded a run_id that no longer matches the live run_id
        (replaced). Notices without a recorded run_id — e.g. legacy events
        emitted before this contract — are only cleared on the archive
        path; without the run_id we can't distinguish replace from steady
        state.

        Successful replace and archive flows publish only session/agents
        events, and those events are (correctly) ignored by ``apply_event``.
        Without reconciliation the operator would follow the stated fix and
        the banner would remain forever. This runs on every ``/api/agents``
        refresh so replaced or archived tickets clear promptly. Fleet-wide
        notices (limit deadlock, rotation failure) are not per-ticket, so
        they are untouched. See WIKI-228 for the durable event-driven
        lifecycle that will supersede this reconciliation.
        """

        def _ticket_matches_live(ticket: str, stored_run_id: str | None) -> bool:
            if ticket not in live_runs:
                return False
            live_run_id = live_runs[ticket]
            if stored_run_id and live_run_id and stored_run_id != live_run_id:
                return False
            return True

        with self._lock:
            if expected_revision is not None and self._revision != expected_revision:
                # Something landed after the caller's snapshot. Preserving
                # notices is always safe; the next refresh reconciles from
                # a paired revision.
                return False
            changed = False
            existing = self._notices.get(_EXHAUSTED_KEY)
            if existing is not None:
                current_tickets = _ticket_list(existing.get("tickets"))
                stored_run_ids = _reason_map(existing.get("run_ids"))
                remaining = [
                    t
                    for t in current_tickets
                    if _ticket_matches_live(t, stored_run_ids.get(t))
                ]
                if remaining != current_tickets:
                    changed = True
                    if remaining:
                        updated = dict(existing)
                        updated["tickets"] = remaining
                        updated["run_ids"] = {
                            ticket: rid
                            for ticket, rid in stored_run_ids.items()
                            if ticket in remaining
                        }
                        self._notices[_EXHAUSTED_KEY] = updated
                    else:
                        self._notices.pop(_EXHAUSTED_KEY, None)
            for key in _REVIVE_FAILED_KEYS.values():
                notice = self._notices.get(key)
                if notice is None:
                    continue
                current_failed = _ticket_list(notice.get("failed"))
                stored_run_ids = _reason_map(notice.get("failed_run_ids"))
                remaining = [
                    t
                    for t in current_failed
                    if _ticket_matches_live(t, stored_run_ids.get(t))
                ]
                if remaining == current_failed:
                    continue
                changed = True
                if remaining:
                    updated = dict(notice)
                    updated["failed"] = remaining
                    updated["failed_reasons"] = {
                        ticket: reason
                        for ticket, reason in _reason_map(notice.get("failed_reasons")).items()
                        if ticket in remaining
                    }
                    updated["failed_run_ids"] = {
                        ticket: rid
                        for ticket, rid in stored_run_ids.items()
                        if ticket in remaining
                    }
                    self._notices[key] = updated
                else:
                    self._notices.pop(key, None)
            # Claude limit notices reconcile the same way as codex worker-
            # scoped notices: absent ticket → dropped (archive), same ticket
            # with a different live run_id → dropped (replace). The headless
            # supervisor now emits ``run_id`` on ``claude_limit_hit``; legacy
            # tmux emissions carry no run_id and reconcile on ticket-only,
            # matching the codex legacy path.
            for key in list(self._notices.keys()):
                if not key.startswith("claude:limit:"):
                    continue
                ticket = key.split(":", 2)[2]
                notice = self._notices.get(key) or {}
                stored_run_id = notice.get("run_id") if isinstance(notice.get("run_id"), str) else None
                if _ticket_matches_live(ticket, stored_run_id):
                    continue
                self._notices.pop(key, None)
                changed = True
            if changed:
                self._revision += 1
                self._persist()
            return changed

    def snapshot(self) -> list[dict]:
        """Unresolved notices, newest first."""

        with self._lock:
            entries = list(self._notices.values())
        return sorted(
            (dict(entry) for entry in entries),
            key=lambda entry: str(entry.get("ts") or ""),
            reverse=True,
        )
