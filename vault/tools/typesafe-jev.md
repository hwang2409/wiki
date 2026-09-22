---
type: reference
tags: [tools]
created: 2026-09-17
updated: 2026-09-22
---

# TypeSafe Jev

Jev is the flagship model of TypeSafe (docs.typesafe.ai) — "the first System One model". It makes fast, structured decisions that code consumes directly; it does not generate prose. Instead of coercing LLM text into structured form, it returns typed values and probability distributions.

Three composable primitives:

- **Choice** — select from predefined options, with probabilities and confidence scores.
- **Score** — evaluate a state against a rubric.
- **Noul** — judge the truthfulness of a statement on a 0–1 scale.

Many questions can be evaluated against the same state in one call, in parallel, with minimal overhead. Design philosophy: ask atomic, well-scoped questions and compose the answers in code, not prompts.

Docs: https://docs.typesafe.ai/introduction (Quick Start + patterns; llms.txt index at https://docs.typesafe.ai/llms.txt).

Competitor watch: [[laya]] (open-weights BERT-based clone of the choice/score/noul API, launched 2026-09-19; beats Jev on small-cardinality vendor benchmarks, loses badly >20 options).

Henry's project dir `~/me/fun/jev` (orchestrator id `jev`; umbrella since 2026-09-18: `router/` = research repo, `harness/` = zeta fork; single PRIVATE repo https://github.com/hwang2409/jev since 2026-09-18 — harness subtree-merged in at Henry's direction, full histories preserved) is named after it; empty as of 2026-09-17 — no code yet.

## API (verified live 2026-09-17)

