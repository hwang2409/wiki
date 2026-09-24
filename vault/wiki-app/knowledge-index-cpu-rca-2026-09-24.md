---
type: reference
tags: [wiki-app, knowledge, performance, rca]
created: 2026-09-24
updated: 2026-09-24
---

# Knowledge index CPU loop (2026-09-24)

**Cause:** A fresh knowledge DB indexes the whole agent archive. Archive commits during the long scan leave the refresh marker set, so the next pass scans the whole archive again. Each pass also verifies every committed archive by fsyncing its directory and walking its manifest tree, even when its events are already indexed.

## Live evidence

- The ninth reset deleted `~/.wiki/knowledge.db` at ~11:31 EDT (2026-09-24). Backend PID 9366 launched 11:33 and logged `knowledge schema missing/mismatched ... rebuilding`.
- `meta.built_at` became `2026-09-24T16:58:49Z` (12:58:49 EDT): the first rebuild took ~85 minutes. It changed again to `17:00:36Z` during the next full refresh, which took ~107 seconds. At 12:57 the 11 GB DB held 2,754 runs, 9,293,186 events, and 9,294,767 chunks. The source archive was 59 GB with 3,657 `events.jsonl` files. Thirty-one archive event files changed in the prior 90 minutes.
- Backend CPU stayed around 120-140% near 13:00 after the first build. A five-second `sample` showed the main thread and several worker threads in directory scans and `lstat`; `lsof` showed open archive directories, manifests, and the knowledge DB. The supervisor was mostly idle in a later three-second sample.
- The zero-byte `knowledge.db.refresh` marker remained present and its mtime changed during the scan. `background_index_loop()` starts another `refresh_all()` whenever it exists (`backend/app/knowledge.py:1040-1060`). `_clear_refresh_request()` only removes it if no newer archive commit touched it (`:412-420`).

## Decision and fix

- Henry chose a notes-only knowledge index. Agent run history stays in its source archive and agent UI. Full transcript search is no longer needed in `knowledge.db`.
- PR [#300](https://github.com/hwang2409/wiki/pull/300) on branch `wiki-cpu-fix` removes archive and live-run ingestion, the run/event tables, archive-triggered refresh requests, and run filters from CLI and MCP search. Schema version 5 rebuilds the derived DB from notes. On version mismatch, the backend recreates the DB file to reclaim the old 11 GB file instead of dropping rows into a large freelist.
- The DB still serves note search, links, and semantic-search fallback. `knowledge.db.semantic` remains a separate note index.
- An isolated notes-only rebuild on PR #300 indexed 165 notes and 1,556 chunks in 0.39 seconds. The derived DB was 3.8 MiB. The old live rebuild took about 85 minutes and left an 11 GB DB.
- A native build of PR #300 staged successfully. The old backend still runs until the new app build is installed. Do not treat the branch result as live CPU proof.

The sample identifies archive scans as an active CPU path. An independent agents-list bottleneck appears in [[agents-list-cpu-rca-2026-09-24]]. The samples do not apportion every CPU cycle between those paths.
