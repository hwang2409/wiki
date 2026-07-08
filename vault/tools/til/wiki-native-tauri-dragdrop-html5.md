---
type: til
tags: [tools/til]
created: 2026-07-08
updated: 2026-07-08
---

# Tauri drag-drop handler vs HTML5 DnD

Tauri 2 installs a native drag-drop handler on every webview window (for OS file-drop events). It swallows `dragstart`/`dragover` before the page sees them — ALL HTML5 drag-and-drop inside the app silently dies (kanban cards, pane-split drags, tree moves). No errors, drags just never start.

Fix: `.disable_drag_drop_handler()` on `WebviewWindowBuilder` (or `dragDropEnabled: false` in window config). Cost: no native OS file-drop events — fine when the app doesn't use them.

Found via manual QA of the wiki native app (WIKI-2 follow-up, commit `c5294ff`); invisible to automated checks — WKWebView AX automation was already degraded, and the browser-based parity checks ran in Chromium where DnD works.
