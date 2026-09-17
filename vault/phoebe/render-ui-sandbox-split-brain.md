---
type: til
tags: [phoebe, modal, gotcha]
created: 2026-09-16
updated: 2026-09-16
---

# render_dynamic_ui local failure: Modal sandbox split-brain from stale API env


RCA of trace `f39904ab-8093-499f-a4c6-2d81f449cd5b` (worktree stack org-snapshot-local, 2026-09-16). Playbooks table from `render_dynamic_ui` shows "This table could not be loaded" plus a "Core API is not configured" toast. Staging works.

## Chain

1. `render_dynamic_ui` succeeded: the worker wrote `/workspace/tables/<hex>.jsonl` (1101 B) into the org's Modal sandbox.
2. The table body loads via `GET .../phoebe_agent_runs/{run}/workspace-files/{path}`, which attaches to the per-org Modal NAMED sandbox `agent-bash-{org}-{image_ver}` (app `agent-bash-sandbox`, scoped by MODAL_ENVIRONMENT) and reads the file directly. Hydration restores only chat uploads, not spilled tables (`libraries/python/agent_sandbox/sandbox.py` `_ensure_hydrated`).
3. `services/api/.env.local` is stale: only ~70 of the 206 keys in `services/api/.env.keys` are present. MODAL_ENVIRONMENT / MODAL_TOKEN_ID / MODAL_TOKEN_SECRET / AGENT_SANDBOX_WORKSPACE_TOKEN_SECRET / CORE_API_* are all missing (main checkout and worktree copies alike).
4. The worker pins MODAL_ENVIRONMENT='dev'; the API falls back to `~/.modal.toml` default profile → different Modal environment → `Sandbox.from_name` misses → the API creates a SECOND, EMPTY sandbox (logs show both processes `lifecycle_event=create modal_reused=False` seconds apart) → read 404 → HTTP "Workspace file is unavailable; regenerate the result" → table load error.
5. Staging: API and worker share Terraform env → same Modal environment → API attaches to the worker's sandbox → works.
6. The "Core API is not configured" toast is the same stale env file: CORE_API_BASE_URL / CORE_API_INTERNAL_TOKEN absent from the API env, so a core-backed endpoint on the page 500s.

## Fix (not applied — Henry runs it)

`env-vars sync-local` (with AWS SSO fresh) in the checkout, then restart the API. Verify: worker and API both log the same MODAL_ENVIRONMENT, and the second process logs `modal_reused=True` for the org sandbox.

Related: [[local-db-grant-drift]]
