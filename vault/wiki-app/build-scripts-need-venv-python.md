---
type: til
tags: [wiki-app]
created: 2026-08-05
updated: 2026-08-05
---

# Native build scripts vs system python3

**Symptom:** `make native-build` (or any `scripts/*.py` helper) dies with `ModuleNotFoundError: No module named 'pydantic'` (or `PIL`).
**Cause:** `build-native-app.sh`/`swap-native-app.sh` invoke helpers with bare `python3`. Importing anything under `backend.app.agent_runtime` runs the package `__init__`, which eagerly pulls client -> supervisor -> normalizer -> `wiki_artifacts` -> pydantic + PIL. The venv only has pydantic transitively via fastapi; system python has neither.
**Fixes (2026-08-05):**
- `scripts/native_backend_fingerprint.py` loads `version.py` via importlib file-path load — stays dependency-free, safe under any interpreter.
- `scripts/swap-native-app.sh` prefers `$ROOT/.venv/bin/python` — `native_swap_transaction.py` genuinely needs `SupervisorClient`, so it must run in the venv.

**Rule:** a build helper either (a) avoids the `agent_runtime` package import entirely (importlib direct file load), or (b) runs under `.venv/bin/python`. Never assume bare `python3` can import backend code.

Related: [[wiki-native-codesign-running]], [[supervisor-fingerprint-swap-wedge]]

