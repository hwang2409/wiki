---
type: til
tags: [agents, tooling, mcp]
created: 2026-07-08
updated: 2026-07-08
---

# MCP vs native agent tooling


## Position (Henry, 2026-07-08)

MCP is generally a bad way to go about tooling for agents. Prefer native, first-class tools calling APIs directly.

Stated during the Core MCP expansion brainstorm, correcting a mis-framing: the phoebe admin agent's Core tools use the **Core REST API directly** (`/api/admin-agent/*`, bearer token) — deliberately NOT the Core MCP server (`core/src/mcp.ts`), even though one exists with 32 tools.

## Why (context; reasoning partly inferred from how phoebe tooling is actually built)

- Native tools get code-aware contracts: typed input/output schemas, caps + artifact spill, audit rows, error forgiveness (nearest-valid-prefix errors), description partitioning (the five-verb standard from [[admin-agent]]). MCP's generic tool surface gives none of that for free.
- MCP adds an indirection/transport layer between agent and API without adding control — harder to enforce budgets, mode context, and org scoping than in-process tools.
- Tool quality lives in the details (PHO-13206 audit: overloaded verbs + hidden provider errors caused the worst failure rates); a generic protocol boundary makes those details someone else's problem.

## Where MCP still fits

- Serving EXTERNAL generic clients you don't control (e.g. Claude/desktop sessions hitting `core.phoebe.work/mcp`) — interoperability is the point there, not tool quality.

## How to apply

- When adding agent capabilities in phoebe (or any project): default to native tools against the API; do not route agents through MCP servers. Do not propose "make the agent an MCP client" architectures.
