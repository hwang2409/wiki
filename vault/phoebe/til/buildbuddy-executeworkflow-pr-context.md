---
type: til
tags: [phoebe, bazel, ci]
created: 2026-07-09
updated: 2026-07-09
---

# BuildBuddy ExecuteWorkflow loses PR context
**Symptom:** Retrying PR `Bazel CI` via BuildBuddy `ExecuteWorkflow` kept the GitHub status on the same commit but the log entered `Push artifacts (only run on main & hotfix branches)` and ran `./ci/push_with_retry.sh bazel run --config=ci_arm64 --config=push //services/api:api_push`.
**Cause:** `ExecuteWorkflow` on a PR branch/commit did not populate `GIT_PR_NUMBER`, so `buildbuddy.yaml` evaluated the run as branch-push workflow context instead of pull-request context.
**Fix:** Do not use `ExecuteWorkflow` to retry PR `Bazel CI`; prefer a real PR/check rerun path. For flaky PR failures, confirm locally from the worktree and inspect the original invocation with `bin/bb view <invocation>` instead of starting a manual workflow.
