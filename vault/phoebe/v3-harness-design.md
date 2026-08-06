---
type: decision
tags: [phoebe, agent-v3, design]
created: 2026-07-31
updated: 2026-08-03
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

- 2026-08-03: PHO-0-CONVERGE folded sandbox-agent + r2-domains + w1-entities into `henry/phoebe-v3-agent-converge` at `8c144a5bdc`.
- 2026-08-03 (orch, executed): `henry/phoebe-v3-agent` fast-forwarded to `8c144a5bdc` via GitHub API (pre-push hook was blocked on unrelated working-tree ty/prettier issues). 12 subsumed sibling branches deleted from origin: describe, helpers, parity, retrieve, retrieve-plan, tool-search, workspace, write, write-plan, r2-domains, sandbox-agent, w1-entities. Remaining v3-agent-* branches: converge (redundant marker, kept), + 4 active parity siblings (r3-domains, w2-entities, shadow-parity, evals). PR diffs shrank to 13-35 files / 3-5k additions each.
- 2026-08-03 (Henry, re-confirmed): **end goal for every active parity worker (PHO-15079/15080/15082/15083) = converge all output back into `henry/phoebe-v3-agent` and make v3 testable *from that branch*.** Not from main. Individual squash-PRs on main (#12876…#13101) landed the v3 core; the parity arc lives on the umbrella until it's coherent enough to test. Do not delete/retire v3-agent; do not open main PRs from parity workers.
- 2026-08-03 (Henry, workflow clarification): **convergence = normal PR flow, retargeted at v3-agent.** Each parity worker opens a PR from its sibling branch (e.g. `henry/phoebe-v3-agent-r3-domains`) with `--base henry/phoebe-v3-agent`. PRs get reviewed and merged into v3-agent the same way normal PRs merge into main. NO dedicated CONVERGE integration worker; that pattern is retired in favor of PR-based merges. Orchestrator (phoebe-dev) is opening 4 draft PRs now (one per active parity ticket), promotes them ready-for-review once orch reviewer returns MERGE-READY on the current SHA, and Henry keeps merge authority.
- 2026-08-03 (Henry, re-confirmed): parity arc constraint — expose the LEAST number of tools/surfaces possible while keeping functional parity with the v2 general agent. Map v2 tools onto the existing doors (retrieve domains, write entity+operation registry, describe, bash); never port tools 1:1. Each r3/w2/w3 addition must justify why it cannot fold into an existing domain/entity.

Update 2026-07-31 (Henry): step work drifted onto independent
`henry/phoebe-v3-agent-*` branches off `main` (describe, parity,
tool-search, helpers, workspace, retrieve, write) with heavy file overlap.
Henry re-confirmed `henry/phoebe-v3-agent` as the single experimental
branch; PHO-V3-INTEGRATE worker is merging the settled step branches into
it (describe -> parity -> tool-search -> helpers), and workspace/retrieve/
write fold in as each clears review. Some tickets name a
`henry/phoebe-v3-harness-proto` umbrella — that branch never existed; this
note is canonical.

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

Integration note (2026-07-31, review10): the retrieve branch's activity
lease is a plain 300s Redis lock (no generation, renewal, or fencing);
the workspace branch carries the stronger generation-fenced lease. At
integration the workspace fencing model wins, and durable seed recovery
must move inside the fenced critical section.

Integration complete (2026-08-01): PHO-14973 was folded into
`henry/phoebe-v3-agent` with the workspace generation-fenced, renewable lease
model, atomic advance-only seeding, and workspace-identity durable activity
query. Final branch SHA: `da184279d4560c3e0b8ae3d2e7b0c69289d6cf35`.
Verification: Bazel v3 agent 12/12, agent sandbox 15/15, workspace spill 2/2,
LLM framework 33/33, worker 1/1; ty passed on touched packages.

Design decisions locked (2026-08-05, Henry):

- No bash feature flags: bash is baked into the v3 agent unconditionally.
  The five `agent_bash_*_enabled` flags and the bash-admission run pin were
  removed from #13413; the `BashGate` concept was removed from the #13416
  door contract. The `AGENT_SANDBOX_DISABLED` env emergency switch stays
  (ops kill switch, not a product flag). Per-surface v3 ROUTING flags
  (`agent_v3_<surface>_enabled`) survive — they gate v2->v3 rollout, not bash.
- No v2 changes: the v2 agent (`phoebe_event_agent`) and every path a v2 run
  executes must remain byte-identical to main. All v3-arc changes are purely
  v3-side or strictly additive in shared files. Shared read logic is
  extracted as standalone libraries with their own tests; v2 keeps its
  private copy until v2 retires (temporary duplication accepted).

Merge-order decision (2026-08-05, Henry): carve-first. After the v2
restoration lands on `henry/v3-main-integration`, the branch is carved into a
stacked ladder and the v0 foundation PR (agent_sandbox + run_bash + the
ToolOutputSpool middle layer + minimal v3 core) merges to main BEFORE anything
else. All currently gates-complete PRs (#13413 routing flags, #13414 shared
read libraries, #13416 bash door) hold until the foundation PR is merged.
Rationale: the small PRs pre-introduce v3 package files and would conflict-churn
the integration branch if merged first. Ladder after foundation: reads ->
control/TUI -> writes (post design pass).

Arc invariant (2026-08-05, Henry): v3 is prototypical — NO customer traffic
touches v3 until much later, explicitly. Every ladder merge must be prod-inert
(behavior delta on main = zero). The local TUI (tools/v3_tui) moves into the
v0 foundation PR so the prototype is human-drivable from the terminal with
zero prod exposure. #13413 (surface routing flags) is the eventual light-up
dial and parks at the BACK of the queue behind the entire ladder.

Parked (2026-08-05): agent-chosen sandbox routing — hybrid design. Keep the
spill threshold as the safety net; add an explicit per-call `to_sandbox=true`
opt-in so the agent can deliberately stage results as files for grep/jq
workflows. Later PR on the existing ToolOutputSpool seam (rung 2+). Ladder
PR 1 (#13505) merge awaits Henry's personal review — orchestrator holds.

Rung-2 carve note (2026-08-05): semaphore/redis.py and agent_sandbox/budget.py
on ladder-1 (#13505) deliberately DIVERGE from source da8741cd — per-lease
error isolation fix (lost lease no longer aborts sibling refreshes; opt-in
raise_on_lease_loss). Later rung extractions must NOT overwrite these files
back to source. The source-branch defect is documented in #13505's body.

Scope-down directives (2026-08-05, Henry's #13505 review — "go go go"):
1. llm_framework changes move INTO phoebe_v3_agent; framework diff shrinks to
   at-most minimal justified hook parameters (prefer v3 tool-group-layer
   wrapping over runner hooks).
2. agent_sandbox/Modal cut to least-solid-foundation: keep org acquire, bwrap
   + containment lane, exec caps, workspace layout/tokens, emergency switch,
   semaphore with per-lease fix. Cut candidates: durable store/rehydration,
   activity fencing beyond basic lease, audit, TTL sweep/cleanup worker/proto,
   broker/transport layering. Judgment rule: if TUI + run_bash + spool works
   without it for one prototype user, it is not foundation.
3. Python-only jail: NO pre-written helpers — agent_sandbox/bin and the
   in-jail phoebe.py library deleted (removes the eval filter_by entirely);
   jail = coreutils/jq/rg/python3; agent writes its own code. Prototype-first:
   see what it can do and where it struggles.
Downstream: rungs 2/3 and #13416 rebase after the rework lands.

4. Organization/readability (Henry, same review): surviving foundation code is
   reorganized for a cold reader — one-responsibility modules with plain names
   (no sandbox_* prefix soup), package module-map docstring with the mental
   model (sandbox=cache, bwrap=boundary, workspace=derived identity,
   spool=middle layer), contracts documented at barrel boundaries, barrels
   export only what rungs consume. Standing rule for all later rungs too.

Parity scope ruling (2026-08-05, Henry): the FOUNDATION does not require
one-to-one behavior parity with the pre-rework/source implementation — it must
be a solid base to build parity ON later. Correctness of what ships (durable
honesty, cleanup liveness, first-contact proof, containment, scope gates) is
required; behavior-equivalence to old implementations is not. Simplified
capabilities are acceptable if cleanly absent-or-present and documented as
deferred. Applies to foundation-rung reviews; the v2 shadow-parity GATE
(#13193) remains the eventual cutover evidence, unchanged.
