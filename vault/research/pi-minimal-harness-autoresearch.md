---
type: reference
tags: [research, agents, harness-design, evals]
created: 2026-08-05
updated: 2026-08-05
---

# distilled report: pi minimal harness + shopify autoresearch

source: https://earendil.com/posts/pi-autoresearch-and-databricks/ (earendil vendor post, 2026-08-04). full distillation: /tmp/PHO-0-READ-PI-AUTORESEARCH-report.md (ephemeral).

## core claims

- pi is a coding-agent harness shipping 4 tools and a <1,000-token system prompt; everything else is user- or agent-built extensions (pi can read its own extension docs and generate new workflow tools).
- databricks internal benchmark (methodology unpublished): >2x cost-per-task spread across harnesses at fixed model; pi sends ~3x less context per turn; pi + opus 4.8 xhigh had the highest pass rate on their private benchmark.
- shopify's pi-autoresearch extension: autonomous loop of change -> run experiments -> discard regressions -> repeat toward a measurable target. anecdotal wins (300x one unit-test path, 20% react mounting) — cherry-picked, scope unclear.

## assessment

- genuinely interesting: minimal-by-default/extensible-by-design as explicit harness strategy; the self-extending agent loop; autoresearch as a clean framing of eval-driven autonomous optimization.
- dressed-up standard practice: small prompts win (known), eval-driven iteration (known). all numbers are vendor-framed marketing without published methodology.

## relevance to phoebe

- validates the v3 bet: bash-only minimal core, complexity via explicit opt-in — same philosophy pi markets, with third-party (if soft) evidence that context discipline pays.
- autoresearch loop == the shadow-parity/eval-gated cutover shape phoebe already runs (measure, refuse regressions, iterate). no new technique to adopt; the value is confirmation.
- self-extending agents: orthogonal to the current carve-first arc; possibly relevant much later.

## related

[[applied-compute-self-distillation]], [[v3-harness-design]]
