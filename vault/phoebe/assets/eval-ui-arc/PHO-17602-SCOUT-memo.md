---
type: reference
tags: [phoebe, evals, memo]
created: 2026-09-17
updated: 2026-09-17
---

# PHO-17602 scout memo: better v3 bash-tool evals

## executive answer

Current coverage can measure whether the agent used Bash, how many tool calls it
made, whether commands failed or repeated, how much Bash stdout it produced,
and whether a small set of golden workflows passed. It cannot yet say whether
each command was useful, whether a production run rewrote a pipeline, whether
an artifact mattered, or whether Python versus `wc` was appropriate in context.

The highest-signal first step is a one-off production audit using existing
`agent_items`, `agent_turns`, `admin_tool_audit_events`, and workspace-file
rows. Add no product behavior until that audit labels the common patterns.
Then add eval-engine metrics and failure-only judges. Add Phoebe emission only
for lineage and intent facts that the existing records cannot reconstruct.

Important current-state boundary:

- Phoebe `origin/main` has the local `v3-evals/features/bash` feature with one
  10,000-row shell-only case. Its measurements count Bash calls and failures.
- eval-engine `main` has six `cases/v3_bash` cases. It owns their contracts,
  deterministic graders, judge questions, and four persisted efficiency
  metrics.
- eval-engine's `tool_efficiency.py` measures total calls, Bash stdout bytes,
  repeated identical calls, and error-then-next-call loops. It does not label
  usefulness or context-dependent tool choice.
- Current Phoebe sandboxes use a network-blocked Modal sandbox and bwrap. They
  cap each command at 16,000 command bytes, 30 seconds, and 30,000 output
  bytes. Middleware spills output above the shared 10,000-token inline cap.
  Workspace reads use a 10 MiB cap. These controls bound harmful context
  growth but do not stop an unnecessary `cat` or prove that its output mattered.
- Current `origin/main` persists workspace bytes in
  `agent_run_workspace_files`. The S3-mounted artifact design from PR #15530
  is closed and not merged. Treat its conversation-lifetime cleanup model as a
  proposed target, not current behavior.

## coverage map

Status uses `covered`, `partial`, and `uncovered`. “Covered” means the current
surface records or tests the question directly. It does not mean production
data has already been summarized.

