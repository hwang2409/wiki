---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-06
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **wiki app**: Obsidian-clone built out in one day (2026-07-06) — mono theme, interactive kanban (todo.md), splits/resize, graph, activity feed w/ split diffs, health page, SSE live push, `wiki` CLI + lint, link index. Direction locked in [[wiki-app-ui-direction]].
- **phoebe admin-agent**: PHO-13073 code-sandbox tool surface (exe.dev backend) + PHO-12306 webhook legs in flight; see [[admin-agent]] + todo.md In Progress owners.
- **exe.dev trial**: pilot blocked on 13073 tools + plan upgrade — henry ([[exe-dev]]).

## Recent facts

- Vault conventions now enforce via `wiki lint`; hot-path writes via `wiki` CLI (todo/done/note-new).
- done.md format: day headers newest-top, `- **project** — summary`.
- Model×task: official boards favor GPT-5.x for terminal AND SWE-bench Pro; Claude verified edge = frontend human-pref ([[model-task-benchmarks]]).

## Watchouts

- Agents rewrite todo.md/map.md concurrently — refetch before line surgery.
- SEO-farm benchmark numbers untrustworthy; primary leaderboards only.
