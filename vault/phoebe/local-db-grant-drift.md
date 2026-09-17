---
type: til
tags: [phoebe, postgres, gotcha]
created: 2026-09-16
updated: 2026-09-16
---

# Local DB grant drift and v3 query spill error masking


RCA of agent trace `c48a4cbe-4b0d-4fc9-8fc0-87687f8cb331` (local, 2026-09-16). The v3 run's `query` tool failed on many tables with "Could not save the large query result. Retry with a narrower request." — even on a one-row `count(*)`.

## Root cause chain

1. On 2026-09-08 the local dev database was rebuilt. Part of the migration history was recorded via stamp (many `schema_migrations` rows share `applied_at` 23:53:58), so those migrations' SQL never ran.
2. `database/schema.sql` carries no ACLs for `agent_v3_query` / `caregiver_query`. Those GRANTs live only in migration files (plus a partial set in `bootstrap.sql`). A schema-load + stamp rebuild silently drops every migration-only GRANT. `migrate diff` does not check ACLs, so the drift stayed invisible.
3. Missing grants locally: table-level SELECT on `callout_settings`, `recommendation_defaults`, `phoebe_agent_run_domain_event_subscriptions`, and others; column-level SELECT on `outreach_candidates.caregiver_tags` (which poisons every full-surface candidates query).
4. Error masking bug: `agent_query.QueryDatabase.stream_rows` is an async generator, so `connection.prepare(sql)` runs on first iteration — inside `workspace._process_stream`'s `async for`. Its broad `except Exception` wraps `asyncpg` errors (here: permission denied) into `ToolOutputSpillError` → the model sees the generic spill message and retries "narrower" queries pointlessly. `libraries/python/phoebe_v3_agent/middleware/workspace.py:369`.

## Fix applied (local)

Replayed all 76 `GRANT ... TO agent_v3_query|caregiver_query` statements extracted from `database/migrations/*.sql` as superuser (idempotent), plus the `caregiver_tags` column grant whose regex extraction was mangled by a comment. Verified: every `outreach_candidates` column granted; all agent_v3_query entity tables pass a `select ... limit 0` probe.

## Repo-level follow-ups (not filed yet)

- Move agent-role table/column GRANTs into `bootstrap.sql` (same principle as function ACLs: "keep function permissions in bootstrap.sql so fresh schema loads preserve migrated ACLs"). `caregiver_query_acl_test` exists; there is no equivalent parity test for `agent_v3_query` table grants.
- Fix the error masking: `_process_stream` should let non-spill DB errors surface as query errors, not `ToolOutputSpillError`.

Related: [[hot]]
