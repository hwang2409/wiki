---
type: reference
tags: [phoebe]
created: 2026-07-18
updated: 2026-07-18
---

# Datasource sync auto-disable ops


## What happens

- Scheduled org syncs count consecutive failures per data-source account (`services/worker/handlers/sync/failure_tracking.py`); 3 failures → `sync_enabled=false` + #engineering Slack alert (HHAExchange exempt — API-key auth, alert-only).
- Self-heal: daily forced shift sync cron at 10:10 UTC (`shifts_orchestrate.py`, `force_sync=True`) — success resets counter AND re-enables the account. Provider-wide overnight vendor outages (AxisCare maintenance ~04:00-05:00Z window) auto-recover by morning; verify before manually forcing anything.
- Manual path if still disabled: `POST /admin/ops/sync/shifts` (or `/sync/caregivers`) with `{"organization_id": ..., "force_sync": true}`.

## 2026-07-18 incident

- 8 orgs auto-disabled 04:24-04:26Z, all axiscare. Cause: AxisCare platform-wide 5xx/429 across many customer subdomains 04:20-04:31Z (1,287 retry events that hour vs single-digit baseline). All 8 re-enabled by the 10:10Z forced sync. No action was needed.

## Observability gotcha

- `before_sleep_logger` in `libraries/python/data_source_integrations/core/http_client.py` only extracts `status_code`/`reason` from *exceptions* (`httpx.HTTPStatusError`). Result-based retries (`retry_if_result(is_retryable_response)` — 5xx/408/429 responses, used by axiscare + hhaexchange clients) log `status_code=null, reason=null, exception_type=null`. During vendor outages every retry log line is blind to the actual HTTP status; final failure surfaces only as opaque `tenacity.RetryError[... returned Response]`. Fix if it bites again: derive status from `retry_state.outcome.result()` when outcome is not an exception.
