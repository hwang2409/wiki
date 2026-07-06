---
type: til
tags: [tools, tmux, codex, claude]
created: 2026-07-06
updated: 2026-07-06
---

# tmux send-keys steer sits unsent in TUI composer

**Symptom:** steering message sent via `tmux send-keys -l "$MSG"` + immediate `send-keys Enter` sits in the codex/Claude composer textbox, never submits; intermittent.
**Cause:** TUI composers paste-detect rapid literal bursts; Enter arriving in the same input cycle is treated as a newline inside the pasted text, not a submit.
**Fix:** `sleep 0.5` between text and Enter, then verify ~2s later — if message text still in bottom composer rows, send bare Enter again (harmless when composer empty). Patched into tmux-ticket-codex/claude skills 2026-07-06.
