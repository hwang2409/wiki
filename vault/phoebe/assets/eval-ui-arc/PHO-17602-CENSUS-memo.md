---
type: reference
tags: [phoebe, evals, memo]
created: 2026-09-17
updated: 2026-09-17
---

# PHO-17602 production v3 Bash and artifact census

## executive summary

v3 has no customer traffic. This census covers all identifiable internal
production v3 runs in the available window.

The window contains 51 runs and 59 `run_bash` calls. Twelve runs used Bash.
The trace shows four rewritten runs and 35 changed-AST revision events. It
shows no exact repeated command. Python heredocs dominate the sample.

The strongest golden-case targets are iterative Python analysis, repair after a
failed command, large-result spill handling, and durable report packaging.

## methodology

The audit used `bin/query-db production` only. The connection used the
analytics read replica. Replica lag was 20.83 ms at the first check.

The fixed window was 2026-08-05 through 2026-09-16 UTC. The upper bound was
2026-09-17 00:00 UTC. The window starts at the first observed v3 run and ends
before the current day. The population was smaller than the requested sample,
so this is a full census, not a sample.

The population predicate selected runs with either:

- `run_metadata.agent_v3 = true`; or
- a present `run_metadata.agent_v3_surface`.

The scope correction removed customer-only exclusions. The audit retained all
internal v3 runs, including the simulator stratum.

The audit excluded only clear test markers:

| exclusion check | rows excluded |
| --- | ---: |
| `v3_agent_test_run = true` | 0 |
| `v3_raw_mode = true` | 0 |
| comparison role `normal` or `raw` | 0 |
| `is_admin_probe_fixture = true` | 0 |
| non-general run type | 0 |

The simulator was identified by its internal name and reported separately.
No customer-facing v3 traffic was present by the supplied scope correction.

The source tables were `phoebe_agent_runs`, `agent_turns`, `agent_items`,
`phoebe_agent_run_events`, `admin_tool_audit_events`,
`agent_run_workspace_files`, `agent_render_artifacts`,
`admin_artifacts`, and `phoebe_agent_run_chat_attachments`.

The parser was a temporary tree-sitter-bash grammar under `/tmp`. Embedded
Python heredocs used Python's AST parser. No parser files entered the repo.

The lag check was:

```sql
SELECT now() - pg_last_xact_replay_timestamp() AS replica_lag;
```

## population and trace completeness

| stratum | runs | Bash runs | Bash calls | completion events |
| --- | ---: | ---: | ---: | ---: |
| other internal and QA | 39 | 9 | 55 | 32 |
| `phoebe-sim-daily` | 12 | 3 | 4 | 11 |
| total | 51 | 12 | 59 | 43 |

All 51 runs had the general run type. The legacy `agent_v3 = true` marker had
no surface field and was treated as the legacy sidebar workflow. The explicit
`internal_readonly` surface had eight runs.

| workflow stratum | runs | Bash runs | Bash calls |
| --- | ---: | ---: | ---: |
| legacy sidebar marker | 43 | 8 | 32 |
| internal read-only | 8 | 4 | 27 |

Fifty runs had at least one durable agent turn. One run had no turn row.
Forty-three runs had a `run_completed` event. One simulator run had only an
interrupt event. Seven runs had no terminal event. No `run_failed` event was
observed.

All 59 Bash calls had an observed model. The call-level model split was:

| model | Bash calls | Bash runs |
| --- | ---: | ---: |
| `gpt-5.6-terra` | 55 | 9 |
| `gpt-5.6-sol` | 2 | 1 |
| `gpt-5.6-luna` | 1 | 1 |
| `claude-opus-4-8` | 1 | 1 |

No separate production agent-version field existed beyond the v3 routing
markers. Seven runs lacked `duration_ms`. One run lacked aggregate input
tokens. Three Bash output rows lacked `text_content`, but each had structured
`output_json`; no Bash output row remained unusable after that fallback.

## command census

Across all 51 runs, Bash-call quantiles were p50 0, p75 0, and p90 1.
Among the 12 Bash-using runs, quantiles were p50 1, p75 4, and p90 14.

| Bash-using stratum | runs | p50 calls | p75 calls | p90 calls |
| --- | ---: | ---: | ---: | ---: |
| all | 12 | 1 | 4 | 14 |
| other internal and QA | 9 | 1 | 10 | 16 |
| simulator | 3 | 1 | 1.5 | 1.8 |
| legacy sidebar marker | 8 | 1 | 1.25 | 8.6 |
| internal read-only | 4 | 6 | 11 | 12.8 |

Top-level command families came from structural parser command nodes. Counts
are calls whose first top-level command had that family.

