---
type: til
tags: [tools, tmux, codex]
created: 2026-07-06
updated: 2026-07-06
---

# tmux worker spawn: inline prompt quoting breaks on double quotes

**Symptom:** worker window dies instantly; log shows `error: unexpected argument 'git' found` (or any prompt word) from codex CLI.
**Cause:** spawn used `tmux new-window ... "codex --yolo -m gpt-5.4 \"$WORKER_PROMPT\""` — a double quote inside the prompt terminates the escaped quoting, remaining prompt words become CLI args.
**Fix:** write prompt to `/tmp/cdx-<TICKET>-prompt.md`, spawn with single-quoted command: `'codex --yolo -m gpt-5.4 "$(cat /tmp/cdx-<TICKET>-prompt.md)"'`. Same for claude workers. Patched into all 4 tmux-ticket-* skills 2026-07-06. See [[tmux-send-keys-composer-paste]] for the steer-message sibling quirk.
