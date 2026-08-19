---
type: til
tags: [wiki-app]
created: 2026-07-29
updated: 2026-07-30
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

## Wedge #4 AFTER the fix (2026-07-30 00:39 EDT)

Supervisor 45859 (started 00:32, running the REBUILT release binary with `5017f26`) wedged at ~00:39: socket unlinked, `supervisor.lock` still flock-held, respawn crash-storm ("wiki supervisor is already running", BlockingIOError 35), all children on both orchestrators killed. Trigger consistent with the pre-fix WIKI-173 worktree client striking again (its worktree predates the fix). Conclusion: the daemon-side early-lock-release in `5017f26` did NOT prevent the wedge — the fix is incomplete or the lock is held by a different path. kill -9 45859 -> fresh supervisor 53892, healthy. WIKI-217 needs a follow-up: reproduce with a pre-fix client striking a post-fix daemon; verify lock/pid release ordering actually runs before adapter drain.

## Wedges #5-#6 and the auto-resume strike loop (2026-07-30 00:41-00:47 EDT)

Two more strikes after #4 (supervisors 53892, 57341). Loop mechanics: supervisor dies -> wiki orch auto-resumes -> replaces WIKI-173 -> the worker's FIRST recovery turn runs pre-fix backend code and strikes -> everyone dies -> repeat. The wiki orch knew (it queued an urgent merge-main steer to WIKI-173 at 00:46) but a queued steer can never win: the strike fires in the recovery turn before queued messages are consumed. Loop broken by phoebe orch at 00:48: archived WIKI-173 (outcome=closed) so it cannot be blindly replaced; the correct respawn is a FRESH spawn whose prompt front-loads "merge origin/main before any backend test run". Worktree state intact on disk.

## Strike loop broken (2026-07-30 00:48-00:50 EDT)

Wiki orch removed the hazard deterministically while workers were dead, instead of racing steers against recovery turns:

- Committed WIKI-173's uncommitted in-flight work as WIP `ff656f4`, then merged origin/main into both implementer branches: `8b4cf03` (wiki-173), `eb125b8` (wiki-188). Both worktrees now carry the fix; backend test runs from them are safe.
- Reviewer worktrees stay pinned at pre-fix PR SHAs by design — reviewers get a STATIC-ONLY constraint (no backend tests, services, or dev servers from the pinned checkout). Steer, not rebase: the pin is the point.
- WIKI-173 respawned FRESH (it was archived during the loop) with the safe-worktree state front-loaded in the kickoff prompt; others revived via replace. Fleet stable as of 00:50.

Recovery gotcha (new): do NOT `rm supervisor.pid` while recovering. The backend's own respawn can land between your kill and your rm — the rm then deletes the FRESH instance's pid file and the supervisor reports "degraded: supervisor PID is absent" while actually running. If it happens: `echo <live pid> > supervisor.pid` (find via `ps aux | grep 'wiki-backend --supervisor'`). kill -9 the old pid + one backend agent-operation is the whole remedy; leave the pid file alone.

## Related: backend restart archives the fleet (2026-07-30)

Separate pathology, same evening: Wiki.app quit ~20:45 EDT (backend 8213 down ~3.7h, machine also slept). Workers kept running under the surviving supervisor (60373) the whole outage — worktree state advanced normally. On app relaunch 00:33 EDT the fresh backend archived every registered run and released all provider processes, including live mid-task workers. Recovery: worktree/git state is durable; respawn each ticket from on-disk state (archived tickets need spawn; a ticket with a dead-but-registered run needs replace). Keep respawn prompts on disk (`/tmp/phoebe-respawn/`) — they made this a 5-minute recovery.

## Lookalike: recovery event-store rebuild (2026-08-18)

Same surface symptoms as a wedge — no `supervisor.sock`, lock held, respawn attempts crash-storm "already running" — but the daemon is NOT hung: startup recovery kicked off a full event-store rebuild (113MB `events.sqlite3` -> `events-rebuild-<tmp>/` in the runtime dir) at ~100% CPU, and the socket only binds after it finishes. Diagnosis: `ps` shows sustained ~100% CPU (a wedged daemon idles in kevent) and `lsof -p <pid>` shows open files under `events-rebuild-*`. A stale `events-rebuild-*` dir in the runtime dir means a prior rebuild was interrupted; kill -9 restarts the rebuild from scratch on next boot. If the rebuild is unacceptable (it blocks all agent ops for many minutes), the blank-state alternative is to stop app+supervisor and move `~/.wiki/agent-runtime` aside — the app recreates a fresh runtime instantly (done 2026-08-17 and 2026-08-18; archives in `~/me/fun/agent-archive` are unaffected, but un-archived run transcripts stay only in the moved dir). Note `/tmp/agent-registry.json` and legacy `/tmp/agent-status/<ID>.json` files live OUTSIDE the runtime dir: the registry is rewritten by a fresh supervisor, but stale status-file workers keep showing in /api/agents until their `<ID>.json` files are removed.

Ticket: WIKI-217 (vault todo, P1). Related: [[mitmweb-rebuild]] fleet ops; ticket WIKI-173.
