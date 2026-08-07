---
type: til
tags: [wiki-app, codex, til]
created: 2026-08-07
updated: 2026-08-07
---

# Codex reasoning summaries are encrypted

**Symptom:** Wiki.app shows short collapsed `Thought` rows, not Codex's full reasoning trace.
**Cause:** Codex app-server `reasoning` items expose `summary` and opaque `encrypted_content`; `content` is empty.
**Fix:** Click a `Thought` row to expand its supplied summary. Full private reasoning cannot be rendered or decrypted by Wiki.app.
**Verified:** 2026-08-07; live `WIKI-263` returned 39 encrypted reasoning summaries through `/api/agents/WIKI-263/session`.
