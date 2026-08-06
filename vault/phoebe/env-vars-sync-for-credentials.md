---
type: til
tags: [phoebe, tooling]
created: 2026-08-03
updated: 2026-08-03
---

# env-vars sync is the credential source of truth

Rule (Henry, 2026-08-03): when a task appears blocked on missing credentials or env vars locally, ALWAYS try the synced env first before declaring it impossible.

- `env-vars sync-local` (phoebe repo) writes `.env.local` files next to every `.env.keys` directory, composed from AWS Secrets Manager (`phoebe-app-env-vars` + infra) plus a per-dev overlay.
- The synced files carry real credentials: `services/worker/.env.local` has `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET`; LLM keys, Twilio, etc. live in their owning service dirs.
- Worktrees do NOT automatically have these files — read them from the MAIN checkout (`/Users/henry/me/fun/phoebe/services/<svc>/.env.local`) or run `env-vars sync-local` in the worktree.
- Bazel py targets bundle their python deps (e.g. `modal`) in their own venv — a missing pip package or CLI is usually NOT a real blocker for `bazel run` targets.

Origin incident: PHO-15112 (2026-08-03) — worker declared the Modal containment lane "not locally runnable" (missing Modal tokens/CLI/package) and burned ~17 six-minute CI cycles as its only debug loop. All three objections were wrong: tokens were in the synced worker env, the CLI was unneeded, and the package came with the bazel target.

How to apply: worker spawn prompts and orchestrator steers should say "check synced .env.local files / run env-vars sync-local before reporting missing credentials." See also [[bazel-slot-shim-pathology]] for the other common false blocker class.

Counter-example (same day): the Modal containment lane still cannot run locally on macOS even with credentials — the local bazel build packages a darwin runtime binary into the linux Modal sandbox (Exec format error, runtime dies pre-hello). Cross-platform runfiles, not credentials, are the real limit; that lane is CI-only. Check WHICH class a local blocker is before assuming either way.

