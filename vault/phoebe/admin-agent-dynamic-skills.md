---
type: reference
tags: [phoebe, admin-agent, skills, decision]
created: 2026-07-16
updated: 2026-07-16
---

# Admin agent dynamic skill editing

Decision (Henry, 2026-07-16): admin agent may edit its own skill prompt bodies via DB overlay, **draft→approve only** — no auto-apply tier; rationale = guard against agent corrupting a SKILL file. Ticket: PHO-13882.

Hard boundary: only prompt TEXT is dynamic. Tool grants, tiering, mounting, skill names stay code-only — skill bodies are prompt-injected, so agent-authored bodies are an injection/self-escalation channel; approval diff view is the mitigation.

Current state: skills baked at deploy (prompts/skills/*.md runfiles + admin_agent_skills.py constants).

Prior art researched: OpenClaw dynamic skill system — lazy metadata-only injection (~24 tok/skill), on-demand body load, requires.bins/env/os gating, session-start snapshot, budget-aware description degradation. Steal-worthy for wiki runtime too. ClawHub marketplace = unreviewed install-and-run, active supply-chain attack surface per 2026 papers.
