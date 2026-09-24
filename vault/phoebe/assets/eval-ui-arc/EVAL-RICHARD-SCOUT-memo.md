---
type: reference
tags: [phoebe, evals, memo]
created: 2026-09-17
updated: 2026-09-24
---

# richard huang eval work: research memo

## current-state correction (2026-09-24)

The sections below record the 2026-09-17 PR state. At Phoebe
`origin/main` `3fdcfb2275`, the v3 harness has changed:

- `v3-evals/engine/grading/jev.py` adds an optional Jev backend that can
  replace deterministic check verdicts. The tool-using diagnostic agent in
  `engine/grading/diagnostic.py` still runs only after a failed check and
  cannot change the verdict. The v3-evals skill's "no model-graded checks"
  rule is now stale.
- The golden index contains eight cases. All eight have
  `awaiting_human_validation`; `golden_set/cases.py` checks status equality
  with the owner, not human approval.
- `.github/workflows/v3-golden-set.yaml` and
  `scripts/daytona/v3_golden_set.sh` are absent from this main commit.
  Do not infer the present hosted launch path from the older PR section.

## scope and state on 2026-09-17

this memo covers richard huang's eval PRs #17024, #17174, and open #17266.
it also traces the v3 harness, the older `evals/` platform, and the planned
production result path.

pr #17024 and pr #17174 are merged into `origin/main`. pr #17266 is open.
its final head is `ef440085ff99e60bb5bf0cf57dc222c3bbd8857c`.

the most important result is this:

- #17024 built the first v3 eval harness and two feature families.
- #17174 made the harness file-driven and deterministic.
- #17266 adds a hosted Daytona job and new production result tables.
- #17266's final golden index is empty, so the hosted job currently runs no
  regression cases and exits successfully.
- v3 results do not appear in the existing voice Eval Explorer. that explorer
  still reads `app.eval_runs` and `app.eval_case_results`.

## pr #17024: build the first v3 eval harness

pr #17024, “Add V3 feature evals with Bash performance checks,” was merged on
2026-09-14. it added 99 files and about 13,400 lines. the implementation
introduced a self-contained v3 harness under `v3-evals/`.

the harness gave engineers isolated application state for each trial. it
created a temporary organization, enabled v3, seeded the case data, ran a
model, captured traces, extracted evidence, graded the result, and deleted the
organization. the initial source placed these concerns under `src/infra`,
`src/v3_agent`, and `src/grading`. #17174 later reorganized them by stage.

the initial feature contract supported an `action()` hook and a
`collect_evidence()` hook. checks could be deterministic or model-based. the
initial Bash policy used direct Python and `jq` checks for obvious commands,
then used a model audit for other shell commands. those model checks added
cost and another source of verdict variation.

the two initial feature families were:

- `outreach`: seed 50 synthetic caregivers, a client, a shift, a playbook,
  and availability. the case supplied an answer key with 10 eligible
  caregivers. it checked automatic selection, persisted selected records, and
  no outbound contact. the case source was
  `v3-evals/features/outreach/cases.py` in the #17024 tree.
- `bash`: attach a 10,000-row JSONL file. the task required shell-only
  analysis and an exact JSON result. it bounded Bash calls, failed calls,
  delivered response bytes, and model input tokens. the initial policy was
  `v3-evals/features/bash/tool_policy.py` in the #17024 tree.

the PR body reported live three-trial runs across Luna, Terra, and Sol. it
also reported cleanup and cost checks. those are PR claims, not a new run in
this research pass.

the PR added important supporting paths. admin trace reads included provider
requests. agent events retained input snapshots. attachment validation kept
file evidence tied to the workspace. organization deletion verified ownership
and the final 404. these changes made later evidence and cleanup checks
possible.

## pr #17174: make verdicts deterministic and cases data-driven

pr #17174, “PHO-17564: V3 general-chat eval, one feature shape, deterministic
review, src by stage,” was merged on 2026-09-16. it added two cases based on a
coordinator bug reported on 2026-09-15.

the cases are:

- `show-my-outreaches`: create three pending manual callouts for one client
  and a decoy client. the answer must name the expected client and date.
- `schedule-today`: create two shifts today and one tomorrow. the answer must
  contain today's clients and must not include the tomorrow decoy.

the case code is in
`v3-evals/features/general_chat/cases.py:166-449`. the data and rubric are
keyed by case id in
`v3-evals/features/general_chat/dataset.json:2-54` and
`v3-evals/features/general_chat/rubric.json:2-17`.

