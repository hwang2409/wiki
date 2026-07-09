---
type: til
tags: [tools/til, wiki-app]
created: 2026-07-09
updated: 2026-07-09
---

# Vite Dist Root-Owned Artifacts

**Symptom:** `make native-build` fails in Vite with `ENOTEMPTY, Directory not empty: .../frontend/dist/assets`.
**Cause:** the build output contains root-owned files/directories; Vite runs as `henry` and cannot empty them.
**Fix:** `sudo chown -R "$(id -un):$(id -gn)" frontend/dist`, then rerun; never run the build with `sudo`.
**Observed:** 2026-07-09 in the wiki repo: 115 root-owned files and 3 root-owned directories.
