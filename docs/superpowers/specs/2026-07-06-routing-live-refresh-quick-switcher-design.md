# Wiki UX: Routing, Live Refresh, Quick Switcher

Approved 2026-07-06. Three independent pieces, one pass.

## 1. Hash routing

- Routes: `#/note/<path>` (reading), `#/edit/<path>` (editing), `#/new` (new-note form), empty hash (empty state). Path is URI-encoded.
- Hash is the source of truth for main-pane state. UI actions set the hash; a `hashchange` handler (plus boot) resolves the route: fetch note, set mode. Handler no-ops when state already matches, so writes and reads can't loop.
- Refresh restores the exact view. Browser back/forward navigates note history.
- Route to a missing/deleted note → empty state + error notice.
- localStorage persists sidebar-only state: collapsed folders, files/search tab.

## 2. Live refresh

- Poll every 4s: notes list always; active note content only in view mode.
- State updates only when `id + updated_at` fingerprint (list) or `updated_at`/content (note) actually changed — no rerender churn, no enter-animation retrigger.
- Paused while editing (draft never clobbered; last-write-wins on save) and while `document.hidden`.
- No backend changes.

## 3. Cmd+K quick switcher

- Global Cmd/Ctrl+K opens a top-centered modal: input + result list, dimmed backdrop.
- Filename/title fuzzy match (prefix > substring > subsequence) over the loaded notes list, client-side. Empty query shows most-recent notes. Sidebar search tab remains the full-text tool.
- ↑/↓ select, Enter opens (sets `#/note/...`), Esc/backdrop closes. Works while editing.
- Own component (`switcher.tsx`), monochrome theme, subtle enter animation.

## Non-goals

Other shortcuts (next batch), kanban drag-drop, edit-conflict resolution beyond pause-while-editing.
