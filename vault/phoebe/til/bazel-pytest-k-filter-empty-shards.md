---
type: til
tags: [phoebe, bazel, testing]
created: 2026-07-09
updated: 2026-07-09
---

# Bazel pytest `-k` can fail empty shards
**Symptom:** `bazel test //libraries/python/phoebe_admin_agent:phoebe_admin_agent_test --test_arg=-k ...` fails with pytest exit code `5` and `collected N items / N deselected / 0 selected` on unrelated `phoebe_admin_agent_test__shard_*__bundle` or `evals_test__*` targets.
**Cause:** Phoebe's Bazel Python tests are split into generated shard/per-file targets; passing one global pytest `-k` expression to the umbrella target hits shards that do not own the selected test names, and pytest treats "0 selected" as a failure.
**Fix:** Query the owning generated target first (`bazel query "attr(srcs, 'admin_agent_run_comparison_test.py', //libraries/python/phoebe_admin_agent:*)"`) and run that exact shard/file target instead of the umbrella test target.
