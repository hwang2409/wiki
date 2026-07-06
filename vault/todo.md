---
type: reference
view: kanban
tags: [todo]
created: 2026-07-06
updated: 2026-07-06
---

Todo:

- [P0] admin-agent: build code-sandbox tool surface, exe.dev first backend — after 1h manual API-shape capture ([PHO-13073](https://linear.app/phoebework/issue/PHO-13073))
- [P1] admin-agent: run exe.dev pilot experiments through the new tools — clone-speed go/no-go, lifecycle, cost ([PHO-12930](https://linear.app/phoebework/issue/PHO-12930))
- [P1] admin-agent: finish native GitHub App/API setup for PR workflows — credentials, installation tokens, bounded PR create/comment/review; no merge authority ([PHO-12306](https://linear.app/phoebework/issue/PHO-12306))
- [P1] admin-agent: port MCP integrations to first-party typed API clients; MCP stays smoke-only ([PHO-12634](https://linear.app/phoebework/issue/PHO-12634))
- [P1] phoebe: rerun full 10475 bank evals on cleaned fixtures once fix branch lands ([#10475](https://github.com/phoebe-health/phoebe/pull/10475))
- [P1] tools: review reconstructed tmux-ticket-pr-window-naming skill for divergence from original
- [P2] phoebe: treat anthropic 429 as infra in `_is_infrastructure_exception` (evals/main.py)
- [P1] deployment: bundle and prune settings + feature flags ([DEP-25](https://linear.app/phoebework/issue/DEP-25))

Backlog:

- admin-agent: re-smoke integration credentials after GitHub App lands ([PHO-12658](https://linear.app/phoebework/issue/PHO-12658))
- admin-agent: type-aware tool-output formatting — diff view, tables, syntax highlighting, collapsible JSON ([PHO-12937](https://linear.app/phoebework/issue/PHO-12937))
- admin-agent: core accounts API query capability — owner filter, pagination, projection ([PHO-12980](https://linear.app/phoebework/issue/PHO-12980))
- admin-agent: re-triage Slack-thread alert investigation — close or narrow to mention-gated Datadog threads ([PHO-11469](https://linear.app/phoebework/issue/PHO-11469))
- admin-agent: root-cause `inspect_codebase_wiki` silent failures, make uncategorized errors impossible ([PHO-13074](https://linear.app/phoebework/issue/PHO-13074))
- phoebe: worker Fargate startup CPU saturation — profile, pick mitigation ([PHO-13075](https://linear.app/phoebework/issue/PHO-13075))
- tools: consolidate PR-lifecycle skills (6 → ~2) and review trio (3 → 1); see [[agent-skills-and-plugins]]
- tools: posthog plugin keep/kill decision
- tools: fix make-interfaces-feel-better symlink (broken outside phoebe)
- tools: decide Claude↔Codex skill sync (manual copy vs symlinks)
- phoebe: migration runner — set lock_timeout + retry for ALTERs on hot tables; 07-06 prod blip was an ADD COLUMN lock queue on app.organizations (see phoebe/til/migration-lock-queue-outage)
- phoebe: set up native Slack integration for Platinum Care Group — High, ops ([PHO-10748](https://linear.app/phoebework/issue/PHO-10748))
- admin-agent: execute independent read-only tool calls concurrently — High ([PHO-11305](https://linear.app/phoebework/issue/PHO-11305))
- admin-agent: fail bootstrap instead of weak admin_agent_readonly default password ([PHO-11344](https://linear.app/phoebework/issue/PHO-11344))
- admin-agent: re-triage PHO-10498 Slack admin mode — In Progress since May, likely zombie ([PHO-10498](https://linear.app/phoebework/issue/PHO-10498))
- admin-agent: texQL series — design/parser/engine/UI/saved queries ([PHO-11256](https://linear.app/phoebework/issue/PHO-11256)–11260)
- admin-agent: canonical data products series — types/adapters/metadata/dashboards ([PHO-11269](https://linear.app/phoebework/issue/PHO-11269)–11272)
- admin-agent: trace UX series — explorer, run comparison, prompt/context diffing ([PHO-11273](https://linear.app/phoebework/issue/PHO-11273)–11275)
- admin-agent: artifact/playbook features — saved-investigation actions, handoffs, known-issue memory, playbook authoring ([PHO-11251](https://linear.app/phoebework/issue/PHO-11251), 11264, 11267, 11268)
- phoebe: phone-first outreach instead of default SMS ([PHO-11052](https://linear.app/phoebework/issue/PHO-11052))
- phoebe: Slack V2 leftovers — coordinator preference memory, reaction-based triggers ([PHO-10497](https://linear.app/phoebework/issue/PHO-10497), [PHO-10493](https://linear.app/phoebework/issue/PHO-10493))
- tools: mine ~/me/dox/workflow.md, internal-admin.md, posthog-slack-v2-dashboard-readability.md into vault/todo
