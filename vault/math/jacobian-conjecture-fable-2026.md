---
type: reference
tags: [math, algebra, ai, agents]
created: 2026-07-21
updated: 2026-07-21
---

# Jacobian Conjecture: Alpoge–Fable 2026 counterexample

Announced 2026-07-19/20 by **Levent Alpoge** (Harvard number theorist). Claude **Fable 5** (Anthropic public deployment of Claude Mythos) produced an explicit polynomial counterexample to Keller's 1939 Jacobian Conjecture in dimension 3 — refuting JC in every $n \geq 3$. The n=2 case remains open.

## The map

$F : \mathbb{C}^3 \to \mathbb{C}^3$, with $u := 1 + xy$:

- $F_1 = u^3 z + y^2 u (4 + 3xy)$
- $F_2 = y + 3x u^2 z + 3x y^2 (4 + 3xy)$
- $F_3 = 2x - 3x^2 y - x^3 z$

Component degrees: $(7, 6, 4)$.

## Verification (reproduced 2026-07-21 in this session)

- **Jacobian determinant**: $\det J_F \equiv -2$ identically (symbolic verify in SymPy — not numerical only).
- **Fiber over $(-1/4, 0, 0)$**: three distinct preimages:
  - $(0, 0, -1/4)$
  - $(1, -3/2, 13/2)$
  - $(-1, 3/2, 13/2)$
- Constant nonzero Jacobian $\Rightarrow$ étale. Non-singleton fiber $\Rightarrow$ not injective $\Rightarrow$ not a polynomial automorphism. This is precisely a Keller-map counterexample.
- Extends to $n \geq 3$ by adjoining identity coordinates; Alexis Gallagher + GPT-5.6-sol reportedly derived an infinite weighted-lift family.
- Arithmetic verification is one-line — anyone can reproduce in SymPy or Mathematica.

## Attribution and human/LLM split

Alpoge (Harvard) directed the search after his friend Akhil posed the question; Fable 5 produced the candidate map and algebra under "time pressure." Naskrecki explicitly cautioned it was "not a one-line prompt" — meaningful prompt engineering / iteration was involved, though exact iteration count and full transcripts have not been published. No external CAS is cited as required for the discovery itself; SymPy (rational arithmetic) was used post hoc by Gallagher for a verification preprint. Reverse-engineering the geometric structure was done by Andy Jiang using ChatGPT.

- **Fable 5**: generated the candidate polynomial map itself (not just verification).
- **Alpoge**: directed the search, verified, announced. Prompt kicked off by a question from his friend Akhil.
- **Andy Jiang** + ChatGPT: reverse-engineered a geometric interpretation via $\mathbb{P}^1 \times \mathrm{Sym}^2(\mathbb{P}^1) \to \mathrm{Sym}^3(\mathbb{P}^1)$.
- **Alexis Gallagher** + GPT-5.6-sol: higher-dimensional family + SymPy verification harness.
- **Piotr Naskrecki caveat**: "it was not a one-line prompt" — meaningful iteration involved; exact transcript unpublished.
- No external CAS cited as necessary for discovery; SymPy used post hoc for verification.

## Reception

- **Tim Gowers** — "amazing"; frames as first time an LLM cracked a well-known problem outside his own area.
- **Abhishek Saha (QMUL)** — likely the biggest conjecture AI has meaningfully touched.
- **Nathan Blumberg** — cooler; frames as expected AI computational-search capability rather than explanatory proof.
- No journal peer review yet. Widely accepted pending formal write-up because arithmetic is trivially checkable.

## Why it detonates

