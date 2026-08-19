---
type: til
tags: [tools]
created: 2026-08-11
updated: 2026-08-11
---

# Kickoff contracts: name the recurring defect classes

cdx implement workers (gpt-5.6-luna) reproduce the same defect classes across independent tickets. Observed in the chimy2 arc (2026-08-10/11), each caught by review 2+ times:

1. **Stale cache behind public fields.** Worker adds a derived/cached value computed in `new()` but leaves source fields publicly mutable — later mutation silently desyncs the cache. Cases: M6 uniform matrices (transform/normal_matrix), CHIMY-9 texture mips. Fix shape: private fields + cache-rebuilding setters + per-mutator probe tests with immediate assertions (a sequence-then-assert probe lets later setters repair earlier ones — M6 round 3).
2. **Internals leaking through a public seam.** Worker plumbs raster-owned state (depth, barycentric weights, gradients, 1/w) through a shader/varying-facing signature instead of a scoped channel. Cases: M2 fragment depth arg (leaked twice — moved one layer down on the first fix), CHIMY-9 Varyings gaining weights+gradients+w. Fix shape: the general trait keeps one job; scoped opt-in structs carry specialist data.
3. **Tests that do not bite.** Test exercises a helper or a special case, not the production path. Cases: M1 resize test bypassing `run`, M3 focus test bypassing event routing, M4 single-face normal test, M6 sequence probe. Countermeasure (works): mandatory mutation gates with pasted FAILED output, and reviewers running UNCLAIMED probes.
4. **Unsafe invariants resting on publicly-mutable state.** First unsafe blocks cite invariants a caller can break. Case: CHIMY-10 NEON depth load segfaulting when the public depth Vec is swapped. Fix shape: checked slice at the unsafe boundary; SAFETY comments must cite locally-enforced facts.

**How to apply:** paste the relevant classes into implement-worker kickoff contracts as named prohibitions with the fix shape, and into reviewer contracts as named hunt targets. Naming them cut round counts in the late chimy2 arc (M4: 2 rounds vs M1/M3: 3-4).

