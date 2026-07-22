---
type: reference
tags: [wiki, agents, harness, spec]
created: 2026-07-22
updated: 2026-07-22
---

# Spec: Graph Engineering Upgrade for Orchestrator/Worker Stack

Owner: wiki orchestrator. Consumers: mastermind (Phoebe), wiki, tooling orchestrators. Reference concept: [[graph-engineering]]. Reference protocol being upgraded: [[orchestrator-worker-protocol]].

## Goal

Turn the current prose-driven orchestrator/worker system into a first-class graph engineering stack. Concretely:

1. Every verification-worker output becomes a typed `Finding[]`.
2. Every orchestrator action (spawn / steer / verdict / archive / handoff) becomes a typed edge in a persistent per-ticket `workgraph.json` DAG.
3. Every ticket kind gets a work-graph template.
4. Zone isolation moves from convention to runtime enforcement.
5. A cross-orchestrator meta-registry replaces per-orch watchlists.

Non-goals: replacing LangGraph / adopting a graph library. This spec keeps everything in plain JSON on disk under `/tmp/agent-status/` and `~/.wiki/`, extending existing conventions rather than introducing a runtime.

## Five deliverables (ordered by ROI)

D1. `Finding` + edge-payload JSON schemas + linter.
D2. Per-ticket `workgraph.json` + wiki-app renderer + composite-health monitor.
D3. Work-graph templates per ticket kind.
D4. Worker-side pre-tool zone-isolation hook.
D5. Cross-orchestrator meta-registry + global `/agents` view.

Ship in D1 → D2 → D3 → D4 → D5 order. D1 must land first because D2 and D3 both reference its schemas.

## D1 — Schemas + linter

### 1.1 Files

- `~/me/fun/wiki/schemas/finding.schema.json`
- `~/me/fun/wiki/schemas/verdict.schema.json`
- `~/me/fun/wiki/schemas/steer.schema.json`
- `~/me/fun/wiki/schemas/edge.schema.json`
- `~/me/fun/wiki/schemas/workgraph.schema.json`
- `~/me/fun/wiki/wiki_cli/graph_lint.py`
- `~/me/fun/wiki/wiki` — add subcommand `wiki graph lint <path>`

### 1.2 Finding schema (JSON Schema draft 2020-12)

```json
{
  "$id": "https://wiki.henry/schemas/finding.json",
  "type": "object",
  "required": ["id", "severity", "title", "observed", "why_wrong", "do_instead", "source_worker", "source_sha", "created_at"],
  "properties": {
    "id":            {"type": "string", "pattern": "^F-[a-z0-9]{6}$"},
    "severity":      {"enum": ["BLOCKING", "HIGH", "MEDIUM", "LOW", "INFO"]},
    "title":         {"type": "string", "maxLength": 140},
    "file":          {"type": "string"},
    "line":          {"type": "integer", "minimum": 1},
    "observed":      {"type": "string"},
    "why_wrong":     {"type": "string"},
    "do_instead":    {"type": "string"},
    "constraint":    {"type": "string"},
    "source_worker": {"type": "string", "description": "worker id, e.g. PHO-14060-REVIEW3"},
    "source_kind":   {"enum": ["review", "sim", "thermo", "audit", "canary", "eval", "human"]},
    "source_sha":    {"type": "string", "pattern": "^[a-f0-9]{7,40}$"},
    "linked_findings": {"type": "array", "items": {"type": "string"}, "description": "F-* ids that supersede/duplicate this one"},
    "resolved_by":   {"type": ["string", "null"], "description": "sha or null while open"},
    "created_at":    {"type": "string", "format": "date-time"},
    "sla_deadline":  {"type": ["string", "null"], "format": "date-time"}
  },
  "additionalProperties": false
}
```

### 1.3 Verdict schema

```json
{
  "$id": "https://wiki.henry/schemas/verdict.json",
  "type": "object",
  "required": ["worker", "sha", "state", "findings", "created_at"],
  "properties": {
    "worker":     {"type": "string"},
    "sha":        {"type": "string"},
    "state":      {"enum": ["MERGE-READY", "NOT-MERGE-READY", "NO-GO", "INSUFFICIENT-CONTEXT"]},
    "findings":   {"type": "array", "items": {"$ref": "finding.json"}},
    "summary":    {"type": "string", "maxLength": 500},
    "created_at": {"type": "string", "format": "date-time"}
  },
  "additionalProperties": false
}
```

### 1.4 Steer schema