- **JC dim 3 falsified.** Bass–Connell–Wright cubic-homogeneous reduction propagates upward — a genuine dim-3 counterexample triggers re-examination of the whole reduction machinery.
- **Dixmier conjecture also dies.** Tsuchimoto 2005 / Belov-Kanel–Kontsevich 2007: JC $\Leftrightarrow$ Dixmier conjecture on $\mathrm{End}(A_n(\mathbb{C}))$. Counterexample here kills a central conjecture in noncommutative algebra.
- **Étale $\neq$ automorphism on $\mathbb{A}^n_\mathbb{C}$.** The map is a nontrivial étale cover of $\mathbb{A}^3$, seemingly contradicting the folk fact $\pi_1^{\text{ét}}(\mathbb{A}^n_\mathbb{C}) = 1$. Foundational implications.
- Complements **Pinchuk 1994** (real JC in dim 2, killed by a nonconstant-Jacobian étale map) with a *constant*-Jacobian complex example — strictly stronger phenomenon.

## Primary sources

- [blog] https://sbseminar.wordpress.com/2026/07/20/the-new-counterexample-to-the-jacobian-conjecture/
  - Secret Blogging Seminar post giving the exact map F1=(1+xy)^3 z + y^2(1+xy)(4+3xy), F2=y+3x(1+xy)^2 z+3xy^2(4+3xy), F3=2x-3x^2 y-x^3 z, det J = -2, generically 3-to-1; attributes discovery to Fable announced by Alpoge on 2026-07-19; Andy Jiang used ChatGPT for a geometric reinterpretation via P^1 x Sym^2 P^1 -> Sym^3 P^1.
- [blog] https://jacobianfun.org/jacobian-explained
  - Independent exposition by Alexis Gallagher with GPT-5.6-sol; verifies the map's determinant symbolically in SymPy and lists the exact three-point collision (0,0,-1/4), (1,-3/2,13/2), (-1,3/2,13/2) mapping to (-1/4,0,0); component degrees (7,6,4).
- [blog] https://explainx.ai/blog/fable-5-jacobian-conjecture-counterexample-alpoge-july-2026
  - Identifies the model as Claude Fable 5; Alpoge directed, model produced candidate construction under time pressure; not peer-reviewed; Gowers quote about first LLM-cracked well-known outside-area problem; Saha quote calling it likely the biggest conjecture AI has touched.
- [blog] https://thenextweb.com/news/jacobian-conjecture-disproved-ai-fable-5
  - Press coverage dated 2026-07-21; Fable 5 = Anthropic's public Claude Mythos; Naskrecki says 'not a one-line prompt'; Blumberg skeptical framing; Gowers 'amazing'.
- [blog] https://finance.biggo.com/news/45a1025a-1142-49a8-9cb3-aeb581340f12
  - Additional press coverage: 'AI Model Claude Fable 5 Overturns 87-Year-Old Jacobian Conjecture with Three-Line Formula.'
- [blog] https://glitchwire.com/news/a-mathematician-used-claude-fable-to-disprove-the-87-year-old-jacobian-conjectur/
  - Reports Alpoge used Claude Fable to produce the disproof in a few hours.

## Status summary

Unverified in the formal sense (no journal peer review yet), but the arithmetic — det J = -2 and the three-fiber collision — is trivial to check symbolically and has been independently reproduced by many mathematicians. Community reception is broadly that the counterexample is correct: Gowers "amazing," Saha calls it likely the largest conjecture AI has meaningfully cracked, Blumberg cooler (frames it as computational search rather than explanatory proof). A verification preprint and follow-on work (Bass–Connell–Wright reduction in 24 variables, n >= 3 generalization) are circulating. n = 2 remains open. Treat as "widely accepted pending formal write-up" rather than fully settled.

## Related in vault

- [[llm-rigorous-proving]] — how a "tight framework" for LLM-driven rigorous proof would be built; contrasts this arithmetic-verifiable artifact with kernel-verified proof.
- [[harness]] — agent harness principles.
- [[hot]] — mentions Fable 5 status; this event is what actually made Fable famous outside coding.

## Provenance

Deep-research workflow `llm-jacobian-and-proving-frameworks` 2026-07-21: 1 primary-source hunt + 5 adversarial verifications. All 6 key sources listed above; 6-source blog cluster (Secret Blogging Seminar is the closest to primary math discussion). Symbolic Jacobian verification reproduced locally via SymPy in the same session. **Not yet peer-reviewed** — treat as widely-accepted-pending-write-up, not settled math.

