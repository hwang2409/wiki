---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-08-03
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **PHOEBE (cc fable-5 orch)**: v3 harness umbrella COMPLETE 08-01 ~09:30Z — all seven steps folded into `henry/phoebe-v3-agent` at 05d616e98d (bazel 74/74 + ty clean; loops ran 10-11 review rounds each; fenced-lease model reconciled across workspace/retrieve/write). Step branches kept, no PR to main. MERGED to main 07-31: #13029 PHO-14968 admin tool surfaces, #13106 PHO-15038 turbopuffer hotfix (same-day prod RCA). #13031 PHO-14969 double-green at 39906b3267 awaiting Henry merge auth (github may still want a human approval click). Next: v2 deprecation plan + eval milestones per [[v3-harness-design]]. Merge auth Henry-only.
- **WIKI orch — WIKI-236 UX split arc, 08-03**: merged this arc: #174 WIKI-238 semantic activity + transcript hierarchy (4r, squash 09b8b83), #175 WIKI-243 supervisor recovery memory streaming (1r, squash 16a3b2a). Earlier 08-02: #167 WIKI-232 (26r), #173 WIKI-240, #172 WIKI-225, #171 WIKI-235, #170 WIKI-237, #168 WIKI-230/231, #166 WIKI-223, #169 WIKI-234. Orch merge auth standing ("just merge & spawn"). NOW RUNNING: WIKI-239 provider-diagnostics coalescing + WIKI-241 long tool-output controls (last two before WIKI-242 final a11y pass; contracts in /tmp/WIKI-239-contract.md, /tmp/WIKI-241-contract.md, both cc opus — fable-5 worker spawns credit-dead until Aug 5). Queued: WIKI-242, WIKI-227 backend /api/agents starvation (P1), WIKI-233 suite reds, WIKI-228/229, design arc 155/158/159/160. WIKI-177 #147 parked. Implementer-model lesson: luna left one-finding residue per round in deep lifecycle domains; opus swaps converged both loops.
- **MISC — LC arc COMPLETE 08-02 ~09:00Z**: local LeetCode clone in `misc/lc/app`, all five tickets merged (LC-1 card lifecycle, LC-2 32 concept visuals 3r, LC-3 themes, LC-4 150-problem bank, LC-5 practice runner 3r). No PRs — local-only. Henry granted standing merge auth for misc 08-02. Review verdicts in /tmp/LC-*-verdict.json. misc orch idle, no queued tickets.
- **WEBSITE**: redesign merged 07-31; push of local main = deploy, pending Henry.
- **Parked Henry gates (phoebe)**: #12574 PHO-14595 direction rethink; VOICE_INTERNAL_SHARED_SECRET_PRODUCTION secret missing (prod deploy blocked, PHO-14944); agent_v3_enabled flip; PHO-14940 BuildBuddy disk.

## Recent facts (07-31 late)

- Bazel slot starvation ROOT-FIXED: agent-shims/bazel leaked its flock fd into the server daemon; shim now holds the lock and releases on client exit ([[bazel-slot-shim-pathology]], backup .bak-20260731).
- Fleet-monitor crash loop: worker ids must match `^[A-Z][A-Z0-9]+-[0-9]+(-[A-Z0-9]+)*$`; invalid id (PHO-V3-INTEGRATE) crash-looped graph-health every tick and degraded the supervisor until renamed (PHO-0-INTEGRATE).
- Codex CLEAR as of 08-03: Henry re-authed; quota concern void — spawn cdx (gpt) workers freely again. Old "quota ~80%, fall back to cc" note is obsolete. `codex login status` proves nothing — watch auth.json mtime.

## Watchouts

- Review-loop protocol: verdict-route → archive reviewer → replace implementer with compact contract each round; verdict step text authoritative, not reviewer state field.
- Reviewers never run tests; implementers defer bazel to CI/integration when slots busy (push --no-verify only for pure slot contention, documented).
- Supervisor MCP ops can 1s-timeout under load; verify effect before retrying; reuse request_id; a failed spawn request_id can poison — retry once fresh after verifying no ghost registration.
- Soft cap 5 workers; above it expect provider timeouts. Backend restart SIGKILLs (-9) live cc worker processes mid-turn (seen 08-02 ~09:11Z: WIKI-232+225 both killed; uncommitted tree work survives) — after any backend restart, check provider pids and replace_agent + steer resume contracts. Status files under /tmp/agent-status stay readable when backend down.
- cdx gotchas: no plugin installs in spawn prompts; phrase reviews "correctness/robustness" not "adversarial"; after token re-auth replace live cdx workers if they wedge. 08-02: cdx sol hard-blocked mid-review by cyberPolicy while probing a local code-exec runner (path-traversal/XSS/loopback wording + probes) — once flagged, rephrase does not recover; remedy = replace_agent to cc opus. Runtime shows blocked but the status file keeps saying working; check state_reason.
- `wiki` CLI not on monitor-shell PATH — use `~/me/fun/wiki/wiki`.
- cc workers hallucinate the PR owner slug in status-file `pr` fields (hen-ry, mo-orchid seen 08-01/02 for hwang2409) — kickoffs should say "PR URLs via gh pr view --json url, not memory"; verify before trusting a status-file URL.
