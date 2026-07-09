---
type: til
tags: [tools/til, wiki-app]
created: 2026-07-09
updated: 2026-07-09
---

# Root-Owned Native Build Artifacts

**Symptom:** `make native-build` fails with Vite `ENOTEMPTY`, PyInstaller `PermissionError`, then Tauri `Permission denied (os error 13)`.
**Cause:** a prior root build left `frontend/dist`, repo `build`/`dist`, PyInstaller cache, and `src-tauri/target` root-owned; the normal user cannot clean them.
**Fix:** `sudo chown -R "$(id -un):$(id -gn)" frontend/dist build dist src-tauri/target "$HOME/Library/Application Support/pyinstaller"`, then rerun; never use `sudo` for the build.
**Observed:** 2026-07-09: `src-tauri/target` had 467 root-owned entries; the referenced sidecar `RECORD` was readable.
