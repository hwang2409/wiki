---
type: reference
tags: [tools, agents, models]
created: 2026-07-06
updated: 2026-07-06
---

# Model × Task Benchmarks (Codex vs Claude)

Audited 2026-07-06 against PRIMARY leaderboards after first-pass blog sources failed credibility review. Trust the primary numbers only.

## Verified (primary sources)

| Task | Signal | Primary evidence |
|---|---|---|
| Repo-scale SWE (official harness) | **GPT-5.4 xHigh leads** | [Scale SWE-bench Pro public](https://labs.scale.com/leaderboard/swe_bench_pro_public): GPT-5.4 xHigh 59.1% > Opus 4.6 (thinking) 51.9% > Opus 4.5 45.9% |
| Terminal/DevOps agentic | **GPT-5.5 harnesses lead** | [Terminal-Bench 2.0](https://www.tbench.ai/leaderboard/terminal-bench/2.0): top 5 all GPT-5.5 (82–85%), best Opus 4.7 harness 80.2%, Claude Code 58% (rank ~50) |
| Frontend / UI human preference | **Claude strong, not #1** | WebDev Arena (LMArena, real human prefs): Claude top-tier; Qwen 3.7 Max debuted above Opus 4.6 |
| Harness > model | — | Same model spans huge ranges per harness (GPT-5.5: 82.2% Codex CLI vs 84.7% NexAU-AHE; Claude Code vs WOZCODE 58%→80.2% on same-family models) |

## Unverified / retracted from v1 of this note

- "Opus 4.8 69.2% SWE-bench Pro" — NOT on official board; echoes across DataCamp/morphllm/etc. without methodology (likely vendor self-report, custom scaffold). Treat as marketing until on Scale's board.
- "Blind reviews prefer Claude 67%" — untraceable methodology (Reddit survey), anecdote tier.
- OSWorld/GraphWalks Opus-wins numbers — vendor-reported, unaudited.

## Source credibility audit

- Scale labs leaderboard, tbench.ai, LMArena — **primary, trust**.
- [CodeAnt SWE-bench post](https://www.codeant.ai/blogs/swe-bench-scores) — numbers matched official board; decent secondary.
- DataCamp/LogRocket/morphllm/mindstudio/evolink/smartscope — SEO/AI content farms echoing one unverified number set; internally inconsistent. **Do not cite.**

## Bottom line for worker assignment

Official standardized harnesses currently favor GPT-5.x on BOTH repo-scale (SWE-bench Pro) and terminal work; Claude's verified edge is human-preference frontend/UI work and harness-dependent quality. Harness choice moves scores more than model choice — benchmark on our own tickets before locking a policy.
