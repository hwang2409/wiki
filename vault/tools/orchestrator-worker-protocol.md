---
type: reference
tags: [tools, agents, tmux]
created: 2026-07-07
updated: 2026-07-08
---

# Orchestrator ↔ Worker Protocol (file/tmux schema)

Machine-readable contract between the mastermind orchestrator session and tmux worker sessions. Canonical behavior lives in the `tmux-ticket-codex` / `tmux-ticket-claude` skills (synced via github.com/hwang2409/agent-config); this note is the SCHEMA — what exists on disk/tmux and who reads/writes it. Written for a future "agents" page in the wiki app to render live state from these files.

## Identity

| Thing | Convention | Writer |
|---|---|---|
| Worker tmux window | name `cdx:<TICKET>` (codex) / `cc:<TICKET>` (claude); TARGET BY `#{window_id}` (e.g. `@327`) — names contain `:` which breaks tmux target parsing | orchestrator (spawn) |
| Worker CLI | `codex --yolo -m <model> -c model_reasoning_effort=<effort> "$(cat <prompt-file>)"` / `claude --model <m> --dangerously-skip-permissions "$(cat <prompt-file>)"` | orchestrator |
| Orchestrator window | `thinker` (never renamed) | — |

## Files (all under /tmp, per ticket)

| Path | Purpose | Writer → Reader | Lifecycle |
|---|---|---|---|
| `/tmp/cdx-<TICKET>-prompt.md` (or `cc-`) | Kickoff prompt. ALWAYS a file — inline `"$PROMPT"` quoting breaks on embedded double quotes (codex parses prompt words as CLI args, instant death) | orchestrator → worker CLI at spawn | deleted at wrap-up |
| `/tmp/agent-status/<TICKET>.json` | PRIMARY status channel. Worker rewrites on EVERY state transition | worker → orchestrator monitors | orchestrator deletes before spawn (stale-state guard) and at wrap-up |
| `/tmp/cdx-<TICKET>.log` (`-2`, `-3` suffixes per session/respawn) | Full pane log via `tmux pipe-pane -o 'cat >> <log>'`. Scrollback truncates; log doesn't. CAUTION: contains the PROMPT ECHO — sentinel greps against it false-positive on prompt text | tmux → orchestrator (fallback signal only) | deleted at wrap-up |

## Status-file schema (the load-bearing contract)

```json
{
  "state": "working | merge-ready | blocked",
  "pr": "<url or null>",
  "step": "<one-line current step>",
  "blocker": "<reason or null>"
}
```

- Worker updates BEFORE long operations, not just after.
- `merge-ready` → orchestrator runs the review gate (never trusts the claim).
- `blocked` + `blocker: "handoff-needed"` → multi-session handoff (kill window, respawn same worktree, prompt points at newest PR handoff comment).
- Planning workers reuse the same file with `pr: null`; `merge-ready` means "plan posted".

## Signal priority (orchestrator monitors)

1. **Status file** — structured, no regex guessing; emit event on state/step change.
2. **GitHub ground truth** — when status file stale (mtime > ~5min) AND pane has no spinner: `gh pr checks`, `mergeStateStatus`, unresolved-thread count. Checks green + threads clear ⇒ merge-ready regardless of worker text; MERGED ⇒ wrap up.
3. **Pane text** — hints only (workers paraphrase, TUI lies): stall detection (`esc to interrupt` spinner absent across 2 polls) and error-line scraping.

Monitors = persistent background shell loops (Claude Code Monitor tool), one per worker, ~180s poll; each stdout line becomes an orchestrator notification. Dedupe per event type or they spam.

## Input channel (orchestrator → worker)

`tmux send-keys -t <window_id> -l "<msg>"` then `sleep 0.5` then `send-keys Enter`, then VERIFY submitted (~2s later, capture pane; text still in composer ⇒ bare Enter again). The 0.5s is load-bearing: composers paste-detect rapid bursts and treat same-cycle Enter as a newline. Steer shape: observed → why wrong → do instead → constraint.

The wiki app exposes the same channel: session-view composer → `POST /api/agents/<id>/message` (`mode: now` = immediate send-keys; `mode: on-idle` = queued in `/tmp/wiki-msg-queue.json`, backend dispatcher delivers when the pane shows no `esc to interrupt` spinner two polls running). Henry can steer orchestrators/workers from the browser.

## Sentinels (worker stdout, backup to status file)

- `MERGE-READY: <pr-url>` — PR open, CI green, review handled
- `BLOCKED: <one-line reason>` — includes `handoff-needed`, `plan-dispute`, `<hook> baseline`
- `PLAN-READY: <ticket>` (planning workers)
- Grep pitfall: the pane log contains the kickoff prompt, which quotes these strings — match against status file or live pane, not raw log, or exclude the echo region.

## Other state the orchestrator tracks (not files)

- **Worktrees**: `~/me/fun/phoebe/.codex/worktrees/<slug>` / `.claude/worktrees/<slug>` — worker-owned; survive handoffs (same worktree across sessions); removed after merge.
- **PR handoff comments**: durable cross-session memory for multi-session tickets (done / remaining / file map) — better than compaction.
- **Vault todo.md**: `In Progress` line names the owning window (`cdx:PHO-1234`); other sessions' ownership preflights grep tmux window names + worktrees + branches + open PRs.

