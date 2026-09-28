---
type: til
tags: [tools, agents, models]
created: 2026-07-09
updated: 2026-09-28
---

# Codex Sol effective context can be smaller than the model window

**Symptom:** `gpt-5.6-sol` advertises a `1,050,000` context window, but Codex CLI reported `model_context_window: 353400` on 2026-07-09 and `258400` in three sessions on 2026-09-28.
**Cause:** The model-card window and the effective Codex product window differ. Zeta session `81f4e1b663cd41b1b920ad3b28fb8815` used a `1,050,000` compaction budget, reached an estimated `1,050,989` tokens, then sent a roughly `920,255`-token summary source. The backend returned `context_length_exceeded: stream error` twice; no compaction marker landed. Zeta's exact backend limit was not recorded.
**Fix:** Zeta PR [#283](https://github.com/hwang2409/zeta/pull/283) strips provider replay metadata from summary input, compacts large ranges in bounded chunks, and retries a context overflow once before output. It applies to all Codex and Claude models through the shared context path. The reported session passed a structural replay with a fake backend; live provider recovery has not been tested.
**Provenance:** Local Codex CLI token-count events, Zeta session `meta.json`/`conversation.jsonl`, Zeta PR #283; model card: https://developers.openai.com/api/docs/models/gpt-5.6-sol.
