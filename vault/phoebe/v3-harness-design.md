---
type: decision
tags: [phoebe, agent-v3, design]
created: 2026-07-31
updated: 2026-07-31
---

# Phoebe Agent V3 harness design

Decision record from Henry + mastermind design session, 2026-07-31. Status:
approved direction, fully experimental. All work stays on an umbrella branch
(`henry/phoebe-v3-agent`, off `main`) until the design survives contact
with implementation; no PRs to `main` until then. Linear project: **Phoebe
Agent V3** (one ticket per build step, all scoped to Henry).

## Implementation status

- 2026-07-31: PHO-14963 provider-neutral tool discovery passed final review at `c7dd622e70` on `henry/phoebe-v3-agent-tool-search`; no PR opened.
- 2026-07-31: PHO-14977 helper bin and eval scaffolding passed final review at `66b3cd5da7` on `henry/phoebe-v3-agent-helpers`; no PR opened.

## Problem

The v2 general agent grows by adding scoped, atomic tools for every
capability. The harness bloats, the prompt bloats, and the agent gets
confused. Expansion cost is linear in tools; correctness degrades with tool
count. The 2026-07-30 admin-agent audit counted 182 tools on the admin side;
the general agent is on the same trajectory.

Prior evidence pointing at the fix: PHO-14938 tool-output tiering
(summary-first + artifact spill won on cost), PHO-14953 / PHO-14624 /
PHO-14625 (fetch-to-JSONL + bash parse + write-from-file loops eval well),
harness doctrine PHO-12982 ("does this piece gain or lose value when the next
model is 2x better?" — one retrieval tool + bash gains; N atomic tools lose).

## Core idea

Harness-level bash support. Every v3 agent instance is implicitly connected
to a sandboxed workspace — no spawn tool, the agent does not know the sandbox
exists as a concept. Large tool outputs become files. The tool zoo collapses
into a small set of doors: `retrieve`, `write`, `describe`, `bash`.

## Invariants (the spine)

1. Data enters the workspace through exactly one door: `retrieve`.
   Authorization (org scope, mode, redaction) is enforced there and nowhere
   else.
2. Workspace is keyed to conversation + mode. Compute is pooled per org.
   The sandbox holds zero secrets and has zero network egress.
3. Writes leave through exactly one door: `write`. Policy hangs off
   entity + operation in a registry, never off tool names.
4. Nothing authoritative lives in the workspace. Agent-writable space holds
   only artifacts and scratch. Bash touches files only — never live data
   sources, never credentials.
5. Content read back from an artifact into model context is untrusted
   evidence — v2 `wrap_untrusted_evidence` applies at read-back.

The single retrieval door is simultaneously the anti-bloat move and the
security design: one auditable choke point, and bash cannot escalate because
the sandbox contains nothing to escalate with.

## Components

### Workspace service
- Lazy attach on first spill or bash call (matches the use-time credential
  model from PR #13011).
- Per-conversation+mode volume mounted into the per-org warm gVisor sandbox;
  per-exec bwrap jail as today (`libraries/python/agent_sandbox`).
- TTL sweep of stale workspaces (start at 7 days).
- Read-only `bin/` of pre-written helper scripts baked into the image
  (`jsonl-filter`, `jsonl-join`, `csv-cut`, `sample`, `schema-sniff`, ...).
  Read-only so the agent cannot overwrite/poison its own utilities.
  Convenience, not authority.

### Spill layer
- Harness middleware on every tool result. Over threshold (start: 500 chars)
  → write typed artifact (JSONL/CSV/JSON) into the workspace, return inline
  envelope `{artifact_path, format, row_count, schema_hint, head_preview}`.
  Under threshold → pass through.
- Uniform: tools do not opt in or out. Envelope failure fails the tool call
  loudly; no silent truncation.

### retrieve
- `retrieve(domain, scope?, time_range?, ids?, cursor?)` → artifact +
  envelope.
- Domain registry row = query implementation + scoping rules + schema +
  redaction policy. Coarse args only; the agent filters locally with
  bash/jq. Registry is the successor of the v2 capability list — one
  auditable table of everything the agent can see. Target: full v2 read
  parity.
- Over-fetch is a cost problem, not a correctness problem; contained by
  pagination + default time windows.

### write
- `write(entity, operation, payload | file)`.
- Entity registry row: pydantic schema derived from generated DB models,
  server-side invariant checks, and policy per entity+operation:
  `direct` | `diff-first` | `human-approval`.
- Diff-first = write produces a proposed-change artifact (rendered diff);
  a second confirm call applies it. Bulk file-input writes are always
  diff-first (one call can carry hundreds of rows; the diff artifact is the
  review surface).
- Human-approval set stays aligned with the v2 rule (PHO-12515): production
  / customer-visible mutations gate; the rest are audit-only.

### describe
- Inline, compact domain/entity catalog — always under the spill threshold.
- Generated from the same registries that enforce authz, so docs cannot
  drift from enforcement.
- Delivered tool-search style (on-demand schema loading — converges with
  PHO-14963 rather than duplicating it). Explicitly NOT files in the
  sandbox: the workspace is scratch, not documentation.

### bash
- Unchanged jail semantics from today (gVisor + bwrap, audit, budgets),
  pointed at the workspace.

## Failure and observability

- A domain/entity not in its registry is a typed refusal, not an empty
  result (`mode_mismatch` over silent `not_found`, as in v2).
- Every retrieve/write emits the standard audit pair (reserved start +
  terminal event) with artifact refs. Bash execs audited as today.
- Registry rows are exhaustively enumerated; exhaustive `match` on policy
  enums.

## Testing / evals

- Contract tests: every registry row round-trips schema + scoping; no code
  path other than the spill layer and `retrieve` can write into a workspace;
  every `read_only=False`-equivalent operation carries an explicit policy.
- Mode fail-closed tests per door.
- Golden bash-loop evals per howtoeval (retrieve → jq → write), extending
  the PHO-14953/14624 cases. PHO-14961 (outreach skillset validated with
  bash harness changes) is effectively the first eval milestone.

## Build order (one Linear ticket per step)

1. Workspace service: implicit attach + per-conversation+mode volumes + TTL
   sweep + spill middleware.
2. `retrieve` + domain registry, first 3-4 domains at v2 parity.
3. `describe` via tool-search delivery (joint with PHO-14963).
4. `write` + entity registry + diff-first machinery.
5. Domain/entity buildout to full v2 parity; v2 tool deprecation plan.
6. Helper-script `bin/` + eval suite hardening.

## Open questions (deliberately deferred)

- Exact spill threshold and per-format envelope tuning (start 500 chars,
  revisit with eval data).
- Which domains land in step 2 (pick from the highest-traffic v2 tools).
- Workspace retention/PHI policy beyond the 7-day TTL default.
- Whether `describe` and PHO-14963 tool search fully merge or stay adjacent.

## Related

[[admin-agent]], [[admin-agent-audit-2026-07-30]],
[[admin-agent-tool-discovery]], [[admin-db-reads-generated-catalog]]
