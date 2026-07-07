---
type: reference
tags: [tools, agents]
created: 2026-07-06
updated: 2026-07-06
---

# CLIProxyAPI

Evaluated 2026-07-06, not adopted. Source: [github.com/router-for-me/CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (Go, MIT, ~39k stars, active).

**What:** local proxy that OAuth-logs into subscription CLI agents (Claude Code/Max, ChatGPT Codex, Gemini/Antigravity, Grok Build) and re-exposes them as OpenAI/Claude/Gemini-compatible API endpoints — flat-rate subscriptions become API keys.

**Mechanics:** translator layer converts any client schema ↔ any upstream (streaming, tool calls, multimodal); multi-account pools w/ round-robin failover; plain API-key upstreams via config; embeddable Go SDK; management API; ecosystem of dashboards ([cpa-usage-keeper](https://github.com/Willxup/cpa-usage-keeper), [CPA-Manager-Plus](https://github.com/seakee/CPA-Manager-Plus)) and account-switcher wrappers (CCS, Quotio, vibeproxy).

**Legit narrow use here:** cheap local experiments (wiki-agent prototypes, one-off scripts) against existing subscriptions through one endpoint instead of API spend.

**Flags (why not adopted):**
- ToS gray zone — consumer subscriptions aren't licensed for programmatic API reuse; ban risk to the Claude Max account.
- Ecosystem signal — README sponsor wall is gray-market relay resellers / account shops.
- Credential concentration — holds live OAuth tokens for every fed account; self-host only.
- **Phoebe hard no** — never route work traffic or PHI-adjacent anything through it.

**Revisit if:** providers ship sanctioned subscription-API bridges, or a personal experiment's API cost would exceed the account-risk tolerance.