```json
{
  "$id": "https://wiki.henry/schemas/steer.json",
  "type": "object",
  "required": ["target_worker", "mode", "findings", "created_at"],
  "properties": {
    "target_worker": {"type": "string"},
    "mode":          {"enum": ["now", "on-idle"]},
    "findings":      {"type": "array", "items": {"$ref": "finding.json"}, "minItems": 1},
    "preamble":      {"type": "string", "description": "one line only"},
    "constraint_bundle": {"type": "string"},
    "created_at":    {"type": "string", "format": "date-time"}
  },
  "additionalProperties": false
}
```

### 1.5 EdgeKind enum + Edge schema

```json
{
  "$id": "https://wiki.henry/schemas/edge.json",
  "type": "object",
  "required": ["kind", "from", "to", "created_at"],
  "properties": {
    "kind": {"enum": [
      "spawn",
      "steer",
      "verdict",
      "archive",
      "handoff",
      "monitor_alarm",
      "capability_grant",
      "escalation"
    ]},
    "from":       {"type": "string", "description": "node id"},
    "to":         {"type": "string", "description": "node id"},
    "payload":    {"type": "object", "description": "kind-specific; see per-kind sub-schemas"},
    "created_at": {"type": "string", "format": "date-time"}
  },
  "additionalProperties": false
}
```

Per-kind payload requirements:

- `spawn`: `{ticket, role, model, effort?, worktree, request_id}`.
- `steer`: full `Steer` object.
- `verdict`: full `Verdict` object.
- `archive`: `{outcome, ended_at}`.
- `handoff`: `{from_session, to_session, worktree, pr_url, remaining_summary}`.
- `monitor_alarm`: `{alarm_kind, message, watchlist_row}`.
- `capability_grant`: `{capability, granted_by}`.
- `escalation`: `{reason, prior_findings, target: "henry" | "orchestrator"}`.

### 1.6 Linter

`wiki graph lint <workgraph.json | verdict.json | steer.json>` validates against the appropriate schema. Exits 1 on any violation. Runs automatically inside the merge-ready gate before a Steer is sent.

### 1.7 Acceptance criteria

- Schemas versioned; every existing `wiki agent steer` call composes a `Steer` object and validates before send.
- `wiki graph lint` in CI on any repo that produces workgraph artifacts.
- Existing status-file JSON is untouched (backward compatible).

## D2 — workgraph.json + wiki-app renderer

### 2.1 File location

`/tmp/agent-status/<TICKET>.workgraph.json` — per ticket. Rewritten atomically (tmp + rename). Owned by orchestrator. Workers do NOT write it — they only write the status file and their verdict (which the orchestrator appends).

Persisted-across-reboot copy: `~/.wiki/workgraphs/<TICKET>-<epoch>.workgraph.json` snapshotted on every meaningful append (spawn / verdict / archive). The `/tmp` copy is the hot pointer; the `~/.wiki` copy is the durable archive that `agent-archive/<TICKET>/<timestamp>/` links into.

### 2.2 Shape

```json
{
  "ticket": "PHO-14060",
  "orch":   "phoebe",
  "template": "phoebe.implement",
  "created_at": "2026-07-22T14:00:00Z",
  "updated_at": "2026-07-22T14:37:00Z",
  "nodes": [
    {"id": "N-1", "kind": "orchestrator", "label": "phoebe orch"},
    {"id": "N-2", "kind": "implement",    "label": "PHO-14060 luna", "worker_id": "PHO-14060", "sha": "af8173b"},
    {"id": "N-3", "kind": "review",       "label": "PHO-14060-REVIEW1 sol", "worker_id": "PHO-14060-REVIEW1", "sha": "af8173b"},
    {"id": "N-4", "kind": "monitor",      "label": "PHO-14060 monitor"}
  ],
  "edges": [
    {"kind": "spawn",   "from": "N-1", "to": "N-2", "payload": {...}, "created_at": "..."},
    {"kind": "spawn",   "from": "N-1", "to": "N-3", "payload": {...}, "created_at": "..."},
    {"kind": "verdict", "from": "N-3", "to": "N-1", "payload": {"state": "NOT-MERGE-READY", "findings": [...]}, "created_at": "..."},
    {"kind": "steer",   "from": "N-1", "to": "N-2", "payload": {...}, "created_at": "..."}
  ],
  "composite_health": {
    "state": "iterating",
    "open_findings": 3,
    "blocking": 1,
    "slowest_node_stall_seconds": 240,
    "iteration_count": 2
  }
}
```

### 2.3 Writer contract

Every orchestrator action goes through a single `wiki graph append` CLI:

```bash
wiki graph append <TICKET> --edge-kind spawn      --from N-1 --to N-2 --payload-file /tmp/spawn.json
wiki graph append <TICKET> --edge-kind verdict    --from N-3 --to N-1 --payload-file /tmp/verdict.json
wiki graph append <TICKET> --edge-kind steer      --from N-1 --to N-2 --payload-file /tmp/steer.json
wiki graph append <TICKET> --edge-kind archive    --from N-1 --to N-3 --payload-file /tmp/archive.json
```