| family | calls |
| --- | ---: |
| `python3` | 38 |
| `cat` | 9 |
| `date` | 4 |
| `mkdir` | 3 |
| `cd` | 2 |
| `python` | 1 |
| `file` | 1 |
| `grep` | 1 |

The most common structural forms were:

| normalized form | calls |
| --- | ---: |
| one `python3` command with a heredoc | 35 |
| one `date` command | 4 |
| `cat` then `python3`, with a heredoc | 4 |
| `cat` then `python3` then `file` then `ls`, with a heredoc | 2 |
| all other forms | 14 |

Fifty-two calls had a heredoc. Fifty-two had a shell redirect. One had a shell
pipeline. No shell command substitution was observed. Shell parsing succeeded
for 59 of 59 calls. Python AST parsing succeeded for 51 of 52 heredocs; one
embedded script remains an opaque structural node.

## waste signals

| signal | calls | rate |
| --- | ---: | ---: |
| clean exit | 54 | 91.5% |
| nonzero exit | 5 | 8.5% |
| timeout | 0 | 0% |
| resource-limit flag | 0 | 0% |
| stdout truncation | 3 | 5.1% |
| exact repeated raw command | 0 | 0% |
| exact repeated normalized AST | 0 | 0% |

Visible stdout totaled 65,277 bytes. Output bands were 45 calls below 1 KB
and 14 calls from 1 KB through 10 KB. No visible stdout reached the 30 KB
runner cap. Thirteen calls spilled context output. Those spills occurred in
three runs, with 2, 3, and 8 spill calls. The visible size is not the stored
spill size.

There were 45 same-family candidate pairs. At the selected similarity cutoff,
35 pairs were high-similarity revisions and 10 were independent pipelines.
The 35 revisions were:

| classification | pairs |
| --- | ---: |
| changed AST after a successful prior call | 32 |
| changed AST after a failed prior call | 3 |
| exact repeat after success | 0 |
| exact repeat after failure | 0 |
| same pipeline after a new user turn | 0 |
| independent new pipeline | 10 |

Four Bash-using runs contained a changed-AST rewrite. All four were in the
other-internal stratum. The mean was 8.75 revision events per rewritten run.
No exact repeat appeared in raw command text or normalized AST form.

Five calls had nonzero exits. Three were followed by a high-similarity,
changed-AST repair. One was followed by a structurally independent pipeline.
One failed call had no later Bash call. This supports separate golden cases
for repair and for choosing a new pipeline after failure.

## rewrite calibration

The similarity score used weighted sequence similarity over normalized shell
AST nodes, embedded Python AST nodes, command families, pipeline count, and
redirect count. Literals became typed placeholders. Workspace tokens and
literal values did not affect the normalized form.

I hand-labeled 10 pairs from the high, middle, and borderline score ranges.
The selected cutoff was 0.72. Counts were stable from 0.70 through 0.80:
35 pairs matched. A 0.68 cutoff added one borderline pair. A 0.85 cutoff
kept only 8 pairs. This calibration is suitable for case discovery, not for a
production classifier.

The trace did not provide a direct call-to-user-turn foreign key. The
new-user-turn label therefore used event timestamps. Zero pairs crossed an
observed `user_message` event. Treat that result as provisional.

## rewritten versus one-pass runs

One-pass means exactly one Bash call. Rewritten means at least one changed-AST
revision under the selected cutoff. Completion is an event proxy, not a task
pass verdict.

| group | runs | p50 Bash calls | p50 stdout bytes | p50 input tokens | p50 latency | completion events |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| rewritten | 4 | 12 | 10,881 | 1.30B | 5.49 h | 4/4 |
| one-pass | 7 | 1 | 24 | 462M | 10.7 min | 6/7 |

The groups are small and confounded by workflow. The current rows do not
contain a deterministic task outcome or pass/fail score. Do not treat the
completion comparison as evidence that rewrites improve correctness.

## artifact census

`agent_run_workspace_files` had zero rows for this population. It therefore
does not describe current retained workspace volume for these runs.

The admin tool audit recorded 113 successful `agent_sandbox.file_write` events
across 16 runs. These events totaled 13,279,704 bytes, with 39 distinct paths
and 76 distinct run-path pairs.

| audited path class | writes | bytes |
| --- | ---: | ---: |
| tool-output spill paths | 97 | 11,845,126 |
| other workspace paths | 16 | 1,434,578 |
| total | 113 | 13,279,704 |

The path extension split was 96 `.jsonl` writes and 17 `.json` writes. The
audit event exposed only `filepath` and `size_bytes` for these writes. It had
no producer call id or intent field.

The render-artifact table had zero rows. The admin-artifact table had zero
rows. Chat attachments had seven rows in one other-internal run: one inbound
inline object, one outbound inline object, and five outbound attachments.
They totaled 995,428 bytes. All seven were storage-backed.

