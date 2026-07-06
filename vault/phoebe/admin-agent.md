---
type: reference
tags: [phoebe, admin-agent]
created: 2026-07-06
updated: 2026-07-06
---

# Internal Admin Agent — Current State

Living state note. Deep queue detail: `~/me/dox/admin-agent-plan.md`; shipped-work history: `~/me/dox/admin-agent-log.md`. Update this note when foundations shift.

## GitHub (PHO-12306)

- **Done:** App exists + credentialed in prod (`ADMIN_AGENT_GITHUB_APP_ID/_INSTALLATION_ID/_PRIVATE_KEY`, MCP variants, `READ_TOKEN` fallback); installation-token minting in `admin_external_api_clients.py`; App-native repo visibility + `dynamic_visibility` readiness (PHO-12666/#10238). Read tools: list repos, search PRs, inspect PR + diff, get file.
- **Remaining:** webhooks (B — no endpoint/secret), review write-back (C), PR creation (D — consumes PHO-13073 `export_sandbox_diff`), write guardrails (E), operator UX (F).

## Integrations

- MCP → first-party API clients migration in progress (PHO-12634; first slice #10110); MCP demoted to smoke-only.
- **Notion token is SHARED with A2P legal docs** (`ADMIN_AGENT_NOTION_INTEGRATION_TOKEN` === `NOTION_API_TOKEN`, same "Phoebe" bot) — page-connection edits hit both. Dedicated integration recommended, not done. (07-06 investigation: A2P opt-in page 404s were a Notion-side page-connection loss, not agent-caused.)
- Dynamic visibility readiness across Slack/Notion/Calendar/GitHub/Granola/Snowflake replaced static allowlists (PHO-12666).

## Sandboxes / code execution

- PHO-13073 tool surface (substrate-agnostic contracts, exe.dev scoped-token client, TTL sweeper, audit-only write lane) — PR #10608 in review-fix loop.
- exe.dev pilot (PHO-12930, In Progress): API shapes captured 07-06 (see [[exe-dev]]); free tier 25 GB pooled disk likely too small for phoebe-base — plan upgrade decision pending; experiments run through 13073 tools once merged. Modal = likely production pivot.
- Hard rules: no PHI/prod DSNs in sandboxes; only network-injected read-only repo creds; PR authority stays on App path.

## Recently shipped foundations (late June – 07-03)

Workflow-skill layer (12785), event-driven subagent fan-in (12584/12585/12587), no-ceiling admin runtime, tool-output tiering (12938), external tool identifier resolution (12939/#10474), tool telemetry dashboard (12951), report_missing_tool (12952), `:eyes:` trace diagnosis (12955), expected-failure alerting (12949/12962), approval policy: external writes audit-only (12515/12929), cancelled-run revival fix (12857), heartbeat starvation fix (12826).

## Active (07-06)

- PHO-13073 sandbox tools — worker on PR #10608
- PHO-12937 type-aware tool-output renderers — worker on PR #10614
- PHO-12980 accounts API query capability — DONE (#10611 merged 07-06)
- Queue: [[todo]]