| Henry question | Status | Existing evidence or gap |
| --- | --- | --- |
| How good is the agent at using sandboxes? | partial | The six eval-engine Bash cases test correct Bash use in fixed workflows. Current metrics and production traces lack an outcome-normalized sandbox score. |
| How many Bash commands does one task use? | covered for evals; partial for production | eval-engine `tool_call_count`; Phoebe `v3-evals/features/bash/measurements.py` counts `run_bash`; production `agent_items` can be grouped by run. No production rollup exists. |
| How many are useful or useless? | uncovered | Call count and pass/fail do not establish causal value. A new lineage analysis and judge policy are needed. |
| Across N production runs, how often does the agent rewrite a script or pipeline? | uncovered | Exact call arguments exist in `agent_items.arguments_json`, but no structural rewrite detector or production aggregate exists. |
| After the audit, would preloading scripts improve or regress performance? | uncovered | No controlled preload arm exists. Run this as an eval-only A/B experiment after the audit. |
| How often does it write persisted artifacts? | partial | `agent_run_workspace_files` and `AdminToolAuditEvent` record workspace writes and sizes. Tool-output spill records include tool, call, path, reason, and size in the index or trace. There is no run-level artifact rollup. |
| Are persisted artifacts all important? | uncovered | Current writes do not carry a durable-versus-scratch intent. A path or extension is not a reliable semantic label. |
| How do we distinguish scratchwork from artifacts that must persist? | uncovered | Add explicit artifact intent and downstream-reference fields at the Phoebe write boundaries. |
| Should we define a rigid S3 persistence criterion? | partial | The proposed volume/S3 design separates ephemeral `/workspace` from durable artifact storage. Current main has no S3 sandbox store, so the criterion is not implemented. |
| S3 is cheap now but costly at scale. How should priority reflect that? | covered as a priority decision | Make S3 governance low priority until the audit shows material volume or retention risk. Keep hard per-run and per-organization quotas because cheap storage still creates cleanup and request costs. |
| How should S3 artifacts live and expire? | partial | The closed PR #15530 proposal deletes by conversation lifecycle, sweeps orphan prefixes, and aborts incomplete multipart uploads. It does not expire completed objects by wall clock. This needs adoption work if S3 returns. |
| What Bash commands does the agent use? | partial | eval-engine persists tool names and arguments; Phoebe `agent_items` persists `arguments_json`. No command-family inventory or structural parser exists for production reporting. |
| How many commands are detrimental? | partial | Existing controls expose nonzero exit, timeout, resource-limit, truncation, bytes, and retry signals. No aggregate combines these with task outcome or downstream use. |
| Does the agent avoid `cat` on a 20k-byte file and then reading the output? | partial | The 30,000-byte runner cap and 10,000-token spill cap prevent an unbounded inline response. A 20k-byte output can still be generated and spilled, and a later read can still consume it. The behavior is not blocked or measured as waste today. |
| How many command choices are appropriate for the task? | partial | Current `v3_bash` cases cover direct query versus Bash and a few file-first transforms. They do not cover a broad task/command matrix. |
| Should a Python script count query output, or should it use `wc`? | uncovered | The answer depends on output format, required semantics, available tools, and whether a durable transform is needed. Existing shell-only tests do not grade this choice. |
| How context-dependent are these decisions? | uncovered | The traces contain task, tools, arguments, outputs, and final result, but no explicit decision context or labeled appropriateness outcome. Use a failure-only judge on ambiguous cases. |
| Does the agent have enough context to decide? | partial | The Bash contract names the workspace, output limits, network boundary, and returned artifact paths. Query metadata describes row shape and limits. There is no measurement of whether the relevant schema, size, or user intent was available at the decision point. |

## classification design

Do not force every command into a binary useful/useless label. Use a fact layer,
then a conservative classification layer.

### deterministic fact layer

For each command, record one row keyed by run, scope, turn, tool call, and
sequence. Derive these facts:

- tool name, command AST shape, top-level command family, pipeline count,
  redirect count, script or heredoc presence, and referenced workspace paths;
- exit class: clean, nonzero exit, timeout, memory kill, CPU or PID kill,
  invalid argument, or infrastructure failure;
- stdout and stderr byte counts, truncation flags, delivered response bytes,
  model input tokens, duration, and resource limits;
- exact repeated-call signature within the same scope;
- whether the next same-tool call follows an error;
- artifact paths written, artifact index entries, artifact sizes, and later
  workspace-reference reads;
- task outcome, deterministic check results, and final user-visible artifact
  or answer.

Parse commands with tree-sitter-bash or an equivalent structural parser. Use a
canonical AST representation for command families. Do not use raw command text
as a rewrite or usefulness definition. Existing `features/bash/tool_policy.py`
is a safety policy for a small eval. It is not a usefulness classifier and
should not be extended for this purpose.

### labels

Use these labels, with `unknown` as a valid result:

- `required`: the case contract or a proven artifact/result dependency needs
  the call.
- `useful`: the call produced required evidence, a required transform, or a
  user-visible durable artifact, and it did not incur a known waste signal.
- `unused_candidate`: the call completed but has no observed downstream read,
  final-answer dependency, required artifact, or task-state effect. This is a
  candidate, not a proven useless call.
- `redundant`: a repeated identical call, or a structurally equivalent call,
  whose earlier successful result remained available and was not invalidated.
- `detrimental`: a failed or timed-out call, a resource-limit hit, an avoidable
  output truncation, a repeated error retry, or a large result that entered
  context without a downstream need.
- `inappropriate_tool`: the selected tool conflicts with a case contract or
  with a clear structured-data/local-file boundary. Use `unknown` when the
  task context does not determine the better tool.

The first four labels are deterministic only when the trace has dependency
evidence. Otherwise report `candidate` plus the reason. A command can be both
`useful` and `detrimental`: for example, a correct but wasteful full-file dump.
Keep task correctness separate from efficiency. A useful label must never turn
a failed task into a pass.