the PR changed the harness shape in several load-bearing ways:

- every feature now has `__init__.py`, `dataset.json`, `rubric.json`,
  `cases.py`, and `cases_test.py`.
- cases implement `setup`, `prompt`, `extract`, and deterministic `checks`.
- discovery loads data as package resources. it validates matching case ids,
  validation status, hashes, and exact feature ownership.
- `src/` is organized as `setup`, `execute`, `extract`, `grader`, and
  `review`.
- staging runtime support was removed. the current v3 harness runs against a
  local API.
- model checks and structured verdict transport were removed.

the file contract is implemented by `CaseFiles` and `FeatureFiles` in
`v3-evals/src/cases.py:31-69`. an `EvalCase` owns the four hooks and the
checks in `v3-evals/src/cases.py:127-182`. discovery is in
`v3-evals/src/discovery.py:14-97`.

the Bash policy is now deterministic. it parses shell syntax with tree-sitter
and applies an allowlist. unknown or unclassified commands fail. wrappers and
heredocs are parsed. Bash call counts and failure counts are measurements in
`v3-evals/features/bash/measurements.py`.

the general-chat checks do not trust answer prose alone. they inspect
persisted shifts and outreaches. the plan notes that live answers can include
`[[callout:<id>]]` badges. a badge receives credit only when it points to a
persisted record. see `docs/notes/plans/pho_17564_general_chat_eval.md:48-53`.

## v3 architecture on origin/main

### case and feature definition

`EvalCase` is the main extension point. it provides:

```python
class EvalCase:
    async def setup(self, ctx): ...
    async def prompt(self, ctx): ...
    async def extract(self, ctx, observation): ...
    checks: tuple[Check, ...]
```

the actual contract also permits dormant conversations and diagnostic
evidence. each `Check` has a name, description, async evaluator, and timeout.
each `CheckOutcome` has a boolean, JSON details, and evidence references.
these types are in `v3-evals/src/cases.py:86-182`.

new features need no central registry edit. discovery scans directories below
`v3-evals/features` in `v3-evals/src/discovery.py:14-34`. it then imports
`features.<feature>.cases`, calls `load_cases(files)`, and validates exact
feature and case ownership in `v3-evals/src/discovery.py:36-97`.

the README documents the authoring shape in
`v3-evals/README.md:53-78`. the normal feature folder is the smallest unit
for a new eval family:

```text
v3-evals/features/<feature>/
  __init__.py
  dataset.json
  rubric.json
  cases.py
  cases_test.py
```

### local execution

`v3-evals/src/run.py:57-284` runs repeated trials. it bounds concurrency,
checkpoints trial results, evaluates deterministic checks, diagnoses failures,
and cleans up. the CLI defaults to five trials and concurrency three at
`v3-evals/src/run.py:342-445`.

`v3-evals/src/execute/hosted.py:23-228` is the trial executor. despite the
name, this is the shared hosted-batch executor used by the local v3 CLI. it
uses the local API client to create and delete isolated organizations. it
posts the prompt, polls root and child traces, uploads attachments, and saves
transcripts and evidence.

`v3-evals/src/setup/local_api.py:45-201` selects the API port from `.api-port`
or 8001. `v3-evals/src/setup/preflight.py:12-33` checks the OpenAI key and
authenticated user before it creates an organization.

the executor checks trace completion in
`v3-evals/src/execute/traces.py:37-55`. a trace is complete only when it has
no pending work and has either an assistant item or an explicitly dormant
conversation.

### evidence and measurements

`v3-evals/src/extract/evidence.py:33-159` normalizes native trace events,
deduplicates billed turns, records provider tool calls, and saves final reply
text, prompts, tools, skills, measurements, and transcript data. it retains
the source of tool evidence.

`v3-evals/src/extract/measurements.py:8-176` records token usage, cache usage,
provider costs, tool-response bytes, and missing-data gaps. incomplete values
remain incomplete. the code does not silently turn missing usage into zero.

outreach-specific persisted records are extracted before cleanup by
`v3-evals/src/extract/evidence.py:162-186`.

### review and diagnosis

`v3-evals/src/review/checks.py:43-108` is the verdict boundary. missing or
incomplete evidence creates an infrastructure error and leaves checks as
`not_run`. otherwise each deterministic check runs against a deep copy. a
trial passes only when all checks pass.

