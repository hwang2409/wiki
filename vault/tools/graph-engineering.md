---
type: reference
tags: [tools, agents, harness]
created: 2026-07-22
updated: 2026-07-22
---



# Graph Engineering

The next layer after **loop engineering**: programming a multi-agent organization as a structured graph rather than a single agent's behavior cycle. Coined / popularized in mid-2026 discourse (0xCodez, explainx.ai, AI Builder Club). If loop engineering was the 2025 – mid-2026 skill, graph engineering picks up when you have more than one loop coordinating.

## Definition

Wire N specialized agents (nodes) via edges (data + handoff routes) with shared state flowing across. A single loop is just a one-node graph with an edge back to itself; the discipline generalizes once responsibilities split.

- **Loop engineering** controls a single agent's autonomous cycle: Trigger → Act → Verify → Retry.
- **Graph engineering** controls how many loops interconnect and depend on each other. "Loops forgiving. Graphs force you to admit how much of the workflow you haven't modeled yet."

Master loops first; move to a graph only when work genuinely requires specialization across roles. The two disciplines are cumulative, not alternative.

## Two graphs at once

- **Org graph (structural, long-lived)** — named roles, zone ownership, preserved memory, persistent dependencies. Stable over many runs. Example roles: researcher, writer, reviewer, orchestrator, worker, gatekeeper.
- **Work graph (dynamic, ephemeral)** — task nodes that split, merge, reorder, or disappear as evidence arrives. Generated per run.

The org graph is the crew; the work graph is what the crew is doing right now.

## Primitives

- **Nodes** — specialized agents or deterministic steps, one clear responsibility each.
- **Edges** — routing between nodes. Sequential, conditional (pass/fail branch), or parallel (fan-out / fan-in).
- **Shared state** — the object riding edges: task payload, drafts, verdicts, accumulated context.
- **Handoff protocol** — the exact format Agent A emits so Agent B consumes without repeating full context.
- **Zone / context isolation** — each agent owns a domain; cross-zone requests go through the work graph; no other agent bleeds into that zone's context.
- **Work-graph generators** — logic that spawns runtime structure from an incoming task.
- **Failure recovery rules** — how a node's error propagates, retries, or short-circuits.
- **Graph observability** — trace per-node, per-edge state; replay a full run.

## Canonical implementation

LangGraph is the reference programming model: nodes + edges + checkpointing + typed shared state. Any graph-oriented orchestrator (LangGraph, CrewAI, custom) implements the same shape.

## Maps onto Henry's stack

Existing patterns in this ecosystem already implement graph engineering under different names:

- Mastermind orchestrator = **org graph** root node.
- Workers (cc / cdx) = **nodes** with zone-isolated worktrees.
- `wiki-artifacts spawn_agent` / `steer_agent` / `archive_agent` = **edges** with handoff payloads.
- `wiki gate` verdicts + `mastermind-merge-ready-loop` = **conditional edges** (pass → merge, fail → steer).
- `/tmp/agent-status/<T>.json` + status files = **shared state**.
- Worktrees under `.worktrees/<ticket>/` = **zone isolation**.
- Fleet monitor watchlist + re-alarms = **graph observability**.
- WIKI-132 ticket/PR dashboard = **work-graph render**.
- `[[orchestrator-worker-protocol]]` = the handoff protocol spec.

The vocabulary just makes what is already built explicit and portable to non-Wiki contexts.

## Design lessons distilled

- Design the org graph first; let the work graph emerge from tasks. Do not hard-code the work graph.
- Every edge needs a schema. Undocumented handoff formats are the top failure mode in multi-agent systems.
- Zone isolation is non-negotiable. Context bleed between agents wastes tokens and corrupts reasoning.
- Observability is a first-class node, not an afterthought. If you can't replay a run edge-by-edge, you can't debug graph-scale failures.
- Failure recovery should be declarative per node, not baked into the orchestrator. Push retry/skip/escalate policy into the node contract.
- A graph is worth building only when specialization actually pays; otherwise a bigger loop is cheaper.

## Related