`append` validates the payload against the edge-kind schema, adds the node if new, sets `updated_at`, recomputes `composite_health`, atomic-writes both `/tmp` and `~/.wiki` copies.

### 2.4 Renderer

Wiki app adds a `/agents/<TICKET>/graph` route that renders the DAG as an interactive Mermaid diagram plus a table of open findings. Read-only — orchestrator remains the sole writer. Two views:

- Live: current DAG + composite health card.
- Replay: slider scrubs through `edges[]` chronologically; each frame shows nodes present at that time.

### 2.5 Composite-health monitor

New watchlist detector `graph_health` reads the composite_health block:

- `blocking > 0` and no live reviewer for this ticket → re-alarm every 5 min (subsumes existing review-gap alarm).
- `slowest_node_stall_seconds > 1800` → escalation edge with `target: "henry"` appended; wiki app pushes a notification.
- `iteration_count > cap_from_template` → escalation edge, orchestrator surfaces to Henry.

### 2.6 Acceptance criteria

- Every mastermind/wiki/tooling orchestrator writes a workgraph.json for every ticket it opens.
- Wiki app `/agents/<TICKET>/graph` renders it live.
- Replay works for any archived ticket via the `~/.wiki/workgraphs/` snapshot.

## D3 — Work-graph templates

### 3.1 Location

`~/me/fun/wiki/templates/workgraphs/*.workgraph.tpl.json`.

### 3.2 Template shape

```json
{
  "template_id": "phoebe.implement",
  "applies_to":  {"repo": "phoebe", "labels_any": ["implement"], "labels_none": ["frontend"]},
  "roles": [
    {"kind": "implement", "runtime": "cdx", "model": "gpt-5.6-luna", "effort": null},
    {"kind": "review",    "runtime": "cdx", "model": "gpt-5.6-sol",  "effort": "high"}
  ],
  "edges": [
    {"kind": "spawn",   "from": "orchestrator", "to": "implement"},
    {"kind": "monitor", "on": "implement"},
    {"kind": "spawn",   "from": "orchestrator", "to": "review", "trigger": "implement.state == 'merge-ready'"},
    {"kind": "verdict", "from": "review",       "to": "orchestrator"},
    {"kind": "steer",   "from": "orchestrator", "to": "implement", "trigger": "verdict.state != 'MERGE-READY'"},
    {"kind": "archive", "from": "orchestrator", "to": "review",     "trigger": "verdict.routed"}
  ],
  "iteration_cap": 8,
  "on_failure": {
    "implement_stall_30min": "steer_ping",
    "review_no_verdict_2h":  "replace_review",
    "iteration_cap_reached": "escalate_to_henry"
  }
}
```

### 3.3 Initial templates

- `phoebe.implement` — impl (luna) → review (sol) loop.
- `phoebe.frontend` — impl (cc fable-5 + /frontend-design) → review (sol).
- `phoebe.migration` — impl → sim (PHO-13944 sim family) → review → sim → review.
- `phoebe.plan` — plan worker only; verdict = plan-ready.
- `wiki.implement` — impl (luna) → review (sol) → self-merge on clean (per Henry 2026-07-13 authority rule).
- `tooling.implement` — impl → review, self-merge under [[feedback_misc_tooling_merge_authority]].

### 3.4 Selector

`wiki graph select-template <ticket-id>` inspects Linear/GitHub labels + repo and picks. Falls back to `<repo>.implement`. Prints template id + roles; orchestrator uses this to spawn.

### 3.5 Acceptance criteria

- Every new spawn call goes through the selector (no ad-hoc model picks in prompts).
- `wiki graph lint --against-template` on a workgraph.json flags any edge not permitted by the template.

## D4 — Worker-side pre-tool zone-isolation hook

### 4.1 Files

- `~/me/fun/wiki/hooks/worker-isolation-pretool.sh`
- Rewire: worker-CLI startup (both codex and claude worker spawn commands) copies this hook into the worker session's `.claude/hooks` or `.codex/hooks` before the CLI boots.

### 4.2 Behavior

Pre-`Bash`, pre-`Edit`, pre-`Write` hook checks the target path. Deny with clear error if the path matches any of:

- `~/.claude/**` (any session, plugin, config)
- `~/.codex/**`
- `~/.wiki/**`
- `~/me/fun/wiki/vault/**`
- `~/me/fun/config/**`
- `/tmp/agent-status/**` (workers write only `<self-ticket>.json` — enforced by pattern)
- `/tmp/agent-registry.json`
- other-worker worktrees under `.codex/worktrees/**` or `.claude/worktrees/**` outside the worker's own slug

