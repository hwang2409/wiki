---
type: decision
tags: [phoebe, v3-agent, sandbox, PHO-16987]
created: 2026-08-26
updated: 2026-08-26
---

# v3 sandbox: single image, no profile split (PHO-16987)

Henry decision 2026-08-26 (PR #15357 round 8): the v3 agent sandbox must
NOT split MINIMAL vs ANALYSIS image profiles. One image for all surfaces:
analysis packages (matplotlib, seaborn, ...) baked at build time from the
hash-locked requirements (`--require-hashes`), runtime network fully
blocked everywhere, phoebe chart style + data_analysis skill retained.

Why: the profile split was the round-1/2 security fix for runtime pip
with PyPI egress leaking to the internal read-only surface via the shared
per-org pool. Rounds 5-7 replaced runtime pip with baked, locked packages
and a network-disabled image — the split's security rationale died, and
maintaining it created two containers per org (both mounting /workspace)
plus a receipt cross-container readback bug (durable spill persists never
bump the workspace generation, so a warm container never re-hydrates).

Accepted tradeoff: every org sandbox carries the heavier image (internal
read-only included); a fingerprint change rebuilds all org sandboxes.

Kept guarantee: `analysis_image_fingerprint` feeds `_image_version` so any
recipe change rebuilds sandboxes (round-7 stale-image-reuse fix).