- [[harness]] — cross-cutting harness principles.
- [[harness-best-of-breed]] — best-of-breed picks per harness.
- [[orchestrator-worker-protocol]] — the concrete handoff spec used across mastermind / wiki / tooling.
- [[mastermind-merge-ready-loop]] — a loop-inside-graph pattern.
- [[harness-claude-agent-sdk]] — Claude SDK's Agent/Task tool + sub-agents = graph primitives.
- [[harness-codex-cli]] — Codex CLI has no sub-agent primitive, so graph engineering there means external orchestration.

## Current-stack gap analysis (2026-07-22)

Rated against Henry's mastermind / wiki / tooling orchestration stack.

### Strengths

- Org graph explicit — orchestrator / implement / review roles + model bindings.
- Zone isolation via worktrees, hard rule.
- Handoff schema on the status-file edge (4 fields).
- Signal priority (monitor > gh ground truth > pane text).
- Autonomy invariant limits Henry-checkpoints to merge auth.
- Re-alarm + watchlist = graph observability with backpressure.
- Immediate-archive-on-report keeps fleet slot count honest.
- Sentinel + status-file dual-channel = redundant edge signal.

### Gaps

1. Work graph is only prose — DAG lives in the merge-ready-loop SKILL narrative, no on-disk artifact. No replay, no diff, no composite health per ticket.
2. `Finding` payload not typed — sim / review / thermo / audit all emit prose; every steer improvises the "observed / why / do / constraint" shape. No schema, no linter.
3. Edge kinds not enumerated — spawn / steer / verdict / archive / handoff exist informally. Adding a new one (unrouted-verdict re-alarm) took a real incident.
4. Work-graph generators = orchestrator prompt — whether a ticket gets SIM before review or THERMO after is judgment each time. No per-ticket-kind template.
5. Retry policy improvised — worker fails and orchestrator decides steer vs respawn vs handoff. No node-declared `on_failure`.
6. Cross-orchestrator graph invisible — mastermind + wiki + tooling run in parallel. Fleet monitor is per-orch. WIKI-132 dashboard is close but wiki-scoped.
7. Zone isolation is convention, not runtime enforcement — 4 leak incidents already codified.
8. No graph dry-run — every new work-graph shape tested live on real tickets.
9. Iteration cap policy scattered across hot.md, protocol note, feedback note.
10. No graph-level SLA / escalation ladder — per-worker stall detected, no "any node blocked > 30 min → page Henry" policy.
11. No per-ticket replay — `agent-archive` stores per-worker artifacts, not stitched DAG.
12. Workers declare no capabilities — nothing says "this luna worker has migration authority" vs "read-only research". Orchestrator infers from ticket.

### Top-5 highest-ROI changes

1. **`Finding` + edge-payload JSON schemas** — unlocks gaps 2, 3, 5, 10.
2. **Per-ticket `workgraph.json` DAG file + wiki-app renderer** — unlocks gaps 1, 6, 11.
3. **Work-graph templates per ticket kind** — unlocks gaps 4, 9.
4. **Worker-side pre-tool hook enforcing zone isolation** — unlocks gap 7.
5. **Cross-orchestrator meta-registry** — unlocks gaps 6, 10.

### Before / after diagrams

Rendered as wiki artifacts in the session that produced this note (2026-07-22):

- Work graph BEFORE (prose only) — no DAG file, no replay, no composite health, no per-ticket monitor.
- Work graph AFTER (`workgraph.json`) — persistent DAG, template instantiation, per-node append, wiki renderer, composite health feedback, graph lint against template.
- Finding payload BEFORE (prose ad-hoc) — every verification worker emits prose, orchestrator hand-batches into steer text; missing schema / linter / severity enum / source trace.
- Finding payload AFTER (typed) — `Finding` + `Steer` + `Verdict` schemas, all verification workers emit `Finding[]`, orchestrator wraps in `Steer`, graph lint validates, wiki finding view + per-severity SLA / escalation ladder.

## Sources

- explainx.ai — "Graph Engineering: Wire Multi-Agent Orgs After Loops" (2026-07-18)
- aibuilderclub.com — "Graph Engineering Guide 2026"
- explainx.ai — "Graphs vs Loops: Agentic AI Orchestration Debate 2026"
- 0xCodez threads (X, mid-July 2026) — direct tweet at `x.com/0xCodez/status/2079165300625330317` (not fetchable via WebFetch, HTTP 402)
