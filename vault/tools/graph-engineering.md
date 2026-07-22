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

## Sources

- explainx.ai — "Graph Engineering: Wire Multi-Agent Orgs After Loops" (2026-07-18)
- aibuilderclub.com — "Graph Engineering Guide 2026"
- explainx.ai — "Graphs vs Loops: Agentic AI Orchestration Debate 2026"
- 0xCodez threads (X, mid-July 2026) — direct tweet at `x.com/0xCodez/status/2079165300625330317` (not fetchable via WebFetch, HTTP 402)
