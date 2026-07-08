---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-07
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **phoebe admin-agent tools arc SHIPPED 2026-07-07**: five merges in one day — PHO-13142 charts (#10722), PHO-13160 Slack-link fixes (#10749), PHO-13165 email hardening (#10754), PHO-13146 python snippets + search (#10734), PHO-13153 read/slice/diff (#10740). Run-data verb family complete: read/search/slice/diff/snippet. All live in staging — dogfood candidate. Details [[admin-agent]].
- **PHO-13157 survey capture (#10751)**: merge-ready, all gates passed, HELD for Henry's team-lead review (PR comment marks the hold; worker in watch mode, window @373).
- **PHO-13138 Phase 2 admin tables (#10716)**: PR1 terraform-parked (needs staging+prod apply of admin_agent_service secret wiring; staging drift on admin.phoebe-staging.dev accepted, do-not-touch). 8-PR series continues after. Worker @369.
- **wiki app**: transcript resolver hardened — codex resume writes NEW rollouts invisible to kickoff scan; resolver now falls back to worktree-cwd slug + newest mtime (self-heals resumes). `--worktree` at register is MANDATORY (resolution key). Resurrection flow codified in both tmux-ticket skills (synced 180e00d). Protocol [[orchestrator-worker-protocol]].

## Recent facts

- Next admin-agent picks per Henry discussion: PHO-13074 (inspect_codebase_wiki silent failures), PHO-11274/75 (trace/prompt diffing — pairs with new diff verb), PHO-12425/26 (sandboxed outreach dogfood), PHO-11727 (nested subagents). Auth gap still needs a deliberate ticket.
- Archive dir names MUST be `YYYYMMDD-HHMMSS` (wiki CLI regex) — `T` separator silently breaks outcome persistence.
- Wrap-up order proven: archive → `wiki agent done --outcome` → delete /tmp → todo complete → log-done.
- Snippet sandbox design ratified by Henry: v0 subprocess right for threat model; evolution seams = network isolation + third-party packages → both route to admin_code_sandbox VM.

## Watchouts

- NEVER `pkill -f` on substrings that appear in worker prompt text (e.g. 'bazel') — argv matching killed the whole codex fleet once (2026-07-07); match server binary paths instead.
- Two-dot `git diff main..HEAD` on a behind-main branch shows main's commits as "reverts" — false blocker in reviews; squash-merge uses merge-base, untouched files are safe. Verify with `gh pr diff --name-only`.
- Bazel cache was fully purged 2026-07-07 — first builds cold everywhere.
- Agents rewrite todo.md/map.md concurrently — refetch before line surgery.
- Codex/Claude JSONL formats are unversioned internals — wiki parsers drift with CLI updates.
