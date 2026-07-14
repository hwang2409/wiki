---
type: til
tags: [tools, wiki, agents, runtime]
created: 2026-07-10
updated: 2026-07-10
---

# Wiki orphan PID cleanup must verify lstart and kill the process group

**Symptom:** detached `/api/agents/<ticket>/archive` cleanup can target the wrong process if a dead provider PID gets recycled, or leak tool grandchildren if only the leaf PID is signaled.
**Cause:** `pid_alive(pid)` alone does not prove identity; a reused PID can now belong to another same-UID process. Codex/Claude also run in their own session/process group and may fork tool children.
**Fix:** when a detached provider PID looks orphaned, capture `psutil.Process(pid).create_time()` with the orphan snapshot, re-check the same high-precision create time immediately before `SIGTERM`/`SIGKILL`, and signal `killpg(pgid, ...)` for the verified provider group instead of only `kill(pid, ...)`.
