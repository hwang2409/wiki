---
type: til
tags: [tools/til]
created: 2026-07-08
updated: 2026-07-08
---

# Wiki Native Bundle Hardened Runtime And Parent Watchdog

**Symptom:** `Wiki.app` launched via `open` but `wiki-backend` died with `mapped file ... have different Team IDs`; later, one-restart crash handling could die silently and external app termination could leave orphaned `wiki-backend` processes.
**Cause:** Tauri re-signed the PyInstaller onefile sidecar with hardened runtime, so macOS library validation rejected the extracted `Python.framework`; PyInstaller onefile also runs the real server as a grandchild, and Tauri termination events arrive on an async worker where `reqwest::blocking` / `thread::sleep` restart logic will panic.
**Fix:** Set `src-tauri/tauri.conf.json` `bundle.macOS.hardenedRuntime` to `false` while keeping `signingIdentity: "-"`, move restart handling onto a plain `std::thread`, pass `--parent-pid` from Rust, and have `backend/native_server.py` escalate from `should_exit` to `force_exit` to `os._exit(1)` if the parent disappears.
