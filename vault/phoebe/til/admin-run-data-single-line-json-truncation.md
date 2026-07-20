---
type: til
tags: [phoebe, admin-agent]
created: 2026-07-20
updated: 2026-07-20
---

# Single-line JSON can bypass run-data truncation markers
**Symptom:** `read_run_data` returns `page_truncated=false` and `has_more=false` while its only line ends `...[truncated 97363 chars]`; `search_run_data` then reports zero matches with `oversized_lines_skipped=1`.
**Cause:** `_search_text` serializes JSON as one line, `_visible_read_lines` clips that line via `_clip_visible_line`, but marks truncation only when it removes whole lines, so no full-page artifact is emitted; search skips oversized lines.
**Fix:** treat per-line clipping as truncation and spill the full page; add structural/chunked JSON search plus root-key discovery for `slice_json`.
**Observed:** production run `019f8067-099d-75f2-82c5-cc6689a18ff7`; verified on Phoebe `origin/main@5a26954d43`.
