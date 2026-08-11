---
type: reference
tags: [tools, agents, tmux]
created: 2026-07-07
updated: 2026-08-11
---

# Orchestrator ↔ Worker Protocol (file/tmux schema)

Machine-readable contract between the mastermind orchestrator session and tmux worker sessions. Canonical behavior lives in the `tmux-ticket-codex` / `tmux-ticket-claude` skills (synced via github.com/hwang2409/agent-config); this note is the SCHEMA — what exists on disk/tmux and who reads/writes it. Written for a future "agents" page in the wiki app to render live state from these files.

## Default role→model pipeline (Henry 2026-07-17)

Unless a ticket or Henry specifies otherwise:

| Role | Runtime | Model |
|---|---|---|
| Orchestrator | cc | claude-fable-5 |
| Implement worker | cdx | gpt-5.6-luna |
| Review worker | cdx | gpt-5.6-sol |

**Reviewer reverted (Henry 2026-08-06b): review workers back to cdx gpt-5.6-sol.** Fable-5 is now orchestrator-only ("fable should just be orchestrators"). The 2026-08-06a fable-reviewer swap is undone. Applies to all repos/orchestrators unless a ticket overrides.

**Skip deep-review for pure UI/polish wiki tickets (Henry 2026-08-06c).** For wiki PRs that only touch CSS / DOM chrome / layout / typography / hover states / small rearrangement / playwright test-flips that mirror the UI change: do NOT spawn a `<TICKET>-REVIEW<n>` worker. Verify the gate (mergeable + threads + head SHA) and orch-merge on green. Henry: "reviews aren't really needed for things like this... it's only the logic that really matters. focus on auditing good software practices for the backend." Keep review for: logic, backend, API, migrations, protocol changes, and UI tickets that carry non-trivial state machines / resize math / a11y-critical widgets / new architectural seams. Does NOT apply to Phoebe (always review + never auto-merge).

**claude-fable-5 is OFF-PLAN (Henry 2026-07-17)** — requires paid Anthropic credits, spawns immediately blocked "Usage credits are required for this model." Never use as default for orchestrator or workers. Only pick fable-5 if Henry explicitly asks and accepts the credit charge.

**Luna/sol for ALL tasks, no opus swaps (Henry 2026-08-03)** — codex usage restored; the default pipeline above applies to every task, including frontend and non-converging review loops. Do not swap an implementer to cc opus-4.7 for convergence — Henry explicitly revoked that pattern ("we don't need to use opus-4.7 for convergence, or any tasks"). Applied case: PHO-15082 round-3 opus spawn replaced with cdx luna on Henry correction. This supersedes the wiki-orch "opus swaps converged deep loops" lesson and the 07-20 frontend fable/opus rule while codex quota is healthy.

**Frontend rule (Henry 2026-07-20, model updated 2026-08-06d): any ticket with frontend/UI changes gets a cc implement worker, and its kickoff prompt must mandate invoking the `frontend-design` skill (/frontend-design) before UI work** (design-polish tickets also add /make-interfaces-feel-better per Henry 2026-07-21). **Model is cc opus-4.7, NOT fable-5 (Henry 2026-08-06d: "for frontend design tickets, we should actually just opus-4.7 instead, fable is too expensive").** Fable stays orchestrator-only; a detailed kickoff ticket/prompt from the orchestrator substitutes for fable's design judgment. Applied case 2026-08-06d: WIKI-258/259/260 fable implementers replaced mid-flight with opus-4.7; WIKI-261 spawned directly on opus-4.7.

Flow: orchestrator spawns luna implementer → worker signals merge-ready → orchestrator spawns sol reviewer (cdx gpt-5.6-sol) as the gate's deep-review step (one reviewer per round: archive the reviewer `closed` as soon as its verdict is routed, then spawn a fresh `<TICKET>-REVIEW<n>` pinned at the new head SHA next round — every SHA gets fresh eyes and idle reviewers don't burn soft-cap slots; Henry 2026-07-15) → sol's severity-tagged findings return to orchestrator → orchestrator structures them into a steer to the luna implementer (observed → why wrong → do instead → constraint, one item per finding) → loop until sol returns MERGE-READY clean → merge per repo authority. Sol never steers luna directly; all routing goes through the orchestrator. Iteration cap and Henry-interrupt rules follow the gate-loop section below. Explicit `--model`/`--effort` overrides remain allowed per ticket.

