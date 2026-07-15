---
type: reference
tags: [wiki-app, design]
created: 2026-07-15
updated: 2026-07-15
---

# Multi-workspace file browsing (WIKI-127)

Decisions (Henry 2026-07-15): workspace list is orchestrator-derived; global switcher at top of file sidebar; vault/notes section always visible; backend approach delegated to orchestrator → approach A.

## Design (approach A)

- `GET /api/workspaces` → `[{id, root, live}]`. Server-side allowlist derived from the agent runtime's registered orchestrators (id + cwd) UNION the backend's own repo root (`ROOT_DIR`, id `wiki`) so the list is never empty. Dedupe by resolved root. Roots validated (exists, is dir) at derivation time.
- `/api/files/tree` + `/api/files/content` gain `workspace=<id>`; default `wiki` (back-compat). Root resolved by id lookup only — client never supplies a path as root. Unknown id → 404.
- All WIKI-119/123 containment guarantees (O_NOFOLLOW, descriptor containment, traversal/dot/NUL rejection, ignore-dirs, size cap, binary detection) parameterized by root and enforced per request.
- Frontend: workspace selector atop Files section; tree state keyed by workspace id; open-file pane paths and WIKI-122 recents carry `(workspace, relpath)` — recents localStorage schema bumps to v2 with tolerant migration (v1 entries assumed workspace `wiki`). Note API untouched (vault-fixed).
- Path scheme: file panes address `file://<workspace>/<relpath>` (explicit, avoids WIKI-123 mixed-path bug class).

Related: [[orchestrator-worker-protocol]], WIKI-128 (code-viewer density, same arc).