## Registry (`/tmp/agent-registry.json`)

Push channel for the wiki `/agents` page — orchestrator announces workers instead of the app scraping tmux names:

```bash
wiki agent orch <ID> --window @120          # at ORCHESTRATOR session start; ID = short label ("phoebe")
wiki agent register <TICKET> --window @327 --kind cdx --role plan|implement|review --orch <ID> [--model --worktree --log]
wiki agent update <TICKET> [--orch --model --window ...]                # mutate live worker in place — NO handoff/session bump (re-register = handoff!)
wiki agent done <TICKET> --outcome merged|closed|plan-ready|abandoned   # at wrap-up, AFTER archiving
wiki agent outcome <TICKET> merged|closed|...                           # fix/mark outcome after the fact
wiki agent orch-done <ID>                    # at orchestrator session end
wiki agent list
```

- LIVE SHARED STATE IS READ-ONLY to workers (registry, ~/.wiki, ~/.codex*, ~/.claude, vault/, live dev stack, others' tmux windows) — isolation via env-overridden paths UP FRONT, never fixture-then-cleanup in live paths (4 leak incidents 2026-07; codified in both tmux-ticket skills). Gate reviewers check shared surfaces for pollution.
- Worker kickoff prompts MUST contain `ticket <ID>` (resolver regex `(?:Linear )?ticket ([A-Z]+-\d+)` scans the first user message; prompts that dropped the phrase broke claude transcript mapping 07-08 — claude workers spawned from repo root have no worktree-slug project dir, so kickoff scan is their ONLY mapping path).
- `orch` reads `$CLAUDE_CODE_SESSION_ID` + cwd → stores the EXACT transcript path (`~/.claude/projects/<escaped-cwd>/<session-id>.jsonl`); the wiki renders the orchestrator's own session from it. Must run from inside the orchestrator's Claude session (env var scope).
- In a detached tmux-launched Claude session, resolve the orchestrator window with `tmux display-message -p -t "$TMUX_PANE" '#{window_id}'` — plain `display-message -p '#{window_id}'` can bind to the last attached client window instead of the orchestrator pane.
- Brand-new project directories can stop on Claude's `Quick safety check` / `Yes, I trust this folder` prompt before the kickoff prompt runs; spawners need to detect that screen and press Enter once or self-registration never happens.
- Workers registered with `--orch <ID>` group under their orchestrator in the wiki; without it they land in "workers" (ungrouped). Multiple concurrent orchestrators = distinct IDs ("phoebe", "phoebe-2", "wiki").

- Re-registering a ticket = handoff: prior session auto-archived into `history` with `outcome: handoff` (plan→implement chains, multi-session respawns). Do NOT `done` between sessions.
- `codex resume` gotcha (REVISED 2026-07-08): `resume <explicit-id>` REUSES the original rollout file (verified via open file handles — no new file, no new id); `resume --last` can start a FRESH session (transcript split-brain). ALWAYS resume by explicit id, and after every resume/revival run `wiki agent update <T> --session <id>` — the resolver prefers the exact registry session id (exact file beats any same-cwd sibling; the old newest-same-cwd chain cross-bled tickets whose sessions shared a cwd, e.g. workers spawned at repo root). Worktree stays the discovery fallback for entries without a session id.
- Revival/respawn must target the worker's ORIGINAL tmux session: `tmux new-window -t <session>:` (capture `#{session_name}` from the old window BEFORE killing it), else windows pile into whatever session the reviver runs in (happened 07-08: watchdog + manual revivals dumped phoebe workers into the wiki session; `tmux move-window -s <wid> -t <session>:` repairs, window ids survive moves).
- Atomic writes (tmp+rename). `/tmp` lifecycle intentional — reboot kills tmux and registry together.
- Division of truth: registry = identity/metadata (window_id, kind, role, model, worktree, log path, session chain); status file = state (worker-written, unchanged); tmux liveness = health. The app renders: registry entry w/ dead window ⇒ "worker died?"; status file w/o registry entry ⇒ "unregistered" (skill drift flag).

## Archive (`~/me/fun/agent-archive/<TICKET>/<timestamp>/`)

Long-term record of every worker session, written at wrap-up AND at each multi-session handoff BEFORE the /tmp artifacts are deleted: kickoff prompt, pane log(s), final status JSON. Local-only and never pushed to a shared repo — pane logs can contain fetched prod data. Native CLI transcripts (richer: structured turns/tool calls) also persist independently in `~/.codex/sessions/` and `~/.claude/projects/`; the archive dir is the per-ticket index into a session's artifacts.

## Wrap-up (on merge/close)

Kill window → stop monitor → ARCHIVE prompt/logs/status to agent-archive → `wiki agent done <TICKET> --outcome …` → delete the /tmp copies → prune vault todo → done.md line → Linear state. No dead-window clutter.

ORDER MATTERS: archive BEFORE `agent done` — done persists `{outcome, ended_at, worker-registry snapshot}` into the newest archive dir's `meta.json` (the wiki `/agents` archived section reads it). Done without an archive dir = outcome lost (CLI warns). `wiki agent outcome <TICKET> merged` retro-fixes a missed one.
