---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-14
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **Knowledge layer v1 SHIPPED 2026-07-14** — WIKI-100 (#82 `0818766b`): rebuildable SQLite index over vault + wiki-managed run history. `wiki search "<q>" [--kind note|run] [--json]`, `wiki links backlinks|orphans|unresolved`, `wiki index rebuild`, `search_knowledge` MCP tool. **Agents: prefer `wiki search` over map.md-walk + grep for recall queries.** Goal test passed live: contact_attempts decision → hot.md note; Modal deploy race → run transcript with run_id:seq citation (unfiled knowledge findable). Spec: docs/superpowers/specs/2026-07-14-knowledge-layer-storage-design.md. v1.5 analytics + v2 embeddings deferred.
- **WIKI-101 SHIPPED 2026-07-14** (#81 `34eb99a9`) — cc render_artifact "rejected" badge fixed: structuredContent dropped from MCP success results (sentinel now reaches cc transcripts) + validated parser fallback. Found when orchestrator's own mermaid artifact showed rejected; root-caused + fixed same day.
- **Wiki fleet EMPTY**; both workers (gpt-5.6-sol) merged + archived same-day. Gate loop worked: WIKI-100 took 2 iterations (1 BLOCKING + 1 HIGH resilience holes found by adversarial review, fixed + revert-tested), WIKI-101 took 2 (2 MEDIUMs).
- **First knowledge.db rebuild in progress** (~700MB, one-time — real corpus ≫ fixture; budget <60s applies to fixture only). Concurrent `wiki index rebuild` during a running rebuild → "database is locked" per-run skips + stale-result degradation (by design, verified live).
- **Wiki.app bundle STALE vs main**: running sidecar predates #81/#82 — transcripts-parser artifact fix + knowledge module reach Wiki.app only after `make native-build` + relaunch. CLI/`wiki search` work from repo checkout regardless.
- **Phoebe (2026-07-14)**: PHO-13646 merged (#11271) — batch account-health rollups behind `ACCOUNT_HEALTH_BATCH_ROLLUP_ENABLED` (enable + watch first nightly pulse); PHO-13647 open (PostHog users metric descoped). Post-#11222 ops still pending: revoke 6 `ADMIN_AGENT_SNOWFLAKE_*` prod secrets. Unpushed staging-deploy race fix parked at `b4d5e7f9` (`henry/staging-modal-deploy-investigation-2`) — PR or drop.

## Recent facts

- Vault reconciled + pushed 2026-07-14 morning: 11 merged tickets pruned, 9 stale worktrees removed; kept `wiki-43-terminal-fidelity` (unmerged ~500-line commit, no PR — ship or drop), `wiki-41-native-surfaces` + `wiki-24-hidden-probe` (dirty) pending Henry call.
- `wiki todo complete` appends the FULL ticket body to done.md — dedupe/replace with terse PR-linked line after (bit us twice 07-14).
- Monitor scripts: status-file merge-ready greps re-emit every poll — dedupe by (state|step|pr) hash AND verify worker runtime_state + PR head SHA before gating; stale echoes otherwise.
- Headless spawn contract: `workdir`=real worktree, `orch`=orchestrator id; steer via POST /message mode=now; wrap-up via POST /archive.
- Provider hang playbook: no events N min → nudge → interrupt + resend → /replace.

## Watchouts

- Two+ workers touching same file surface: note overlap in both prompts; whoever merges second rebases (WIKI-100 rebased over #81's wiki_artifacts change cleanly after steer).
- Schema-touching phoebe PRs: `migrate apply` locally; catalog conflicts → regenerate.
- Ship-shaped findings → file ticket IMMEDIATELY (WIKI-101 same-day proof).
- Local main PUSHED before spawns; refetch todo/map/hot before writing; screenshots = LOCAL /tmp paths.
