---
type: reference
tags: [tools, agents, auth]
created: 2026-07-08
updated: 2026-07-08
---

# Multi-Account Auth Rotation (codex + claude)

Verified research (2026-07-08, deep-research workflow, primary sources + local verification on codex-cli 0.143.0 / claude-code 2.1.153). Basis for the WIKI-15 backend watchdog (autonomous codex account rotation + fleet revival).

## Codex

- Creds: `$CODEX_HOME/auth.json` (default `~/.codex/`, mode 0600). ChatGPT login = OAuth material (id/access/refresh tokens, `account_id`, `last_refresh`); API-key login = `OPENAI_API_KEY` field.
- `CODEX_HOME` relocates EVERYTHING (auth, config.toml, sessions/, history) — per-account CODEX_HOME at runtime breaks the wiki transcript reader (fixed path `~/.codex/sessions`) and resume continuity. Dir must pre-exist or codex exits.
- **Swap-in-place is the rotation mechanism**: live session on old account re-reads auth.json before token refresh and ABORTS on `account_id` mismatch ("signed in to another account") instead of clobbering — no silent cross-account overwrite (narrow TOCTOU mid-`persist_tokens` remains; swap only after target workers are limit-dead / killed).
- auth.json is rewritten on refresh (load-merge-save, no lock) → vault copies rot. Rule: snapshot live auth.json BACK to the outgoing account's dir before overwriting.
- Scripted login: `codex login --with-api-key` (stdin), `--with-access-token`, `--device-auth` (headless). Old `--api-key` flag removed.
- Enrollment without touching active account: `CODEX_HOME=$(mktemp -d) codex login` → copy that auth.json to `~/.codex-accounts/<name>/auth.json` → rm temp dir.
- Detection false positive: benign pane string "usage limit resets available. Run /usage" — limit regex must match only the "You've hit your usage limit" family.
- Revival: `codex resume <session-id>` with launch cwd = `session_meta.cwd` from the rollout header (for workers = worktree); fallback `resume --last` in registry worktree. Verify swap with `codex login status`.

## Claude

- macOS creds: login Keychain generic-password `service="Claude Code-credentials"` (account=$USER). `~/.claude/.credentials.json` = Linux/headless fallback only. `~/.claude.json` = non-secret state.
- `CLAUDE_CONFIG_DIR` relocates everything incl. `projects/` AND isolates auth (Keychain service name keyed by sha256 of config dir) — verified locally. Same runtime rejection as CODEX_HOME: moving projects/ breaks the wiki transcript reader.
- Rotation approach (v2, unbuilt): per-account setup-tokens (`claude setup-token`) injected as `CLAUDE_CODE_OAUTH_TOKEN` inline at worker respawn — env outranks Keychain; `claude --resume` is account-agnostic. NEVER export shell-wide (would hijack orchestrator identity).
- UNVERIFIED before building cc rotation: (a) CLAUDE_CODE_OAUTH_TOKEN docs say "inference-only scope" — confirm full interactive worker (tools, MCP) runs on it; (b) no real cc limit banner ever captured on disk — detector speculative, capture live sample; (c) `--resume` may mint new session id — confirm + registry-update.
- /login is browser-interactive — not scriptable; Keychain swap races refresh + known ACL bugs (#19456/#10665). Both rejected.

## Design (WIKI-15)

Backend watchdog (FastAPI loop, ~60s): scan registry cdx worker panes for limit signature → debounced rotation: kill all cdx windows → snapshot outgoing auth.json → swap in next eligible account (`~/.codex-accounts/<name>/auth.json`, state.json tracks active + per-account `limit_reset_at` parsed from "try again at <date>") → revive each worker (resume by id, fresh log suffix, `wiki agent update`, re-poke via send-keys protocol) → SSE banner on /agents. cc limits: alert-only v1. Kill switch `WIKI_ACCOUNT_WATCHDOG=off`. Manual `POST /api/accounts/rotate`.

Related: [[orchestrator-worker-protocol]], check-aliveness skill (LIMIT-DEAD subclass candidate).
