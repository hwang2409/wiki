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

## Rejected

- **Per-element CSS customizer prototype** (`wiki-styles` fenced blocks, style inspector, media placement controls) — removed entirely; legacy blocks stripped at render. Do not reintroduce.
- **Obsidian default color theme** (purple accent `hsl(254,80%,68%)`, per-type callout colors) — replaced same day by monochrome at Henry's request.

## Key files

- `frontend/src/markdown.tsx` — renderer + callout/wikilink/tag/highlight plugins
- `frontend/src/App.tsx` — shell, theme toggle
- `frontend/src/styles.css` — both theme palettes
- `vault/meta/obsidian-feature-test.md` — exercises every renderer feature

## Known gaps (deliberate, "expand later")

`![[embeds]]` render as links not transclusions; no live-preview editing; reading-view checkboxes not clickable; no math/mermaid/graph/backlinks.
