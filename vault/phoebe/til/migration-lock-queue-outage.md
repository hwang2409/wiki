---
type: til
tags: [phoebe, database, migrations, incident]
created: 2026-07-06
updated: 2026-07-06
---

# ALTER TABLE on hot table queues all traffic behind it

**Symptom:** prod "down" 16:36–16:40Z 2026-07-06; ~6.5k `asyncpg.exceptions.QueryCanceledError: canceling statement due to statement timeout` on trivial PK lookups (api+worker); app rendered nothing.
**Cause:** migration `20260703173814` (#10526) ran `ALTER TABLE app.organizations ADD COLUMN …` — instant once locked, but ACCESS EXCLUSIVE waits behind any long reader, and Postgres's fair lock queue stacks ALL new organizations queries behind the waiting ALTER. Cancelled deploy attempt at 16:32 likely aggravated.
**Fix:** none needed at the time — cleared when ALTER acquired+committed. Prevention: migrations on hot tables should set `lock_timeout` (e.g. 2s) + retry loop so the ALTER aborts instead of poisoning the queue; see [[todo]] backlog item.
