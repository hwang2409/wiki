---
type: reference
tags: [phoebe, onboarding]
created: 2026-07-06
updated: 2026-07-06
---

# Phoebe Repo Guide (0→1 routing)

Fastest orientation for an agent touching the phoebe monorepo (`~/me/fun/phoebe`). This note ROUTES — the repo docs are canonical; do not trust this over them. Derived from a full docs/ inventory sweep 2026-07-06 (every claim cites its source file).

## Read-first stack, by task

| Task | Read |
|---|---|
| Anything at all | repo `CLAUDE.md` / `AGENTS.md` (auto-loaded in-repo; load-bearing rules live there) |
| Working in one package | that package's `AGENTS.md`: `apps/web/`, `services/api/`, `services/worker/`, `services/voice/`, `database/`, `evals/` — each has conventions + hazards |
| Code style | `docs/styleguide.md`; DB/session rules enforced by PHB ruff rules → `docs/ruff_patches.md` |
| Local stack | `docs/getting_started/local_development.md` (ports: API 8001, web 5173, voice 8002, Temporal 7233/8233) |
| Migrations | `docs/development_guides/database_migrations.md` + `database/AGENTS.md` |
| New API endpoint | `docs/development_guides/adding_new_api_endpoints.md` (then `pnpm generate:types`) |
| Queue/worker | `docs/development_guides/message_queue_and_worker.md` + `services/worker/AGENTS.md` |
| Bazel pain | `docs/development_guides/bazel.md` |
| Deploys/hotfix | `docs/development_guides/deployment.md` (staging auto on main; prod = manual promote or `scripts/hotfix.sh`) |
| Modal/agent runtime | `docs/development_guides/agent_runtime_modal.md` + `agent_sessions_deploy.md` (dev/prod Modal envs, secret names) |
| Sandbox/live data isolation | `docs/features/sandbox_mode.md` (mode column, RLS, `X-Mode` header) |
| Testing/eval philosophy | `docs/testing.md` (floor-raising golden cases; howtoeval.com) |
| Voice | `docs/development_guides/voice_agents.md`, `voice_architecture.md`, `services/voice/AGENTS.md` |
| Slack V2 | `docs/development_guides/slack/` (9 docs: architecture → rollout checklists) |

## Working rule: always use a worktree

Never work directly on the primary checkout at `~/me/fun/phoebe` — Henry runs multiple concurrent agent sessions against it, and branch switches/dirty state collide. For ANY code work: reuse the ticket's existing worktree if one exists (`git worktree list | grep -i <ticket>`; conventional homes `.claude/worktrees/<name>` and `.codex/worktrees/<name>`), otherwise create a new one. The primary checkout is for reads, greps, and orchestration only. (Henry, 2026-07-06.)

**Local dev DB is always disposable.** Henry never cares about local DB state — when local data is stale/missing (e.g. missing "Phoebe Home Care" pinned admin org), re-seed without asking: `bazel run //scripts:seed_local_db -- --email henry@phoebe.work --password password` (wipes + recreates), then re-create the agent login via /setup-agent-account. (Henry, 2026-07-06.)

## The mistakes cold agents actually make

(Each rule's canonical home cited; this list exists because these cause real damage when unknown.)

- Gazelle is update-only: `touch` an empty `BUILD.bazel` before expecting generation; never `bazel clean`, use `bazel shutdown` (`docs/development_guides/bazel.md`).
- Worker handlers are dead until registered in `services/worker/main.py` handler lists — the decorator alone does nothing (`services/worker/AGENTS.md`).
- Never hand-edit generated files: `schema.sql`, `generated_database_models/`, `generated_api_types.ts` — regenerate via `migrate apply` / `pnpm generate:types` (`CLAUDE.md`).
- Column/table drops are two-deploy: code removal + pending-deletion YAML first, physical drop later (`database/AGENTS.md`).
- No network/LLM calls inside `db_session.begin()` blocks — PHB ruff rules will catch it; fix, don't suppress (`docs/ruff_patches.md`).
- `uuidv7()` PK defaults; domains-over-enums; org-scoped tables need mode column + both RLS policies (`docs/development_guides/database_migrations.md`).
- Outreach skip/allow lists → `OutreachFilterRule` table, never new org-settings fields (`CLAUDE.md`).
- e2e must never target real caregivers — only `E2E Caregiver …` named test data; real ones route to real phones (`docs/testing.md`).
- Fresh worktrees fail the pre-push ty hook (no venv): `sync-venv`, then `./scripts/worktree_setup.sh` if it persists (see [[tmux-spawn-inline-prompt-quoting]] sibling TILs in tools/til/).
- Prod data mutations: per-command explicit approval with preview + rollback notes; read-only queries fine (`CLAUDE.md`).

## Documented gaps (docs don't cover; vault candidates as they get learned)

From the 2026-07-06 sweep: import-linter boundary specifics; RLS mode-context debugging steps when propagation fails; Temporal workflow/activity scoping guide; queue message-ordering guarantees; Kratos auth-flow extension guide; `.web-port`/`.api-port` worktree file lifecycle. Write a TIL/feature note when one of these costs real time.

## Related vault notes

[[admin-agent]] — the Internal Admin Agent architecture doc (deeper than any repo doc for that surface). [[conventions]] — vault rules.
