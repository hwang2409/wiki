---
type: til
tags: [tools/til]
created: 2026-07-09
updated: 2026-07-09
---

# Wkwebview Eval Defer Clobber

Symptom: Wiki.app stuck on the injected "Launching backend" card forever while logs show `backend healthy` + `GET /` + assets all 200 — the app WAS served, then vanished.

Mechanism: Tauri shell called `window.eval(LOADING_PAGE)` (a `document.open(); document.write(...)` script) right after window creation, then a thread waited for backend health and called `window.navigate(app_url)`. WKWebView can DEFER the eval until after the first real navigation completes. Slow backend (onefile sidecar, 4-6s): eval runs first, navigate replaces it — fine. Fast backend (onedir, ~0.4s): navigate's load wins, the deferred eval then `document.write`s the static card OVER the loaded app. No error anywhere.

Fix: guard the injected script — `if (location.protocol === "about:") { ... }` — the card can only paint on the initial about:blank document. General rule: any eval-injected bootstrap HTML must be idempotent against late execution; never assume eval ordering vs navigation in WKWebView.

Greppables: LOADING_PAGE, document.open clobber, "Waiting for the local API to become healthy" stuck, wkwebview eval defer navigate race.
