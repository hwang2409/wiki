---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-07
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **wiki app**: Obsidian-clone (2026-07-06) now agent-first monitor. `/agents` page (2026-07-07): push registry (`wiki agent register/done`) + status files + tmux liveness → worker cards; Conductor-style session sidebar renders native codex/claude transcripts (parsed JSONL, tool groups collapsed) + tmux capture-pane terminal tab. Details in [[wiki-app-ui-direction]]. V2 candidates: worker steering from UI, agent-archive browsing.
- **phoebe admin-agent**: PHO-13073 code-sandbox tool surface (exe.dev backend) + PHO-12306 webhook legs in flight; see [[admin-agent]] + todo.md In Progress owners.
- **exe.dev trial**: pilot blocked on 13073 tools + plan upgrade — henry ([[exe-dev]]).

## Recent facts

- Orchestrators must call `wiki agent register/done` at spawn/wrap-up — feeds `/agents` page ([[orchestrator-worker-protocol]]).
- Vault conventions enforce via `wiki lint`; hot-path writes via `wiki` CLI (todo/done/note-new).
- done.md format: day headers newest-top, `- **project** — summary`.
- Model×task: official boards favor GPT-5.x for terminal AND SWE-bench Pro; Claude verified edge = frontend human-pref ([[model-task-benchmarks]]).

## Watchouts

- Agents rewrite todo.md/map.md concurrently — refetch before line surgery.
- Codex/Claude transcript JSONL formats are unversioned internals — `backend/app/transcripts.py` parsers will drift with CLI updates.
- SEO-farm benchmark numbers untrustworthy; primary leaderboards only.
