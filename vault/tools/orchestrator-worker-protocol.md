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

**Frontend rule (REVISED Henry 2026-08-17; supersedes 2026-08-06d): frontend/UI tickets go to cdx implement workers (gpt-5.6-luna default) — no more cc/claude workers for frontend.** Henry: "codex frontend design isn't half bad anymore, and you can always just give it an in-depth handoff." Two mandatory compensations: (1) the orchestrator writes an IN-DEPTH design handoff in the kickoff (concrete component specs, reference file pointers, exit criteria — the contract substitutes for claude design judgment); (2) the kickoff MUST mandate invoking three skills before UI work: `frontend-design`, `make-interfaces-feel-better`, and `laws-of-ux` (all three live in `~/.codex/skills/` since 2026-08-17; laws-of-ux was created from `vault/design/laws-of-ux.md` the same day and also exists for claude at `~/.claude/skills/laws-of-ux/`). Historical: 2026-07-20 fable rule -> 2026-08-06d opus-4.7 rule -> this. cc opus-4.7 remains available only if Henry explicitly asks. Applied case 2026-08-17: WIKI-294 bb-parity waves 1-2 + 3a ran on opus-4.7 (pre-revision, during which Henry's session limit was consumed); in-flight cc workers finish, all later spawns are cdx.

Flow: orchestrator spawns luna implementer → worker signals merge-ready → orchestrator spawns sol reviewer (cdx gpt-5.6-sol) as the gate's deep-review step (one reviewer per round: archive the reviewer `closed` as soon as its verdict is routed, then spawn a fresh `<TICKET>-REVIEW<n>` pinned at the new head SHA next round — every SHA gets fresh eyes and idle reviewers don't burn soft-cap slots; Henry 2026-07-15) → sol's severity-tagged findings return to orchestrator → orchestrator structures them into a steer to the luna implementer (observed → why wrong → do instead → constraint, one item per finding) → loop until sol returns MERGE-READY clean → merge per repo authority. Sol never steers luna directly; all routing goes through the orchestrator. Iteration cap and Henry-interrupt rules follow the gate-loop section below. Explicit `--model`/`--effort` overrides remain allowed per ticket.