**Immediate-archive rule applies to ALL one-shot verification workers, not just reviewers (Henry 2026-07-17b).** Sim runners (`-SIM<n>`), eval runners (`-EVAL<n>`), auditors (`-AUDIT<n>`), canary runs (`-CANARY<n>`), thermo-nuclear reviews (`-THERMO<n>`), and any other "produce one report → done" worker follows the same rule: the moment their output is routed (steered to the implementer OR clean-pass surfaced), the very next tool call is `archive_agent` on that worker with `outcome=closed`. Leaving them idle-merge-ready burns a soft-cap slot and (for reviewers) triggers unrouted-verdict re-alarms every 5 min. Applied case 2026-07-17b: PHO-13944-SIM4 findings routed to PHO-13944-PR2 but sim worker not archived; Henry corrected — rule widened from reviewers-only to every one-shot verification worker.

The supervisor now guarantees one-shot auto-archive (viewed => prompt, unviewed => ~10min grace); orchestrator archive-on-read remains best-practice hygiene, not the safety net.

## Worker lifecycle doctrine (Henry 2026-08-06e)

Henry's model of the optimal Wiki workflow, locked as canon:

- **Orchestrators are the ONLY long-running sessions.** Everything else is bounded.
- **Workers are one-shot contract executors.** They receive a contract (kickoff prompt), complete it, signal, and are archived. Workers do NOT do anything expensive: no spawning subagents, no fan-out, no side quests, no scope expansion beyond the contract. Kickoff prompts must state the no-subagents rule explicitly — implicit was not enough.
- **Default loop shape: worker1 → reviewer1 → worker2 (fresh, with fix contract) → reviewer2 → ... .** When a review verdict has findings, the default is to archive the implementer and spawn a FRESH implementer with a compact fix contract (findings compressed to observed → why wrong → do instead → constraint). Fresh workers carry no stale context, and writing the contract forces the orchestrator to actually digest the findings.
- **Steering escape hatch (Henry-approved 2026-08-06e): the orchestrator MAY steer the live implementer instead of replacing it when BOTH hold: (a) the fix contract is small, and (b) the worker's in-flight state — root-cause understanding, debugging context, a built mental model — is the expensive part to rebuild.** Example: a multi-round root-cause hunt like WIKI-259's virtualization bug, where a fresh worker would re-pay full ramp-up for a two-line fix list. Replacement stays the default; steering is the exception and is an orchestrator judgment call, never a worker request.

## Autonomy invariant (Henry 2026-07-17)

**The orchestrator is autonomous. The only human checkpoint is merge authorization.** Every intermediate step — spawn, steer, respawn, sim, review, eval, audit, thermo, rerun — is orchestrator action, taken without confirming with Henry.

If a spawned worker (sim, review, audit, eval, canary, thermo) returns a NO-GO / findings / regression, the very next tool call is a `steer_agent` on the implementer (or a fresh `spawn_agent` if the implementer was archived) with the findings structured as fix items. **Never a status message to Henry.** Reporting the finding to Henry and waiting for him to say "steer them to fix this" is the exact violation this invariant was created to stop.

Applied loop (2026-07-17 PHO-13944-PR2 case):
- PHO-13944-SIM3 returned NO-GO with 3 defects (output-cap truncation, intra-pass semantic dedupe, input-token overshoot).
- WRONG: report "sim3 no-go" to Henry, wait for direction.
- RIGHT: respawn PHO-13944-PR2 implementer with the sim findings as a structured fix steer (observed → why wrong → do instead → constraint), let it push fixes, spawn PHO-13944-SIM4 on the new head SHA, loop.

