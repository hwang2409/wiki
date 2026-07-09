---
type: decision
tags: [phoebe/decisions]
created: 2026-07-09
updated: 2026-07-09
---

# Admin Db Reads Generated Catalog


# Decision: schema-wide admin DB reads via generated catalog (2026-07-09)

Henry + orchestrator brainstorm, 2026-07-09. Ticket: PHO-13273.

**Decision:** Retire hand-curated `_DOMAIN_SPECS` allow-list in `query_admin_database`. Replace with a catalog generated from `schema.sql` in the `migrate apply` codegen pipeline — every app-schema table/column readable by default. Hard deny-tier only for secret-class columns (auth secrets, tokens, SSN-class) in a checked-in exceptions file with CI lint on secret-pattern names. Query-cost guardrails: role-level statement_timeout + work_mem on admin_agent_readonly, EXPLAIN cost gate, FK-edge-only joins, existing pagination/spill. Discovery via catalog-backed search_schema/describe_table.

**Why:** Allow-list decays as schema grows — every gap becomes a bespoke tool build (PHO-13251: 3 organic missing-tool reports for one natural question). Mutation risk already bounded by SELECT-only role; remaining real constraints are secrets and query cost, both cheaper than per-field curation.

**Rejected:** raw SQL surface (unbounded query shapes, composes badly with structured spill machinery); runtime information_schema introspection (semantics like mode/org-anchor/sensitivity not in catalog — allow-list problem reappears as annotation side-table); sensitivity tag+caveat machinery (Henry: internal tool, admins have prod access; only true secrets matter).

**Beyond-DB gap map** (future, discussed same session): known-issue memory (PHO-11267), cross-run learning (PHO-11275), approval-gated writes (T3, trigger parked per PHO-13226), Temporal/queue ops visibility (no ticket yet).
