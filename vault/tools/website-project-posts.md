---
type: decision
tags: [tools]
created: 2026-09-10
updated: 2026-09-10
---

# Website project posts: code-ified 0-to-1 convention


Henry locked this direction 2026-09-10: project posts on
hwang2409.github.io should be more technical and read 0 -> 1 — "define
the different data structures and classes that goes into making
something. More code-ified."

## The convention

- Each major post section is anchored by the REAL core types from the
  project source: short faithful Rust excerpts (trimmed with `// ...`),
  then lowercase prose explaining the fields.
- 0 -> 1 arc: the post builds the system from nothing; each section's
  type composes the previous ones.
- Excerpts are copied from source, never invented; accuracy is a review
  gate (wrong field names = HIGH finding).
- Interactive widgets stay; code anchors replace vague prose, not demos.
- Repo copy of the convention lives in website DESIGN.md ("project
  posts" subsection, added in WEB-39).

## Rollout

COMPLETE 2026-09-10: WEB-39 chimy2 (#40, adds the DESIGN.md
subsection), WEB-40 newt (#41), WEB-41 pufferclone retrofit (#42).
Applies to every future project post.

## Review lesson (WEB-41, 5 rounds)

The excerpt-elision class (silent elision, misplaced markers,
attributes-as-comments, whitespace drift) survived point-fix rounds in
every lane and terminated only under a structural invariant plus an
uncommitted mechanical checker (strict ordered line-subset of one
contiguous source region; `// ...` at every omitted span; kept lines
verbatim after leading-indent strip; <=10 physical lines; checker
mutation-tested). Future code-ify kickoffs should mandate the invariant
+ checker from round 1. Also: marker-elided attribute lines ARE
compliant — a reviewer demand to show elided attributes was overruled
(2026-09-10).