### exact source coverage

| Signal | Derivable today | New Phoebe-side emission needed |
| --- | --- | --- |
| Total calls by scope and tool | eval-engine `tool_efficiency.py`; eval-engine `eval_tool_call`; production `agent_items` | no |
| Command arguments and exact repeated calls | eval-engine persisted call args; production `agent_items.arguments_json` | no for analysis; a privacy-safe export may be needed |
| Exit code, timeout, truncation, resource limit | eval-engine tool results; production `agent_items.output_json` and sandbox trace fields | no for basic counts |
| Bash stdout bytes | eval-engine `tool_efficiency.py`; production output rows and audit sizes where present | no for basic counts |
| Delivered tool bytes and model input tokens | Phoebe local `v3-evals/src/extract/measurements.py` provider snapshots; production `agent_turns.input_items` and usage when retained | no for retained turns; verify retention first |
| Exact repeated-identical calls and immediate error retries | eval-engine `tool_efficiency.py`; reconstructable from production call/result rows | no |
| Command families and structural similarity | command text is present in eval and production call args | no storage needed; add an eval-engine parser |
| Tool-output spill path, reason, size, row count | local middleware index and traces; production workspace rows plus audit/index data where retained | add a stable export if current production trace retention omits the index |
| Bash-created file read/write lineage | current workspace file audit has path and size; current sandbox traces have call id in the event stream | yes: record producer/consumer call ids, read bytes, write intent, and read-after-write links |
| Output used by a later tool | provider input snapshots show which call result was delivered to later turns; workspace refs show some reads | yes for direct file lineage from Bash and for final-answer dependency |
| Scratch versus durable intent | not present as a semantic field | yes: explicit `scratch`, `spill`, `register`, `presentation`, or `downloadable` intent |
| User-visible persistence and deletion | render or attachment records cover some surfaces; workspace deletion is not a unified lifecycle fact | yes: stable artifact id, referenced-by conversation, retention state, and deletion event |
| Context-dependent tool choice | only case contracts and existing judge questions | no new runtime field; add labeled eval cases and failure-only judge coverage |

### failure-only judge

Keep deterministic correctness and efficiency metrics authoritative. Invoke a
judge only for a failed or ambiguous case, and ask it to decide among
`useful`, `unused_candidate`, `detrimental`, `appropriate`, `inappropriate`,
and `unknown` from a small evidence bundle. Require citations to call ids,
results, artifact refs, and the case contract. Do not let the judge override a
deterministic task verdict. Promote a judge pattern into a deterministic rule
only after repeated human validation.

## rewrite-frequency audit

### data source and sample

Use a one-off read-only production audit. Join v3 customer runs to their
`agent_items` and `agent_turns`. Keep root and child scopes separate. Filter by
agent version, model, surface, and a fixed date window. Exclude internal,
admin-probe, and test organizations. Export aggregates and structural forms,
not user messages, names, phone numbers, or raw file contents.

Start with a stratified sample of 500 to 1,000 completed runs. Stratify by
workflow class, run length, model/version, and whether Bash appeared. Report
the denominator and missing-trace rate. This is an audit, not a recurring eval.

### structural rewrite definition

Build a normalized representation for each `run_bash` command:

1. Parse shell syntax into an AST.
2. Preserve command nodes, pipeline and control-flow shape, redirects,
   wrapper structure, and script boundaries.
3. Replace literals, UUIDs, dates, counts, and workspace tokens with typed
   placeholders. Preserve the output target class and the read/write role.
4. Parse embedded heredoc scripts with the matching language parser when
   available. Otherwise retain a typed opaque-script node.
5. Hash the canonical tree and retain a compact tree shape for similarity.

Count a rewrite when a later Bash call in the same run has the same task-local
pipeline family and a high structural similarity to an earlier call, but has
changed executable structure, filters, quoting, or output behavior. Count the
first version as one attempt and later revisions separately. Distinguish:

- exact repeat after success;
- retry after a failed command;
- repair with a changed AST;
- independent new pipeline;
- same pipeline after a new user turn.

