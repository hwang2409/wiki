---
type: reference
tags: [tools, agents, math, proving]
created: 2026-07-21
updated: 2026-07-21
---

# LLM Rigorous Proving: landscape + tight framework

Deep-research pass 2026-07-21. Companion to [[jacobian-conjecture-fable-2026]] — that incident produced an *arithmetically-checkable artifact*, not a kernel-verified proof. This note is about the pipeline you'd build if you wanted LLM-produced proofs that survive as **actual mathematics** rather than as "model output that happens to check."

## Landscape summary

The LLM+formal-proof stack has stratified into four clear mechanism tiers by mid-2026. (1) Whole-proof RL samplers: Kimina-Prover-72B (Moonshot/Numina, July 2025) leads open miniF2F-test at 87.7% pass@1024 / 92.2% with TTRL search; DeepSeek-Prover-V2-671B (April 2025) hits 88.9% pass@8192; Goedel-Prover-V2-32B (Aug 2025) matches at 90.4% pass@32 with a 20x smaller model via verifier-guided self-correction, and beats V2-671B on PutnamBench pass-for-pass. (2) AlphaZero-style tree search with autoformalization: AlphaProof (DeepMind, Nature Nov 2025) is the only system to Lean-verify IMO problems at silver level (28/42 at IMO 2024, 4/6 problems), but is closed-weights and consumed ~80,000 TPU-days plus per-problem TTRL. (3) Tactic-level copilots: LeanCopilot and LLMLean plug models into Lean's tactic loop with in-kernel type-checking; LeanDojo/ReProver is the reproducible baseline (26.5% miniF2F-test pass@1, one A100-week to retrain) and its toolkit underpins nearly every downstream Lean RL paper. (4) In-context agents and sketch pipelines: COPRA (COLM 2024) runs GPT-4-turbo as a DFS policy across Lean+Coq+Isabelle with no fine-tuning, reaching ~30% miniF2F in 60 queries; Draft-Sketch-Prove (ICLR 2023) established the informal-draft -> Isar-sketch -> Sledgehammer paradigm still echoed in Goedel and Kimina. Autoformalization is the standing bottleneck: MMA back-translation and Kimina-Autoformalizer-7B (Apache-2.0) are the current open defaults, but statement-level accuracy sits at 16-18% "acceptable with minor fixes" on ProofNet. Lean 4 + Mathlib has decisively won substrate share; Isabelle remains alive mainly through Sledgehammer's strength; Coq/Rocq is a rounding error in this line of work. Terence Tao's blueprint-first human-in-the-loop workflow (PFR, Equational Theories Project) shows the frontier of research-scale usage: LLMs are minor contributors, classical ATPs (Vampire, Prover9) and human decomposition still do most work.

## Systems surveyed

### DeepSeek-Prover-V2 (671B and 7B; supersedes V1.5)
*Ecosystem:* Lean 4 (autoformalization + native tactic-style proof generation; verified by the Lean 4 kernel)

*Core mechanism:* Two-model pipeline built on a recursive-subgoal cold-start + RL loop, with tree search at inference.

Cold-start data synthesis (V2): DeepSeek-V3 is prompted to (a) produce an informal chain-of-thought proof sketch in natural language and (b) decompose the theorem into a sequence of Lean 4 `have` subgoal statements (a "proof skeleton"). Each subgoal is dispatched to the smaller 7B DeepSeek-Prover-V1.5-based prover, which searches for a Lean 4 tactic proof of that subgoal. When every subgoal closes, the resolved Lean subproofs are stitched back into a complete formal proof and paired with the original DeepSeek-V3 informal CoT. This produces (informal reasoning -> formal Lean proof) training pairs on hard problems the small prover could not solve monolithically — the "cold-start" corpus.

SFT + RL: The 671B model (DeepSeek-V3-Base backbone) is SFT'd on this synthetic subgoal-decomposition corpus (unifying informal math reasoning and formal proof synthesis in one policy), then RL'd with GRPO-style group-relative policy optimization using binary rewards from the Lean 4 verifier (RLPAF — reinforcement learning from proof-assistant feedback). Consistency reward aligns the emitted formal proof's structure with the CoT decomposition.

Inference (V1.5, still applicable to the 7B): RMaxTS — a Monte Carlo tree search variant with intrinsic-reward-driven exploration. The prover generates partial tactic sequences; each Lean state after tactic execution is a tree node; RMax intrinsic rewards push exploration toward novel proof states, avoiding collapse into a single sampled trajectory. V2 (671B) primarily uses long-CoT sampling with pass@N and its subgoal-decomposition prompting; the smaller 7B contributes non-CoT tree search that solves problems the CoT model misses.

Concretely a builder would: (1) pick a formal math base LM (Lean 4 REPL-integrated), (2) SFT on a corpus of `have`-skeleton decompositions paired with per-subgoal tactic proofs, (3) RL against the Lean kernel with 0/1 reward and GRPO across a group of samples per problem, (4) at inference either sample many long-CoT proofs and verify, or run RMaxTS over the tactic tree.

*Strengths:*
- State-of-the-art on miniF2F-test (88.9% pass ratio, V2-671B) — highest reported for an open-weights Lean 4 prover at release
- Solves 49/658 PutnamBench problems (V2-671B), a hard competition-math formalization benchmark
- Fully open weights on Hugging Face (both 7B and 671B), MIT-licensed code + a separate Model Agreement for weights — reproducible unlike closed systems (AlphaProof)
- Unifies informal chain-of-thought math reasoning with formal Lean 4 proof generation in a single policy, so the model can 'think in math' then emit verified proofs
- Recursive subgoal decomposition scales to hard problems that monolithic tactic search fails on
- Kernel-verified: every reported success is checked by Lean 4, so no hallucinated proofs count
- Small (7B) variant available for cheap tree-search inference; complements the 671B CoT sampler
- New ProverBench (325 problems incl. 15 AIME 2024–25) released alongside for finer-grained eval

*Limitations:*
- 671B is enormous — inference is expensive; realistically needs a serving cluster. Practical use for most builders is the 7B variant
- PutnamBench score (49/658 ≈ 7.4%) shows competition-math formalization is still largely unsolved
- Restricted to Lean 4; no Coq/Isabelle/HOL support
- Cold-start pipeline requires DeepSeek-V3 as the decomposer — reproducing the training corpus needs access to a comparable frontier model
- Autoformalization of the problem statement itself is out of scope — inputs are assumed already Lean-formalized (miniF2F/PutnamBench statements are hand-formalized)
- Long-CoT sampling with large pass@N budgets drives most of the headline numbers; pass@1 is meaningfully lower
- Model weights are under a bespoke Model Agreement, not a standard OSI license — commercial use terms differ from the MIT-licensed code

