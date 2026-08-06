---
type: reference
tags: [phoebe, evals, llm-judge, observability]
created: 2026-08-05
updated: 2026-08-05
---

# phoebe llm judge (agent_trace_auto_taggers)

surveyed 2026-08-05 at main dfa8c36b; full report was /tmp/PHO-0-JUDGE-SURVEY-report.md (ephemeral). package: `libraries/python/agent_trace_auto_taggers`.

## architecture

- two layers. conversation judge: continuous reviewer over idle live `general`/`outreach_analysis` conversations (idle 30m, <48h), temporal dispatch q5m, max 20 traces, single `gpt-5.6-terra` judge at temp 0 with a 374-line evidence-required rubric (`prompts/agent_trace_judge.md`); emits 19 checks, severity, formula-backed quality verdict, cancellation reason, unmet demand, request flow. episode judge (added 2026-08-04): classifies each settled RUN_STARTED episode into relationship/use case/outcome/capability/behavior; `gpt-5.6-luna` clean-facts, `gemini-3.6-flash` suspect-facts; default-off cron.
- deterministic taggers run before the LLM (TAGGER_VERSION=2); uuid-leak detection is code, not model.
- results: review_threads/notes/annotations tables, snowflake export view, admin trace UI, Agent Court routing for bad/urgent, triager alerts (unmet-demand alert 24h-suppressed).
- long traces: 400k char cap, head+tail keep with digested middle, 15-turn expansion windows, 8 iterations / 300s / 2M-token tripwire.
- judge trace preserved as its own conversation (auditable).

## known gaps (as of survey)

- NO judge goldens or judge-vs-human agreement testing anywhere; evals reuse the judge to grade subjects only. Add goldens before using verdicts as any release gate.
- fail-open: absent check blocks -> false; absent failure blocks -> "nothing wrong"; episode evidence accepts any valid uuid even outside the episode.
- raw user text/tool outputs go to providers truncated but unredacted (PII).
- conversation prompt dated but not content-hashed; episode prompt neither.
- taxonomy duplicated in frontend url contracts (drift risk).

## fit

- cannot compare v2/v3 trace PAIRS (case owns one conversation) — complements, does not replace, the shadow-parity gate.
- Agent Court already converts production failures into seeded eval drafts (matches golden-case doctrine).
- consolidation candidate: shared trace formatting/rubrics/model routing/cost reporting with `evals/eval_judge.py`.
- gotcha: the "veto candidate forks on tag conflicts" work is a recommendation-candidate safety prompt, NOT part of this judge.

## history

taggers apr 2026; keegan built the llm judge (f9413cea72, jun 9) + scoring + Agent Court; pyphoebe tagger integration; casper episode layer aug 4. fast-moving.

## related

[[v3-harness-design]], [[pi-minimal-harness-autoresearch]]
