---
type: til
tags: [tools/til]
created: 2026-07-10
updated: 2026-07-10
---

# Wiki Native Codesign Fails While App Is Running

**Symptom:** `wiki-native: replacing existing signature` followed by `Operation not permitted` and Tauri's `failed to sign app`.
**Cause in one observed build:** the target `wiki-native` executable was still running. Confirm with `lsof <path-to-wiki-native>`; if no process has it open, inspect ownership, flags, and TCC access.
**Fix:** quit Wiki and stop any stale `wiki-native`/sidecar processes, verify the executable is no longer open, then rerun the native build. Do not use `sudo` for the build.
