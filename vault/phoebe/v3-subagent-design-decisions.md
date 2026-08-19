---
type: decision
tags: [phoebe]
created: 2026-08-10
updated: 2026-08-10
---

# v3 subagent platform: design decisions for Henry


One-page memo distilling five review rounds on PR #13746 (subagents), eight on
PR #13745 (capability library), and the open retention thread — into the four
decisions that unlock PHO-15434 (org skill files). Both PRs are parked as
clean, CI-green DRAFTS pending these calls.

## Decision 1 — subagent runtime shape (PR #13746)

What five review rounds established:
- The architecture is sound: worker-hosted child loop, sandboxed tools,
  register handoff, durable fan-in via the forwarding outbox. Verified safe:
  grandchild rejection, spawn serialization, cancel cascade, cost roll-up,
  bounded no-follow input copies, single-child seal-before-publish ordering.
- What kept failing: PARALLEL fan-in (3 distinct races across rounds), budget
  accounting across provider fallback (2 rounds), pause-tool exclusion
  (name-list -> trait, still fail-open at the executable layer).
- Round 5 (in the draft) implements: single-child clamp, idempotent
  reservation-keyed budgets, fail-closed executable-layer pause filtering,
  full register-contract adoption.

**The call:** accept the single-child v1 (sequential clones; parallel fan-out
becomes a designed v2) or insist on parallel v1 (reopens the race surface that
consumed three rounds). Orchestrator recommendation: accept the clamp.

## Decision 2 — is the clamp acceptable for the product?

Henry's motivating flow (multi-shift outreach analysis) under single-child:
the parent spawns clones sequentially, or does the fan-out itself via
registers + bash (which round-trips nothing). Cost: wall-clock, not tokens.
If sequential is unacceptable for the end-of-week org-skill workflows
(PHO-15434's demo), parallel v2 moves up the priority list.

## Decision 3 — capability library integration (PR #13745)

The library is finished and structurally isolated (import-linter enforced;
main bundle provably untouched). Its sole consumer is the child-bundle path.
**The call:** land it when #13746 lands (its rebase recipe is mapped), or fold
both into one integration PR after the design pass. No open defects; the last
round's two residuals (as_packages contract hole, NUL-scan) are fixed in the
draft's final push.

## Decision 4 — TTL / durable retention (the oldest open thread)

AGENT_SANDBOX_WORKSPACE_TTL_S = 7d sweeps durable run files; caps: 100MiB/run.
Now load-bearing for: register refs (#13744, merged), bash widgets (#13724,
merged), child result envelopes (#13746), and org SKILL FILES (PHO-15434 —
which must be sweep-EXEMPT or stored apart).
Proposal on the table: retention tied to run lifecycle (sweep on archival,
~30d fallback for abandoned runs) + a distinct non-swept class for org-scoped
durable content (skill files). **The call:** approve the lifecycle-retention
shape (then it becomes a ticket), or keep the flat TTL and PHO-15434 gets a
dedicated storage table instead.

## What each outcome unlocks

Decisions 1+3 -> merge #13746 + #13745 -> PHO-15434's child runtime exists.
Decision 4 -> PHO-15434's storage answer. Then 15434 = scoping doc + demo
(org-authored "grade this week's outreaches" skill running in the perf org),
already spec'd in the ticket.
