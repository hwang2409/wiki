---
type: reference
tags: [zeta, agents]
created: 2026-09-02
updated: 2026-09-02
---

# Henry Daily Driver Profile


## Source

Trace audit 2026-09-02 by the zeta orchestrator: ~30 genuine Claude Code sessions (Jul-Sep), 1,105 codex sessions classified (only ~10 direct), all 2,094 zeta session dirs (92 real, ~25 substantive, one dogfood week Aug 20-27).

## Profile: Henry as operator, not pair-programmer

1. Ops chore-runner (~50% of usage). Most-typed prompt (~8x): "refetch origin/main, rebuild, relaunch, kill the sqlite files, thanks." Canned chores dominate.
2. Status glass. 99 /status calls + constant "hows it going" polls. Fire-and-poll pattern; wants ambient push progress, not pull.
3. Delegation box. "RCA this and make a PR: <linear url>". Inputs: URLs (Linear/GitHub/Datadog) and screenshots. Multi-turn collaboration rare.
4. Style: terse lowercase fragments, minimal context, mid-task steering ("wait", "stop stop stop"), chains unrelated tasks in one session, resumes after shutdown. Hates permission prompts and unwanted autonomy (subagents spawning unasked).

## Zeta dogfood history

- One intense week Aug 20-27: hellos -> tool smoke tests (created zeta_test.txt Aug 26) -> real coding (3D Game of Life 78 turns, dsa-vis project).
- Quit causes, visible in traces: 5 fresh sessions re-typing "can you recover that?" (no working resume), mid-stream session deaths, fetch 10k truncation, doubted sub-agent parallelism.
- ALL fixed after he left: ZETA-42 resume replay, ZETA-43 resilience/draft/retry, ZETA-34 fetch pagination, ZETA-41 parallel sub-agents, #78 history/undo, #80 transcript nav.

## Implications for zeta arcs

- Highest-leverage next build: user-defined slash commands / macro DSL (canned ops chores).
- Then: push-style ambient status for background work.
- Re-dogfood invitation stands: the frictions that ended week one are all resolved on main (2883c7b).
