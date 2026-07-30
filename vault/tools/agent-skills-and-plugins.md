---
type: reference
tags: [tools, claude, codex, skills]
created: 2026-07-06
updated: 2026-07-29
---

# Agent Skills & Plugins — Current State

Living inventory. **Update in place on any skill/plugin change.** Current state only, no changelog trail.

## Global Claude skills (`~/.claude/skills/`) — 15

| Skill | Purpose |
|---|---|
| wiki-vault | Vault read/write loop (this system) |
| handoff | Compact session → brief for fresh agent |
| codex-goal-loop | Codex `/goal` contracts + operation |
| folder-specific-claude-and-agents-md | Author folder-scoped AGENTS.md/CLAUDE.md |
| tmux-ticket-claude / tmux-ticket-codex | Spawn ticket worker in tmux window (cc:/cx: prefix) |
| tmux-ticket-pr-window-naming | Rename window to ticket/PR; never rename `thinker`. RECONSTRUCTED 2026-07-06 after accidental deletion — review pending |
| linear-ticket-to-pr | Ticket → worktree → PR → babysit pipeline |
| henry-review | Review in Henry's style |
| thermo-nuclear-code-quality-review | Max-severity review |
| prompt-polish | Prompt cleanup |
| karpathy-guidelines | Coding guidelines |
| admin-run-audit | Audit orchestrated agent runs |
| mastermind-merge-ready-loop | Autonomous merge-ready review loop |
| check-aliveness | Check worker/session health |

wiki-vault, handoff, codex-goal-loop, folder-specific adapted 2026-07-06 from [davidondrej/skills](https://github.com/davidondrej/skills) + original work.

## Codex skills (`~/.codex/skills/`) — 26

Synced ports (copies, NOT symlinks — edits on Claude side need manual re-copy): admin-run-audit, tmux-ticket-claude/codex, tmux-ticket-pr-window-naming, linear-ticket-to-pr, thermo-nuclear, wiki-vault, handoff, codex-goal-loop. Caveman ×7 removed 2026-07-29 (style retired, see Plugins).

Codex-native keepers: gh-fix-ci, gh-address-comments, pr-review-ci-fix, review, create-plan, issue-triage, sentry-triage, datadog-logs, langsmith-fetch, webapp-testing, codebase-migrate, changelog-generator, linear, mcp-builder, agent-deep-links, ponytail-review.

## Phoebe project skills (`.claude/skills/`) — ~54

Clusters: PR lifecycle (ship-pr, babysit-pr, own-pr, commit-push-pr, pushing-code, codex-pr-babysitter-automation), Codex (codex-cli, codex-tmux, cloud-agent-starter), review/quality, worktrees/tmux, testing ×7, observability (dd-*, sentry-cli, cloudwatch-logs, vlogs, braintrust, buildbuddy), voice/preview, DB, planning, misc. Repo-committed; changes go through phoebe PRs.

## Plugins

| Host | Plugin | Ships |
|---|---|---|
| Claude | claude-plugins-official | superpowers (14 skills), frontend-design |
| Claude | openai-codex | codex:* skills + codex-rescue agent |
| Claude + Codex | posthog | ~140 skills + MCP tools — flagged as biggest cleanse lever if unused; all-or-nothing (plugin skills can't be cherry-picked) |
| Codex | documents / presentations / spreadsheets | OpenAI doc bundles |
| Codex | rename-pane@personal | tmux helper |
| Codex | frontend-design | same as Claude side |

**Style retirement (2026-07-29, Henry):** `caveman` and `i-have-adhd` plugins disabled (Claude `settings.json` `enabledPlugins: false`), Codex caveman ×7 skills deleted. All agent prose — orchestrators AND workers — now follows **ASD-STE100 Simplified Technical English** per global `CLAUDE.md` / `AGENTS.md` (one word per idea, sentences ≤20 words, active voice, short paragraphs). Machine/team-facing artifacts (code, commits, PRs, tickets, docs, vault notes) keep normal conventions. Lowercase + no-emoji preferences unchanged. agent-config commit `b234fb4`.

## Cross-machine sync (2026-07-20)

Canonical portable copy lives in the private repo **github.com/hwang2409/agent-config** (local checkout `~/me/fun/config`). Contains `claude/skills/` (13), `codex/skills/` (22), global `CLAUDE.md`/`AGENTS.md`, `install.sh` (idempotent, copy-mode, conflict-safe on instruction files) and `sync.sh` (live → repo, commit+push). ASD-STE100 style switch + caveman removal at commit `b234fb4` (2026-07-29). Flow: edit live skills → `./sync.sh` → other laptop `git pull && ./install.sh`. Plugins/MCP/settings.json deliberately NOT synced (machine-specific); plugin list documented in the repo README. After any skill edit session, run sync.sh or the repo drifts.

## Cleanse log (2026-07-06)

- Removed: 14 stock OpenAI samples from `~/.codex/skills/`; source-command-* orphans from phoebe `.agents/skills/`.
- **Incident:** tmux-ticket-pr-window-naming deleted as "orphan" — actually invoked by 7 skills; reconstructed. Rule: grep for references before deleting any skill.

## Open items

See [[todo]] (tools entries). Also pending: phoebe builtin-dups (code-simplifier vs /simplify, code-debug-skill vs superpowers:systematic-debugging).
