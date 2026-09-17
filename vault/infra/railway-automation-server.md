---
type: reference
tags: [infra, automations, railway]
created: 2026-09-16
updated: 2026-09-16
---

# Railway automation server

Decision (Henry, 2026-09-16, via zeta orch session): use Railway as the always-on runtime for timed automations, so jobs run without Zeta / the laptop being open. Chosen over buying hardware for now; a Mac mini stays the later option if the FLEET (Wiki.app, supervisor, worktrees — all macOS-shaped) should move off the laptop.

Shape note: Railway is a PaaS, not a classic VPS — services deployed from repos/images with cron schedules, volumes, and metrics; no long-lived SSH VM. That fits the automation use case (cron agents, PR/CI watchers, webhook receivers, headless `zeta serve`-style processes) but does NOT host Wiki.app or anything macOS-only.

Starting points when work begins:
- Railway MCP tools + `use-railway` skill are already wired into orch sessions.
- Candidate first workloads: nightly repo audits, vault lint, PR babysitters that survive laptop sleep, a GitHub-webhook -> spawn-worker receiver.
- Related: [[backend-outage-2026-09-16]] (motivation: laptop-bound monitors die silently).

Status (2026-09-17): **AUTO-1 COMPLETE — the smoke test passed.** Scheduler skeleton LIVE and review-hardened at https://automations-production-7928.up.railway.app (Railway project `genuine-integrity`, Hobby plan, id 23eb2b5d-8a27-47b2-82f3-3050ebccceb6, account henryuni6688@gmail.com; service `automations`, production env). Repo: `~/me/fun/automations` (local only, no GitHub remote). 3 review rounds with injected-failure probes: tick errors retry with backoff and `/healthz` reports scheduler staleness honestly; request bodies stream and abort at 256 KiB (chunked included); event retention; off-loop SQLite; `secrets.compare_digest`. Token in `~/me/fun/automations/.env.local` (never committed). KNOWN LIMITS: data on ephemeral container storage — redeploys erase schedules/events (volume = next lane); events-only, no payload execution yet; an incomplete client can hold a socket open post-422 (uvicorn discards bytes; noted, not fixed). GOTCHA: the Railway MCP tools hold a stale token until their server restarts — the CLI (`railway` at /opt/homebrew/bin, logged in) is the working channel.

Next-lane candidates: AUTO-2 volume mount (durable schedules), AUTO-3 GitHub remote + deploy-from-repo, AUTO-4 real trigger execution (webhook out / spawn hooks), AUTO-5 cron-expression schedules.