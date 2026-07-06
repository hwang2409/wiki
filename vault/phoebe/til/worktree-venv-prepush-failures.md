---
type: til
tags: [phoebe, worktrees, bazel, tooling]
created: 2026-07-06
updated: 2026-07-06
---

# Fresh worktree venv fails pre-push ty hook

**Symptom:** `error: failed to push some refs` — pre-push hook fails in fresh `.codex/worktrees/*`; ty/import checks miss pinned deps (`pipecat-ai`, `aws-bedrock-token-generator`, `pyyaml-include`).
**Cause:** worktree venv stale vs pyproject pins; pre-push runs ty against it.
**Fix:** `sync-venv` (or re-pin the listed packages) in the worktree before first push; workers self-recover but burn a push cycle. Worker prompts now say run sync-venv before validation.