Structural command inspection found:

- 35 calls that referenced a tool-output path class;
- 6 calls that referenced an artifacts path class;
- 5 calls with artifact-path write operations;
- 1 call that referenced a user-upload path class.

The artifact path forms included report-like PDF and HTML outputs, a ZIP
package, PNG image inputs, and Python helper files. These are structural
references only. The database has no matching render or admin artifact rows.

No later Bash call read the exact spill path emitted by an earlier Bash call.
The 35 tool-output references could not be linked to a producing call because
the current records lack producer and consumer linkage.

## concrete golden-case targets

The following cases are justified by repeated structural patterns. Each case
should use synthetic fixtures and assert both task correctness and efficiency.

1. **iterative Python analysis**

   Use a JSONL result set. Require bounded filtering, aggregation, and a final
   report. Exercise repeated Python-heredoc revisions with changed filters or
   output fields. Assert that the agent does not repeat a successful transform
   without a changed need.

2. **failed Python transform repair**

   Make a Python-heredoc transform fail. Require the next call to change the
   AST and fix the specific failure. Assert no exact retry and no unrelated
   pipeline detour.

3. **failed packaging repair**

   Require report packaging after a generated HTML artifact. Make a shell
   packaging shape fail. Accept a changed Python or archive strategy only when
   it verifies the resulting artifact.

4. **large-result spill and bounded read**

   Return a result above the inline context threshold. Require a spill-backed
   reference and a later bounded read. Assert no full-file dump and no
   unreferenced large output.

5. **structured query versus Bash**

   Provide a task where the query tool can return a bounded structured result.
   Compare it with a Bash `cat` plus Python transform. Grade output bytes,
   call count, and whether the chosen tool matches the data shape.

6. **counting versus semantic parsing**

   Pair a row-count task with a semantic field-validation task. The first case
   should favor a count operation. The second should require parsing. Keep
   the contract explicit so a judge does not guess intent.

7. **durable report versus scratch file**

   Require a user-visible PDF or HTML report plus a ZIP package. Mark helper
   files scratch. Assert stable artifact references, attachment validity, and
   no durable registration for intermediate JSONL or Python files.

8. **image inspection**

   Provide a synthetic uploaded image. Require capability discovery before
   inspection. Assert that the agent reports unavailable readers clearly and
   does not create a misleading artifact.

## measurement gaps

- No customer-facing v3 denominator exists. This report cannot estimate
  customer behavior.
- No deterministic task outcome or pass score exists in these rows.
- `phoebe_agent_runs.status` remained `idle`; event types supplied the more
  useful completion proxy.
- Seven run durations and one aggregate input-token value were missing.
- One embedded heredoc script could not be parsed as Python.
- Three output rows required the structured JSON fallback because text content
  was absent.
- The records lack a direct user-turn boundary on each Bash call.
- `agent_run_workspace_files` was empty despite file-write audit events.
- File-write events lack producer call ids, consumer call ids, write intent,
  stable artifact ids, retention state, and deletion state.
- Spill references lack a durable producing-call link. Direct downstream use
  is therefore under-counted or unknown.
- Render, admin-artifact, download, and attachment lineage is incomplete.
- The artifact audit exposes path and size but does not prove scratch versus
  durable intent.
- The 51-run population cannot support stable model or workflow comparisons.

## what this implies for lanes 2-6

### lane 2: offline command fact extractor

Proceed. Existing rows support command counts, top-level families, exit class,
heredoc detection, output bands, spill flags, and changed-AST candidates.
The extractor must parse embedded scripts and retain `unknown` for opaque
scripts. It must not turn similarity into a usefulness verdict.

### lane 3: Bash golden cases

Prioritize the eight case targets above. The strongest evidence is the
Python-heredoc family, 35 revision events, 5 failures, 13 spills, and report
packaging references. Use synthetic JSONL and artifact fixtures.

### lane 4: failure-only efficiency judge

Add judge questions only for ambiguous usefulness and tool-choice cases.
Deterministic graders can own nonzero exits, output limits, exact repeats,
artifact validity, and repair shape. The judge must cite structural call ids
and must not override task correctness.

### lane 5: Phoebe lineage and intent emission

The census directly justifies producer and consumer call ids, Bash read/write
links, explicit artifact intent, stable artifact ids, and deletion state.
These fields are needed before the system can measure durable value or prove
that a spill was consumed.

### lane 6: preload experiment

Do not use this production census to decide preload adoption. Only 12 runs used
Bash, and four internal runs account for most revisions. Run the controlled
experiment after lanes 2 through 4 define cases, graders, and thresholds.

## provenance and privacy

This memo contains aggregate counts, normalized command families, structural
forms, byte bands, and model labels. It contains no raw command text, user
messages, names, phone numbers, or file contents.
