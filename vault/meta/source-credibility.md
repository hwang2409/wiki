---
type: reference
tags: [meta, agents, research]
created: 2026-07-06
updated: 2026-07-06
---

# Source Credibility (for research tasks)

Apply this when a research query NEEDS trustworthy sourcing — not always. Read this note when the finding will be acted on; skip the overhead when it won't.

## When to apply

Agent judgment call at task start:

- **Apply** — the output drives a decision with cost: adopting a tool/pattern, paying for something, security posture, architecture direction, anything Henry will act on or that lands in a decision note.
- **Skip** — casual exploration, inspiration gathering, "what's out there" surveys where wrong claims are cheap, anything Henry will personally re-judge anyway. Say in the note that sourcing was casual.

## Core principle: verifiability beats reputation

Agents cannot feel community reputation or smell content marketing the way Henry can. Do not imitate that judgment — substitute structural verification:

- **Primary over secondary.** Code, specs, official docs, the tool's own repo cannot be wrong about themselves; blogs restating them can. Verify claims against the primary artifact, then the intermediary's reputation stops mattering.
- **Checkable provenance.** A claim that traces to something openable (repo, RFC, changelog, API response) is verifiable; a claim that traces to nothing is the red flag — regardless of where it appeared.
- **Specificity texture.** Exact versions, exact commands, admitted failures = author did the thing. Vague superlatives, listicle shape, affiliate links = SEO output.
- **Independent triangulation.** Agreement counts only when sources differ in phrasing/structure — aggregators copy each other (citation circles share sentences).
- **Skin in the game.** Maintainer writing about own tool, practitioner showing real usage, repo with real issue traffic — weak alone, meaningful stacked.

Known agent failure mode: fluent confident prose reads as credible to a model, and SEO content is optimized for exactly that. Verification must be structural, not vibes.

## Mechanics when applying

1. Any claim taken from a blog/aggregator gets verified against the primary (code/spec/docs) before inclusion — or is explicitly marked unverified.
2. The resulting note ends with a source table: link + one line on why it is citable (who wrote it, what was verified against what). Henry scans this table in minutes — that is where his reputability instinct plugs in.
3. High-stakes claims: adversarial verification — spawn verifiers that try to REFUTE each key claim against primaries; keep only claims that survive. (Exemplar: [[agent-vault-patterns]] — 25 claims verified 3-0.)

Related: the provenance rule in [[conventions]] (derived notes cite sources) applies to ALL notes; this note is the stricter opt-in layer for when the sources themselves must be trustworthy.