Use tree edit distance or a weighted tree similarity. Do not compare raw
strings. Calibrate the similarity threshold on a hand-labeled sample, then
report sensitivity around that threshold. Never claim a rewrite from command
text alone when the trace lacks the task boundary or result.

Report per run and by workflow:

- Bash calls per run: median, p75, p90;
- runs with any rewrite;
- rewrites per Bash-using run and per rewritten run;
- repair rate after failure;
- exact-repeat rate;
- output bytes, input tokens, latency, and task pass rate for rewritten versus
  one-pass runs;
- missing or ambiguous structural parses.

### preload experiment

Run this only in eval-engine against isolated v3 cases. Do not add a
production feature flag for the first experiment. Use a test-only sandbox image
or environment variant with a recorded image fingerprint:

- control: current image and current workspace;
- treatment: a read-only directory of generalized, audited helpers selected
  from the rewrite audit, with no production data and no case-specific answer
  script;
- same model, prompts, fixtures, tool limits, and randomized arm order;
- at least three trials per case for screening, then a pre-registered larger
  matrix for a decision;
- record the arm in the eval metadata so results cannot be mixed.

Success requires all of these:

- no regression in deterministic task pass rate, safety, or artifact validity;
- fewer rewrite events and failed Bash calls;
- lower or non-inferior tool calls, delivered bytes, input tokens, latency,
  and cost;
- no increase in unreferenced or durable junk artifacts;
- no reliance on a helper that hides a missing capability or leaks data.

Set the non-inferiority margin and minimum call reduction before the larger
matrix. A reasonable screening target is a 10% reduction in calls or rewrites
with no more than a 2 percentage-point correctness loss, but Henry should lock
these thresholds. Sandbox LOAD testing remains out of scope.

## artifact-persistence audit

### measurements

For each run, aggregate:

- workspace writes, unique paths, bytes, file type, producer tool, and write
  reason;
- tool-output spills versus Bash-created files versus render or attachment
  outputs;
- artifacts read by the agent, rendered to the UI, downloaded, attached, or
  referenced in a final reply;
- bytes and objects remaining after the run, after conversation deletion, and
  after orphan cleanup;
- failed writes, torn or unindexed files, stale references, and cleanup age;
- cost proxies: object count, PUT/GET count, bytes stored, and cleanup calls.

Use `agent_run_workspace_files`, `AdminToolAuditEvent`, agent items, and
provider input snapshots for the first pass. Keep the output aggregate-only.
Include the missing-data rate because current records do not prove that every
workspace read or reference was observed.

### scratch versus durable

Add an explicit write intent at the Phoebe boundary:

- `scratch`: intermediate files under ephemeral workspace storage;
- `spill`: a bounded copy of a tool result needed for the current run;
- `register`: a file the agent intentionally writes for later tool use or
  recovery;
- `presentation`: a validated UI or downloadable artifact;
- `attachment`: a user-visible file sent or retained by the product.

Treat an object as durable only when it has a stable downstream reference, is
user-visible, is required for a later run or reopened conversation, or the
agent explicitly requests a register or attachment. Treat intermediate Bash
files as scratch by default. Do not infer intent from `.jsonl`, file names, or
directory names. The write event should carry the intent, producer call id,
conversation id, run id, size, and expected expiry.

The current system cannot apply this distinction reliably because all workspace
file writes enter the Postgres persistence path. The first instrumentation
change should make the distinction observable. It should not change retention.

### S3 recommendation

Keep this low priority until the audit shows scale pressure. If the S3 design
returns, use the following rule:

- scratch stays in ephemeral `/workspace`;
- spill and durable objects use an organization/run prefix and an index entry;
- an object is addressable only after its index records the exact size;
- delete completed objects when their conversation is deleted or archived,
  after the database transaction commits;
- sweep orphan run prefixes whose conversation no longer exists;
- abort incomplete multipart uploads after one day;
- do not use a blanket completed-object TTL while conversations remain
  reopenable;
- consider a cold tier only after measured storage and request cost justify
  the extra state machine.

