---
type: reference
tags: [tools, claude, codex, skills]
created: 2026-07-06
updated: 2026-07-06
---

# Agent Skills & Plugins — Current State

Living inventory of Henry's Claude Code and Codex skills/plugins. **Agents: update this note in place whenever skills or plugins are added, removed, or restructured.** No changelog trail — current state only, `updated` bumped.

## Global Claude skills (`~/.claude/skills/`)

| Skill | Purpose |
|---|---|
| wiki-vault | Vault read/write loop (this system) |
| handoff | Compact session → brief for fresh agent |
| codex-goal-loop | Codex `/goal` contracts + operation |
| folder-specific-claude-and-agents-md | Author folder-scoped AGENTS.md/CLAUDE.md |
| tmux-ticket-claude / tmux-ticket-codex | Spawn ticket worker in tmux window (cc:/cx: prefix) |
| tmux-ticket-pr-window-naming | Rename window to ticket/PR; never rename `thinker`. RECONSTRUCTED 2026-07-06 after accidental deletion — review pending |
| linear-ticket-to-pr | Ticket → worktree → PR → babysit pipeline |
| make-interfaces-feel-better | UI polish (symlink into phoebe `.agents/skills` — broken outside phoebe) |
| henry-review | Review in Henry's style |
| thermo-nuclear-code-quality-review | Max-severity review |
| prompt-polish | Prompt cleanup |
| karpathy-guidelines | Coding guidelines |

wiki-vault, handoff, codex-goal-loop, folder-specific adapted 2026-07-06 from [davidondrej/skills](https://github.com/davidondrej/skills) + original work.

## Codex skills (`~/.codex/skills/`) — 29

Synced ports (copies, NOT symlinks — edits on Claude side need manual re-copy): caveman ×5, tmux-ticket-claude/codex, tmux-ticket-pr-window-naming, linear-ticket-to-pr, thermo-nuclear, wiki-vault, handoff, codex-goal-loop.

Codex-native keepers: gh-fix-ci, gh-address-comments, pr-review-ci-fix, review, create-plan, issue-triage, sentry-triage, datadog-logs, langsmith-fetch, webapp-testing, codebase-migrate, changelog-generator, linear, mcp-builder, agent-deep-links, ponytail-review.

## Phoebe project skills (`.claude/skills/` — ~54)

Clusters: PR lifecycle (ship-pr, babysit-pr, own-pr, commit-push-pr, pushing-code, codex-pr-babysitter-automation), Codex (codex-cli, codex-tmux, cloud-agent-starter), review/quality, worktrees/tmux, testing ×7, observability (dd-*, sentry-cli, cloudwatch-logs, vlogs, braintrust, buildbuddy), voice/preview, DB, planning, misc. Repo-committed; changes go through phoebe PRs.

## Plugins

| Host | Plugin | Ships |
|---|---|---|
| Claude | caveman | 7 skills + 3 cavecrew agents + mode hooks |
| Claude | claude-plugins-official | superpowers (14 skills), frontend-design |
| Claude | openai-codex | codex:* skills + codex-rescue agent |
| Claude + Codex | posthog | ~140 skills + MCP tools — flagged as biggest cleanse lever if unused; all-or-nothing (plugin skills can't be cherry-picked) |
| Codex | documents / presentations / spreadsheets | OpenAI doc bundles |
| Codex | rename-pane@personal | tmux helper |
| Codex | frontend-design | same as Claude side |

## Cleanse log (2026-07-06)

- Nuked from `~/.codex/skills/`: 14 stock OpenAI samples (brand-guidelines, canvas-design, theme-factory, vercel-design-×2, spreadsheet-formula-helper, email-draft-polish, meeting-notes-and-actions, internal-comms, notion-* ×4, support-ticket-triage).
- Nuked phoebe `.agents/skills/` orphans: source-command-own-pr/-start-dev/-stop-dev (not git-tracked).
- **Incident:** tmux-ticket-pr-window-naming wrongly deleted as "orphan" — actually invoked by 7 skills. Reconstructed from call sites. Rule since: grep for references before deleting any skill.

## Open items

- PR-lifecycle six-pack → consolidate to ~2 (ship-pr + linear-ticket-to-pr) — not done.
- Review trio (henry-review / thermo-nuclear / builtin code-review) → pick one — not done.
- Builtin-dups in phoebe (code-simplifier vs /simplify, code-debug-skill vs superpowers:systematic-debugging) — not done.
- posthog plugin keep/kill decision — pending Henry.
- make-interfaces-feel-better symlink broken outside phoebe — fix or move.
- Claude↔Codex skill sync is manual copy — symlink option offered, undecided.