`v3-evals/src/review/checks.py:156-201` keeps invalid repetitions visible. it
computes `pass_at_k` and `pass_power_k` over valid trials and requires the
requested number of passing trials for acceptance.

`v3-evals/src/review/results.py:9-183` writes final results and trial records
atomically. trial rows link to transcript and grader artifacts.

diagnosis cannot change a verdict. `v3-evals/src/grader/feedback.py:86-124`
adds requirements, evidence, hypotheses, and suggested fixes only for failed
trials. the diagnostic prompt in
`v3-evals/src/grader/diagnostic.py:31-165` requires a chronological chain from
the requirement to delivered evidence, agent decision, tool action, and
persisted result. it also requires provenance and uncertainty.

## pr #17266: hosted golden set and production landing

pr #17266 is open. its body only references PHO-17563, but its six commits
add a large hosted reporting path.

### Daytona execution

`.github/workflows/v3-golden-set.yaml:1-224` runs after a production deploy,
on a daily schedule, or by manual dispatch. deploy and daily runs require the
`V3_GOLDEN_SET_ENABLED` repository variable. manual runs do not.

the workflow runs a fresh private stack at a target commit through
`scripts/daytona_preview.sh`. the job definition is in
`scripts/daytona/jobs/v3_golden_set.toml:1-52`. it requests a local app with a
worker, a 90-minute timeout, three-way concurrency, and the OpenAI and Google
secrets.

the wrapper is `scripts/daytona/v3_golden_set.sh:1-78`. it runs the v3
command one feature or case at a time, then runs `v3-evals:report`. it stores
reports and artifacts, and posts failures to Slack `#deploy`.

the wrapper has an important empty-index branch:

```bash
if [[ ! -s "$GOLDEN_INDEX" ]]; then
  echo "The golden set has no cases; nothing to run."
  exit 0
fi
```

the final #17266 tree has `v3-evals/features/golden_set/dataset.json` and
`rubric.json` set to `{}`. the discovery layer therefore rejects
`--feature golden_set` as empty. the wrapper sees the empty index and exits
zero. this is the current hosted behavior.

### golden-set design

`v3-evals/features/golden_set/cases.py:1-105` wraps cases from ordinary
feature folders. `GoldenCase` preserves the source setup, prompt, extraction,
checks, timezone, and feature metadata, while marking the case as golden.
the index is intended to select cases without copying their implementation.

the golden README says the set should contain observed production failures,
use persisted state and tool calls, and use deterministic checks. it rejects
phrase matching, prose-quality checks, disconnected metric budgets, and
unvalidated synthetic keys. see
`v3-evals/features/golden_set/README.md:1-70`.

the PR's intermediate commits moved outreach and Bash out of the golden
folder, added a production-derived tool-result-honesty case, added playbook
and hard-filter cases, and then removed those cases from the final golden
index. the final tree keeps the ordinary feature folders but leaves the
golden index empty. this makes the PR's next step promotion and validation,
not more runner plumbing.

### result payload and database

`v3-evals/report.py:56-345` reads result folders and builds a production
payload. it combines `trials.jsonl`, stats, failed-grader summaries, costs,
models, hashes, validation status, and git data. it does not read or mutate
the harness state. raw transcripts and query responses remain artifacts.

the reporter accepts deploy, scheduled, manual, or local triggers. when
`EVAL_INGEST_URL` and its token exist, it posts to
`/webhooks/v3-eval-runs`. without those variables it only builds a report.
the reporter CLI and its defaults are in `v3-evals/report.py:357-468`.

the migration is
`database/migrations/20260916204142_add_v3_eval_run_and_result_tables.sql:1-134`.
it creates internal engineering tables `app.v3_eval_runs` and
`app.v3_eval_results`. a run stores the commit, trigger, feature and case
filters, models, repetition settings, costs, success flags, and errors. a
result stores hashes, validation status, actual models, pass metrics, costs,
check summaries, and trial data.

the ingest route is
`services/api/routes/internal/v3_eval_runs.py:1-209`. it authenticates with
the internal eval token, upserts by `ingest_key`, deletes replaced result
rows, and inserts the current result set. the internal router includes it in
`services/api/routes/internal/__init__.py:11-13,34-36`.

## local versus hosted systems

there are two related but separate systems.

the older voice eval platform uses `evals/cloud`. the alias
`evals/BUILD.bazel:105-106` maps `//evals:cloud` to its cloud CLI. that CLI
builds a typed manifest, reserves a run with the control plane, starts a
Daytona worker, waits for completion, downloads artifacts, and can open the
voice Eval Explorer. the key launcher code is in
`evals/cloud/cloud_cli.py:163-354`.

