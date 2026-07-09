---
type: til
tags: [tools/til, wiki-app]
created: 2026-07-09
updated: 2026-07-09
---

# Root-Owned Native Build Artifacts

**Symptom:** `make native-build` fails with Vite `ENOTEMPTY .../frontend/dist/assets`, then PyInstaller `PermissionError .../pyinstaller/bincache00py31464bit/arm64/adhoc/no-entitlements`.
**Cause:** a prior root build left `frontend/dist`, repo `build`/`dist`, and the PyInstaller cache root-owned; the normal user cannot clean them.
**Fix:** `sudo chown -R "$(id -un):$(id -gn)" frontend/dist build dist "$HOME/Library/Application Support/pyinstaller"`, then rerun; never use `sudo` for the build.
**Observed:** 2026-07-09: 115 root-owned frontend files, then 83 root-owned PyInstaller-cache entries.
