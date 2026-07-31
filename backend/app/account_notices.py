"""Durable store of unresolved provider-health notices.

Account events (usage limits, rotation failures, dead auth, failed worker
revivals) arrive over SSE only while the app is open. This store keeps the
unresolved ones on disk so the action guidance survives an app reload, and
clears a notice only when a matching recovery event proves resolution.

Worker-scoped failures (dead-auth exhaustion, failed revivals) are tracked
per ticket inside one aggregated notice per failure kind: new events merge
their tickets in, and a recovery event removes only the tickets it proves
revived. Codex fleet conditions use one notice key per condition, with the
affected tickets and run identities tracked inside that notice.

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
_MUTATING_NOTICE_TYPES = frozenset(
    {
        "codex_limit_no_eligible",
        "codex_rotation_failed",
        "codex_rotation",
        "codex_auth_dead_revival",
        "codex_auth_dead_exhausted",
        "claude_limit_hit",
    }
)


# Complete per-type schema validation. AccountEventsBanner has an exhaustive
# switch on ``type`` and reads specific fields on each branch, so a payload
# with the right ``type`` but the wrong shape crashes the Runs view. Every
# ticket, revived, and failed list item must be a non-empty string; reason
# maps and per-ticket run_id maps must be string-to-string; every required
# scalar (e.g. codex_rotation.to) must be present. Anything else is dropped
# safely at load time.
def _all_non_empty_strings(value: object) -> bool:
    if not isinstance(value, list):
        return False
    return all(isinstance(item, str) and bool(item) for item in value)


def _all_string_to_string(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return all(
        isinstance(key, str) and bool(key) and isinstance(val, str)
        for key, val in value.items()
    )


def _optional_string_map(value: object) -> bool:
    return value is None or _all_string_to_string(value)


def _optional_non_empty_string(value: object) -> bool:
    return value is None or (isinstance(value, str) and bool(value))


def _valid_common_fields(payload: dict, *, optional_fields: tuple[str, ...] = ()) -> bool:
    """Validate fields shared by provider-health notices.

    Older tmux events omit provider metadata, so those fields stay optional.
    When present, every known field still needs its persisted scalar type.
    """

    if not isinstance(payload.get("ts"), str) or not payload["ts"]:
        return False
    for field in optional_fields:
        if field in payload and not _optional_non_empty_string(payload[field]):
            return False
    if "exhausted" in payload and not isinstance(payload["exhausted"], bool):
        return False
    return True


def _valid_notice(kind: str, payload: dict) -> bool:
    if kind == "codex_limit_no_eligible":
        return _valid_common_fields(payload, optional_fields=("provider", "failure", "credential_source")) and (
            _all_non_empty_strings(payload.get("tickets"))
            or (
                # tickets may be [] when the emitter had no live workers yet.
                isinstance(payload.get("tickets"), list)
                and len(payload["tickets"]) == 0
            )
        ) and (
            "reset_at" in payload
            and (
                payload["reset_at"] is None
                or _optional_non_empty_string(payload["reset_at"])
            )
        ) and _optional_string_map(payload.get("run_ids"))
    if kind == "codex_rotation_failed":
        error = payload.get("error")
        tickets = payload.get("tickets")
        return (
            _valid_common_fields(payload)
            and isinstance(error, str)
            and bool(error)
            and ("tickets" not in payload or _all_non_empty_strings(tickets) or tickets == [])
            and _optional_string_map(payload.get("run_ids"))
        )
    if kind == "codex_rotation":
        revived = payload.get("revived")
        failed = payload.get("failed")
        to_value = payload.get("to")
        if not _valid_common_fields(payload, optional_fields=("provider", "failure", "credential_source")):
            return False
        if "from" not in payload:
            return False
        if payload["from"] is not None and (
            not isinstance(payload["from"], str) or not payload["from"]
        ):
            return False
        if not isinstance(to_value, str) or not to_value:
            return False
        if not isinstance(revived, list) or not all(isinstance(t, str) and t for t in revived):
            return False
        if not isinstance(failed, list) or not all(isinstance(t, str) and t for t in failed):
            return False
        if not _optional_string_map(payload.get("failed_reasons")):
            return False
        if not _optional_string_map(payload.get("failed_run_ids")):
            return False
        if not _optional_string_map(payload.get("revived_run_ids")):
            return False
        return True
    if kind == "codex_auth_dead_revival":
        revived = payload.get("revived")
        failed = payload.get("failed")
        if not _valid_common_fields(payload, optional_fields=("provider", "failure", "credential_source")):
            return False
        if not isinstance(revived, list) or not all(isinstance(t, str) and t for t in revived):
            return False
        if not isinstance(failed, list) or not all(isinstance(t, str) and t for t in failed):
            return False
        if not _optional_string_map(payload.get("failed_reasons")):
            return False
        if not _optional_string_map(payload.get("failed_run_ids")):
            return False
        if not _optional_string_map(payload.get("revived_run_ids")):
            return False
        return True
    if kind == "codex_auth_dead_exhausted":
        if not _valid_common_fields(payload, optional_fields=("provider", "failure", "credential_source")):
            return False
        if not _all_non_empty_strings(payload.get("tickets")):
            return False
        if not _optional_string_map(payload.get("run_ids")):
            return False
        return True
    if kind == "claude_limit_hit":
        if not _valid_common_fields(payload, optional_fields=("provider",)):
            return False
        ticket = payload.get("ticket")
        if not isinstance(ticket, str) or not ticket:
            return False
        if not isinstance(payload.get("window"), str):
            return False
        run_id = payload.get("run_id")
        if run_id is not None and (not isinstance(run_id, str) or not run_id):
            return False
        provider = payload.get("provider")
        if provider is not None and not isinstance(provider, str):
            return False
        return True
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
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._path.parent.chmod(0o700)
            tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    fd = -1
                    handle.write(json.dumps(self._notices, indent=2))
            finally:
                if fd >= 0:
                    os.close(fd)
            tmp.chmod(0o600)
            tmp.replace(self._path)
            self._path.chmod(0o600)
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
        kind = event["type"]
        if kind in _MUTATING_NOTICE_TYPES and not _valid_notice(kind, event):
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

        def merge_codex_fleet_notice(key: str) -> None:
            existing = self._notices.get(key)
            prior_tickets = _ticket_list(existing.get("tickets")) if existing else []
            event_tickets = _ticket_list(event.get("tickets"))
            has_tickets = (
                (existing is not None and "tickets" in existing)
                or "tickets" in event
            )
            merged_tickets = sorted(set(prior_tickets) | set(event_tickets))
            prior_run_ids = _reason_map(existing.get("run_ids")) if existing else {}
            merged_run_ids = {**prior_run_ids, **_reason_map(event.get("run_ids"))}
            payload = dict(event)
            if has_tickets:
                payload["tickets"] = merged_tickets
                payload["run_ids"] = {
                    ticket: run_id
                    for ticket, run_id in merged_run_ids.items()
                    if ticket in set(merged_tickets)
                }
            set_notice(key, payload)

        def remove_revived(
            revived: list[str],
            *,
            revived_run_ids: object = None,
        ) -> None:
            """A revived worker is proven running: drop it from every
            worker-scoped failure notice; keep tickets not yet proven.

            Events with ``revived_run_ids`` are run-scoped. Legacy events that
            omit that map keep the ticket-only fallback for old tmux workers.
            """

            if not revived:
                return
            revived_set = set(revived)

            recovery_ids = _reason_map(revived_run_ids)

            def can_clear(ticket: str, stored_run_ids: object) -> bool:
                if revived_run_ids is None:
                    return True
                recovery_run_id = recovery_ids.get(ticket)
                stored_run_id = _reason_map(stored_run_ids).get(ticket)
                return bool(recovery_run_id and stored_run_id == recovery_run_id)

            existing = self._notices.get(_EXHAUSTED_KEY)
            if existing is not None:
                remaining = [
                    ticket
                    for ticket in _ticket_list(existing.get("tickets"))
                    if ticket not in revived_set
                    or not can_clear(ticket, existing.get("run_ids"))
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
                    or not can_clear(ticket, notice.get("failed_run_ids"))
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

        def remove_codex_fleet_ticket(
            ticket: str,
            run_id: str | None,
            *,
            allow_legacy: bool = False,
        ) -> None:
            """Clear a recovered run from Codex fleet notices only."""

            if run_id is None and not allow_legacy:
                return
            for key in ("codex:limit", "codex:rotation-failed"):
                notice = self._notices.get(key)
                if notice is None:
                    continue
                stored_run_id = _reason_map(notice.get("run_ids")).get(ticket)
                if run_id is not None and stored_run_id != run_id:
                    continue
                if run_id is None and stored_run_id is not None:
                    continue
                remaining = [
                    current_ticket
                    for current_ticket in _ticket_list(notice.get("tickets"))
                    if current_ticket != ticket
                ]
                if remaining:
                    updated = dict(notice)
                    updated["tickets"] = remaining
                    updated["run_ids"] = {
                        current_ticket: stored_id
                        for current_ticket, stored_id in _reason_map(notice.get("run_ids")).items()
                        if current_ticket in remaining
                    }
                    set_notice(key, updated)
                else:
                    clear(key)

        def remove_codex_fleet_revived(
            revived: list[str],
            *,
            revived_run_ids: object = None,
        ) -> None:
            """Clear only fleet tickets proven recovered by this event."""

            if not revived:
                return
            revived_set = set(revived)
            recovery_ids = _reason_map(revived_run_ids)
            for key in ("codex:limit", "codex:rotation-failed"):
                notice = self._notices.get(key)
                if notice is None:
                    continue
                if "tickets" not in notice:
                    clear(key)
                    continue
                stored_run_ids = _reason_map(notice.get("run_ids"))
                remaining = [
                    ticket
                    for ticket in _ticket_list(notice.get("tickets"))
                    if ticket not in revived_set
                    or (
                        revived_run_ids is not None
                        and (
                            not recovery_ids.get(ticket)
                            or stored_run_ids.get(ticket) != recovery_ids[ticket]
                        )
                    )
                ]
                if remaining == _ticket_list(notice.get("tickets")):
                    continue
                if remaining:
                    updated = dict(notice)
                    updated["tickets"] = remaining
                    updated["run_ids"] = {
                        ticket: run_id
                        for ticket, run_id in stored_run_ids.items()
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
            merge_codex_fleet_notice("codex:limit")
        elif kind == "codex_rotation_failed":
            merge_codex_fleet_notice("codex:rotation-failed")
        elif kind == "codex_auth_dead_exhausted":
            merge_exhausted(_ticket_list(event.get("tickets")))
        elif kind in ("codex_rotation", "codex_auth_dead_revival"):
            if kind == "codex_rotation":
                remove_codex_fleet_revived(
                    _ticket_list(event.get("revived")),
                    revived_run_ids=event.get("revived_run_ids")
                    if "revived_run_ids" in event
                    else None,
                )
            remove_revived(
                _ticket_list(event.get("revived")),
                revived_run_ids=event.get("revived_run_ids")
                if "revived_run_ids" in event
                else None,
            )
            merge_revive_failures(_REVIVE_FAILED_KEYS[kind])
        elif kind == "codex_auth_verified":
            # Per-ticket recovery only: a successful Codex turn proves the run
            # that emitted it can authenticate, not that every exhausted or
            # failed worker recovered. A missing ticket (legacy events) is
            # ignored so we never clear another worker's unresolved failure.
            if event.get("success") is True and event.get("credential_source") == "current":
                ticket = event.get("ticket")
                if isinstance(ticket, str) and ticket:
                    run_id = event.get("run_id")
                    remove_revived(
                        [ticket],
                        revived_run_ids={ticket: run_id}
                        if isinstance(run_id, str) and run_id
                        else None,
                    )
                    remove_codex_fleet_ticket(
                        ticket,
                        run_id if isinstance(run_id, str) and run_id else None,
                    )
        elif kind == "codex_limit_cleared":
            ticket = event.get("ticket")
            if isinstance(ticket, str) and ticket:
                remove_codex_fleet_ticket(ticket, None, allow_legacy=True)
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
        live_providers: dict[str, str | None] | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        """Drop notice tickets that no longer match a live run.

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
        refresh so replaced or archived tickets clear promptly. Codex fleet
        notices carry affected tickets and run ids, so provider identity also
        clears a stale notice after a Claude replacement.
        """

        def _ticket_matches_live(
            ticket: str,
            stored_run_id: str | None,
            *,
            expected_provider: str | None = None,
        ) -> bool:
            if ticket not in live_runs:
                return False
            if expected_provider and live_providers is not None:
                live_provider = live_providers.get(ticket)
                if live_provider is not None and live_provider != expected_provider:
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
            for key in ("codex:limit", "codex:rotation-failed"):
                notice = self._notices.get(key)
                if notice is None or "tickets" not in notice:
                    continue
                current_tickets = _ticket_list(notice.get("tickets"))
                stored_run_ids = _reason_map(notice.get("run_ids"))
                if not current_tickets:
                    self._notices.pop(key, None)
                    changed = True
                    continue
                remaining = [
                    ticket
                    for ticket in current_tickets
                    if _ticket_matches_live(
                        ticket,
                        stored_run_ids.get(ticket),
                        expected_provider="codex",
                    )
                ]
                if remaining == current_tickets:
                    continue
                changed = True
                if remaining:
                    updated = dict(notice)
                    updated["tickets"] = remaining
                    updated["run_ids"] = {
                        ticket: run_id
                        for ticket, run_id in stored_run_ids.items()
                        if ticket in remaining
                    }
                    self._notices[key] = updated
                else:
                    self._notices.pop(key, None)

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
                if _ticket_matches_live(ticket, stored_run_id, expected_provider="claude"):
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
