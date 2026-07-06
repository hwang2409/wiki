---
type: reference
view: kanban
tags: [todo]
created: 2026-07-06
updated: 2026-07-06
---

Todo:

- [P1] admin-agent: finish native GitHub App/API setup for PR workflows — credentials, installation tokens, bounded PR create/comment/review; no merge authority ([PHO-12306](https://linear.app/phoebework/issue/PHO-12306))
- [P1] phoebe: rerun full 10475 bank evals on cleaned fixtures once fix branch lands ([#10475](https://github.com/phoebe-health/phoebe/pull/10475))
- [P1] tools: review reconstructed tmux-ticket-pr-window-naming skill for divergence from original

In Progress:

- [P1] admin-agent: core accounts API — owner filter, pagination, projection ([PHO-12980](https://linear.app/phoebework/issue/PHO-12980)) — cdx:PHO-12980 worker
- [P1] admin-agent: type-aware tool-output formatting ([PHO-12937](https://linear.app/phoebework/issue/PHO-12937)) — cdx:PHO-12937 worker
- [P0] admin-agent: build code-sandbox tool surface, exe.dev first backend ([PHO-13073](https://linear.app/phoebework/issue/PHO-13073)) — cdx:PHO-13073 worker
- [P1] admin-agent: exe.dev pilot — shapes captured, experiments blocked on PHO-13073 tools + plan-upgrade decision ([PHO-12930](https://linear.app/phoebework/issue/PHO-12930)) — henry
- [P1] admin-agent: port MCP integrations to first-party typed API clients ([PHO-12634](https://linear.app/phoebework/issue/PHO-12634)) — henry, paused (collides with 13073 worktree area)

Backlog:

- admin-agent: re-triage Slack-thread alert investigation — close or narrow to mention-gated Datadog threads ([PHO-11469](https://linear.app/phoebework/issue/PHO-11469))
- admin-agent: root-cause `inspect_codebase_wiki` silent failures, make uncategorized errors impossible ([PHO-13074](https://linear.app/phoebework/issue/PHO-13074))
- tools: consolidate PR-lifecycle skills (6 → ~2) and review trio (3 → 1); see [[agent-skills-and-plugins]]
- tools: posthog plugin keep/kill decision
- tools: fix make-interfaces-feel-better symlink (broken outside phoebe)
- tools: decide Claude↔Codex skill sync (manual copy vs symlinks)
- phoebe: migration runner — set lock_timeout + retry for ALTERs on hot tables; 07-06 prod blip was an ADD COLUMN lock queue on app.organizations (see phoebe/til/migration-lock-queue-outage)
- admin-agent: texQL series — design/parser/engine/UI/saved queries ([PHO-11256](https://linear.app/phoebework/issue/PHO-11256)–11260)
- admin-agent: canonical data products series — types/adapters/metadata/dashboards ([PHO-11269](https://linear.app/phoebework/issue/PHO-11269)–11272)
- admin-agent: trace UX series — explorer, run comparison, prompt/context diffing ([PHO-11273](https://linear.app/phoebework/issue/PHO-11273)–11275)
- admin-agent: artifact/playbook features — saved-investigation actions, handoffs, known-issue memory, playbook authoring ([PHO-11251](https://linear.app/phoebework/issue/PHO-11251), 11264, 11267, 11268)
- phoebe: phone-first outreach instead of default SMS ([PHO-11052](https://linear.app/phoebework/issue/PHO-11052))
- tools: mine ~/me/dox/workflow.md, internal-admin.md, posthog-slack-v2-dashboard-readability.md into vault/todo
