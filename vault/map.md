# Vault Map

Topic → note index. **Agents: read this file first, before grepping.** Every note create/delete/rename must update this map in the same action. One line per note: link + hook. Families listed as patterns, not per-file.

## Meta

- [[conventions]] — vault rules: hierarchy, families, frontmatter, templates, git, gardening. Read before writing any note.
- [[hot]] — rolling ≤500-word session cache injected at Claude session start; rewrite at work-arc boundaries.
- [[todo]] — general cross-project scrap todo. Read when Henry asks "what's next"; update when items land.
- [[done]] — worldwide completion tally (`log/done.md`). Append on every merge/completion; check before writing todo.
- [[wiki-app-ui-direction]] — decision: wiki app is an Obsidian clone with restrained chrome and a standard editor-theme set; family toggle behavior + key files. Read before wiki-app UI work.
- [[ui-demo]] — renderer demo fixture exercising every wiki-app markdown feature. Keep when pruning; used to verify rendering.
- [[agent-vault-patterns]] — verified survey of how others structure markdown vaults for agent memory; gap analysis vs this vault
- [[source-credibility]] — structural source-verification method for research tasks; read when a query needs reputable sourcing (acted-on findings), skip for casual surveys.

## Tools & agent setup

- [[orchestrator-worker-protocol]] — file/tmux schema for mastermind↔worker sessions: status-file JSON contract, prompt/log paths, sentinels, signal priority. Read before building anything that renders or drives worker state.
- [[agent-skills-and-plugins]] — living inventory of Claude/Codex skills + plugins, cleanse log, open items. Read/update on any skills or plugins question or change.
- [[exe-dev]] — external SSH-first cloud for persistent VMs, agent sandboxes, devboxes, pricing/security notes, and Phoebe fit.
- [[cliproxyapi]] — OAuth-subscription→API proxy evaluation: mechanics, use cases, ToS/account-risk flags. Read before routing any agent traffic through subscriptions.
- [[wiki-native-app-research]] — port wiki web app to native macOS: Tauri-sidecar verdict, product→stack table, effort/risks. Read before starting the native-app build.
- [[model-task-benchmarks]] — which model for which coding task — verified benchmark numbers + caveats; read before assigning models to workers
- [[harness]] — what makes agent harnesses great: verified cross-cutting principles + Claude Code/OpenHands/Codex CLI/Devin/SWE-agent design breakdowns
- [[conductor]] — Conductor desktop app stack: Tauri/Rust/WebKit + mostly TypeScript; local bundle evidence and public founder statements.

## Phoebe

- [[repo-guide]] — phoebe monorepo 0→1 routing: read-first stack per task, cold-start mistake list, documented gaps. Start here for any phoebe repo work.
- [[admin-agent]] — Internal Admin Agent FULL architecture + state doc: code map, runtime, tool inventory, safety model, incidents, chronology, active work. THE one-read context for any admin-agent session.
- [[recommendation-subagents]] — recommendation-subagents campaign (June–July 2026): changes, eval numbers, overfit arc.

## Phoebe/til

- [[codebuild-secrets-json-key-drift]] — CodeBuild migrate failure: Secrets Manager JSON key missing during environment processing
- [[voice-qa-clock-writeback-action-family]] — Voice QA clock writeback can be blocked by QA_VOICE_AGENT_ENABLED_ACTION_FAMILIES vs call audit_context mismatch

## Tools/til

- [[wiki-native-bundle-hardened-runtime-parent-watchdog]] — Tauri Wiki.app with a PyInstaller onefile sidecar needs hardenedRuntime false and a parent-pid watchdog on macOS.
- [[wiki-native-tauri-dragdrop-html5]] — Tauri webview drag-drop handler kills HTML5 DnD — check before debugging dead drags in any Tauri app

## Families (path patterns, not enumerated)

- `log/YYYY-MM-DD.md` — daily end-of-day changelogs, cross-project.
- `<topic>/decisions/` — locked decisions + rationale.
- `<topic>/til/` — gotchas, tool quirks (greppable exact strings).
- `<topic>/features/` — feature breakdowns.
- `<topic>/specs/` — architecture specs.
