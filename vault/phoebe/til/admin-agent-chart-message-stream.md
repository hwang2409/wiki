---
type: til
tags: [phoebe, admin-agent]
created: 2026-07-09
updated: 2026-07-09
---

# admin-agent-chart-message-stream
**Symptom:** `admin.chart` / `admin.visualization` render twice unless the transcript treats them as assistant-message artifacts, not tool-card artifacts.
**Cause:** Admin tools still return artifact refs in `tool_output.output_json`, but PHO-13277 adds a follow-on `assistant_text` event with `artifact_refs`; that assistant event is the display contract.
**Fix:** Suppress `admin.chart` / `admin.visualization` refs in tool-card renderers, render them from assistant-message `artifactRefs`, keep the raw tool-output refs for audit/JSON inspection, and store `time_window.timezone` on the artifact payload. Current guardrails: 32 series max, 5,000 total points max.
**Hardening:** PHO-13307 keeps artifact refs when Slack reply projection suppresses assistant follow-up text, renders `Attached: <title>` chips on surfaces without a custom message-artifact renderer, and ignores whitespace-only `assistant_text` rows in `latest_assistant_text_before_terminal_event`.
