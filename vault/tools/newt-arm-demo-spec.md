---
type: reference
tags: [newt, chimy2, tooling, demo]
created: 2026-08-24
updated: 2026-08-24
---

# newt humanoid arm demo spec (ARMDEMO-1)

Henry's ask (2026-08-24, restated after the wipe ate the original thread): **realistic demo of a humanoid arm using newt + chimy2.** Prior thread context was lost in the second fleet wipe; this note is the durable spec.

## Current baseline

- `newt/examples/arm.rs` (411 lines): 3-link commanded arm, PD position servos, model in `newt/models/arm.json` (tier-5 loader reference), chimy2-rendered mp4 via `showcase_support.rs`. Output `demo-arm.mp4` last rendered 2026-08-19.
- newt→chimy2 bridge exists and is the standard demo pipeline (`showcase_support.rs`, ffmpeg mp4 assembly).
- newt has MuJoCo-semantics muscle actuators (PR #73 fixed semantics; `muscle_pendulum.rs` is the reference), ragdoll helpers (NEWT-53), friction extensions (NEWT-49).
- Worktree `armdemo-1` at repo root exists, clean, branch `armdemo-1`.

## Target

A humanoid arm (not a generic 3-link):

- **Anatomy:** humanoid proportions and masses — upper arm, forearm, hand; shoulder 3-dof, elbow hinge, forearm pronation/supination if cheap, wrist 2-dof. Capsule/rounded geometry, not sticks.
- **Actuation:** muscle actuators (MuJoCo semantics) for the main motions — antagonist pairs (biceps/triceps at minimum) — realistic activation dynamics over pure PD. PD assist allowed where muscles would bloat scope.
- **Motion:** natural reach sequence (reach up, reach across, settle), smooth, gravity on; no teleporting or servo snap.
- **Render:** chimy2 lit path (blinn-phong shaded solids), fixed camera or slow orbit; NOT wireframe.
- **Output:** mp4 via system ffmpeg to `newt/demos/` per [[newt-demo-convention]] (video-first, never delete media, untracked).
- Deterministic like the other showcase examples (fixed dt, hand-written trig, bit-for-bit reproducible at fixed `--frames`).

## Status

- 2026-08-24: spec written; queued as next tooling worker slot, ahead of NEWT-61 (Henry pinged the demo twice today). Worker cap 2/2 (NEWT-52-PR6, NEWT-62-PR6 fix rounds live).
- 2026-08-24 ~22:20Z: Henry authorized running the demo lane IN PARALLEL with the fix lanes ("shouldn't be too intensive") — ARMDEMO-1 spawned (cdx luna high) in `armdemo-1` worktree; cargo builds mutex-serialized, render runs off the built binary without the mutex. Deliverable: `newt/demos/demo-arm-humanoid.mp4` + PR with new `arm_humanoid.rs` example.

Related: [[newt-demo-convention]], [[orchestrator-worker-protocol]].