- `POST https://api.typesafe.ai/v1/systemone`, header `Authorization: Bearer $JEV_API_KEY` (key in Henry's `~/.zshrc`).
- Body: `{"state": <string|object|array>, "model": "jev-latest", "questions": {<id>: <question>, ...}}` — all questions evaluate in parallel against the same state in one call.
- Question shapes: `{"type": "noul", "instructions": ...}`; `{"type": "choice", "instructions": ..., "criteria": {<option>: <description>}}`; `{"type": "score", "instructions": ..., "criteria": [<2-10 ordered level descriptions, low to high>]}`.
- Answers: noul → `noul` 0–1; choice → `choice` + `probabilities` + `confidence`; score → probability-weighted `score` + `legend` + `probabilities` + `confidence`.
- `jev-latest` resolved to `jev-1.13.0`. A 4-question call cost 459 input / 87 output tokens. Errors: 401 auth, 422 validation, 429 rate limit, 529 overload (retry with backoff).
- SDKs exist: Python + JavaScript (docs.typesafe.ai/sdk/) — typed questions, retries, sync/async clients; we hand-roll HTTP (works fine).
- **Full docs sweep 2026-09-18:** 18 cookbooks exist (function_calling, llm_guardrails, rerank, hierarchical_classification, sde_cascade, parallel_questions...). Advanced criteria: structured objects with `what`/`not_for`/`examples` per option (sharpens boundaries — plain strings undersell the API); structured instructions naming state fields; nested taxonomy-walking Choice criteria. **Jaggedness page (model-jaggedness/jev-1.13.md) is required reading:** no counting/date-math/hex (do arithmetic in code), literal instruction reading (state exact conditions), large irrelevant state degrades accuracy (pre-filter), state is treated as neutral data (prompt-injection risk — harden), thresholds do NOT transfer between primitive types, score outputs are for thresholds not magnitudes, not a text generator. Multi-question calls: report the LEAST certain judgment as call confidence (function_calling cookbook). Refactor ticket filed: `~/me/fun/jev/harness/docs/superpowers/specs/2026-09-18-jev-docs-refactor-ticket.md`.

## Tool-router experiment (phase 1 complete 2026-09-17)

Henry's idea: an agent harness with ONE tool — the agent describes its current step, Jev classifies it (Choice over the tool catalog + `needs_tool`/`step_clarity` Noul gates) and returns the tool to call. Phase 1 built the standalone router + 60-case eval in `~/me/fun/jev` (local git; spec + plan in `docs/superpowers/`; results in `RESULTS.md`).

Results (15-tool catalog, jev-1.13.0): top-1 accuracy 1.0 (0 confusions, incl. confusable pairs), needs_tool AUC 0.995, clarity AUC 1.0, ~762 in / 182 out tokens per route. Gotchas: `step_clarity` is conservative in absolute terms (mean 0.51 on clear steps) — threshold relatively, not at 0.5; calibration (wrong routes get lower confidence) untestable at 100% accuracy — needs phase 2's 50-150 synthetic-tool catalog to produce misses. Phase 3 idea: attach router to a real agent loop.

**jev-zeta (2026-09-18, WORKING):** fork of zeta at `~/me/fun/jev/harness` (part of the hwang2409/jev repo; never merged back into upstream zeta) where the agent sees ONE tool — `route` — and Jev picks every tool from the live registry catalog per turn (top-3 schemas advertised below 0.8 confidence; fail-open to full toolset for one turn on Jev errors; state resets at user-turn boundaries). Live smoke passed on Henry's Claude subscription OAuth: `route -> read -> route -> write`, top-3 expansion fired at 0.79, zero unrouted attempts. Spec + build history: fork's `docs/superpowers/specs/2026-09-17-jev-router-mode-design.md`. `--no-router` restores stock zeta.

**New-surface tools arc (2026-09-18, first wave SHIPPED):** pausanias memory tools (`memory_search`/`memory_read` over the CLI; live smoke recalled real vault content end-to-end; project auto-scoping after a live-smoke fix) and Apple Calendar tools (`calendar_events` + approval-gated `calendar_create`, EventKit via adapter seam, DST-probed wall-time semantics) merged into the harness. Calendar live read pending Henry's TCC grant (System Settings -> Privacy & Security -> Calendars). Repo consolidated: hwang2409/jev PRIVATE = umbrella + router/ + harness/ single repo, full histories subtree-merged; inner harness repo retired; interim jev-harness repo deleted. Next tool waves parked: vision suite, voice, outbound comms.

**Lane process (Henry 2026-09-18): jev workers open PRs.** Every implement lane: branch `jev-<n>-<slug>` in a worktree, push, `gh pr create` titled `JEV-<n>: ...` (<70 chars, Henry's PR body convention). Reviews route through the orch loop as before; fix commits land on the same PR. Orch squash-merges on clean pass + deletes branch (Henry tracks via PRs; flip to Henry-approves if he asks).

**Roadmap:** arc 2 COMPLETE 09-18 (jev-compaction + router v2 quality parity + in-turn history caching — see harness evals/RESULTS.md for the full three-way tables incl. the cache-arc rerun where stock got 40% cheaper too). **Upstream decision (Henry 2026-09-18): everything stays in the experimental fork; NOTHING ports to real zeta — incl. the caching win — until Henry explicitly decides. Do not re-raise.** Arc 3 approved = **Jev-navigated browsing**: Playwright toolset in the fork, page elements as the routing catalog (phase-2 economics apply — catalogs always huge, no cache to lose), page-state Noul gates, confidence-gated approvals for risky clicks, Jev-scored search-result triage. Live eval learning that motivates v2: dynamic per-turn tools defeat prompt caching — v1 router was ~2x stock's dollar cost at 10 cached tools despite fewer raw tokens (`~/me/fun/jev/harness/evals/RESULTS.md`).

Phase 2 (complete 2026-09-17, same day): 120-tool synthetic catalog, nested 15/30/60/120 subsets. Flat 120-option Choice works (no hierarchy needed). Degradation curve: top-1 1.0 / 1.0 / 0.975 / 0.975 across sizes; top-3 always 1.0. Full 140-case run: top-1 0.993, hard cases 19/20. Calibration: every route at confidence >= 0.5 was correct; all misses (n=3 across runs) sat at 0.35-0.76; Brier 0.0046. Harness rule that falls out: "confidence < 0.8 -> expose top-3" recovers every observed miss. Cost ~900 in / ~390 out tokens per route at 120 tools (criteria tokens dominate, linear in catalog size). Caveat: miss sample tiny even after a reviewer-audited hard-case sharpening round — the router beats our evalset's ability to trick it. Full data: `~/me/fun/jev/RESULTS.md` + `results/phase2-*.json`.

**Arc 4 safety tier ("yolo seatbelt", MERGED 2026-09-21, PR #5 squash 40c6fc4):** layer-0 static screen + Jev Score 0-3 + two Nouls, fail-closed. Three adversarial review rounds each broke it: round 1 = 8 findings (inline-batch `_skip_approval` bypass, cwd mismatch, pattern gaps); round 2 = 4 novel bypasses past the widened patterns; round 3 = 30+ fresh bypasses (`python3 -c`, `perl -e`, wrapper chains, heredocs, compound commands, even bare `rm -rf /etc`) PLUS false denials of benign commands PLUS a fail-open decision matrix that ignored positive Nouls. **Locked lesson: enumerating dangerous shell shapes never converges — auto-approve must be an allowlist privilege.** JEV-57 pivot: layer-0 classifies DENY / ESCALATE / ANALYZABLE; only positively-parsed simple commands (resolved wrappers, literal args, workspace-contained destructive targets) may auto-approve; anything un-parseable escalates to ask by construction. Rounds 4-6 confirmed the pivot: round 4 found only set-membership gaps (su/runas, credential-store breadth, osascript, persistence/exfil cluster, runner wrappers); round 5 found one blocker (runner-wrapper flag bypass: `uv run --with x python -c` resolved a flag as argv0); round 6 clean. Final shape: enforced `_LAYER0_RULES` reason taxonomy, escalate-on-ambiguity everywhere, 429 targeted tests, telemetry on the production late-sink path. Known tradeoff pending Henry: any `python3 script.py` escalates (exec-capable interpreter) — python-heavy work sees frequent prompts. Ops note: codex REFUSES adversarial safety-REVIEW contracts (cyberPolicy flag) — route those reviews to cc opus with an authorization-context preamble; implement contracts pass codex fine. Verdicts in the orch transcript; spec amendment in the fork's 2026-09-21 safety-tier design doc.
