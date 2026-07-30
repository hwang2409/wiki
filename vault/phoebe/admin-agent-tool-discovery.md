---
type: decision
tags: [phoebe, admin-agent]
created: 2026-07-22
updated: 2026-07-22
---

# Admin agent tool discovery (PHO-14264 arc)


## Decision (locked 2026-07-22, Henry)

Adopt Anthropic-native tool discovery for the internal admin agent:

- `tool_search_tool_regex_20251119` primitive + `defer_loading: true` on cold tools (Option A), combined with Option C description hints for canonical-alternative overlaps (e.g. prefer `read_run_data(destination="sandbox")` over the sandbox 3-tool path).
- Registry partition at decision time: 157 tools / 66 definitions; 46 hot, 108 cold. Hot-set is a runtime knob; v2 target 20-30, eval-driven.
- Regex search first; BM25 only as later A/B. Keep `view_skills` separate (skills ≠ tools). Parent-level defer first; subagent defer is follow-up. cache_control on a deferred tool must fail loudly (framework assertion).
- Preserve PHO-14168 preload closure (methodology prompts only).

Design doc: `docs/notes/plans/admin_agent_tool_discovery_execplan.md` (merged PR #12080, squash c0e3bdee). Deterministic measurement: ~64% prefix-cost reduction; synthetic top-5 regex tool-selection eval scored 90%.

## Shipping chain (parent PHO-14264)

1. PHO-14285 — framework prerequisite: `defer_loading` + `tool_search_tool_regex_20251119` + cache_control assertion in `libraries/python/llm_framework/anthropic`.
2. PHO-14286 — LLM eval harness: 20-30 real prod prompts, baseline vs prototype. Bars: ≥30% median cost reduction, ≥95% answer parity AND ≥95% tool-selection score.
3. PHO-14287 — runtime wiring in `runtime_assembly` behind `admin_agent_tool_search` flag (ships OFF).
4. PHO-14288 — rollout: flag flip, Datadog monitors, hard revert guardrails (cache_creation_cost_share <55% within 48h or revert; P95 tool_search calls/run ≤5), 2-week bake, then cement.

blockedBy chain set in Linear: 14285 → 14286 → 14287(+14285) → 14288.

## Orchestrator assessment (2026-07-22)

Approach judged correct — architecture and sequencing sound. Native defer beats client-side custom (prefix untouched on discovery, cache survives, no bespoke serialization). Rollout shape cheap to abort at every stage.

Watch items (the load-bearing caveats):

1. **Synthetic eval scored 90% vs own 95% bar.** Whole bet rides on PHO-14286. Failure mode: agent doesn't search for tools it doesn't know exist — discovery quality = description quality, and cold-tail descriptions were never written for searchability. If 14286 misses the bar, the fix is description rewrites, not architecture change; budget for that pass.
2. **Cost fix ≠ selection fix.** Original problem had two halves: bloated prefix AND wrong-path tool choice. Defer solves cost; canonical-path selection rides entirely on Option C description hints. PHO-14286's tool-selection score must specifically count canonical-overlap cases (not just hot/cold recall) — else the eval passes while wrong-path behavior persists.
3. Minor: static hot-set constant (46) will drift with usage; knob exists. SDK-pin risk handled — 14285 must surface a fallback decision to Henry instead of silently building client-side Option E.

**Treat PHO-14286 as the go/no-go gate, not a formality.**

Related: [[mitmweb-rebuild]]-style arc notes; cost observability PHO-14259 (rollout monitors reuse its metrics); PHO-14168 preload closure; PHO-14258 classifier widening (smoking-gun prompt feeds 14286 corpus).
