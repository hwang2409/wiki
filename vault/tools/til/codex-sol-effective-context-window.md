---
type: til
tags: [tools, agents, models]
created: 2026-07-09
updated: 2026-07-09
---

# Codex Sol effective context can be smaller than the model window

**Symptom:** The `gpt-5.6-sol` model card says `1,050,000 context window`, but a Codex CLI `0.144.0` ultra session reported `payload.info.model_context_window: 353400` in its `event_msg/token_count` records.
**Cause:** The model-card capacity and the effective window exposed by a Codex product session are separate limits; model specs alone do not predict root-session longevity.
**Fix:** Read `model_context_window` and `last_token_usage.input_tokens` from the active `~/.codex/sessions/...jsonl`. The measured session began at `21076/353400`; one tool-heavy research turn reached `122234/353400` with zero `compacted` / `context_compacted` events.
**Provenance:** Observed locally 2026-07-09; model-card capacity verified at https://developers.openai.com/api/docs/models/gpt-5.6-sol.
