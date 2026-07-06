---
type: reference
tags: [todo]
created: 2026-07-06
updated: 2026-07-06
---

Todo:

- [P0] admin-agent: finish native GitHub App/API setup for PR workflows — credentials, installation tokens, bounded PR create/comment/review; no merge authority ([PHO-12306](https://linear.app/phoebe/issue/PHO-12306))
- [P1] admin-agent: port MCP integrations to first-party typed API clients; MCP stays smoke-only ([PHO-12634](https://linear.app/phoebe/issue/PHO-12634))
- [P1] phoebe: rerun full 10475 bank evals on cleaned fixtures once fix branch lands ([#10475](https://github.com/phoebe-health/phoebe/pull/10475))
- [P1] tools: review reconstructed tmux-ticket-pr-window-naming skill for divergence from original
- [P2] admin-agent: run exe.dev week trial — clone speed go/no-go, scoped-token /exec lifecycle; Modal is likely production pivot ([PHO-12930](https://linear.app/phoebe/issue/PHO-12930))
- [P2] phoebe: treat anthropic 429 as infra in `_is_infrastructure_exception` (evals/main.py)

Backlog:

- admin-agent: re-smoke integration credentials after GitHub App lands ([PHO-12658](https://linear.app/phoebe/issue/PHO-12658))
- admin-agent: type-aware tool-output formatting — diff view, tables, syntax highlighting, collapsible JSON ([PHO-12937](https://linear.app/phoebe/issue/PHO-12937))
- admin-agent: core accounts API query capability — owner filter, pagination, projection ([PHO-12980](https://linear.app/phoebe/issue/PHO-12980))
- admin-agent: re-triage Slack-thread alert investigation — close or narrow to mention-gated Datadog threads ([PHO-11469](https://linear.app/phoebe/issue/PHO-11469))
- admin-agent: file ticket — `inspect_codebase_wiki` failing 58/59 calls with no error_category, silently down
- phoebe: file ticket — worker Fargate CPU saturation from per-task startup pinning × deploy churn; continuous profiler is next step
- tools: consolidate PR-lifecycle skills (6 → ~2) and review trio (3 → 1); see [[agent-skills-and-plugins]]
- tools: posthog plugin keep/kill decision
- tools: fix make-interfaces-feel-better symlink (broken outside phoebe)
- tools: decide Claude↔Codex skill sync (manual copy vs symlinks)