the older cloud worker runs compiled eval adapters. the adapter selection is
in `evals/cloud/cloud_worker_adapters.py:166-278`. its report uploader targets
`/webhooks/eval-runs`; the results land in `app.eval_runs` and
`app.eval_case_results`, which back `/admin/voice-evals`.

v3 on `origin/main` has a local API harness under `v3-evals`. it uses isolated
organizations, native trace polling, deterministic checks, and local result
artifacts. #17266 adds a separate Daytona workflow around that v3 harness.
it uses `scripts/daytona_preview.sh`, not `//evals:cloud`.

this split matters for result visibility. #17266's v3 webhook and tables are
not the existing voice webhook and tables. the current admin voice UI has no
v3 result route or page. the new records land in production storage, GitHub
artifacts, GitHub summaries, and Slack failure alerts, but not in the current
Eval Explorer.

`docs/testing.md:5-32` describes the older provider-backed voice philosophy:
golden cases raise the floor, run in hosted Daytona, and preserve a tight
debug loop. the v3 README instead documents local v3 execution. #17266 is the
bridge for hosted v3 runs, but it does not yet bridge v3 results into the
existing explorer.

## exact extension points for a new eval family

for a new v3 family, add one directory under `v3-evals/features` with the five
files shown above. Put case data and rubric metadata in the JSON maps. Set a
real `validation_status`. Implement `load_cases(files)` in `cases.py`.

the implementation should use these hooks:

- `setup(ctx)`: create isolated records and save any starting-state facts.
- `prompt(ctx)`: send the user task and attachments.
- `extract(ctx, observation)`: read native traces and persisted application
  state. return structured evidence.
- `checks`: compare the evidence with the rubric. return JSON details and
  evidence references.

for UI-output evaluation, keep the check on structured application state or
native tool results. if the UI emits stable record identifiers, extract those
identifiers and resolve them to persisted records. do not grade prose or a
screen string in isolation. the existing callout badge handling in
`pho_17564_general_chat_eval.md:48-53` is the concrete pattern.

use `TrialContext` in `v3-evals/src/cases.py:112-124` for the API client,
artifact directory, transcript, mutable state, organization id, and atomic
checkpoint. use the existing evidence and measurement extractors when the
new case uses agent traces or provider calls.

if the case is a production regression, promote it through the golden index.
the owner adds an entry to
`v3-evals/features/golden_set/dataset.json` and matching rubric metadata,
then validates the case. the golden CODEOWNERS entry is in the #17266 tree at
`CODEOWNERS:123-126`.

## gaps and direction for #17266

the open PR needs a real golden index before it provides a regression floor.
the next useful work is:

- promote validated cases into the golden index, with an owner and the
  production failure that each case protects against;
- run the hosted job against a known commit and inspect both harness results
  and the v3 ingest payload;
- decide whether v3 needs a new admin results page or an extension of the
  existing Eval Explorer;
- add tests for report ingestion and the empty-index behavior if the zero-case
  success state is intentional;
- document the difference between `//evals:cloud` voice runs and the v3
  Daytona workflow.

the PR's current empty-index success can hide a disabled regression suite.
the workflow variable also gates deploy and daily runs, while manual runs
ignore it. operators need clear visibility for both states.

the new v3 tables are internal and not organization-scoped. that is correct
for engineering eval results, but it means normal org RLS patterns do not
provide the access model. the future admin surface must define its own auth
and authorization.

## surprising or load-bearing facts

- the v3 executor is called `hosted.py`, but `origin/main` uses it through a
  local API client. “hosted” describes the batch executor shape, not a
  Daytona launch.
- #17024's model-based Bash audit was removed by #17174. deterministic review
  now defines the verdict boundary.
- incomplete evidence is visible and fails acceptance as infrastructure. it
  is not converted into a passing or zero-cost result.
- diagnosis is evidence-bound and cannot amend the verdict.
- the golden wrapper can exit zero with no cases. #17266's final golden JSON
  maps are empty, so this is the current default path.
- the existing `/admin/voice-evals` surface is not a v3 surface. v3 uses new
  tables and a new webhook.
- golden cases are wrappers around ordinary feature cases. promotion can
  preserve one implementation while changing the reporting and regression
  classification.
- the v3 build still has a `//v3-evals:report` binary and a hosted job, but
  this research pass did not run Bazel or any evals, per the task contract.
