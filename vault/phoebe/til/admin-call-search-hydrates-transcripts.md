---
type: til
tags: [phoebe/til]
created: 2026-07-08
updated: 2026-07-08
---

# Admin Call Search Hydrates Transcripts
**Symptom:** `search_call_recordings` hit `httpx.ReadTimeout` / p95 ~20s in prod even though `/api/calls` only returned list items.
**Cause:** `core/src/meeting_recordings/read_api.ts:listCalls()` used `getRecordingsByIds()` to hydrate full `meeting_call_recordings` rows, including `transcriptText`, before filters/pagination and `callListItemJson(...)` dropped the transcript.
**Fix:** Split list/search from transcript reads. Keep `/api/calls` metadata-only via a summary projection, and add transcript-specific read/search endpoints for one call at a time.
