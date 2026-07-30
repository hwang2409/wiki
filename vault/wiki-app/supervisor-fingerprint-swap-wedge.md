---
type: til
tags: [wiki-app]
created: 2026-07-29
updated: 2026-07-29
---

# Supervisor fingerprint-swap wedge (2026-07-29 outage RCA)

Outage ~20:12:45 local: backend 503 "Agent supervisor is unavailable: supervisor did not become ready: [Errno 61] Connection refused". Second occurrence same evening (~19:4x, same signature, prompted Henry's rebuild/relaunch at 19:57-20:02).

## Chain

1. A client running DEV backend code from `.codex/worktrees/wiki-173-orch-autopilot` (WIKI-173 orch-autopilot worker) called `SupervisorClient.ensure_running()` with default runtime paths (`~/.wiki/agent-runtime`).
2. Dev runtime fingerprint (source hash) != frozen sidecar fingerprint (binary inode stat) -> client treated the healthy prod supervisor (pid 21618) as stale -> WIKI-38 swap path: SIGTERM.
3. Supervisor began graceful shutdown: closed unix listener (connects now ECONNREFUSED 61 while socket file exists), unlinked `supervisor.sock`, then hung in `supervisor.close()` draining adapters — waiting for its provider children (`claude -p` orchestrator turns) to exit. They run for many minutes.
4. Hung daemon still holds `supervisor.lock` flock + pid file. Every respawn attempt (dev client's, then every backend request's ensure_running) died at `_acquire_single_instance`: "wiki supervisor is already running". 2,334 frozen crashes + 607 dev-path traceback lines in `supervisor.log`, storm ended 20:20:06.
5. Backend `main.py:3600` wrapped the failure as the 503; Wiki.app injected it into agent sessions.

## Bugs exposed

- Fingerprint swap treats "different code" as "stale" — any dev-code client with default env paths hijack-kills the production supervisor. Guard: `WIKI_SUPERVISOR_AUTOSTART=off` for dev/test clients, or isolated `WIKI_AGENT_RUNTIME_DIR`.
- Shutdown ordering: socket closed/unlinked BEFORE adapter drain, and lock held throughout -> half-dead daemon blocks replacement indefinitely; swap protocol assumes fast old-daemon exit.
- Deadlock irony: shutdown waits for orchestrator children; the children were busy investigating the outage the shutdown caused.

## Symptoms (cheat sheet)

- Control ops 503: "supervisor did not become ready" with `[Errno 61]` (listener closed, socket file still present) or `[Errno 2]` (file already unlinked).
- Reads like `list_agents` still serve stale snapshots; supervisor pid alive; `supervisor.lock` flock held; respawns crash-loop "wiki supervisor is already running".
- Providers die around the wedge; main thread idles in kevent (not a C-level deadlock).
- Spindump samples (phoebe, 07-29): `/tmp/wiki-backend_2026-07-29_202028_DiBe.sample.txt`, `/tmp/wiki-backend_2026-07-29_204045_bIWq.sample.txt`.

## Remediation (proven 3x on 07-29)

1. `kill -9 $(cat ~/.wiki/agent-runtime/supervisor.pid)` — TERM is a no-op mid-shutdown; the wedge IS a hung TERM.
2. Any backend request (or UI poll) autostarts a fresh supervisor within seconds; verify `supervisor.sock` exists + ping.
3. Orchestrators auto-resume; dead-but-registered workers return via replace; re-arm fleet monitors.

## Evidence

- `~/.wiki/agent-runtime/supervisor.log` lines ~159080-160257 (dev worktree tracebacks), ~160956+ (PYI burst, pids 41848-43721).
- `~/Library/Logs/Wiki/wiki-backend-1785369748.log` 503 flood after last fleet spawn.
- phoebe run c0ed3921 parked on pending AskUserQuestion proposing `kill -TERM 21618` (remediation, not cause; needs kill -9 or child exit — TERM is a no-op mid-shutdown).

## Recurrence + remediation (same evening)

- 20:33 kill -9 21618 -> fresh supervisor 51477, healthy.
- Henry relaunched Wiki.app ~20:35 -> supervisor 52419 -> wedged again within minutes: NEW `wiki-173-orch-autopilot` dev tracebacks in supervisor.log (~line 163487). The WIKI-173 worker re-strikes every fresh supervisor when its turns run dev backend code.
- 20:42 kill -9 52419 -> supervisor 60373 healthy. Stable only while the WIKI-173 worker stays idle.

FIXED 2026-07-30, merged `5017f26` (WIKI-217): dev-client swap guard in `ensure_running`, early lock/pid release in daemon shutdown, socket unlink guarded by bound inode. Takes effect in the app after the next `make native-build` + relaunch. Worktrees created before the fix still carry the old swapping client until rebased — but with a rebuilt app, their strike now completes as a clean swap instead of a wedge.

Ticket: WIKI-217 (vault todo, P1). Related: [[mitmweb-rebuild]] fleet ops; ticket WIKI-173.
