---
type: til
tags: [agents]
created: 2026-08-27
updated: 2026-08-27
---

# Orchestrator spec overreach: raw-byte cache property


## What happened (ZETA-39, PR #65, 2026-08-27)

The cache-tuning lane failed three review rounds. Rounds 1-2 were real
worker failures (repro-patching instead of property implementation).
Round 3 failed on ONE property: "full raw provider-byte prefix
stability across turns". The reviewer proved the implementation passes
every scenario under metadata-stripped semantics — the only raw-byte
divergence was the `cache_control` markers moving forward each turn,
which is CORRECT Anthropic incremental-cache usage (cache matches on
content-block prefixes; marker placement is metadata).

The orchestrator's property spec was unsatisfiable by any correct
implementation. Ruled the spec wrong, treated round 3 as a clean pass,
merged with the ruling documented on the PR.

## Rule

When one property survives multiple fix rounds while everything else
converges, audit the property itself before spawning another round:
re-derive it from the underlying system's real semantics (API docs,
wire behavior), not from the proxy you wrote first. A property no
correct implementation can satisfy looks identical to worker
non-convergence from the outside.

Corollary: reviewers verifying "spec deviations" faithfully will
reject correct code — the orchestrator owns spec bugs, and the ruling
belongs on the PR record.
