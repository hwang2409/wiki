---
type: reference
tags: [newt, tooling]
created: 2026-08-20
updated: 2026-08-20
---

# Newt audit 2026-08-20

Audit of `tooling/newt` at `5f6765a`; local gate passed on 2026-08-20.

- penalty-mode tree contacts apply a wrench only to the active tree. A
  link-to-body pair drops the body's reaction; a cross-tree pair uses the
  other tree at identity pose. Use PGS/Newton, or reject these pairs.
- sensor evaluation uses start-of-step contacts with post-step bodies and
  re-solves forces. Dynamic touch and accelerometer readings match neither
  the applied force nor a post-step `mj_forward` state.
- `World::auto_pairs` remains O(n²). NEWT-39 needs a deterministic broad
  phase before scenes grow past roughly 100 geoms.

Provenance: Codex code audit; `cargo test`, alloc guard, clippy, and fmt
passed. See `newt/src/world.rs` and [[newt-demo-convention]].
