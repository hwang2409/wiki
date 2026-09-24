---
type: reference
tags: [phoebe, evals]
created: 2026-09-24
updated: 2026-09-24
---

# v3 eval improvement tasks

Each checkbox is a reviewable change. Keep the existing isolated trial runner and
persisted-state checks. This list is based on Phoebe `origin/main`
`3fdcfb22759c352a13604b66511adbf682bd41d4`, Henry's requested target,
[[EVAL-RICHARD-SCOUT-memo]], and the [Anthropic](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
and [LangChain](https://www.langchain.com/blog/how-we-build-evals-for-deep-agents)
eval guides.

## Trust the verdict

- [ ] **E01 — Keep hard checks authoritative.** Store model scores beside
  deterministic checks. A model pass must never turn a failed state, permission,
  or safety check into a trial pass. Done when tests cover both disagreement
  directions and preserve both results in the artifact.
- [ ] **E02 — Mark required grader failure unavailable.** Add an explicit
  required-versus-shadow grader setting. Missing credentials, timeouts, invalid
  output, and missing evidence must prevent a required grade from passing.
  Done when each failure saves its cause and cannot produce a green run.
- [ ] **E03 — Pin the tested runtime.** Capture the actual API and worker
  build identities, including local changes, plus the agent model, grader
  version, and dataset/rubric hashes. Compare the running services with the
  requested ref before a trial starts. Done when a mismatched local stack
  fails preflight and a result identifies every tested version.
- [ ] **E04 — Correct the authoring contract.** Update the v3-evals skill and
  README to describe deterministic gates, optional model scores, unavailable
  grades, and the real run commands. Done when examples match the CLI and
  offline contract tests.

## Establish reviewed tasks and labels

- [ ] **E05 — Review the golden answer keys.** Ask a Phoebe domain reviewer to
  confirm, revise, or remove each of the eight `awaiting_human_validation`
  cases. Record the evidence and reviewer decision with each case. Done when
  no unreviewed key is treated as a trusted regression gate.
- [ ] **E06 — Define two pilot task rubrics.** Select one state-changing
  outreach task and one multi-turn conversation task. Specify outcomes,
  prohibited actions, acceptable variation, and required evidence. Done when
  two reviewers can apply each rubric to the same saved runs and resolve any
  disagreement.
- [ ] **E07 — Build a grader reference set.** Save human labels and evidence
  references for passing, failing, and ambiguous pilot runs. Keep a holdout
  portion separate from grader development. Redact customer data before
  storing reusable examples or sending them to an external grader. Done when
  the set can score a grader without rerunning the subject agent.

## Add and verify agentic grading

- [ ] **E08 — Define the agentic grade contract.** Return a criterion,
  `pass`/`fail`/`insufficient_evidence`, cited evidence, explanation, grader
  version, usage, and cost. Validate citations against the saved bundle. Done
  when invalid or absent citations cannot produce a valid grade.
- [ ] **E09 — Give the grader read-only evidence tools.** Compare the v3
  diagnostic agent with `agent_trace_auto_taggers` and reuse the appropriate
  trace reader and judge contract. Include persisted outcome evidence,
  approved redaction, and no write tools. Done when the grader can inspect a
  full pilot run, follow citations, and stop at its configured budget.
- [ ] **E10 — Score both passes and failures in shadow mode.** Run the agentic
  grader on completed pilot trials, including trials that code checks pass.
  Save its result separately from the release verdict and show it in the
  workbench. Done when a reviewer can open every disagreement and its cited
  evidence.
- [ ] **E11 — Measure grader errors.** Run the grader against the held-out
  labels. Report false passes, false failures, abstentions, and disagreement
  by criterion and task type. Review each high-risk false pass with a domain
  expert. Done when the report and labeled examples are saved with the grader
  version.
- [ ] **E12 — Promote criteria one at a time.** Let a reviewed semantic
  criterion become a required gate only after its error review. Keep hard
  checks as separate required gates and treat insufficient evidence as
  unavailable. Done when one pilot criterion gates a run and a rollback to
  shadow mode is tested.

## Compare versions and use results

- [ ] **E13 — Launch a pushed ref from the Eval Explorer.** Resolve a branch
  or SHA to an immutable bundle without changing a local checkout. Show the
  resolved commit before launch. Done when a branch run records the same
  commit that its API and worker execute.
- [ ] **E14 — Compare paired runs in the workbench.** Launch baseline and
  candidate with matching case selectors and trial counts. Show per-case and
  per-criterion verdicts, costs, time, grader disagreements, and evidence
  links. Compare shared case IDs and show unmatched cases separately. Done
  when a reviewer can explain a changed verdict from the two saved runs.
- [ ] **E15 — Run v3 cases in hosted validation.** Connect the v3 runner to
  the current hosted execution path. Keep fixtures isolated, verify cleanup,
  and retain artifacts with the tested commit. Done when one branch run and
  one main run finish with inspectable results and no leftover test orgs.
- [ ] **E16 — Add CI selection and release reporting.** Run a small, relevant
  case set on candidate changes and the broader reviewed set on a schedule.
  Keep agent failures, grader failures, and infrastructure failures distinct.
  Done when a failing required criterion blocks the candidate and a missing
  required grade cannot appear green.
- [ ] **E17 — Turn confirmed failures into cases.** Check whether the current
  Agent Court path can supply reviewed traces. Convert one confirmed failure
  into an isolated fixture, rubric, and regression case. Record the incident,
  case owner, and expected outcome. Done when the case reproduces before its
  fix and passes after the fix.
- [ ] **E18 — Maintain suite ownership.** Show each case's owner, intent,
  validation status, last useful failure, and review state. Add a review path
  for stale or disputed cases. Done when an owner can remove or revise a case
  without losing its past results.