Only stop the loop for:
1. Merge authorization (surface: "PR #N clean, ready for your merge auth"). NEVER self-merge.
2. Product/scope/security decision only Henry can own.
3. Iteration cap (3+ consecutive review rounds with no net progress).
4. Blocker the worker literally cannot resolve (missing credential, external outage, external decision).

Sim/eval/audit outputs are DRIVERS, not FYIs. Reading the report is the middle of the loop; steering the implementer is the next step. See `~/.claude/skills/mastermind-merge-ready-loop/SKILL.md` §"Autonomy invariant" for the loop-skill mirror of this rule.

## Output rendering convention (Henry 2026-08-06)

Agents choose how their output renders by DECLARING it in the markdown they emit. Workers and orchestrators MUST tag code fences with a language (```python, ```bash, ```json, ...) — never bare ``` fences — so the wiki transcript renders them highlighted. Tables use markdown table syntax. Rich artifacts (diagrams, plots) go through `render_artifact` where available. Kickoff prompts should carry this line for cc/cdx workers whose output lands in transcripts.

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

**Monitors are the PRIMARY wake signal.** `ScheduleWakeup`/cron ticks are a fallback heartbeat only (idle at 1200–1800s) — never the path a merge-ready/blocked transition travels. Orchestrator that drives iteration cadence off timed ticks instead of state-transition monitors is broken by construction: it misses fast transitions, burns tokens, and drifts against the "watchlist file + re-alarms" doctrine (`hot.md`). If you catch yourself scheduling a short wakeup to re-check `merge-ready`, stop — arm the Monitor instead.

**Foreground-wait ban (Henry 2026-08-07): orchestrators must stay available for conversation.** Before worker, CI, review, eval, or deployment work can outlast one tool call, arm a persistent background monitor or supervisor autopilot and verify its first state. Then return control to Henry. Never occupy the session with repeated `sleep`, `gh pr checks --watch`, short polling calls, or manual 15–180 second wait loops. Direct checks are only for monitor setup, a monitor signal, the silence backstop, or an explicit status request. If the preferred monitor fails, install a working background fallback; do not resume foreground polling.

**Monitor construction rules (MANDATORY — a merge-ready sat undetected 21min on 2026-07-15 because of rule 1):**

1. **Extract fields, never truncate raw JSON.** `wiki agent status` serializes keys alphabetically — `state`/`step`/`status_age_seconds` land PAST character 400. Any `cut -c1-N` / `head -c` on the raw JSON silently blinds the monitor to state transitions while early fields (`blocker`, `pr`) still produce events, so the monitor LOOKS alive. Always parse: `python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('state'),'|',d.get('step'),'|',d.get('pr'),'|',d.get('blocker'))"`.
2. **Change-detection must include the `state` field specifically.** A monitor that cannot distinguish `working`→`merge-ready` is not a monitor.
3. **Arm-time self-verify.** After starting a monitor, confirm the initial-state event arrives AND the printed line contains the `state` value. No state in the first event ⇒ extraction broken ⇒ fix before relying on it.
4. **Every live worker has exactly one state monitor at all times.** Spawn ⇒ arm; archive/replace ⇒ stop + re-arm. A worker without a monitor is invisible; silence is not "still working".
5. **Silence backstop:** if a monitor produces no event for a long stretch while a PR is open, do a one-shot direct status read instead of trusting silence — GitHub ground truth (checks green + threads clear) overrides a stale/blind monitor per the signal-priority list above.
6. **Watch `runtime_state`, not just the status file.** The worker-written status file says `working` while the provider run sits in `waiting-approval` (codex elicitation/approval prompts — e.g. the "install GitHub plugin?" tool suggestion stalled two workers 50-75 min on 2026-07-15). Monitors must print `state / runtime_state`; on `waiting-approval`, read the pending request from the run's raw.jsonl (`grep -i approval`/`elicitation`) and answer it via `POST /api/agents/<id>/respond` with `{"request_id": <id from the request payload>, "response": {"action": "decline"}}` (or accept when genuinely wanted). Plugin/tool-install suggestions: decline — workers use `gh` CLI. Kickoff prompts should pre-empt: workers decline install suggestions themselves.

**`next_review` gate quirk (2026-08-02):** `expected_sha` must be the FULL 40-char SHA. A short SHA returns `{"status":"gate_failed","detail":"not-mergeable"}` even when GitHub reports MERGEABLE/CLEAN — misleading detail text. Reproduced twice (WIKI-234 R2, WIKI-232 R9); full-SHA retry with a fresh request_id spawned cleanly both times.

## Input channel (orchestrator → worker)

`tmux send-keys -t <window_id> -l "<msg>"` then `sleep 0.5` then `send-keys Enter`, then VERIFY submitted (~2s later, capture pane; text still in composer ⇒ bare Enter again). The 0.5s is load-bearing: composers paste-detect rapid bursts and treat same-cycle Enter as a newline. Steer shape: observed → why wrong → do instead → constraint.

**Fleet monitoring discipline (Henry 2026-07-16): one persistent watchlist-driven monitor, never rebuilt.** Per-spawn monitor rebuilds re-emit current states as fake events, training the orchestrator to dismiss real transitions — that is how stale workers get missed. Instead: the monitor reads its ticket set from a watchlist file (`/tmp/agent-status/.<orch>-watchlist`) each cycle; spawns/archives update the FILE only. Required detectors: (1) state transitions + blockers; (2) unrouted-verdict re-alarm — a live reviewer whose step contains a MERGE-READY/NOT-MERGE-READY verdict re-alarms every 5 min until archived, so deferred routing self-corrects; (3) review-gap alarm — implement worker at merge-ready >5 min with no live reviewer for its ticket, re-alarm q10m; (4) staleness probe — state=working with status file silent 30+ min → verify runtime via read_agent. Verdict routing preempts all other orchestration work in a turn.

**Spawn discipline — every worker MUST be on the watchlist before the spawn is "done" (Henry 2026-07-17).** Every `spawn_agent` / `replace_agent` call must be followed, in the same tool batch and before surfacing to Henry, by an append to `/tmp/agent-status/.<orch>-watchlist`. Every `archive_agent` must be followed by removing the line. This applies to implementers AND reviewers — a `-REVIEW<n>` sibling is a first-class watchlist row and drives the unrouted-verdict + review-gap detectors. A worker that isn't on the watchlist is invisible to the fleet monitor; silence looks identical to "still working" and the loop stalls silently. Applied case 2026-07-17: PHO-14003 + PHO-13944-SIM3 spawned without watchlist adds → fleet ran without state-change detection until Henry asked "did you set up monitors?" — codified here so future spawns can't skip it.

**Steer mode discipline (Henry 2026-07-16): default `mode: now`.** `on-idle` queues the message until the worker’s current turn ends — for a worker mid-suite or mid-implementation that can be an hour later, which defeats the point of steering. Use `now` for anything meant to influence work in progress (policy changes, review findings, stop-doing-X, rebase-before-push). Reserve `on-idle` ONLY for messages that are genuinely next-task input and harmless if delayed. If a message was mistakenly queued on-idle, resend as `now` with a "supersedes the queued copy" note so the duplicate is ignored.

The wiki app exposes the same channel: session-view composer → `POST /api/agents/<id>/message` (`mode: now` = immediate send-keys; `mode: on-idle` = queued in `/tmp/wiki-msg-queue.json`, backend dispatcher delivers when the pane shows no `esc to interrupt` spinner two polls running). Henry can steer orchestrators/workers from the browser.

## Sentinels (worker stdout, backup to status file)

- `MERGE-READY: <pr-url>` — PR open, CI green, review handled
- `BLOCKED: <one-line reason>` — includes `handoff-needed`, `plan-dispute`, `<hook> baseline`
- `PLAN-READY: <ticket>` (planning workers)
- Grep pitfall: the pane log contains the kickoff prompt, which quotes these strings — match against status file or live pane, not raw log, or exclude the echo region.

## Other state the orchestrator tracks (not files)

- **Worktrees**: `~/me/fun/phoebe/.codex/worktrees/<slug>` / `.claude/worktrees/<slug>` — worker-owned; survive handoffs (same worktree across sessions); removed after merge.
- **No stray dirs directly under `~/me/fun/`** (Henry, 2026-08-02): worktrees and worker output must NEVER land at `~/me/fun/<ticket-or-branch>/`. Observed failure mode: a worker in `.worktrees/<name>` mis-resolves a path and writes `~/me/fun/<name>/services/...`, leaving an orphan file tree Henry has to look at. Orchestrators sweep for these; delete a stray once no active agent's ticket relates to it, and if an active worker's ticket matches the dir name, wait until that worker wraps before deleting.
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

## Merge-ready gate loop (autonomous — do not human-in-the-middle)

When a worker signals `merge-ready` (status file state OR `MERGE-READY: <pr-url>` sentinel), the orchestrator runs a FIXED autonomous loop and does NOT pause for Henry between iterations. Only surface at final clean pass or a true blocker Henry alone can decide.

Loop:

1. **Verification gate** — `gh pr view` + `gh pr checks` + reviewThreads GraphQL. Verify: PR open + not draft, `mergeable=MERGEABLE`, all non-skipped checks pass, `reviewThreads` with `isResolved==false` count is 0, head SHA matches worker's claimed SHA. Review-bot threads (Bugbot, Greptile, Codex, Cursor, and Devin `devin-ai-integration[bot]`) count toward the zero-unresolved requirement; Devin comments are ALWAYS addressed — fix or reasoned reply — and their threads resolved either way (Henry 2026-08-06). The babysit-pr watcher allowlist includes Devin, but the gate still audits threads directly.
2a. **Bazel repos — gate suite reruns bypass the shared test cache**: `bazel test --cache_test_results=no <targets>`. The user-level `~/.bazelrc` shares a content-addressed disk cache (`~/.cache/bazel-disk`) across all worktrees/workers — an explicit, accepted exception to worker shared-state isolation (content-addressing prevents accidental cross-pollution). Workers iterate WITH the cache; the gate forces real execution because cached `PASSED` masks timing flakes and defeats the 2x back-to-back reproducibility check.
2. **Deep code review (MANDATORY, no shortcuts)** — default reviewer is a cdx gpt-5.6-sol review worker (see Default pipeline section); spawn it (or an in-session `code-review` subagent as fallback when the fleet is constrained) with adversarial framing and the project's domain context (for Phoebe: admin-agent RLS role, schema catalog, `admin_tool_result_caps` pipeline, sandbox mode, styleguide, banned APIs). Ask for verdict + severity-tagged findings with file:line. CI-green + threads-clear is a gate, NOT a review. Grep scans, checklist walks, and any "we already reviewed once this PR" skip = violation. Every new head SHA earns a fresh review. A `REVIEW NEEDED` or `REVIEW_REQUIRED` state signals the orchestrator to spawn the review worker immediately. It never blocks that spawn, and the independent review can run before human approval.
3. **Steer on findings** — if verdict != MERGE-READY (has BLOCKING/HIGH/actionable MEDIUM), the orchestrator itself composes the steer and sends it via the wiki composer (`send_now`) to the worker. Do NOT ask Henry "should I steer?" — just steer. Steer shape: observed → why wrong → do instead → constraint (per Input Channel section). Include severity, file:line, concrete fix per finding. Tell the worker not to re-declare merge-ready until every BLOCKING+HIGH is resolved and MEDIUMs are either fixed or explicitly deferred with a follow-up ticket link.
4. **Re-enter the loop** — wait for the worker's next `merge-ready`, then GOTO 1. **NO iteration cap** (Henry 2026-07-15: "keep iterating until it's ready") — iterate until the review returns a clean pass. Surface to Henry mid-loop ONLY on true non-convergence: the same finding survives two consecutive steers unaddressed, findings are growing rather than shrinking across iterations, the worker is thrashing (reverting its own fixes), or a finding needs a product/security/scope decision only Henry owns. Iteration count alone is never a reason to stop.
5. **Clean pass** — only after deep review returns MERGE-READY. Merge authority is PER-REPO (Henry 2026-07-13): **wiki** (hwang2409/wiki) — orchestrator squash-merges itself after the clean pass, then auto wrap-up. **Phoebe** (phoebe-health/phoebe) — NEVER auto-merge; surface to Henry: "PR #N merge-ready; reviewer approval required" and wait.

Categorically not-Henry-interruptions in this loop: "should I run the review", "should I send the steer", per-iteration finding reports. Categorically Henry-interruptions: final clean pass, product/security/scope decisions only Henry can own, true non-convergence as defined in step 4, merge authorization.

This applies to ALL orchestrator sessions (mastermind, wiki, website) and any provider (cc, cdx). Codified after PR #11192 (PHO-13554) 2026-07-13, a repeat of the #10608 incident from 2026-07-06.

## Wrap-up (on merge/close)

The orchestrator triggers wrap-up AUTONOMOUSLY on detection of PR terminal state (MERGED or CLOSED), NOT on user prompt. This is a distinct step from the "clean pass, merge-ready reported" surface — after surfacing, keep monitoring PR state (`gh pr view --json state,mergedAt,mergeCommit,closedAt` every ~60-180s alongside the state monitor) and fire wrap-up on the transition. Failing to auto-wrap-up leaves ghost "active" workers in the wiki `/agents` view and burns provider subprocess capacity (bit mastermind 2026-07-13 on PHO-13556: PR merged, worker sat idle registered as `working` until Henry asked "why didn't you kill the worker").

### Headless supervisor (WIKI-42+, current default)

Archive endpoint atomically archives raw+normalized events, persists outcome + registry snapshot, releases the provider subprocess:

```bash
BASE="http://127.0.0.1:${WIKI_BACKEND_PORT:-8213}"
curl -sS -X POST "$BASE/api/agents/<TICKET>/archive" \
  -H "Content-Type: application/json" \
  -d '{"outcome":"merged"}'   # or "closed" | "abandoned"
```

Then stop the ticket's state Monitor (orchestrator-side `TaskStop`), remove the worktree if merged AND clean, and log the outcome. There is no tmux window to kill and no `/tmp/cdx-<TICKET>*` files to delete — the supervisor stores everything under `WIKI_AGENT_RUNTIME_DIR/runs/<run-id>/` and the archive endpoint persists it in place.

**Sweep ALL of the ticket's workers, not just the implementer.** Wrap-up (merge, park, or abandon) must `list_agents` and archive every worker whose ticket prefix matches the wrapped ticket — including every `<TICKET>-REVIEW`/`-REVIEW-N` spawn (reviewers: `outcome=closed`). A leaked reviewer holds a provider subprocess and clutters the fleet (Henry correction 2026-07-15, PHO-13763 park left REVIEW4 live).

### Legacy tmux worker (pre-WIKI-42)

Kill window → stop monitor → ARCHIVE prompt/logs/status to agent-archive → `wiki agent done <TICKET> --outcome …` → delete the /tmp copies → prune vault todo → done.md line → Linear state. No dead-window clutter.

GOTCHA: `wiki agent done` AUTO-KILLS the window (`--keep-window` opt-out) — an explicit `tmux kill-window` after it fails with "can't find window", and if that sits in a `&&` chain the later cleanup steps (rm status file / logs, worktree remove) silently never run, leaving a ghost "active" worker in the app (bit the wiki orchestrator 2026-07-09). Skip the manual kill after `agent done`, or join cleanup with `;` not `&&`.

ORDER MATTERS (tmux path): archive BEFORE `agent done` — done persists `{outcome, ended_at, worker-registry snapshot}` into the newest archive dir's `meta.json` (the wiki `/agents` archived section reads it). Done without an archive dir = outcome lost (CLI warns). `wiki agent outcome <TICKET> merged` retro-fixes a missed one.

### Do NOT ask the user

Wrap-up is automatic. Do not ask "should I clean up now?" — that's a "no human-in-the-middle" violation. Report a one-line receipt after wrap-up: `Wrapped up <TICKET> (outcome=merged, PR #N). Worker archived, monitor stopped, worktree removed.` Done.