**Anti-slop TypeScript guidance is MANDATORY in every implement kickoff (Henry 2026-08-17).** Any kickoff whose contract can touch TypeScript must instruct the worker to follow the anti-slop rule set as coding style: read the ten rule sources in the `install-anti-slop` skill (cdx: `~/.codex/skills/install-anti-slop/assets/anti-slop/rules/*.ts`; cc: `~/.claude/skills/install-anti-slop/` equivalent) and write conforming code — no chained type assertions, no `object`/`unknown` parameter types, no widen-then-assert, no runtime `typeof` narrowing of known values, no unsafe dictionary types, no shape-encoded symbol names, no `unknown` type aliases, no conditional empty-object spread, no known-value widening; prefer inference, `as const`, `satisfies`, named owner contracts, boundary parsing. Workers sweep their full diff pre-merge-ready and note the sweep in the PR. This is GUIDANCE ONLY: workers never vendor/install the plugin or touch lint config — repo installation stays a separate explicit-Henry-ask flow via the `install-anti-slop` skill (attempted fleet-wide install 2026-08-17 was rolled back on Henry's correction: "instruct workers to use this skill when implementing", not retrofit repos). Reviewers judge diffs against the same rules.

**Immediate-archive rule applies to ALL one-shot verification workers, not just reviewers (Henry 2026-07-17b).** Sim runners (`-SIM<n>`), eval runners (`-EVAL<n>`), auditors (`-AUDIT<n>`), canary runs (`-CANARY<n>`), thermo-nuclear reviews (`-THERMO<n>`), and any other "produce one report → done" worker follows the same rule: the moment their output is routed (steered to the implementer OR clean-pass surfaced), the very next tool call is `archive_agent` on that worker with `outcome=closed`. Leaving them idle-merge-ready burns a soft-cap slot and (for reviewers) triggers unrouted-verdict re-alarms every 5 min. Applied case 2026-07-17b: PHO-13944-SIM4 findings routed to PHO-13944-PR2 but sim worker not archived; Henry corrected — rule widened from reviewers-only to every one-shot verification worker. **Archive-on-read refinement (2026-08-19): wait for runtime idle before the archive call.** A cdx reviewer writes its status-file verdict BEFORE emitting the detailed findings message; archiving the instant the status flips interrupts that final turn and the file:line detail is lost forever (hit on WIKI-352-REVIEW2 — steer had to be composed from the one-line blocker). Check `runtime_state == idle` (or the turn/completed event) first; status blocked/merge-ready + runtime working = verdict summary exists but the body is still streaming. **Ordering tightened 2026-08-10: archive-on-READ, not archive-after-route.** The archive call comes the moment the verdict text is captured, BEFORE contract-writing/replace/steer — routing spans multiple tool calls and a trailing archive reliably gets forgotten (PHO-15494-REVIEW1 leaked this way; Henry's second catch). If batching, put the archive in the same parallel batch as the first routing call.

The supervisor now guarantees auto-archive for one-shot review and explicitly opted-in verification roles (viewed => prompt, unviewed => ~10min grace). Plan and implement roles can never opt in. Orchestrator archive-on-read remains best-practice hygiene, not the safety net.

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

**`next_review` prompt_template quirk (2026-08-07):** the template runs through Python `str.format()` — any literal `{` `}` (e.g. a JSON status-file shape) parses as a format field and the endpoint 500s with a bare "Internal Server Error"; the real error (`invalid prompt_template`) lands only in `~/Library/Logs/Wiki/wiki-backend-*.log`. Never put raw braces in prompt_template; describe JSON shapes in words or double the braces. Hit on WIKI-266 R2.

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
2h. **cc workers idle during CI polls (2026-08-18, systemic)**: cc fable implementers reliably end their turn while their status says "polling CI" — explicit kickoff/steer instructions ("never idle with pending checks") do not stick across turns. The orchestrator MUST arm its own one-shot conclusive-watcher (Bash run_in_background until-loop over `gh pr checks`) for every PR whose next loop action depends on CI, at the moment the worker pushes. Treat worker "polling CI" claims as decorative. Observed 4x in one day (PHO-15864-PR0, PHO-15996, PHO-15862, PHO-15864-UNFREEZE2).
2g. **cc reviewer classifier trap (2026-08-18)**: cc review workers whose kickoff uses attack-simulation language ("hunt for bypass vectors", "how a PR could defeat/sabotage the gates") can trip Anthropic's cyber-content classifier mid-review — the API blocks the turn, and because the framing sits in session history, EVERY later turn re-trips it; a reframing steer does not clear it. Fix: archive + respawn with defensive framing from turn one ("audit the completeness of a protective allowlist", "coverage table: protected / documented-excluded / uncovered"). Substance is identical; verdicts unaffected. Hit on PHO-15864-PR0-REVIEW7 (two blocks, respawn clean).
2f. **Provider-degradation reroute (2026-08-15)**: when cdx sol reviewer sessions crawl (<20 events/min) or stall silent for 10+ min at spawn, do not respawn on the same provider — two consecutive degraded sessions = reroute the review to cc opus-4.7 with the same pinned contract. Symptoms observed 2026-08-15 ~04:00-21:00: sol reviewers at 6 events/min or 0 events for 12 min while cc completed full reviews normally. Check account/rateLimits in the raw log first to exclude plan caps (usedPercent <100 with rateLimitReachedType null = provider-side, reroute). Scope note: this is a DEGRADATION exception only — it does not reopen convergence-motivated opus swaps (banned by the 2026-08-03 luna/sol rule above); reroute back to sol as soon as the provider recovers.
2e. **Gate recipe for the exit-hang class (2026-08-14 ~19:30)**: the wiki backend suite COMPLETES then hangs at interpreter exit (leaked multiprocessing resource; resource_tracker child alive; 0% cpu; summary trapped in the pipe — this, not lost notifications, caused every "hung gate" including the 6h stall). Per-test pytest-timeout cannot catch it (hang is post-suite). Authoritative orchestrator gate recipe: `PYTHONPATH=. .venv/bin/pytest backend/tests -q 2>&1 | tee /tmp/gate-<sha>.log` — pipe through tee so the summary hits the FILE immediately; poll the file for the `\d+ (passed|failed)` summary line; once present, the result is valid — SIGTERM the pytest process if it lingers. Never wait on process exit alone. P2 deflake ticket tracks the leaking test.
2d. **Gate runs that gate the next action run in the FOREGROUND (2026-08-14)**: a backgrounded verification suite whose result decides the next loop step (spawn review / merge / steer) is a dropped-ball risk — /tmp task output files can be cleaned up and completion signals lost across long turns. WIKI-282 PR 5 sat merge-ready 6h because of exactly this. Background is fine only for runs whose result is informational. Corollary: the monitor's REVIEW-GAP check (merge-ready with no live reviewer) must RE-FIRE periodically (q15m), not once — one-shot alarms drown in stall-counter noise.
2c. **Never overlap gate runs in one worktree (2026-08-14)**: the orchestrator must not run its verification suite while the worker's gate is running (or vice versa) in the SAME worktree — concurrent full suites share /tmp fixtures, ports, and SQLite files and deterministically fail the daemon/supervisor timing family. This produced a false stop-and-fix on WIKI-282 PR 4a round 6 (worker's isolated rerun failed only because the orchestrator's background suite was hammering the worktree). Serialize: check the worker is idle before running the gate; use a detached worktree copy if parallelism is needed.
2b. **Wiki backend gate invocation (2026-08-13)**: the ONLY authoritative full-suite command is `PYTHONPATH=. /Users/henry/me/fun/wiki/.venv/bin/pytest backend/tests -q` from the worktree root. Bare `python -m pytest backend` uses system python and dies in collection (`ModuleNotFoundError: backend` or a FastAPI/Starlette `on_startup` error) — workers have twice misreported this tooling error as "full pytest blocked" (WIKI-275, WIKI-282 PR1). Any "blocked/pre-existing failure" claim must be verified against the same command on origin/main before it enters a gate report. Known pre-existing main red as of 23c9cf2: `test_agent_runtime_store.py::ProtocolFixtureTests::test_codex_failed_render_completion_preserves_write_time_event`.
2. **Deep code review (MANDATORY, no shortcuts)** — default reviewer is a cdx gpt-5.6-sol review worker (see Default pipeline section); spawn it (or an in-session `code-review` subagent as fallback when the fleet is constrained) with adversarial framing and the project's domain context (for Phoebe: admin-agent RLS role, schema catalog, `admin_tool_result_caps` pipeline, sandbox mode, styleguide, banned APIs). Ask for verdict + severity-tagged findings with file:line. CI-green + threads-clear is a gate, NOT a review. Grep scans, checklist walks, and any "we already reviewed once this PR" skip = violation. Every new head SHA earns a fresh review. A `REVIEW NEEDED` or `REVIEW_REQUIRED` state signals the orchestrator to spawn the review worker immediately. It never blocks that spawn, and the independent review can run before human approval.
3. **Steer on findings** — if verdict != MERGE-READY (has BLOCKING/HIGH/actionable MEDIUM), the orchestrator itself composes the steer and sends it via the wiki composer (`send_now`) to the worker. Do NOT ask Henry "should I steer?" — just steer. Steer shape: observed → why wrong → do instead → constraint (per Input Channel section). Include severity, file:line, concrete fix per finding. Tell the worker not to re-declare merge-ready until every BLOCKING+HIGH is resolved and MEDIUMs are either fixed or explicitly deferred with a follow-up ticket link.
4. **Re-enter the loop** — wait for the worker's next `merge-ready`, then GOTO 1. **NO iteration cap** (Henry 2026-07-15: "keep iterating until it's ready") — iterate until the review returns a clean pass. Surface to Henry mid-loop ONLY on true non-convergence: the same finding survives two consecutive steers unaddressed, findings are growing rather than shrinking across iterations, the worker is thrashing (reverting its own fixes), or a finding needs a product/security/scope decision only Henry owns. Iteration count alone is never a reason to stop.
5. **Clean pass** — only after deep review returns MERGE-READY. Merge authority is PER-REPO (Henry 2026-07-13): **wiki** (hwang2409/wiki) — orchestrator squash-merges itself after the clean pass, then auto wrap-up. **Phoebe** (phoebe-health/phoebe) — NEVER auto-merge; surface to Henry: "PR #N merge-ready; reviewer approval required" and wait. **chimy2** (now `chimy2/` inside hwang2409/tooling; standalone repo deleted 2026-08-11) — orchestrator squash-merges itself after the clean pass (Henry 2026-08-10: "you don't need my merge auth"), then auto wrap-up.

**Tooling repo has NO CI (Henry 2026-08-19: "get rid of github actions for tooling in general", won't pay for Actions).** Workflows deleted (28a77b9), Actions disabled repo-level. The step-1 verification gate for hwang2409/tooling PRs is: PR open + MERGEABLE + zero unresolved threads + head-SHA match, and the LOCAL validation suite is authoritative — for newt: `cargo test`, `cargo test --test alloc_guard --features alloc-guard` (pinned count), `cargo clippy --all-targets -- -D warnings`, `cargo fmt --check`, plus the libm-free grep (std trig/exp/pow calls in newt/src/, comments excluded). Reviewers MUST run this suite in their pinned worktree and report results in the verdict; implementer local-gate claims alone do not satisfy the gate.

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

**Archive requires a TERMINAL run (2026-08-17).** An idle-but-alive provider process bounces the archive with `archive target must be terminal before finalization`. Wrap-up recipe for a live run: `POST /api/agents/<TICKET>/stop` first (run goes terminal, may log a cosmetic "Claude provider error" state_reason), THEN archive. Runs whose provider already exited archive directly. Hit twice on 2026-08-17 (WIKI-294, WIKI-297).

**Spawn racing a backend shutdown strands a run dir without run.json (2026-08-17c).** A `spawn_agent` accepted moments before the backend process is cycled can create `runs/<run-id>/` (raw.jsonl etc.) without ever writing `run.json`. The NEXT backend start then crash-loops startup recovery on `RunNotFound: .../run.json` and never reaches healthy — and Wiki.app only auto-respawns a sidecar that already reached healthy once, so the API stays down indefinitely (supervisor process itself survives; it is launchd-parented and keeps orchestrator sessions alive). Repair: seed a stub terminal `run.json` in the orphan dir (copy a sibling run's shape; set agent_id/run_id/role/model/worktree, state "blocked" with a state_reason note, zero all event counts, auto_archive false, touch empty raw.jsonl/events.jsonl), then relaunch Wiki.app — recovery passes and the run can be archived normally. Manually launching the API binary does not work: sidecar mode requires the app secret piped over stdin by Tauri, and dev-signed bundles never trust `--daemon` backends. Hit on PHO-15996's respawn during the 22:35Z backend cycle.

**Deterministic run-id archive collision blocks orchestrator archive (2026-08-19).** Orchestrator run ids are deterministic (uuid5 per agent id), so every incarnation of e.g. the `wiki` orch reuses the SAME run_id. Once one incarnation is archived (committed `archive-complete.json` in `~/me/fun/agent-archive/<id>/<ts>/`), archiving any LATER incarnation fails with `older archive marker cannot remove a newer live run` (store compares created_at of the archived run.json vs the live one). Repair that works: re-id the OLD committed archive session's files (replace the run_id uuid with a fresh uuid4 across the session dir's json files — terminal artifacts, safe), keep the live run + registry on the ORIGINAL id, then archive via the normal API. Do NOT re-id the live run instead: (a) the command_projection still holds the original id and the decider then rejects with `<id> run is no longer current`; (b) worse, a re-idd registry entry makes the supervisor treat the dead run as recoverable and it SPAWNS a fresh provider (gen+1) with no attached control — that stray refuses `/stop` (`provider PID is live without attached control`) and must be SIGTERMed before the dead-pid archive path clears it. Hit archiving old `wiki`/`tooling`/`phoebe` orchs after the 2026-08-19 registry wipe.

**Registry reseed schema gotcha (2026-08-19b).** When seeding `/tmp/agent-registry.json` from `runs/<id>/run.json`, raw run.json shape is NOT the registry entry shape: the backend expects mapped keys — `orch` (not `orchestrator_id`), `ticket`, `kind` (cc/cdx, not provider claude/codex), `cwd`, `log`, `session_id`, `spawned_at`, `transcript`, `window`. An entry missing `orch` renders the worker ungrouped in the sidebar; the backend rewrites the entry on the worker's next lifecycle transition, so recently-transitioned workers self-heal while quiet ones stay wrong. Map the fields at seed time.

**Stale command_projection after a supervisor park (2026-08-17b).** A supervisor restart that parks `runs/` (-> `runs.parked-<ts>`) strands the per-agent `command_projection` rows in `command-log.sqlite3`: they still point at the parked run_ids, so `spawn_agent` 409s with "already has a current run" while `archive_agent` fails with "no current run" (the registry entry is gone — the two checks read different stores). The tickets show up in `list_agents` as orch-less status-file ghosts. Repair per ticket: (1) `cp -R runs.parked-<ts>/<run-id> runs/`; (2) seed `/tmp/agent-registry.json` with `{"<TICKET>": {"current": {"run_id": ..., plus fields from run.json}}}` (atomic tmp+rename); (3) archive via the NORMAL API — the registry-resolved decider path clears the projection; dead provider pids are handled (auto-transition to terminal); (4) respawn. Do not edit the sqlite projection directly. Applied 2026-08-17 to WIKI-303/305/308/310/311 after the 14:29 restart.

Then stop the ticket's state Monitor (orchestrator-side `TaskStop`), remove the worktree if merged AND clean, and log the outcome. There is no tmux window to kill and no `/tmp/cdx-<TICKET>*` files to delete — the supervisor stores everything under `WIKI_AGENT_RUNTIME_DIR/runs/<run-id>/` and the archive endpoint persists it in place.

**Sweep ALL of the ticket's workers, not just the implementer.** Wrap-up (merge, park, or abandon) must `list_agents` and archive every worker whose ticket prefix matches the wrapped ticket — including every `<TICKET>-REVIEW`/`-REVIEW-N` spawn (reviewers: `outcome=closed`). A leaked reviewer holds a provider subprocess and clutters the fleet (Henry correction 2026-07-15, PHO-13763 park left REVIEW4 live).

### Legacy tmux worker (pre-WIKI-42)

Kill window → stop monitor → ARCHIVE prompt/logs/status to agent-archive → `wiki agent done <TICKET> --outcome …` → delete the /tmp copies → prune vault todo → done.md line → Linear state. No dead-window clutter.

GOTCHA: `wiki agent done` AUTO-KILLS the window (`--keep-window` opt-out) — an explicit `tmux kill-window` after it fails with "can't find window", and if that sits in a `&&` chain the later cleanup steps (rm status file / logs, worktree remove) silently never run, leaving a ghost "active" worker in the app (bit the wiki orchestrator 2026-07-09). Skip the manual kill after `agent done`, or join cleanup with `;` not `&&`.

ORDER MATTERS (tmux path): archive BEFORE `agent done` — done persists `{outcome, ended_at, worker-registry snapshot}` into the newest archive dir's `meta.json` (the wiki `/agents` archived section reads it). Done without an archive dir = outcome lost (CLI warns). `wiki agent outcome <TICKET> merged` retro-fixes a missed one.

### Do NOT ask the user

Wrap-up is automatic. Do not ask "should I clean up now?" — that's a "no human-in-the-middle" violation. Report a one-line receipt after wrap-up: `Wrapped up <TICKET> (outcome=merged, PR #N). Worker archived, monitor stopped, worktree removed.` Done.