*Maturity:* Actively maintained, released April 2025 (V2; arXiv 2504.21801) superseding V1.5 (Aug 2024; arXiv 2408.08152). Open-weights on Hugging Face: DeepSeek-Prover-V2-671B (DeepSeek-V3-Base backbone, MoE) and DeepSeek-Prover-V2-7B (32K context, built on V1.5-Base). Headline benchmarks: miniF2F-test 88.9% pass ratio (671B, up from V1.5's 63.5%); PutnamBench 49/658 solved; 6/15 selected recent AIME problems solved in formal Lean. Ships with ProverBench (325 problems) as a new eval set. Code MIT-licensed, weights under a separate Model Agreement. Fully reproducible inference; training recipe described in the paper but full training data/scripts not released.

*Source:* https://github.com/deepseek-ai/DeepSeek-Prover-V2

### Kimina-Prover (Moonshot AI + Project Numina). Latest: Kimina-Prover-72B (blog post July 10, 2025). Preview version: arXiv 2504.11354 (April 2025). Distilled variants: Kimina-Prover-Distill-8B (Qwen3-8B) and Kimina-Prover-Distill-1.7B (Qwen3-1.7B).
*Ecosystem:* Lean 4 (with Mathlib; miniF2F-test reported against Mathlib v4.15). Whole-proof generation model, not a tactic-level model.

*Core mechanism:* Whole-proof generation LLM trained with long chain-of-thought RL, no external tree search, no value function, no PRM. Pipeline: (1) Base = Qwen2.5-72B. (2) Continued pre-training on ~6B tokens of Lean data (260M tokens scraped Lean/GitHub + 5.5B tokens of compiler-validated RL rollout data structured as state->tactic->state and state->tactic->error). (3) SFT cold start teaches a "Formal Reasoning Pattern": the model writes an informal human-style CoT interleaved with Lean 4 code blocks, iteratively drafts/refines proof steps before emitting the final proof. Cold-start CoT traces synthesized with Claude 3.7 Sonnet. (4) Large-scale RL using the Kimi k1.5 pipeline with 32K-token context; reward is binary from Lean compiler on the whole proof (no prover feedback interleaved at inference), plus a preference reward that rewards using provided lemmas and penalizes ignoring relevant ones. (5) Prompt set curated from NuminaMath 1.5 olympiads-ref subset, dynamically filtered from 300K down to ~90K problems - too-easy ones removed, too-hard ones decomposed into sublemmas. (6) Error-fixing SFT on (bad_proof, Lean_error, good_proof) triplets plus batched failure replay: each RL iteration mixes ~500 failures from prior iteration with ~500 fresh problems, teaching the model to read Lean errors and self-correct. (7) Random Proof Cut augmentation: truncate proof end or replace an internal block with `sorry`, model completes. (8) Inference: pass@N sampling at temperature 0.6, top-p 0.95, up to 8096 output tokens; for hard problems, "attempt-and-fix" alternates generation with Lean feedback (e.g. 16+16 or 32+32 rounds). (9) Test-Time RL (TTRL) search for the 92.2% number: agentic loop that proposes lemmas, tracks per-lemma utilization score, keeps top scorers (60% exploit / 40% explore), prunes below tau=0.10 after 50 attempts, generates sublemmas after N=128 failures, filters by attempting to prove the negation; recursive lemma decomposition can produce 500+ line proofs (e.g. IMO 1969 P2 via 4-layer decomposition).

*Strengths:*
- Simple architecture: pure whole-proof generation, no MCTS/value function/PRM - easier to reproduce than DeepSeek-Prover-V1.5 or InternLM2.5-StepProver style tactic-search systems.
- Strong sample efficiency at low budgets on miniF2F-test: pass@1 = 63.9%, pass@8 = 65.16% (Preview), pass@32 = 84.0%, pass@1024 = 87.7% (72B).
- Human-readable proofs: interleaved natural-language CoT with Lean code, bridging informal math intuition and formal verification.
- Clear scaling with model size (1.7B -> 8B -> 72B all monotonically improve) - previously unobserved for neural theorem provers.
- Distilled 1.7B and 8B models are strong (pass@32 = 73.4% and 78.3% on miniF2F-test) and Qwen3-based, so consumer-GPU deployable.
- Error-correction loop (attempt-and-fix) beats brute-force sampling at equal budget (44.1% vs 28.8% at 32 samples on 59 hard miniF2F problems).
- Open ecosystem: MIT-licensed weights on HuggingFace for 72B and distills, open-source Kimina Lean Server for scalable RL rollouts, open Kimina-Autoformalizer-7B, rectified miniF2F-test dataset released.

*Limitations:*
- Public numbers focus on miniF2F-test; official PutnamBench and ProofNet numbers are not prominent in the primary blog/README (though third-party evals exist).
- Massive test-time compute for headline 92.2% number: TTRL uses an estimated ~42,000-sample budget - not comparable to pass@32 or pass@1 baselines.
- 72B inference needs 8-GPU tensor parallelism (per model card) - not cheap to serve.
- RL reward is binary Lean-compiles signal + lemma-usage preference; no fine-grained credit assignment, so training is sample-hungry (~6B tokens of rollout data).
- Cold-start reasoning traces distilled from a closed model (Claude 3.7 Sonnet) - reproducing the SFT stage independently requires re-synthesizing traces.
- Preview paper (arXiv 2504.11354) documents the older single-step version; the 72B TTRL/error-fix machinery is only in the HuggingFace blog post - a full 72B paper was noted as in-preparation.
- Coupled to a specific Mathlib version (v4.15 referenced for the IMO 1969 P2 proof); Mathlib churn is a real maintenance cost.
- Prompt set skewed to olympiad-style competition problems; generalization to research-level formalization (ProofNet-style) is less established in official materials.

*Maturity:* Production-quality open release. Kimina-Prover-72B (MIT license, Qwen2.5-72B base) released July 10, 2025 on HuggingFace (AI-MO/Kimina-Prover-72B) alongside Kimina-Prover-Distill-8B and Kimina-Prover-Distill-1.7B. Benchmark numbers (miniF2F-test, Mathlib v4.15, rectified variant): Preview = 80.7% pass@8192; 72B = 63.9% pass@1, 84.0% pass@32, 87.7% pass@1024, 86.4% pass@32 with error-correction, 92.2% with full TTRL search (~42K sample budget). Also solves IMO 1969 P2 with a 520-line Lean proof. Supporting infra open: Kimina Lean Server, Kimina-Autoformalizer-7B, rectified miniF2F-test dataset, RL pipeline at github.com/project-numina/kimina-prover-rl. Widely cited by follow-up provers (Goedel-Prover-V2, Seed-Prover, etc.).

*Source:* https://huggingface.co/blog/AI-MO/kimina-prover

### AlphaProof (Google DeepMind) — Nature 2025 version, incl. AlphaProof Nexus outputs release
*Ecosystem:* Lean 4. Autoformalization pipeline fine-tuned from Gemini translates natural-language problems into Lean statements; all proofs are written and checked in Lean 4.

*Core mechanism:* Two-stage pretraining + AlphaZero-style RL loop against the Lean kernel, plus test-time RL (TTRL) per problem. (1) Base model: a large encoder-decoder transformer trained ONLY on code + math (no general web text) using a mix of next-token prediction and span reconstruction; decoder sees ~3T tokens, encoder ~12T, ~50 epochs. The encoder-decoder split is deliberate — the encoder embeds a long Lean proof state (thousands of tokens) once, then the decoder samples many short tactics (tens of tokens) in parallel with high batch efficiency. (2) Autoformalizer: a Gemini model fine-tuned to translate ~1M+ informal problems into candidate Lean statements (many noisy variants per problem); statements that Lean type-checks and that the prover can prove or disprove become training data. (3) Proof search: AlphaZero-style tree search over Lean tactics, guided by a policy/value proof network. Innovation: adds "product nodes" (AND-nodes) alongside standard OR-nodes so multi-goal tactics like induction don't force sequential backtracking — you must prove all children, but siblings can be searched in parallel. Every completed proof is Lean-verified and fed back to fine-tune the proof network — a closed self-play/expert-iteration loop, ~80,000 TPU-days for the main RL phase. (4) Test-time RL (TTRL): for a hard held-out problem (e.g., an IMO problem), a variant generator produces a bespoke curriculum of related, usually easier, formal variants; a focused AlphaZero-style RL run trains on those variants at inference time, adapting the proof network to the specific problem's structure before attempting it. This is what let it crack IMO 2024 P6.

*Strengths:*
- Only system to date to formally solve IMO problems at silver-medal level (IMO 2024: 4/6 problems, 28/42 points, incl. P6 which only 5/609 humans fully solved); proofs are Lean-checked, not just plausible-looking.
- End-to-end formal: no trust in an LLM's chain-of-thought — Lean kernel is the ground-truth verifier, so results are reproducible and not hallucinated.
- Autoformalization pipeline unlocks orders-of-magnitude more training data than the ~hand-curated Lean/mathlib corpus (millions to ~100M formal problems).
- Test-time RL (variant curriculum) is a genuinely novel inference-time compute knob — trades TPU-days per problem for solving previously intractable problems.
- AND/product-node tree search is a clean architectural fit for tactic-based proving with multi-goal tactics like induction.
- Saturates miniF2F-valid (reported near-perfect / perfect on valid split in the Nature extended tables) and reaches ~56% on PutnamBench, well above prior open provers at time of release.

*Limitations:*
- Not open-weights and not open-source. Only the AlphaProof Nexus proof outputs (Lean files + NL prose) are released at google-deepmind/alphaproof-nexus-results. No model, no autoformalizer, no training/search code — third-party reimplementations (e.g. Kripner/nanoproof) exist but are unofficial and much smaller.
- Extreme compute: ~80,000 TPU-days for the main RL phase, plus per-problem TTRL runs that took up to ~3 days on IMO problems. Not something a small lab can reproduce.
- Geometry gap: at IMO 2024 the geometry problem was handled by a separate system (AlphaGeometry 2), not AlphaProof itself — AlphaProof is not a general geometry solver.
- Autoformalization is noisy: relies on generating many candidate Lean statements per informal problem and filtering by Lean type-checking + provability, which biases the training distribution toward statements the system can already handle.
- Solve times on hard problems are hours-to-days, not interactive; the reported IMO run required grading-style budgets, not real-time use.
- Restricted to problems expressible in Lean 4 / mathlib's formal vocabulary; long-form research-math conjectures with informal or missing definitions are still out of reach unless someone formalizes the setup.
- miniF2F is a known-flawed benchmark (see the 2025 miniF2F-Lean Revisited paper) — 'saturation' partly reflects benchmark limits, not solved mathematics.

*Maturity:* "Peer-reviewed in Nature, Nov 12 2025 ('Olympiad-level formal mathematical reasoning with reinforcement learning', s41586-025-09833-y). Headline results: IMO 2024 silver-medal equivalent, 28/42 points, 4/6 problems solved with fully Lean-checked proofs (P1, P2, P4, P6); ~56% on PutnamBench (672 college-level Putnam problems); saturates miniF2F-valid and reports near-perfect scores on miniF2F-test in the extended tables. NOT open-weights and NOT open-source — DeepMind released only the AlphaProof Nexus proof artifacts (Lean + NL prose) at github.com/google-deepmind/alphaproof-nexus-results. No independent full reproduction exists; nanoproof is a minimal community reimplementation from the paper's pseudocode. Latest public delta beyond the IMO 2024 result is AlphaProof Nexus, which applies the same pipeline to research-math targets (Erdős problems, OEIS, Stacks Project, additive combinatorics, algebraic geometry, graph theory, optimization, quantum optics)."

*Source:* https://www.nature.com/articles/s41586-025-09833-y

### LeanCopilot (and its sibling LLMLean / originally LLMSTEP)
*Ecosystem:* Lean 4 (Mathlib4). Requires lean4:v4.3.0-rc2+; LeanCopilot latest v4.31.0 (June 2026) tracks recent toolchains. LLMLean is also Lean 4 only. Neither targets Coq/Isabelle.

*Core mechanism:* Two related but distinct systems, both plug an LLM into Lean's tactic loop rather than doing whole-proof autoformalization.

LeanCopilot (Song, Yang, Anandkumar, arXiv 2404.12534, NeuS 2025). Three tactics: (1) suggest_tactics — one-shot next-tactic generation from the pretty-printed goal state; (2) search_proof — best-first proof search where LeanCopilot's LLM-generated tactics are injected into aesop's rule set as goal-dependent rules, scored by the LLM's confidence (log-prob) which is used as the aesop critic; (3) select_premises — dense retrieval over Mathlib using an encoder that embeds the goal and does nearest-neighbor over a precomputed premise embedding index. Default generator is ct2-leandojo-lean4-tacgen-byt5-small and retriever ct2-leandojo-lean4-retriever-byt5-small — these are the ReProver ByT5-small models from LeanDojo (Yang et al. NeurIPS 2023), fine-tuned on the LeanDojo Benchmark 4 (state -> next-tactic pairs mined from Mathlib traces) plus retrieval-augmented premise selection contrastive training. Inference is done natively inside the Lean process via CTranslate2 FFI (C++ transformer runtime), which is why the shipped models are small (byt5-small ~300M) — no Python server needed, models load in-process, quantized. Users can swap in "external" models over HTTP or "generic" model backends.

LLMLean (cmu-l3, evolved from Welleck & Saha's LLMSTEP, arXiv 2310.18457). Two tactics: llmstep (next-tactic, optional prefix like `apply `) and llmqed (attempt to close the whole goal). Original LLMSTEP: Lean 4 tactic sends the current goal state to a Python server hosting an LLM, server generates k candidate tactics via sampling/beam, each candidate is elaborated inside Lean, and only those that typecheck are surfaced in the InfoView. Baseline model was Pythia-2.8b-deduped fine-tuned on LeanDojo Benchmark 4 in the Han et al. proofstep format (wellecks/llmstep-mathlib4-pythia2.8b on HF). LLMLean generalizes this to (a) cloud APIs — OpenAI GPT-4o default, Anthropic, Together, any OpenAI-compatible endpoint — and (b) local via Ollama, with prover-specific weights like BFS-Prover-V1/V2 and Kimina-Prover. llmqed additionally supports parallel sampling and iterative refinement: on failure, error messages from Lean are fed back as feedback for the next round.

Builder-imitation sketch: (1) mine (state, next_tactic) pairs from Mathlib using LeanDojo's tracing; (2) fine-tune a small enc-dec (byt5-small) with tactic generation loss and, optionally, a retrieval encoder with in-batch contrastive loss over premise usage; (3) expose a Lean tactic that pretty-prints the goal, calls the model (native via CTranslate2 or over HTTP), returns top-k tactic strings + logprobs; (4) for whole-proof search, wrap aesop's best-first frontier and inject each generated tactic as a candidate edge weighted by -logprob; (5) elaborate each candidate in Lean to check well-typedness before adding to the InfoView or the search frontier.

*Strengths:*
- Native in-Lean integration - suggestions are typechecked against the real goal state before being shown, so no hallucinated-syntax noise reaches the user.
- LeanCopilot runs fully offline via CTranslate2 - no API keys, no server, works in air-gapped envs, models are quantized and small enough to run on CPU.
- search_proof composes cleanly with aesop rather than replacing it - you inherit the mature symbolic search infra and just add LLM-guided edges scored by log-prob.
- Both are open source (MIT). ReProver ByT5 weights and llmstep-mathlib4-pythia2.8b are open on HuggingFace under wellecks/ and kaiyuy/.
- LLMLean is model-agnostic and swappable - trivially point it at GPT-4o, Claude, a local Ollama BFS-Prover, or Kimina-Prover, which lets it ride SOTA prover releases without code changes.
- Retrieval (select_premises) tackles the real bottleneck in Mathlib work - finding the right lemma name - which pure LLM generation still fumbles.

*Limitations:*
- Default LeanCopilot generator is byt5-small (~300M) - weak versus modern 7-70B provers; the paper's headline numbers come from Mathematics in Lean (MIL, 168 theorems), not miniF2F-test or the full LeanDojo Benchmark, so absolute strength is understated by the paper but the shipped default is genuinely small.
- ReProver's own miniF2F-test result is 26.5% pass@1 (LeanDojo NeurIPS 2023) - respectable in 2023 but far below current SOTA (Kimina, DeepSeek-Prover-V2, BFS-Prover-V2 all clear 80%+ on miniF2F-test with larger models and RL).
- LeanCopilot paper only benchmarks on MIL: 74.2% of proof steps automated (autonomous) vs aesop 40.1%; human-assisted mode averages 2.08 manual steps vs aesop's 3.86. No head-to-head miniF2F number is published.
- LLMLean with cloud models leaks proof states to third-party APIs - a real concern for confidential formalization work; the local Ollama path fixes this but with the usual local-inference latency and VRAM cost.
- Neither system does end-to-end autoformalization (informal statement -> formal statement); both assume the human has already stated the goal in Lean.
- Tactic-level generation with a small model + best-first is dominated on hard olympiad problems by whole-proof samplers with large RL-trained models; these tools are copilots for interactive work, not autonomous olympiad solvers.
- Lean version churn: because LeanCopilot links C++ (CTranslate2) into Lean, toolchain upgrades occasionally break the build until a matching release ships.

*Maturity:* Both are stable, actively maintained, open-source (MIT), and in real use by Mathlib contributors. LeanCopilot: v4.31.0 as of June 2026, weights on HuggingFace (kaiyuy/leandojo-lean4-tacgen-byt5-small, kaiyuy/leandojo-lean4-retriever-byt5-small). Paper published as NeuS 2025; reported eval only on Mathematics in Lean (168 theorems): 74.2% of proof steps automated autonomously vs aesop 40.1%; human-assisted mode averages 2.08 manual steps vs 3.86 for aesop. No miniF2F number in the LeanCopilot paper itself; the underlying ReProver ByT5 model hits 26.5% pass@1 on miniF2F-test per the LeanDojo NeurIPS 2023 paper. LLMLean/LLMSTEP: baseline pythia-2.8b weights open on HF (wellecks/llmstep-mathlib4-pythia2.8b), no headline miniF2F number claimed for llmstep itself since it is intentionally model-agnostic - performance tracks whichever backend (GPT-4o, BFS-Prover-V2, Kimina) is plugged in. Reproducibility is high for both; installation is a lakefile dependency.

*Source:* https://github.com/lean-dojo/LeanCopilot

### LeanDojo / ReProver (LeanDojo v2 as of NeurIPS MATH-AI 2025; original NeurIPS 2023 Datasets & Benchmarks paper, arXiv 2306.15626)
*Ecosystem:* Lean 4 (main branch); Lean 3 supported only on a deprecated legacy branch. Toolkit extracts proof data by instrumenting Lean's elaborator and exposes a Python `Dojo` environment for programmatic tactic-level interaction with the kernel.

*Core mechanism:* Two ByT5-small models trained on the LeanDojo Benchmark (98,734 theorems extracted from mathlib with fine-grained premise annotations). (1) Premise Retriever: DPR-style ByT5 encoder that embeds the current proof state and every in-scope premise into a shared vector space; trained with in-batch negatives plus "hard negatives" surfaced via LeanDojo's static analysis of which premises are actually accessible/used. (2) Tactic Generator: ByT5 encoder-decoder that takes `retrieved_premises ++ proof_state` as input and generates the next tactic string. Inference is best-first tree search over proof states: at each node, sample k tactics from the generator, execute each in a Lean `Dojo` session, expand successful children, until `no goals` or budget exhausted. Training is one-shot supervised (teacher-forced next-tactic prediction) on human mathlib proofs — no RL, no expert iteration, no self-play. Fits in "one GPU-week" on a single A100 80GB.

*Strengths:*
- Fully open: MIT license, open weights on HuggingFace (kaiyuy/leandojo-lean4-{tacgen,retriever,retriever-tacgen}-byt5-small), open benchmark, open data-extraction toolkit — the de facto reproducible baseline every subsequent Lean prover (DeepSeek-Prover, InternLM2.5-StepProver, Lean-STaR, etc.) compares against.
- LeanDojo toolkit itself is arguably the bigger contribution than the model: reliable Lean 4 proof-state extraction + programmatic Dojo interaction unblocked most of the 2024-2026 Lean RL / search work.
- Cheap: one A100 for ~a week, tiny ByT5-small backbones — a builder can retrain end-to-end without a research budget.
- Retrieval genuinely helps on the 'novel_premises' split (theorems whose proofs need premises unseen at training time), addressing the main failure mode of pure-LM tactic generators.
- LeanDojo Benchmark's novel_premises split is a much harder and more honest generalization test than random or miniF2F splits.

*Limitations:*
- Weak by 2026 standards on miniF2F: ~26.5% Pass@1 on miniF2F-test (Lean), vs 90%+ pass@many for recent 7B–32B specialized provers (DeepSeek-Prover, Pythagoras-Prover). Model capacity (ByT5-small) and lack of RL/expert-iteration are the ceiling.
- Best-first search is naive — no value/critic model, no learned search policy (later work like InternLM2.5-StepProver adds critic-guided search on top).
- Retrieval is over a static premise set snapshot; adapting to a moving mathlib requires re-extraction and re-embedding.
- Tactic-level only — no informal-to-formal autoformalization, no natural-language reasoning interleaving (Lean-STaR added that later).
- Original benchmark numbers were reported on a specific mathlib commit; direct comparison across papers requires matching the LeanDojo Benchmark version (v4 for Lean 4).

*Maturity:* Original paper: NeurIPS 2023 Datasets & Benchmarks (oral). Headline numbers from the paper: 51.2% Pass@1 on LeanDojo Benchmark random split, ~26.5% Pass@1 on miniF2F-test (Lean). LeanDojo v2 presented at NeurIPS MATH-AI 2025 as "a comprehensive library for AI-assisted theorem proving in Lean" — primarily a toolkit/library refresh (better Lean 4 support, extraction, Dojo API), not a new SOTA prover model. Fully open-weights (HuggingFace), fully open-source (MIT), no gating. Reproducibility is unusually good — every downstream Lean prover paper in 2024–2026 reuses its benchmark and extraction pipeline.

*Source:* https://leandojo.org/

### COPRA (In-Context Prover Agent) — Thakur, Tsoukalas, Wen, Xin, Chaudhuri (UT Austin), COLM 2024 (arXiv 2310.04353, v5 Aug 2024). LLM-agnostic in-context proof agent; primary instantiation uses GPT-4-turbo.
*Ecosystem:* Lean (Lean 4 in current repo, originally Lean 3 / mathlib for miniF2F eval) and Coq (CompCert). Also supports Isabelle in the current open-source implementation. First open-source agent to target both Lean and Coq from a single codebase.

*Core mechanism:* GPT-4-directed depth-first search over tactic sequences with no fine-tuning — the LLM is a black-box policy. Concrete loop (Fig. 3 of paper):

1. Maintain a stack of proof-environment states and a per-state "failure dictionary" Bad(O) mapping each state to tactics already known to be unproductive.
2. At each step: PUSH current state, RETRIEVE relevant lemmas/definitions from an external DB via BM25 (mathlib for Lean; the CompCert training set for Coq).
3. PROMPTIFY serializes: current goals + hypotheses ([GOALS]/[HYPOTHESES] tags), recent [STEPS] from the stack, [INCORRECT STEPS] from Bad(O) with [ERROR MESSAGE] feedback from the previous failed tactic, retrieved lemmas, and optional [GLOBAL CONTEXT] (natural-language informal proof, DSP-style, few-shot-generated from the theorem statement).
4. Query LLM (single response, temperature 0 by default) for one next tactic; PARSETACTIC parses and re-queries on format errors.
5. Execute tactic in Lean/Coq. Outcomes: QED → terminate; error state → add tactic to Bad(O) and stay; non-progress (detected via a symbolic "progress check" — a partial order ⊑ over states from Sanchez-Stern 2020 ruling out cyclic/no-progress tactics) → add to Bad(O); progress → recurse on new state.
6. On dead-end, POP and backtrack. Query budget capped at 60 LLM calls per theorem in main experiments (100 for the best config).
7. Two-phase strategy: run without retrieval first; on remaining unsolved problems, restart with retrieved lemmas and (for miniF2F) informal proofs in the global context.

Prompt has a fixed "system prompt" defining an output grammar the LLM must follow ([RUN TACTIC] ... [END]) plus a synthetically generated "agent prompt" containing serialized state. No training; entirely in-context.

*Strengths:*
- Zero fine-tuning — a plain black-box GPT-4-turbo beats fine-tuned ReProver on miniF2F-test pass@1 within 60 queries vs ReProver's thousands (~16x fewer queries, ~3x faster wall-clock).
- First open-source agent that plugs into both Lean and Coq (and now Isabelle) with the same search harness — easy to swap LLMs (OpenAI, Anthropic, Bedrock, vLLM: Llama/Mistral/DeepSeek/LLeMMA/GPT-OSS).
- Rich error-feedback + failure-dictionary loop makes the LLM's mistakes productive instead of thrown away; symbolic progress check kills cyclic tactic sequences without needing the LLM to notice.
- Ablations cleanly attribute gains: retrieval (+2.5pp on miniF2F), backtracking (+2pp), informal proofs (+~1pp, up to 30.74% at 100 queries).
- Modular — retrieval is BM25 over any corpus; progress check is prover-agnostic; grammar-constrained parsing tolerates malformed LLM output.

*Limitations:*
- Depends on a frontier closed model to work well: GPT-3.5 gets 9.02% and CodeLlama 5.73% on miniF2F-test vs 26.63% for GPT-4 — the agent scaffolding does not rescue weak LLMs.
- Query cost is real — 60 GPT-4-turbo calls per theorem, each with a large serialized context (stack + failures + retrieved lemmas + informal proof); the paper explicitly caps queries for budget reasons.
- miniF2F pass@1 of ~29–31% is well behind current SOTA fine-tuned/RL provers (DeepSeek-Prover, Goedel-Prover-V2, etc. now >80% on miniF2F-test); COPRA's headline result is 'competitive without training,' not absolute SOTA.
- Test-set contamination risk with GPT-4 on miniF2F is acknowledged; paper argues it's small (COPRA doesn't reproduce long verbatim proofs) but cannot rule out.
- CompCert eval uses only 118 of 501 theorems (budget) — Coq numbers are on a subsample.
- Informal-proof augmentation only works where informal statements exist (miniF2F yes, CompCert no).

*Maturity:* Open-source (github.com/trishullab/copra, pip: copra-theorem-prover), no gated weights — model choice is user-supplied. Published at COLM 2024. Headline numbers, all pass@1 with n-query budgets and 600s wall-clock timeout unless noted:

miniF2F-test (Lean, Table 1):
- Few-shot GPT-4 (1×1): 13.52%; Few-shot GPT-4 T=0.7 (60×1): 15.98%
- ReProver (fine-tuned, 1×3751): 25.00%; LLeMMA-7b: 26.23%; LLeMMA-34b: 25.82%
- COPRA (GPT-4, no retrieval, 1×60): 26.63%
- COPRA (GPT-4 + Retrieval, 1×60): 29.09%
- COPRA (GPT-4 + Retrieval + Informal, 1×60): 29.92%
- COPRA (GPT-4 + Retrieval + Informal, 1×100, 1200s): 30.74%
- Ablations: COPRA GPT-3.5 9.02%; COPRA CodeLlama 5.73%; COPRA (−Backtracking) 24.59%; COPRA (−Retrieval) 26.63%

CompCert (Coq, 118/501 theorems): COPRA converges to correct proofs with fewer queries than Proverbot9001 (see paper Fig./Appendix).

Later community-reported number on the current repo: GPT-OSS-20b, miniF2F-test Lean 4, 42.798% pass@5 (README) — reflects newer LLMs, same COPRA harness.

Reproducible: full code, prompts, and eval scripts open; retrieval DB is mathlib (Lean) / CompCert train set (Coq).

*Source:* https://arxiv.org/abs/2310.04353

### Draft-Sketch-Prove (DSP) — Jiang, Welleck, Zhou et al., ICLR 2023 (arxiv 2210.12283)
*Ecosystem:* Isabelle/HOL (via Portal-to-ISAbelle API, Isabelle2021). Hammer = Sledgehammer with the 5 default backends (Z3, CVC4, SPASS, Vampire, E) plus 11 hand-picked Isabelle tactics (auto, simp, blast, fastforce, force, eval, presburger, sos, arith, linarith, auto simp: field_simps). Chosen over Lean/Coq because Isabelle has (a) the largest formal corpus for LM pretraining, (b) declarative Isar proof style that naturally supports sketches with open subgoals, (c) Sledgehammer, the strongest ITP hammer available at the time. Not a Lean system — direct comparisons to Lean-based provers (GPT-f, HTPS) are noted but flagged as non-comparable.

*Core mechanism:* Three-stage, inference-only pipeline (no fine-tuning; pure few-shot prompting of frozen LLMs). Stage 1 DRAFT: given the natural-language problem statement, obtain an informal proof — either (a) a human-written proof (from MATH / AoPS / hand-written for the crafted subset) or (b) an LLM-drafted proof by sampling from Codex code-davinci-002 (greedy) or Minerva 8B/62B/540B (nucleus, T=0.6, top_p=0.95). Stage 2 SKETCH (autoformalize): prompt Codex code-davinci-002 with 3 few-shot examples drawn uniformly from a pool of 20 hand-curated (informal_stmt, informal_proof, formal_stmt, formal_sketch) demonstrations (10 algebra + 10 number-theory, all from miniF2F-valid; the pool is filtered to matching problem type when the name reveals it, and the target problem itself is excluded). Append the target (informal_stmt, informal_proof, formal_stmt) and greedy-decode up to 2048 tokens to produce an Isar proof skeleton: a declarative proof with named intermediate `have` conjectures whose justifications are replaced by a special <…> "open" token; informal proof fragments are copied in as in-line comments immediately preceding the formal step they justify (this alignment trick is worth +4.9%/+2.8% valid/test — big ablation win). Total Codex calls per problem are capped at 100 (100 sketches per human draft; 1 sketch per LM draft × 100 drafts). Stage 3 PROVE: iterate over every open <…> conjecture in each sketch and dispatch it to the "Sledgehammer + 11-heuristics" prover (each tactic 10s timeout, Sledgehammer 120s timeout). If every gap in a sketch closes, the whole Isar proof is checked by Isabelle via Portal-to-ISAbelle; a success under either the human or LM draft branch counts as solved. Pass@100 over the 100 sketches per problem.

*Strengths:*
- Nearly doubles Sledgehammer+heuristics on miniF2F-test (20.9% → 39.3% with human drafts, 38.9% with 540B Minerva drafts) with only few-shot prompting — no ITP-specific fine-tuning or RL.
- First real demonstration that informal proofs can guide formal proving via 'autoformalization of proofs' rather than just statements, unlocking the abundant NL math corpus.
- LM-drafted proofs (Minerva-540B) come within 0.4% of human-written drafts on miniF2F-test — the human is nearly replaceable.
- In-line NL comments interleaved with Isar steps materially improve autoformalization quality — a portable prompting recipe.
- Modular: the prover is a black box, so any future hammer/neural prover slots in; the LLM is a black box, so any future model slots in. Spawned a whole family of follow-ups (Lyra, LEGO-Prover, ProofAug, Goedel-Prover).

*Limitations:*
- Isabelle-only; the sketch/hammer trick doesn't transfer cleanly to Lean/Coq where the hammer is much weaker (miniF2F-Lean comparisons are explicitly disclaimed as apples-to-oranges).
- Depends on closed frontier LLMs — Codex code-davinci-002 (since deprecated by OpenAI) and Minerva (never open-weights, Google-internal PaLM-based). Reproducing the exact numbers today is impossible without substitute models.
- Miniature few-shot pool: 20 hand-crafted algebra/number-theory demonstrations. Generalization to geometry, IMO combinatorics, or research-level math is unexplored and likely poor.
- Pass@100 with 120s Sledgehammer timeouts per open conjecture makes it compute-heavy at eval time; not a real-time interactive assistant.
- Ceiling is bounded by Sledgehammer — problems whose subgoals defeat all 5 ATPs are unreachable regardless of sketch quality (ablation confirms removing the hammer collapses pass rate ~9%).
- Evaluated only on high-school competition math (miniF2F, 488 problems); no undergraduate / research math evaluation.
- The released repo ships prompts, scripts, results — but not the LLM weights (Codex/Minerva), so end-to-end reproduction requires paid API access to a substitute frontier model.

*Maturity:* "Published ICLR 2023 (arxiv v2 Nov 2022). Benchmark: miniF2F (Zheng et al. 2022), 488 problems / 244 valid + 244 test, evaluated in Isabelle2021. Headline numbers from Table 1 of the paper — DSP with human informal proofs: 42.6% miniF2F-valid, 39.3% miniF2F-test. DSP with Minerva-540B drafts: 42.6% valid, 38.9% test. DSP with Minerva-62B: 43.9% valid, 37.7% test. DSP with Codex drafts: 40.6% / 35.3%. Baselines in same setup: Sledgehammer alone 9.9% / 10.4%; Sledgehammer+heuristics 18.0% / 20.9%; Thor 28.3% / 29.9%; Thor+expert-iteration (Wu et al. 2022) 37.3% / 35.2%. Was SOTA on miniF2F-Isabelle at publication. Code + prompts + result logs open-sourced at github.com/albertqjiang/draft_sketch_prove (MIT-style license per repo LICENSE). NOT open-weights: relies on Codex code-davinci-002 (OpenAI, deprecated) and Minerva (Google, never released). Portal-to-ISAbelle dependency is separately open-sourced. Superseded on miniF2F by later systems it inspired (Lyra 2023, LEGO-Prover 2023, ProofAug 2025, DeepSeek-Prover, Goedel-Prover) — but DSP itself has not been formally V2'd; it remains the canonical reference implementation of the sketch-then-hammer paradigm."

*Source:* https://arxiv.org/abs/2210.12283

### Autoformalization line: Wu et al. 2022 (Codex/PaLM few-shot) -> MMA (Jiang 2023, back-translation fine-tune) -> current open-weights SOTA Kimina-Autoformalizer-7B (2025)
*Ecosystem:* Hybrid Isabelle/HOL + Lean 4. Wu et al. targeted Isabelle; MMA covered both Isabelle (Archive of Formal Proofs) and Lean 4 (mathlib4); ProofNet was originally Lean 3, now community-ported to Lean 4; Kimina-Autoformalizer is Lean 4 only.

*Core mechanism:* Three-generation recipe, each layer built on the previous:

(1) Wu et al. 2022 (arXiv:2205.12615) — pure few-shot prompting. Feed Codex and PaLM ~a handful of (informal statement, Isabelle statement) exemplars, then ask the LLM to translate a new informal problem. No fine-tuning. To close the loop, they take autoformalized theorem statements, prove them with an existing neural prover (e.g. Thor), and add the resulting (statement, proof) pairs to the prover's training set — an expert-iteration-style data flywheel. The prover, not the LLM, is what improves; the LLM is just a one-shot translator.

(2) MMA (Jiang, Li, Jamnik, arXiv:2311.03755, Nov 2023) — back-translation to bootstrap a training corpus. Key insight: informalization (formal -> NL) is much easier than formalization because formal syntax constrains the output. So they run GPT-4 over the two largest formal corpora — the Archive of Formal Proofs (Isabelle) and mathlib4 (Lean 4) — to synthesize natural-language paraphrases for every theorem. That yields 332,774 (informal, formal) pairs across two languages. Then supervised fine-tune an LLM (LLaMA-2-70B in the paper) on this multilingual corpus with the arrow reversed: NL -> formal. Multilingual fine-tuning transfers: models fine-tuned on Isabelle+Lean beat models fine-tuned on either alone, even when evaluated monolingually.

(3) Kimina-Autoformalizer-7B (Numina/Moonshot, 2025, released alongside Kimina-Prover Preview, arXiv:2504.11354) — same NL->Lean-4 SFT recipe as MMA but scaled and specialized. Base is Qwen2.5-Coder-7B-Instruct. Training corpus aggregates formal problem pairs from PutnamBench, miniF2F, ProofNet, and Compfiles (competition-style focus, unlike MMA which was library-focused). Output format is fixed: Lean 4 theorem statement ending in `by sorry` (a hole for the downstream prover to fill). This is the "front end" that feeds Kimina-Prover, a separate 72B RL-trained prover that closes the sorry. So the pipeline at inference time is: NL problem -> Kimina-Autoformalizer -> Lean 4 statement with sorry -> Kimina-Prover (RL-trained on Kimi k1.5 pipeline, does long-CoT tactic generation with Lean feedback) -> full proof, checked by Lean. Autoformalizer and prover are trained separately; the coupling is the Lean file format.

The line's arc: few-shot prompt -> back-translated SFT -> specialized SFT + separate RL prover downstream. Verifier coupling is always via the proof assistant itself (Isabelle/Lean type-checks the output).

*Strengths:*
- Back-translation solves the data-scarcity bottleneck: turn any large formal library into unlimited (NL, formal) training pairs by informalizing with a strong LLM, then reverse the arrow for SFT.
- Multilingual fine-tuning is a free lunch — Isabelle + Lean training beats monolingual training even at monolingual eval (MMA finding).
- Clean separation of concerns: autoformalizer produces the Lean statement, a separately-trained prover fills the sorry. Each component can be improved independently.
- Lean/Isabelle acts as a hard verifier — no reward hacking on the final proof, and type-checking on the statement catches malformed autoformalizations for free.
- Kimina-Autoformalizer is open-weights Apache-2.0 at 7B — runs on a single GPU, easy to build on.

*Limitations:*
- Autoformalization quality is still the bottleneck. MMA reported only 16-18% of statements 'acceptable with minimal corrections' on miniF2F and ProofNet — most outputs need human editing. Kimina numbers on statement-level autoformalization accuracy (as opposed to end-to-end proving) aren't cleanly published.
- Statement-level correctness is judged by human/LLM ratings, not by the proof assistant — Lean will accept many statements that are subtly wrong (off-by-one hypotheses, wrong quantifier scope, weakened conclusion). ProofNet-style human-formalized gold is the only reliable comparison.
- Benchmark contamination risk: Kimina-Autoformalizer was trained on formal pairs 'from miniF2F and ProofNet' according to the paper — so miniF2F/ProofNet autoformalization numbers from Kimina are not clean held-out evaluations. Use PutnamBench or newer benchmarks (FormalMATH, IndiMathBench) for honest measurement.
- Benchmark versioning matters and is often muddled: miniF2F-valid (244) vs miniF2F-test (244), ProofNet Lean 3 original (371) vs Lean 4 port with a 186-theorem test split. Papers frequently report one and let the reader assume the other.
- Competition-math bias: Kimina's training corpus is competition-style (Putnam, olympiad), so it degrades on library/research mathematics that MMA-style training covered better.
- Back-translation inherits the informalizer LLM's biases — GPT-4 tends to produce a specific paraphrase style, so trained autoformalizers overfit to that NL distribution and struggle on real textbook prose (documented in the DRIFT and ReForm follow-ups).

*Maturity:* "Wu et al. 2022: Codex+PaLM, few-shot, 25.3% of competition problems formalized 'perfectly' by human judgment; downstream prover jumped from 29.6% -> 35.2% on miniF2F (split not clearly separated in the paper, generally read as miniF2F-test). Not open — depended on Codex.

MMA 2023: 332,774 pairs, LLaMA-2-70B fine-tune. 16-18% acceptable-with-minor-fixes on both miniF2F and ProofNet, vs 0% for the base LLaMA-2. Dataset released on HuggingFace (casey-martin/multilingual-mathematical-autoformalization); fine-tuned model weights released.

Kimina-Autoformalizer-7B (2025, Apache-2.0, HuggingFace AI-MO/Kimina-Autoformalizer-7B): the current default open-weights autoformalizer for Lean 4. Statement-level autoformalization numbers aren't the headline — the headline is Kimina-Prover Preview hitting 80.7% pass@8192 on miniF2F-test (superseded by Kimina-Prover full release at 92.2% miniF2F). That end-to-end number depends on the autoformalizer producing usable statements, so it's an indirect quality signal. Fully reproducible via vLLM/SGLang, weights + inference recipe public. Superseded Herald and DeepSeek-Prover-V2's own autoformalizer as the community default in 2025."

*Source:* https://arxiv.org/abs/2504.11354

### Goedel-Prover (V1 + V2) vs InternLM2.5-StepProver vs TheoremLlama — three open-weights Lean4 provers, late-2024 / 2025.
*Ecosystem:* Lean 4 for all three. All three are autoformalization-adjacent (train on formal Lean proofs, not Coq/Isabelle/Metamath). Goedel-V1 explicitly autoformalizes NuminaMath NL problems into Lean 4 statements at scale (Goedel-Pset-v1, ~1.64M statements).

*Core mechanism:* Three distinct recipes on the same substrate (LLM emits Lean 4, Lean kernel verifies):

(1) Goedel-Prover-V1 (Feb 2025, arXiv 2502.07640): expert-iteration / iterative-SFT loop with no RL required for the headline number. Pipeline: (a) train two autoformalizer LLMs to translate NuminaMath NL problems -> Lean 4 statements, filter by faithfulness + Lean-compilability, yielding Goedel-Pset-v1 (~1.64M statements); (b) SFT a whole-proof generator (base: DeepSeek-Prover-V1.5-Base, 7B) on seed Lean proofs; (c) sample proofs against the 1.64M statement bank, keep the Lean-verified ones, add to training set, retrain; (d) repeat for 8 iterations. Final SFT model then optionally gets RL/DPO. Inference is whole-proof sampling (no tree search).

(2) Goedel-Prover-V2 (Aug 2025, arXiv 2508.03613): same family, three new tricks — (a) 'scaffolded data synthesis' that generates synthetic problems with a difficulty curriculum bridging easy solved statements to hard unsolved ones; (b) 'verifier-guided self-correction': at inference the model gets Lean compiler error messages back and revises the proof in-context, iteratively; (c) checkpoint model averaging to fight diversity collapse late in training. Released at 8B and 32B.

(3) InternLM2.5-StepProver (Oct 2024, arXiv 2410.15700): tactic-level tree search + expert iteration + critic model. Base: InternLM2.5-Math-Plus 7B. Two heads trained jointly — a policy that emits the next Lean tactic given the current goal state, and a critic that scores partial proof states. At inference: critic-guided best-first search over the tactic tree, cross-checked by Lean. Trained via massive expert iteration on Lean-Workbook (>20,000 CPU-days): run search on the workbook, harvest successful tactic trajectories + state-value labels for the critic, retrain both, resample harder problems. Ships with 'searched proofs' (the successful trajectories) as a public dataset.

(4) TheoremLlama (Jun 2024, arXiv 2407.03203): the lightest of the three — a bootstrapping recipe rather than a search system. Takes a general-purpose LLM (Llama-3-8B) and fine-tunes it into a Lean 4 prover using (a) an NL-FL aligned dataset called Open Bootstrapped Theorems (OBT), built by pairing informal statements with Lean 4 proofs and inserting NL comments inside proofs so the model learns to 'think in NL, write in FL'; (b) curriculum + block training over that data; (c) an iterative proof-writing loop at inference where the model rewrites failed attempts using Lean feedback. Whole-proof generation, no explicit search.

*Strengths:*
- Goedel-V2 32B is a genuine frontier open-weights result on miniF2F: 88.1% pass@32 standard, 90.4% pass@32 in self-correction mode; solved 86/640 PutnamBench at pass@184, beating the 671B DeepSeek-Prover-V2 at pass@1024 with a 20x smaller model.
- Goedel-V1 proved that pure SFT + iterative expert-data-generation (no RL) can match/beat DeepSeek-Prover-V1.5's RL pipeline — a simpler, more reproducible recipe.
- Goedel line fully open: MIT code, model weights on HF, and the Lean-workbook-proofs dataset (29.7K verified proofs) released.
- InternLM2.5-StepProver's critic + tactic-level search is the strongest published recipe for step-wise (as opposed to whole-proof) proving; it complements whole-proof approaches and its trajectory dataset is directly reusable.
- TheoremLlama is the cheapest entry point: shows a general-purpose Llama-3-8B can be bootstrapped into a working Lean 4 prover with an NL-FL aligned dataset, useful as a baseline / teaching example.

*Limitations:*
- TheoremLlama's 33.6% pass rate on miniF2F-test is now far behind the frontier (Goedel-V2 32B at 88-90%); it is essentially of historical/pedagogical interest by mid-2026.
- Goedel-V1 (57.6% pass@32 miniF2F-test) is superseded by Goedel-V2 — use V2 unless you specifically want the simpler SFT-only recipe to study.
- InternLM2.5-StepProver's numbers (59.4% pass@256 miniF2F-test, 27% ProofNet, 6/640 Putnam) are also now well behind Goedel-V2 and Kimina/Seed-Prover; its value is the search+critic recipe, not the checkpoint.
- Compute cost is heavy across all three: InternLM used 20,000+ CPU-days for expert iteration; Goedel-V2 32B training and its scaffolded synthesis pipeline are not cheap to reproduce.
- miniF2F saturation is a real concern — the miniF2F-Lean Revisited paper (arXiv 2511.03108) flags contamination and statement-quality issues at these pass rates, so 88%+ numbers should be read with a grain of salt.
- All three are Lean-4-only; no Coq/Isabelle transfer out of the box.
- Autoformalization pipeline (Goedel) inherits errors from the translator LLMs — the 1.64M statement bank contains mistranslations, filtered but not perfectly.

*Maturity:* ["Goedel-Prover-V1 (arXiv 2502.07640, Feb 2025): 57.6% pass@32 miniF2F-test (SFT), >60% pass@32 with RL. Base DeepSeek-Prover-V1.5-Base 7B. Solves 29.7K statements in Lean-Workbook (vs InternLM2.5-StepProver + InternLM-Math-Plus combined at 15.7K). Fully open-weights (HF: Goedel-LM/Goedel-Prover-SFT), MIT code.", "Goedel-Prover-V2 (arXiv 2508.03613, Aug 2025): 8B at 84.6% pass@32 miniF2F-test; 32B at 88.1% pass@32 standard / 90.4% pass@32 with verifier-guided self-correction; 86/640 PutnamBench at pass@184. Open-weights (8B + 32B), open code + data.", "InternLM2.5-StepProver (arXiv 2410.15700, Oct 2024): 47.3% pass@1, 59.2% pass@64, 59.4% pass@256 on miniF2F-test; 27.0% pass@256 ProofNet; 6/640 PutnamBench. Base InternLM2.5-Math-Plus 7B. Open-weights on HF, searched-proofs dataset released. Predecessor InternLM2-StepProver was 48.8% pass@1 / 54.5% pass@64 miniF2F-test.", "TheoremLlama (arXiv 2407.03203, Jul 2024): 33.61% miniF2F-test (Llama-3-8B base). Open-weights checkpoints + OBT dataset on GitHub. Superseded by everything above but remains a clean reference implementation of the NL-comment-in-proof bootstrapping trick."]

*Source:* https://arxiv.org/abs/2508.03613

### Terence Tao's human-in-the-loop workflow: Lean 4 + Blueprint + LLM copilots (GitHub Copilot -> Claude/o4 -> Claude Code agent) + classical ATPs (Vampire, Prover9/Mace4)
*Ecosystem:* Lean 4 + Mathlib, with Patrick Massot's `leanblueprint` LaTeX/HTML tooling for human-readable proof scaffolds. ATP layer (Vampire, Prover9, Mace4) is used for equational logic. LLM layer is model-agnostic and swapped over time: GitHub Copilot (2023 PFR), Claude + o4 (May 2025 exercise), Claude Code agent (Mar 2026 video). Not an autoformalization-only system - Tao always writes the LaTeX blueprint and steers Lean tactic choices himself.

*Core mechanism:* A "blueprint-first, human-orchestrated, tool-tiered" loop, not a monolithic prover. Concrete recipe a builder can imitate:

1. Write the proof in LaTeX using Massot's `leanblueprint` package. Each lemma gets `\lemma`, `\uses{...}` dependency labels, and `\leanok` / `\proved` markers. Blueprint compiles to a DAG website showing which nodes are stated, proved on paper, stated in Lean, proved in Lean. This DAG is the shared work-queue for humans + AI.

2. Decompose the proof into small lemmas (Tao's "recipe" step). Before spawning any agent, hand-write which subtasks are (a) safe to delegate, (b) need human eyes, (c) need classical automation. Skipping this step is what caused his first Claude Code attempt to burn 45 min of tokens and crash.

3. For each leaf lemma:
   - Try Lean's own tactics first: `exact?`, `decide`, `polyrith`, `linarith`, `nlinarith`, `norm_num`, `aesop`. Often solves it with no LLM.
   - If the lemma is combinatorial equational logic, hand it to Vampire / Prover9 / Mace4; translate the ATP proof back to Lean (in the Equational Theories Project this handled the overwhelming majority of the 22M implications, far cheaper than LLMs).
   - Otherwise let the LLM propose a Lean skeleton: Copilot inline in VSCode for tab-completion (2023), or an agentic loop (Claude / o4 / Claude Code) that reads the file, edits, runs `lake build`, reads the error, and retries.

4. Parallel human+agent work: while Claude Code skeletonizes lemma N+1 in one pane, Tao debugs lemma N by hand in another. Explicitly "two workers on the same job," not autonomous end-to-end.

5. Blueprint stays the ground truth. Every closed lemma flips `\leanok`; the DAG website tells the crowd (or the agent) what to grab next. This is how the Equational Theories Project scaled to 32 contributors + bots without merge chaos.

6. For research-scale discovery (equational theories, Erdos-type problems) an outer generate-and-check loop runs: brute-force small models -> ATP -> LLM/human for the resistant residue (~1000 of 22M implications needed clever human constructions).

Training loop: none. There is no RL, no expert iteration, no fine-tune. It is purely inference-time orchestration of off-the-shelf LLMs against Lean's kernel and blueprint DAG.

*Strengths:*
- Blueprint DAG turns a monolithic proof into a parallelizable task queue that humans, LLMs, ATPs, and crowd contributors can all pull from without stepping on each other - proven at scale (PFR ~3 weeks; Equational Theories 22M implications in ~2 months informal + 5 months Lean by 32 people).
- Tool-tiered: cheap tactics -> classical ATPs -> LLMs, in that order. Avoids paying LLM cost for things `exact?` or Vampire solve for free.
- Model-agnostic and swap-friendly: the same workflow absorbed Copilot -> Claude+o4 -> Claude Code with no rewrite, because Lean's kernel is the verifier, not the LLM.
- Decomposition-first discipline (write the recipe before spawning the agent) reliably beats end-to-end 'do the whole proof' prompting - Tao's own A/B: 45-min crash vs 25-min success on the same target.
- Produces machine-checked Lean artifacts plus human-readable LaTeX simultaneously, so mathematicians and the kernel agree on what was proved. Caught a real bug in the PFR paper (missing 2-torsion hypothesis).

*Limitations:*
- Not a system - a workflow. There is no released model, no benchmark score, no reproducible pipeline you can `pip install`. Reproducing Tao's results requires Tao-level decomposition skill.
- LLMs play a 'minor role' in the actual proof volume (Tao's own words re: Equational Theories); the heavy lifting is Vampire/Prover9 + human construction. Easy to overstate the LLM contribution.
- Copilot/Claude suggestions are often 'almost correct' and need Lean-fluent human repair - useless to someone who can't read Lean tactic state.
- Agentic runs are brittle and expensive: 45-min token-burn crashes are normal without a pre-written recipe. Context management is manual.
- No miniF2F / PutnamBench numbers exist for 'the Tao workflow' - it is a human process, not a benchmarkable system, so it cannot be directly compared to DeepSeek-Prover, AlphaProof, Kimina, etc.
- Scales via social coordination (Blueprint + Zulip + GitHub), which is a soft dependency on Tao's convening power - unclear a random group can replicate the throughput.
- Requires Lean 4 + Mathlib mastery from at least the orchestrator; the LLM cannot yet cover for a non-expert.

*Maturity:* Production-grade as a human workflow, with three concrete completed artifacts: (1) PFR conjecture formalized in Lean 4 in ~3 weeks Nov-Dec 2023 (Tao, Dillies, Mehta) - `github.com/teorth/pfr`; (2) Equational Theories Project - 22,028,942 implications between 4,694 magma laws resolved, all but 1 finite-magma case closed, Lean-formalized with 32 contributors, arXiv:2512.07087 (Dec 2025) - `github.com/teorth/equational_theories`; (3) symmetric_project (Maclaurin-type inequality) Lean 4 formalization, Oct 2023 - `github.com/teorth/symmetric_project`. No standard benchmark numbers (miniF2F, PutnamBench) apply because this is a human-orchestrated workflow, not a solver. Everything is open-source (Apache/MIT-style Lean projects on GitHub); LLM components (Copilot, Claude, o4) are closed-weights third-party APIs.

*Source:* https://terrytao.wordpress.com/2023/11/18/formalizing-the-proof-of-pfr-in-lean4-using-blueprint-a-short-tour/

## Tight framework spec (opinionated)

Substrate. Lean 4 + Mathlib, no negotiation. The Lean kernel is the trust root; every "proof" the system emits must reduce to `#print axioms thm` returning only the classical trio (`Classical.choice`, `propext`, `Quot.sound`), programmatically enforced in CI. Reject Isabelle (weaker LLM training coverage, Isar sketching is a dead branch by 2026), Coq/Rocq (thin ecosystem for LLM tooling). Track a pinned Mathlib SHA and Lean toolchain per experiment; Mathlib churn will bite you if you don't.

Data extraction and environment. LeanDojo v2 for state extraction, tactic tracing, and the Python `Dojo` REPL. Use the Kimina Lean Server (open-source, from Numina) as the concurrent RL rollout backend - it is the only battle-tested server for thousands of parallel Lean sessions.

Model stack (three roles, three checkpoints).
1. Autoformalizer: Kimina-Autoformalizer-7B (Apache-2.0, Qwen2.5-Coder-7B base). Emits `theorem ... := by sorry`. Filter outputs by (a) Lean type-checks, (b) round-trip informalization equivalence via a judge LLM, (c) statement-preservation checks against a small ProofNet-style gold set to catch systematic drift.
2. Prover: Goedel-Prover-V2-32B as the default (best open efficiency at pass@32 on miniF2F and PutnamBench, verifier-guided self-correction built in). Fall back to Kimina-Prover-Distill-8B for latency-sensitive interactive use. Reserve Kimina-Prover-72B for the hardest problems with a TTRL search budget.
3. Retriever + copilot: LeanCopilot's ReProver ByT5 retriever for premise selection, wired as an aesop rule source. This is the piece pure whole-proof samplers keep flunking - lemma name recall.

Pipeline. Informal statement -> Kimina-Autoformalizer -> candidate Lean statements -> filter (typecheck + judge) -> Goedel-V2-32B whole-proof pass@32 with self-correction (feed Lean error messages back in-context, 3-5 rounds) -> on failure, escalate to COPRA-style agentic DFS around Goedel with LeanCopilot retrieval injected at each node -> on continued failure, decompose to `have` skeleton via the DeepSeek-Prover-V2 recursive-subgoal prompt and recurse per subgoal on the 8B distill. Every leaf checked by Lean kernel; every intermediate `sorry` tracked and blocked from release.

Agent loop. Wrap the prover in a COPRA-shaped harness: maintain a state stack + failure dictionary Bad(O) mapping (state -> failed_tactic -> Lean_error) so the model stops re-proposing the same wrong step; add DSP-style informal-proof injection in the prompt when an NL sketch is available. Cap per-problem budget in (samples, wall-clock, tokens); log every rollout to a trace store.

Rigor guarantees. (a) `#print axioms` gate in CI - any new axiom fails the build. (b) No `sorry` in released proofs; a linter fails on `sorry`, `admit`, or `native_decide` without an allowlist. (c) All autoformalized statements shipped alongside the original informal statement and a diff summary from an independent judge model. (d) Pin Mathlib SHA + Lean toolchain per proof artifact. (e) For every headline claim, require an independent reproduction on a clean machine before publication. (f) Adopt Tao's blueprint discipline: LaTeX `leanblueprint` DAG as the shared queue between humans and agents, `\leanok` markers as the source of truth, human-authored decomposition before any agent spawn.

Infra. GPU: 8xH100 for 72B TTRL runs; single H100 sufficient for 8B interactive; 4xH100 for 32B pass@32 batches. Lean CI on GitHub Actions with Mathlib cache. Tracing: LangSmith or PostHog LLM Analytics for prompt/rollout inspection. Benchmark harness: miniF2F-test (rectified Kimina variant), PutnamBench, ProofNet-Lean4, and one held-out contamination-free set (e.g. FormalMATH or recent AIME) - never trust a single benchmark.

Rationale for the picks. Goedel-V2 over Kimina-72B as default: same accuracy tier at 20x less compute, self-correction is built in, weights are truly open. Kimina-Autoformalizer over MMA/Herald: Apache-2.0, single-GPU, currently the community default and paired with a matching prover. LeanDojo over rolling your own tracer: everyone else builds on it, so bugs are shared. COPRA agent harness over pure whole-proof sampling: retrieval + failure memory + informal-proof injection each add measurable points and stack cleanly. Blueprint DAG over autonomous end-to-end agents: Tao's A/B evidence (45-min crash vs 25-min success) is unambiguous that human decomposition beats agent autonomy at research scale today.

## Recommended starter stack

- Lean 4 (latest stable toolchain matching Mathlib pin) + Mathlib4 at a pinned SHA
- LeanDojo v2 (github.com/lean-dojo/LeanDojo) for state extraction and the Python Dojo REPL
- Kimina Lean Server (github.com/project-numina/kimina-lean-server) for concurrent RL/inference rollouts
- Kimina-Autoformalizer-7B (HF: AI-MO/Kimina-Autoformalizer-7B, Apache-2.0) as the NL->Lean front end
- Goedel-Prover-V2-32B (HF: Goedel-LM/Goedel-Prover-V2-32B) as default whole-proof prover with self-correction; Goedel-Prover-V2-8B for interactive latency
- Kimina-Prover-Distill-8B (HF: AI-MO/Kimina-Prover-Distill-8B) as a second sampler for diversity; Kimina-Prover-72B reserved for hard-problem TTRL
- LeanCopilot v4.31+ (github.com/lean-dojo/LeanCopilot) for in-editor tactic suggestion and premise retrieval via ReProver ByT5
- LLMLean (github.com/cmu-l3/llmlean) as the model-agnostic tactic bridge for swapping in cloud/local LLMs
- COPRA (pip: copra-theorem-prover) as the agent harness with failure-dictionary DFS and grammar-constrained tactic parsing
- Patrick Massot's leanblueprint (github.com/PatrickMassot/leanblueprint) for LaTeX+DAG project scaffolding on research-scale proofs
- Benchmarks: miniF2F-test (rectified Kimina variant), PutnamBench, ProofNet-Lean4, plus one held-out set (FormalMATH or recent AIME) for contamination control
- Optional classical ATP layer for equational goals: Vampire, Prover9/Mace4, wired as fallback below the LLM tier (per Tao's Equational Theories playbook)
- 8xH100 (or equivalent) for 32B/72B batched sampling; 1xH100 sufficient for 8B interactive; CTranslate2 CPU fallback for the ReProver retriever

## Risks (how frameworks fail silently)

- Axiom sneaking: models learn to close goals with `native_decide`, `Classical.choice` misuse, or added axioms that quietly widen the trust base. Mitigation: enforce `#print axioms` allowlist in CI on every merged proof.
- Sorry leakage: nested `have ... := by sorry` scaffolds ship as 'proved' if not linted. Enforce a repo-wide grep gate for `sorry`, `admit`, `sorry_ax`.
- Autoformalization drift: Lean will happily typecheck a statement whose quantifiers, hypotheses, or conclusion differ from the informal claim. 16-18% acceptable rate on ProofNet (MMA) means most autoformalized statements are subtly wrong. Require independent judge + human spot-check on any headline claim.
- Hallucinated lemma names: whole-proof samplers cite Mathlib lemmas that do not exist or have wrong signatures. Retrieval-augmented prompting (LeanCopilot ReProver) mitigates but does not eliminate.
- Benchmark contamination and saturation: miniF2F is known-contaminated for Kimina and DeepSeek (they trained on adjacent sets); miniF2F-Lean Revisited (arXiv 2511.03108) documents statement-quality issues. Do not read 88%+ numbers as 'nearly solved'.
- Mathlib version pinning: proofs that verify against Mathlib SHA X silently break on SHA Y. Pin toolchain + Mathlib per artifact; rerun CI on Mathlib bumps.
- Closed-model dependency: Kimina cold-start traces come from Claude 3.7 Sonnet; Alphaproof is fully closed; MMA relied on GPT-4 informalization. Reproducibility depends on continued API access to models you don't control.
- Cost blow-ups: TTRL budgets (~42K samples/problem for Kimina 92.2%, per-problem days for AlphaProof) can hide real cost per solve. Publish per-problem token+GPU cost, not just pass rate.
- Overstated LLM contribution: on Tao-style projects the LLM does <10% of the work; ATPs and humans do the rest. Do not credit the LLM for what Vampire, `exact?`, or `linarith` closed.
- Contest-benchmark overfit: pipelines tuned for miniF2F/PutnamBench (competition math) degrade sharply on ProofNet-style textbook/research math and on definitions that require formalizing setup first.
- Jacobian-style overclaim risk: an LLM producing a specific polynomial artifact is arithmetic verifiable and low-risk; an LLM producing a *proof* that isn't kernel-checked is high-risk. Never conflate 'model output that checks' with 'model proved something new' unless the check is Lean's kernel.

## Adversarial verification results

Five claims were adversarially checked (default: refuted unless primary-source evidence found). Results:

- **not refuted** (conf=high) — Polynomial map F1=(1+xy)^3 z+y^2(1+xy)(4+3xy), F2=y+3x(1+xy)^2 z+3xy^2(4+3xy), F3=2x-3x^2y-x^3 z was published in the last 12 months as an LLM-produced candidate counterexample to the Jacobian Conjecture and is credited to an LLM system.
  - I read the primary-source verification preprint at ulam.ai/research/jacobian.pdf (dated July 20, 2026, within the last 12 months as of 2026-07-21). It defines F = (P, Q, R): C^3 -> C^3 with P = (1+xy)^3 z + y^2 (1+xy)(4+3xy), Q = y + 3x(1+xy)^2 z + 3xy^2 (4+3xy), R = 2x - 3x^2 y - x^3 z, exactly matching the claim. The preprint explicitly states the formula was announced by Levent Alpoge and 'credits Akhil Mathew for asking the question and Fable for the work leading to the example.' Fable is an LLM (news coverage identifies it as Anthropic's Claude Fable 5). It is presented as a counterexample (not merely a candidate); the preprint proves det Jac F = -2 and exhibits a 3-point fiber. Caveats: the preprint is not yet peer-reviewed in a journal and the plane (n=2) case remains open, but the claim as stated is supported.
- *refuted* (conf=high) — The complex Jacobian Conjecture in dimension >= 2 remains OPEN as of late 2025 / early 2026, and no confirmed counterexample has been peer-review accepted.
  - The claim is subtly but materially overstated. As of 2026-07-20 (one day before today, 2026-07-21), Levent Alpoge announced an explicit, self-verifiable polynomial counterexample in C^3: a = (1+xy)^3 z + y^2(1+xy)(4+3xy), b = y + 3x(1+xy)^2 z + 3xy^2(4+3xy), c = 2x - 3x^2 y - x^3 z, with Jacobian determinant -2 and a generic three-to-one collision. By adjoining identity coordinates this refutes the conjecture in every dimension n >= 3. Only the n=2 (plane) case remains open. The arithmetic has been independently checked and written up as a verification preprint, discussed on the Secret Blogging Seminar (a professional-mathematician blog), and endorsed by Timothy Gowers. So while it is true that no *journal-accepted* counterexample exists yet (only a preprint plus community verification), the claim's assertion that dim >= 2 "remains open" is now false: the n >= 3 cases are refuted by an explicit, mechanically checkable map. The claim would only be correct if narrowed to "the two-dimensional (plane) Jacobian Conjecture remains open."
- *refuted* (conf=high) — AlphaProof (DeepMind) genuinely produced Lean 4 proofs of 2024 IMO problems P1, P2, P4, P6 that were independently verified in Lean, and problem P3 remains unsolved by the system.
  - The claim is subtly but materially overstated vs the primary sources. Two errors: (1) AlphaProof did NOT produce a Lean proof of P4 — P4 (geometry) was solved by a separate system, AlphaGeometry 2, per DeepMind's own announcement and the Nature paper. AlphaProof produced Lean proofs of P1, P2, and P6 only. (2) The claim implies only P3 was unsolved, but P5 (combinatorics) was ALSO unsolved by both systems; P5 reportedly took human experts over a day even to formalize into Lean, and was not attempted end-to-end. Additional caveat the claim glosses over: the problem statements were manually formalized into Lean by human experts before AlphaProof attempted them (an acknowledged weak spot), and P2 required several days of compute. Verification via Lean's kernel is genuine, but 'independently verified' is only true in the automatic-kernel-check sense, not in the sense of an external audit.
- *refuted* (conf=high) — DeepSeek-Prover-V2 (671B or its distill) achieved the highest reported miniF2F-test pass rate among openly-published Lean provers as of early 2026.
  - DeepSeek-Prover-V2-671B's reported miniF2F-test peak is 88.9% (Pass@8192, per arxiv 2504.21801, April 2025). Multiple later openly-published Lean provers report substantially higher miniF2F-test pass rates: Kimina-Prover with TTRL reports 92.2% (HuggingFace/AI-MO blog + arxiv 2504.11354 for the preview at 80.7%); Seed-Prover (ByteDance, arxiv 2507.23726, July 2025) reports 99.6% on miniF2F-test ("saturates MiniF2F"); HILBERT (arxiv 2509.22819, ICLR 2026) reports up to 99.2% on miniF2F in Lean; Goedel-Prover-V2 (arxiv 2508.03613) and Leanabell-Prover-V2 (arxiv 2507.08649) are additional openly-published competitors post-DPSv2. Even Pythagoras-Prover-4B is reported to surpass DPSv2-671B at pass@32 (86.1% vs 82.4%). The claim is therefore overstated — DPSv2 was SOTA briefly in April/May 2025 but not "as of early 2026."
- *refuted* (conf=high) — Terence Tao has publicly demonstrated a workflow where Lean 4 + LLM tools formalize a substantial theorem (e.g. PFR) faster than a pure-Lean workflow, and is willing to be quoted saying formalization is the right substrate for AI-driven mathematics.
  - The claim is subtly but materially overstated versus what Tao's primary-source writings actually say. Two problems: (1) The PFR formalization (Tao, Dillies, Mehta, ~23 days in late 2023) was NOT presented by Tao as an LLM-accelerated workflow. In his own blog post (terrytao.wordpress.com, 2023-11-18), LLM tooling appears only as a passing anecdote about a GitHub Copilot suggestion that was "almost correct" but not needed because `exact?` worked instead. He explicitly notes that AI+formal-verifier loops at that time could only handle "about a dozen lines" and needed "further advances." The speed of PFR came from Blueprint + a large human collaboration, not from LLM assistance, and Tao does not compare LLM+Lean vs pure-Lean speed anywhere I could find. (2) On the "right substrate" quote: Tao is on record enthusiastically about Lean and about AI-as-copilot for math (Scientific American, Quanta), and has said things like "in three years AI will become useful for mathematicians" and that formalization enables massive collaboration — but I could not find a primary source where he says formalization is *the right substrate for AI-driven mathematics* in those (or equivalent) terms. He frames formalization primarily as a substrate for human collaboration and verification, with AI as one downstream beneficiary, not as the thesis the claim asserts. So the "willing to be quoted saying X" half is a paraphrase that goes beyond what he has actually said. Net: no primary source supports the compound claim as stated; components are either overstated (LLM speedup on PFR) or unverified in that exact framing (substrate quote).

Key takeaway: even widely-cited claims about specific systems (AlphaProof's exact IMO coverage, DeepSeek-V2 being current SOTA on miniF2F, Tao's demonstrated speedups) do not survive close reading of primary sources — a strong argument for the framework's rigor gates below.

## Open questions

- Is n = 2 of the Jacobian Conjecture still open, and can the Fable-style search generalize downward or is 3 a hard floor?
- How many prompt iterations and how much human steering actually produced the Fable counterexample? No transcript has been published; the honest LLM-vs-human credit split is unknown.
- Does the Alpoge counterexample survive journal peer review as originally stated, and does the Bass-Connell-Wright 24-variable reduction actually give the claimed infinite family?
- Can whole-proof RL provers (Kimina/Goedel/DeepSeek) escape competition-math overfit and produce ProofNet/research-math results at comparable pass rates?
- Is the miniF2F ceiling (~90%+) a real signal or a benchmark artifact? The miniF2F-Lean Revisited paper suggests significant statement-quality problems at the top of the leaderboard.
- Can autoformalization statement-level accuracy be pushed past ~20% acceptable-with-minor-fixes on ProofNet without a much larger human-formalized gold corpus?
- Is there a reproducible open equivalent of AlphaProof's test-time RL curriculum, or does that recipe require Google-scale TPU budgets to work at all?
- How well does the Tao blueprint workflow scale when the orchestrator is not Terence Tao - can a random team of Lean-fluent engineers hit similar throughput on a novel research target?
- Are current provers producing proofs that humans find *explanatory*, or just kernel-valid? Blumberg's critique of the Jacobian result generalizes: computational search != mathematical insight.
- What is the right benchmark for 'LLM proved something new'? miniF2F saturates, PutnamBench is competition math, Erdos Problems set is small; the field lacks a clean 'novel mathematics produced' metric.

## Related in vault

- [[jacobian-conjecture-fable-2026]] — the artifact-not-proof case study that prompted this note.
- [[harness]], [[harness-best-of-breed]] — general agent-harness principles.
- [[model-task-benchmarks]] — model-picking guidance for coding; math proving picks are separate.
- [[orchestrator-worker-protocol]] — decomposition + verification-in-loop patterns transferable to blueprint-DAG proof orchestration.

## Provenance

Workflow `llm-jacobian-and-proving-frameworks` 2026-07-21: 10 parallel system readers + 5 adversarial verifiers + 1 synthesis. Primary sources fetched: arxiv PDFs/abstracts + GitHub READMEs + official blog posts. Single-pass mining; verify any load-bearing benchmark number against the specific paper's most recent version before quoting.

