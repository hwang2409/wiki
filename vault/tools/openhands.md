---
type: reference
tags: [tools, agents]
created: 2026-07-07
updated: 2026-07-07
---

# OpenHands

Open-source autonomous coding agent (Devin-style), started March 2024 as **OpenDevin** — community reconstruction after Cognition's closed Devin launch; renamed OpenHands ~late 2024. MIT-licensed, repo: https://github.com/All-Hands-AI/OpenHands. Backed by All Hands AI (company formed around the project; Graham Neubig, CMU, among leads).

## What it is

- Full agent harness: agent gets sandboxed Docker runtime with shell, code editor (file ops), browser, Jupyter — plans and executes multi-step SWE tasks, opens PRs.
- Model-agnostic: bring your own LLM (Claude, GPT, local) via LiteLLM-style config.
- Interfaces: web UI (chat + workspace panes), CLI/headless mode, GitHub-Action-style resolver for issue→PR automation.
- Event-stream architecture: agent loop = stream of actions/observations; agents are pluggable (CodeAct agent is the default — actions expressed as executable code rather than fixed tool schemas).
- Strong SWE-bench presence: was among top open-source harnesses on SWE-bench-verified leaderboards (score depends on backing model).

## Why it matters here

- Best readable reference for Devin-style harness internals — Devin itself is closed, philosophy-blog-posts only (single-threaded agent + context compression, "Don't Build Multi-Agents", Cognition June 2025).
- Relevant to [[orchestrator-worker-protocol]] and the wiki `/agents` monitor: their event-stream + session-state model is a working example of transcript/session structuring, vs our parsed-JSONL approach ([[wiki-app-ui-direction]]).
- Compare also SWE-agent (Princeton) — published agent-computer-interface design, simpler harness.

## Caveats

- Source: model training knowledge (cutoff well before 2026-07) — project moves fast; verify current repo state/name/leaderboard before relying on specifics.
