---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-31
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **PHOEBE (cc fable-5 orch, replaced ~20:13Z 07-31)**: v3 harness arc. Henry locked `henry/phoebe-v3-agent` as THE experimental branch (no PR to main); PHO-0-INTEGRATE merged the four settled step branches (describe, parity, tool-search PHO-14963, helpers PHO-14977) at 4ffbde953b and is running bazel+ty on the result. In review: PHO-14972 workspace (R8 @e80e3db303), PHO-14973 retrieve (R9 @ef35af3fe9), PHO-14975 write (R7 @cda7160bf6) — these three fold into the umbrella after clean verdicts. PRs: #13029 PHO-14968 (CI green @d1d11cba9e, REVIEW2 running), #13031 PHO-14969 (CI green @5864536c, REVIEW2 running + needs human GitHub approval). RCA-2222 (cc opus): prod turbopuffer `distance_metric` 400 breaking every admin-note index write; RCA then orch-steered fix, Henry authorized. Merge auth Henry-only.
- **WIKI orch**: PR #150 WIKI-190/191 CLEAN after 46 rounds — awaiting Henry merge auth. WIKI-154 PR #160 and WIKI-219 PR #165 in review loops. Queue: WIKI-223, 226, 144/149/155/158/159/160 + artifact arc. WIKI-230: main frontend suite red (pre-existing, excluded from gates).
- **WEBSITE**: redesign merged 07-31; push of local main = deploy, pending Henry.
- **Parked Henry gates (phoebe)**: #12574 PHO-14595 direction rethink; VOICE_INTERNAL_SHARED_SECRET_PRODUCTION secret missing (prod deploy blocked, PHO-14944); agent_v3_enabled flip; PHO-14940 BuildBuddy disk.

## Recent facts (07-31 late)

- Bazel slot starvation ROOT-FIXED: agent-shims/bazel leaked its flock fd into the server daemon; shim now holds the lock and releases on client exit ([[bazel-slot-shim-pathology]], backup .bak-20260731).
- Fleet-monitor crash loop: worker ids must match `^[A-Z][A-Z0-9]+-[0-9]+(-[A-Z0-9]+)*$`; invalid id (PHO-V3-INTEGRATE) crash-looped graph-health every tick and degraded the supervisor until renamed (PHO-0-INTEGRATE).
- Codex auth died (refresh unauthorized) ~21:30Z, Henry re-authed ~22:10Z; parked spawn queue drained. `codex login status` proves nothing — watch auth.json mtime.
- Codex weekly quota was ~80% — may exhaust before reset; fall back to cc workers.

## Watchouts

- Review-loop protocol: verdict-route → archive reviewer → replace implementer with compact contract each round; verdict step text authoritative, not reviewer state field.
- Reviewers never run tests; implementers defer bazel to CI/integration when slots busy (push --no-verify only for pure slot contention, documented).
- Supervisor MCP ops can 1s-timeout under load; verify effect before retrying; reuse request_id; a failed spawn request_id can poison — retry once fresh after verifying no ghost registration.
- Soft cap 5 workers; above it expect provider timeouts. Workers survive backend restarts; status files under /tmp/agent-status readable when backend down.
- cdx gotchas: no plugin installs in spawn prompts; phrase reviews "correctness/robustness" not "adversarial"; after token re-auth replace live cdx workers if they wedge.
- `wiki` CLI not on monitor-shell PATH — use `~/me/fun/wiki/wiki`.