Whitelist: worker's own worktree, `/tmp/<self-ticket>*`, `/tmp/cdx-<TICKET>*` log path.

### 4.3 Escape hatch

Env var `HENRY_ISOLATION_OVERRIDE=1` disables — only orchestrator sessions set it. Workers never do.

### 4.4 Acceptance criteria

- The 4 documented leak incidents (protocol note) all become synthetic tests; each must fail closed under the hook.
- Zero false positives across a 24-hour Phoebe/wiki run.

## D5 — Cross-orchestrator meta-registry

### 5.1 File

`~/.wiki/meta-registry.json` — single source of truth across orchestrators.

### 5.2 Shape

```json
{
  "orchestrators": [
    {"orch_id": "phoebe",  "session_id": "...", "started_at": "...", "watchlist": "/tmp/agent-status/.phoebe-watchlist"},
    {"orch_id": "wiki",    "session_id": "...", "started_at": "...", "watchlist": "/tmp/agent-status/.wiki-watchlist"},
    {"orch_id": "tooling", "session_id": "...", "started_at": "...", "watchlist": "/tmp/agent-status/.tooling-watchlist"}
  ],
  "workers_by_orch": {
    "phoebe":  [{"ticket": "PHO-14060", "state": "iterating", "workgraph": "...", "composite_health": {...}}, ...],
    "wiki":    [...],
    "tooling": [...]
  }
}
```

Updated by every `wiki agent orch` / `wiki agent register` / `wiki agent done` call; keeps a rolling window (60s TTL) of live states pulled from each per-ticket workgraph.

### 5.3 Global view

- Wiki app `/agents` page groups by `orch_id`, with cross-repo overlap highlighted (same repo referenced by two orchestrators).
- `wiki agents summary` CLI prints one line per orchestrator + per-ticket health.

### 5.4 Cross-orch handoff

New edge kind `capability_grant` for when work spans orchestrators (e.g. wiki UI change requires mastermind reviewer). Spec:

```json
{
  "kind": "capability_grant",
  "from": "orch:wiki",
  "to":   "orch:phoebe",
  "payload": {"capability": "review", "ticket": "WIKI-155", "reason": "Phoebe reviewer expertise"}
}
```

### 5.5 Acceptance criteria

- `/agents` view is single-pane across all orchestrators.
- Fleet monitor operates off the meta-registry, not per-orch watchlists.
- Cross-repo overlap detection surfaces before the second orchestrator spawns a duplicate worker.

## Migration order + rollback

1. Land D1 (schemas + linter). Zero behavior change; adds validation gate.
2. Land D2 (workgraph.json + writer). Every orchestrator writes it starting from spawn call #1 of the next ticket; existing tickets carry on without a graph until they finish or handoff.
3. Land D3 (templates). Selector defaults to `<repo>.implement`; overrides via label. Grandfather existing tickets on prior ad-hoc shape.
4. Land D4 (isolation hook). Gate with `HENRY_ISOLATION_HOOK=1` env var for 48h across all workers; flip to always-on after zero false positives.
5. Land D5 (meta-registry). Reads from existing watchlists first (backfill), then flips per-orch watchlists to "shadow of meta-registry" and eventually removes them.

Rollback per deliverable is `git revert` + delete the added files; nothing in D1/D2/D3/D5 removes existing files. D4 rollback is `unset HENRY_ISOLATION_HOOK`.

## Open questions

1. Should verdicts be signed (worker HMAC) to prevent orchestrator forgery? Nice-to-have; skip v1.
2. workgraph.json per ticket vs one giant file per orch? Per-ticket keeps atomic writes cheap; go per-ticket.
3. Do we hydrate replay from `agent-archive` or from `~/.wiki/workgraphs/` snapshots? Both — snapshots are authoritative, archive is human-readable index.
4. Template inheritance? Skip v1; if two templates share 80% edges, extract a base later.
5. Isolation hook: fail-closed vs warn-and-log for a burn-in period? Warn-and-log for 48h with a nag banner, then fail-closed.

## Related

- [[graph-engineering]] — concept + primitives (nodes/edges/shared-state).
- [[orchestrator-worker-protocol]] — the file/tmux schema this spec extends.
- [[mastermind-merge-ready-loop]] — the loop this spec makes machine-readable.
- [[harness-best-of-breed]] — where the primitives came from.
- [[wiki-workspaces-design]] — precedent for per-orch scoping.
- [[polish-census]] — same wiki-app rendering surface.

## Non-goals (explicit)

- Not adopting LangGraph / a Python graph library. JSON on disk + CLI is enough.
- Not changing the status-file schema. Backward-compatible extension only.
- Not centralizing tmux management; workers still run in tmux windows.
- Not removing PR handoff comments; they remain the human-readable multi-session memory.
