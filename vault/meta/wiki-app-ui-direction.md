---
type: decision
tags: [meta, wiki-app, frontend]
created: 2026-07-06
updated: 2026-07-06
---

# Wiki App UI Direction

Decision (2026-07-06): the wiki frontend is an **Obsidian clone in structure/behavior, monochrome black-and-white in appearance**. Light mode default, light/dark switcher in ribbon (persisted to `localStorage["wiki-theme"]`).

## Chosen

- Obsidian layout: ribbon, file-explorer sidebar, tab header, breadcrumbs, reading/source mode toggle, status bar with word/char count.
- Markdown parity with Obsidian core: callouts (all 13 families, foldable `+`/`-`), `[[wikilinks]]` + aliases, `==highlights==` (rendered inverted fg/bg), `#tags` (gray pills), `%%comments%%` hidden, footnotes, task lists, tables, syntax highlighting (grayscale weights/italics, not colors).
- Monochrome palette via CSS vars on `:root` (light) + `[data-theme="dark"]` overrides in `frontend/src/styles.css`. Callouts differ by icon only, not color.
- Backend full-text search: `GET /api/notes?q=`.
- Custom views, frontmatter-gated: `view: kanban` renders sections as read-only board lanes, list items as cards (used by `todo.md`). First deliberate departure from pure markdown parity.
- Kanban target look: obsidian-community/obsidian-kanban plugin — multi-column lanes (width proportional to card count, internal card grid; single-column tried and reverted same day), panel lanes on flat pane, rich markdown cards. Shipped 2026-07-06: drag-drop between/within lanes + per-lane add-card, both write todo.md via line surgery (exact-line ops on latest content, last-write-wins). Drop-to-done shipped: drag card onto floating zone → line appended to log/done.md (`- YYYY-MM-DD: <text>`, priority tag stripped) + removed from todo.md; done-append happens first so failures can't lose the card. Remaining: card edit-in-place, lane menus.
- Split panes (2026-07-06): recursive binary split tree, unlimited panes. Every pane shows a 5-zone overlay while dragging a file from the tree — edge drop splits that pane, center drop replaces its note; closing a pane lifts its sibling; dividers drag-resize each split (15–85% clamp). Secondary panes are read-view + full kanban interactivity, ephemeral (not in URL). Cmd+B toggles sidebar; Cmd+K quick switcher.

## Rejected

- **Per-element CSS customizer prototype** (`wiki-styles` fenced blocks, style inspector, media placement controls) — removed entirely; legacy blocks stripped at render. Do not reintroduce.
- **Obsidian default color theme** (purple accent `hsl(254,80%,68%)`, per-type callout colors) — replaced same day by monochrome at Henry's request.

## Key files

- `frontend/src/markdown.tsx` — renderer + callout/wikilink/tag/highlight plugins
- `frontend/src/App.tsx` — shell, theme toggle
- `frontend/src/styles.css` — both theme palettes
- `vault/meta/ui-demo.md` — exercises every renderer feature

## Known gaps (deliberate, "expand later")

`![[embeds]]` render as links not transclusions; reading-view checkboxes not clickable; no math/mermaid/graph/backlinks.

Locked 2026-07-06: plain-textarea editor is deliberate and permanent — the wiki is read-first for Henry; agents write the notes. No CodeMirror/live-preview investment. Shipped same day: link index (`GET /api/links` — outgoing/incoming/unresolved per note; Linked-mentions section in reading view) and the `wiki` CLI (repo root — todo add/move/complete, log-done, note new with map update, lint sweep). Option 3 locked: CLI for hot paths + lint detection; raw markdown stays sanctioned for prose. Also shipped: activity feed (`#/activity`, git-log-backed — day-grouped vault commits, file pills, expandable diffs, 8s refresh) and graph view (`#/graph`, canvas force layout — degree-sized nodes, hover neighborhood, drag, click-to-open, unresolved ghost nodes). Both on the ribbon. Batch 2 same day: kanban card edit-in-place (dbl-click), SSE live push (`/api/events` mtime+git-refs watcher — replaced all polls), vault-health page (`#/health`, staleness buckets from frontmatter `updated`), and `vault/hot.md` rolling session cache w/ global SessionStart injection hook. Batch 3: file ops (shared `backend/app/vaultops.py` — smart rename rewrites wikilinks code-aware + map.md, hard delete prunes map; `wiki note rename/delete`; tree right-click menu, drag-file-to-folder move, click-unresolved-wikilink → create dialog). Activity merged-Shipped layer built then reverted same day — Henry prefers the feed as pure commit stream; done.md stays the separate ledger.
