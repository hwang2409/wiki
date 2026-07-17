---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-16
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **2026-07-16: WAVE 4b COMPLETE** — WIKI-117+118 merged (#103, squash 8f68d53, 4 sol review rounds): test-env hygiene (role tests pin WIKI_AGENT_ROLE/WIKI_AGENT_ID) + all five #91 hardening items, every fix mutation-verified. Arc survived two codex auth revocations + an app restart mid-gate. Waves 3+4 earlier same night: WIKI-127 (#98), WIKI-128 (#99), WIKI-116+124 (#100), WIKI-112 (#101), WIKI-129 (#102).
- **NEW SIDECAR REBUILD DEBT**: #103 backend changes (store.py, transcripts.py, wiki_artifacts.py) landed AFTER the WIKI-130 swap — deployed sidecar doesn't have them. Test-focused, low urgency; fold into next swap (staged flow: `FORCE_STAGE_ONLY=1` while app open, SIGTERM supervisor → swap-native-app.sh → relaunch).
- **WIKI-130 DEPLOYED 2026-07-15 ~21:10**: launch regression fixed (`.native-build-staging` ancestor marker, first Rust unit tests). Safe deploy flow proven; fleet auto-resumed via `recover_on_start`. 127 endpoints, 116 dedupe, 129 app.lock guard all live.
- **Open design threads**: (1) daemon-ize backend (launchd) — Henry interested, unticketed, next big structural win; (2) WIKI-126 rebrand BLOCKED on name pick; (3) cross-workspace switcher, file editing, semantic search (sqlite-vec) — unticketed.
- **Open wiki tickets**: compact-mermaid-preview (unfiled), WIKI-92/WIKI-109 deterministic frontend test failures at main (candidates for filing), WIKI-94 latency guard flake (candidate ticket).
- **2026-07-16: MISC CLONE ARC — 3 projects shipped in one day** (misc orch). Pufferclone v1+v2 complete (PUF-8..12, main@6bc5d64: non-blocking loads, MinIO e2e, bench baseline, memory budget, `puf` CLI). `tix` Linear-clone ticket CLI (TIX-1, master@9524a17). **`gauge` Prometheus-lite TSDB v0 COMPLETE** (~/me/fun/misc/gauge, GAU-1..5, master@9d32f0b, 22 sol rounds total: bit-audited Gorilla codec, hardened scraper w/ target identity, query engine w/ depth+point budgets, grapheme-safe terminal charts, node+proc exporters; success criterion verified live — `gauge graph` of real CPU). Follow-ups filed P3: wiki + pufferclone /metrics endpoints. Next clone candidates in [[local-cloud]] Clone pipeline: workflow engine (Temporal-lite) vs LSM KV — Henry discussion pending. Also unticketed: cargo install tix/puf/gauge, tix vault migration, PUF-13 NVMe tier.
- **Admin agent security doctrine (Henry 2026-07-16)**: agent-authored code executes ONLY in Modal sandboxes (gVisor, no network, no worker env); server-side stays for our fixed code over data. run_admin_python_snippet retired via PHO-13957 (post-13950 sequel, run_code_in_sandbox one-call wrapper).
- **Phoebe roadmap thread**: PHO-13940 org context graph (why orgs use Phoebe / feature-want tracking / gap-detection nudges) — 4 workstreams, filed 2026-07-16, next big admin-agent arc; deps: 12880 deliverable-2 (org<->Granola link), DEP-108 (Linear<->accounts).
- **Phoebe**: fleet archived 2026-07-16. Merged tonight: 11441/11444/11445 (batch), 11447 Modal sandbox (3 sol rounds), 11472 Slack self-join (4 rounds; NEEDS manual admin-bot Slack reauth for channels:join). Parked open drafts: #11383 scratchpad (PHO-13763, 3 review majors PARTIALLY fixed — worker archived mid-fix, resume before merge-driving) + #11475 tpuf scoping (PHO-13858, awaiting Henry read, open questions in PR body). Flake ticket PHO-13860 (admin_python_snippet + voice pre_greeting timing tests, blocked 2 PRs).

## Recent facts

- **Codex refresh token revoked TWICE 2026-07-15/16** — stale blocked workers holding old tokens suspected of poisoning the rotation chain on auto-resume. After re-auth: replace_agent every live cdx worker so all processes hold fresh tokens; kill/archive stale blocked ones FIRST. Probe auth cheaply: `codex exec --skip-git-repo-check -m gpt-5.4-mini "Reply OK"`.
- Workers survive backend restarts (own MCP sidecar, in-memory session) — status file `/tmp/agent-status/<T>.json` stays readable when backend down; monitors need CLI→file fallback.
- Reviewer state field unreliable — verdict step text authoritative, always.
- Supervisor MCP ops can 1s-timeout under load — verify effect before retrying steers.
- `wiki` CLI: log-done + todo add/move/complete; not on monitor-shell PATH — use `~/me/fun/wiki/wiki` absolute.
- cdx spawns can hang at provider startup (no status file) — startup-hang check in monitor template; fix via replace_agent.
- cdx workers can call request_plugin_install (github@openai-curated-remote) → unanswerable elicitation, turn hangs FOREVER (killed 3 phoebe sessions 2026-07-16). Spawn prompts must ban plugin installs + mandate `gh` CLI; fix via replace_agent + immediate ban steer.
- codex cyberPolicy flag can kill review turns whose prompts say "adversarial/hostile/attack" about test inputs (GAU-3-REVIEW1 2026-07-16) — phrase as "correctness/robustness/hand-compute", fix via replace_agent + rephrased steer.

- **Fleet monitor: watchlist file + re-alarms** (Henry 2026-07-16) — never rebuild monitors per spawn/archive; verdict re-alarm q5m until archived, review-gap q10m, staleness 30m. Reference impl in phoebe orch session.
- **Steer mode: default `now`** (Henry 2026-07-16) — on-idle queues behind the current turn (can be an hour); use on-idle only for genuinely-next-task input. Mistakenly queued → resend as now with supersedes note.
- **Phoebe worker test policy (Henry 2026-07-16)**: workers never run full local Bazel suites (no :all / multi-shard / //...). Pre-push = directly-touched targets only. CI authoritative; on red, pull failing targets from buildbuddy (bin/bb), rerun only those with --test_filter. Big changes: affected package targets OK, single pass. Reviewers: trust green CI, run only claim-guarding tests via --test_filter. Bake into every spawn prompt.

## Watchouts

- Next free ticket ID: WIKI-131.
- Pin review worktrees + gates to SHA (`--match-head-commit` on merge, worktree add at pinned SHA).
- Local main can hold unpushed vault/ticket commits — `git pull --rebase` (stash dirty vault notes first), then push.
- Stop BOTH worker+reviewer monitors at wrap-up; archive reviewer immediately after verdict routed.
- Workers' PR bodies can overclaim — reviewers verify claims by mutation, keep ordering it.
- `.codex/worktrees/` ~40 stale entries from older arcs — prune pending Henry (wave 4b's cleaned).
