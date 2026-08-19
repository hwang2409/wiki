---
type: decision
tags: [wiki-app, wk, tui]
created: 2026-08-19
updated: 2026-08-19
---

# wk TUI design

Arc goal (Henry 2026-08-19): make `./wk` a good TUI on par with Claude Code / Codex CLI. Brainstormed with Henry; decisions locked below. Tickets WIKI-345..351.

## Locked decisions (Henry, 2026-08-19)

- **Purpose:** both daily-driver and lane-debug harness, driver first. Polish is the bar; keep a verbose/raw-event mode underneath.
- **Screen model:** inline renderer + sticky bottom composer (Claude Code / Codex CLI style). Transcript flows into native terminal scrollback (searchable/copyable/survives exit); composer + status line redrawn at the bottom. NOT a full-screen alt-screen app.
- **Dependencies:** add `prompt_toolkit` + `rich` (pure-python, mature). No node/ink sidecar; no stdlib hand-rolling.
- **Scope:** core (markdown rendering, tool-call lines, spinner/status bar, multiline composer + history) PLUS token streaming, interactive approvals, session resume, slash commands.
- **Approach chosen:** A — layer a `wk_tui` package on the existing wk_cli engine (keep its proven lane lifecycle / interrupt / shutdown-reap machinery). Rejected: node/ink sidecar (second toolchain, 2-process protocol), incremental REPL rich-ification (throwaway middle state).

## Architecture

New package `backend/app/agent_runtime/wk_tui/`:

- `engine.py` — extracted from `wk_cli.py`: lane build, `run_turn`, `turn_end_status`, interrupt plumbing, shutdown/provider-reap. Importable WITHOUT prompt_toolkit/rich (plain path must not require the new deps).
- `render.py` — pure functions: lane event -> rich renderables. Claude events (system/assistant/user/result/control_request/provider_error) + codex methods (thread/*, turn/*, item/*, errors). Markdown for assistant text, syntax-highlighted code fences, dim thinking, compact tool one-liners, plain-text status markers (no emojis). Verbose flag -> raw event lines.
- `app.py` — composition root: background asyncio loop thread (existing pattern), prompt_toolkit session with `patch_stdout` (transcript prints above composer), render pump via thread-safe queue.
- `composer.py` — keybindings, multiline rules, persistent history file, slash-command parsing.
- `approvals.py`, `sessions.py` — later phases.

**Entry behavior** (`./wk` launcher unchanged): tty + no `-p` + not `--plain` -> TUI; `-p` / `--plain` / non-tty stdout / TUI import failure -> existing plain REPL, byte-for-byte today's behavior. Driver selection lives in `wk_cli.py`.

**Streaming model:** commit-on-newline (Codex CLI style). Completed lines flush to scrollback; the in-progress partial line renders in the status area. Avoids fragile cursor-up in-place redraw under wrapping; ~90% of the streamed feel.

**Interrupts:** Ctrl-C during a turn -> `lane.interrupt()`, same 3-strike escalation as today. Ctrl-C at idle composer clears input; Ctrl-D exits.

**Approvals:** `control_request` mid-turn switches the composer into an inline y/n(/always) prompt mode; response goes back through the lane's respond path.

**Session resume:** capture provider session ids from init events; persist session id + transcript JSONL under `~/.wiki/wk-sessions/`; `./wk --continue` / `--resume <id>` pass lane-level resume args (claude `--resume`, codex `resume <id>`). Needs lane support work in wk_core lanes — scoped inside WIKI-351.

## Ticket ladder

| Ticket | Scope | Depends |
|---|---|---|
| WIKI-345 | engine extraction + `--plain`/tty driver selection + deps added w/ import guard; no behavior change | — |
| WIKI-346 | `render.py` pure event->rich renderables + unit tests | — (parallel w/ 345) |
| WIKI-347 | TUI shell: composer, patch_stdout pump, status bar, history, interrupt ladder | 345, 346 |
| WIKI-348 | token streaming, commit-on-newline | 347 |
| WIKI-349 | slash commands: /model /effort /clear /verbose /help /quit | 347 |
| WIKI-350 | interactive approvals | 347 |
| WIKI-351 | session resume (lane-level + CLI flags) | 347 |

## Testing

- `render.py`: pure unit tests (event dict in, captured rich output asserted).
- Composer/slash parsing: prompt_toolkit pipe-input tests.
- Engine extraction: existing `backend/tests/test_wk_cli.py` must pass unchanged; plain REPL is the regression anchor.
- Gate: `PYTHONPATH=. .venv/bin/pytest backend/tests -q` per protocol.

## Execution notes

- Fleet: codex credits 0 until 2026-08-19 23:31 EDT; implementers run cc `opus-4.7` until the window resets (NEWT-32 playbook, Henry override precedent 2026-08-18), then revert to cdx luna/sol default loop. Reviews required (backend/logic, not pure UI).
- WIKI-345 owns `wk_cli.py` + `engine.py`; WIKI-346 only creates `render.py` + tests (trivial `__init__.py` overlap accepted, 345 merges first).