This follows the design in closed PR #15530. It is not a claim about current
`origin/main`.

## proposed work breakdown

Ordered by expected signal per effort:

| Order | Lane | Home | Deliverable | One-off or recurring |
| ---: | --- | --- | --- | --- |
| 1 | Production command and artifact census | Phoebe read-only audit, with an aggregate report consumed by eval-engine | 500–1,000 run sample; Bash counts, structural rewrite rates, output waste, artifact volume, missing-data rate | one-off |
| 2 | Offline command fact extractor | eval-engine | Parse persisted v3 Bash calls and results into command families, exit classes, repeat/retry facts, output-size bands, and candidate labels. Keep Phoebe out of platform code. | recurring metric code after validation |
| 3 | Bash golden-case expansion | eval-engine `cases/v3_bash` | Add paired cases for large-file bounded reading, structured query versus Bash, `wc` versus semantic parsing, repair after failure, and durable register versus scratch output. Use deterministic contracts. | recurring eval |
| 4 | Failure-only efficiency judge | eval-engine `v3_bash` criteria and judge panel | Add questions for command usefulness, harmful output delivery, and context-dependent tool choice. Run only on failures or ambiguous cases. | recurring diagnostic |
| 5 | Phoebe lineage and intent emission | Phoebe `agent_sandbox` and v3 middleware | Emit producer/consumer call ids, Bash file read/write facts, artifact intent, stable artifact id, and deletion state. Keep payloads redacted or aggregate-safe. | recurring instrumentation |
| 6 | Preload A/B | eval-engine orchestration plus Phoebe sandbox image/config | Run the control/treatment experiment after lanes 1–4. Use a test-only arm and record image fingerprints. | one-off experiment |
| 7 | Artifact lifecycle governance | Phoebe storage and worker cleanup, then eval-engine reporting | Adopt S3 only if the census supports it. Add quotas, index integrity, conversation deletion, orphan sweep, and cost dashboards. | recurring operations |

The current Phoebe `v3-evals/features/bash` feature should remain a local
delivery and measurement harness. Do not duplicate the eval-engine-owned
`cases/v3_bash` suite there. Its existing delivered-byte and input-token
measurements are useful for validating Phoebe emission and provider delivery.

## open questions for Henry

1. What production window and population should the first audit cover? The
   recommendation assumes customer-facing v3 runs only, excluding admin,
   internal, test, and automation runs.
2. Should a “durable artifact” mean only user-visible or reopened-conversation
   outputs, or also every spilled tool result that the current turn can read?
   This changes both the intent enum and storage totals.
3. Which success threshold should decide preload adoption: minimum reduction
   in rewrites/calls, non-inferiority margin for correctness, and maximum cost
   regression?
4. Is it acceptable to retain command text in the audit workspace, or must
   Phoebe emit only normalized AST shapes and aggregate bytes? This changes
   whether lane 1 can run entirely from existing rows.
5. If S3 returns, must artifacts survive conversation archival for a fixed
   compliance period? This changes the conversation-lifecycle deletion rule.

## source map

- Phoebe `v3-evals/features/bash/measurements.py`, `cases.py`,
  `tool_policy.py`, and `README.md`.
- Phoebe `libraries/python/phoebe_v3_agent/tools/bash/tool.py`.
- Phoebe `libraries/python/phoebe_v3_agent/middleware/bash_output.py`,
  `workspace.py`, `workspace_refs.py`, and `output_contract.py`.
- Phoebe `libraries/python/agent_sandbox/settings.py`, `confinement.py`,
  `sandbox.py`, `audit.py`, `telemetry.py`, and `workspace.py`.
- Phoebe `database/schema.sql`: `agent_run_workspace_files`.
- eval-engine `backend/eval_engine/tool_efficiency.py`,
  `backend/eval_engine/runner/ingest.py`,
  `backend/eval_engine/regression/engine.py`,
  `backend/eval_engine/judges/criteria.py`, and `cases/v3_bash/README.md`.
- Linear issues PHO-17482, PHO-17446, and PHO-17602.
- Closed Phoebe PR #15530: proposed S3-mounted sandbox artifacts.
