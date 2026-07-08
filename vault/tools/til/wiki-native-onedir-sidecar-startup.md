---
type: til
tags: [tools/til, wiki-app, macos]
created: 2026-07-08
updated: 2026-07-08
---

# Wiki Native Onedir Sidecar Startup

**Symptom:** PyInstaller `onefile` Wiki backend startup stayed at `4.626s`, `4.937s`, `5.479s` to `/health`; `onedir` dropped steady-state launches to `0.319s`, `0.361s` but the first post-build launch still hit `5.280s`.
**Cause:** `onefile` unpacks the Python runtime on every launch; `onedir` reuses the unpacked tree, but the first fresh bundle launch still pays macOS file/signature warmup.
**Fix:** Default `packaging/wiki-backend.spec` to `onedir`, bundle `dist/wiki-backend-sidecar/` into `Wiki.app` resources, and spawn it through an `exec` wrapper binary (`dist/wiki-backend` -> `Contents/Resources/wiki-backend-sidecar/wiki-backend`) so Tauri keeps the same pid/lifecycle contract.
