---
type: reference
tags: [wiki-app, performance, archive, rca]
created: 2026-09-24
updated: 2026-09-24
---

# Agents list CPU path (2026-09-24)

**Cause:** `GET /api/agents` enumerates the full archive on every poll. `_archive_sessions(ticket_dir)` reloads and parses the same 363 KB `archive-catalog.json` for each of ~2,872 ticket directories. `list_archived(limit=20)` then reads status/meta/run JSON for every candidate and applies the 20-row limit only at the end.

## Live evidence

- Backend PID 9366 remained near 100-120% CPU at 14:06 EDT, more than an hour after the knowledge rebuild. A five-second sample found one AnyIO worker spending ~1.8 of 3.9 sampled seconds in `_json.scan_once_unicode`; other threads walked directories. The supervisor sample was mostly idle.
- The backend log had ~1,010 completed `GET /api/agents?include_history=true` requests since launch. A direct read-only call took 32.46 seconds; an earlier default call timed out after 20 seconds. The route always calls `list_archived()` (`backend/app/main.py:2523`).
- `backend/app/main.py:1813-1830` calls `read_archive_catalog(ticket_dir.parent)` for each ticket. `list_archived()` at `:1925-1993` loops every ticket and opens every selected session body before slicing to 20. A 100-parse local benchmark put repeated catalog JSON parsing alone near 2.2 seconds per request; it does not explain the full 32-second live latency.

## Fix and measurement

- PR [#300](https://github.com/hwang2409/wiki/pull/300) on branch `wiki-cpu-fix` reads the catalog once per archive-list call. It sorts session paths first, then verifies uncataloged sessions and opens only the requested bodies. The default 20-row call returned in 0.13 seconds against the live archive from the branch, compared with 32.46 seconds through the old route. A direct `agents(include_history=True)` call from the branch took 0.18 seconds with mutation hooks mocked. The running app still uses the old code.
- The same branch accepts safe lowercase workgraph IDs and stops the worker loop-state UI poll for orchestrators. This removes the `feebs` validation error loop. The live app still runs the old build.

The sample and route timing show an independent high-cost agents-list path. They do not prove the exact CPU share of each substep under concurrent knowledge refresh.
