# Vault Map

Topic → note index. **Agents: read this file first, before grepping.** Every note create/delete/rename must update this map in the same action. One line per note: link + hook. Families listed as patterns, not per-file.

## Meta

- [[conventions]] — vault rules: hierarchy, families, frontmatter, templates, git, gardening. Read before writing any note.
- [[hot]] — rolling ≤500-word session cache injected at Claude session start; rewrite at work-arc boundaries.
- [[todo]] — general cross-project scrap todo. Read when Henry asks "what's next"; update when items land.
- [[done]] — worldwide completion tally (`log/done.md`). Append on every merge/completion; check before writing todo.
- [[ticket-archive-2026-07-14]] — archived WIKI backlog tickets (2026-07-14 sweep) — full text preserved, restore by re-adding to todo
- [[wiki-app-ui-direction]] — decision: wiki app is an Obsidian clone with restrained chrome and a standard editor-theme set; family toggle behavior + key files. Read before wiki-app UI work.
- [[ui-demo]] — renderer demo fixture exercising every wiki-app markdown feature. Keep when pruning; used to verify rendering.
- [[agent-vault-patterns]] — verified survey of how others structure markdown vaults for agent memory; gap analysis vs this vault
- [[source-credibility]] — structural source-verification method for research tasks; read when a query needs reputable sourcing (acted-on findings), skip for casual surveys.

## Tools & agent setup

- [[orchestrator-worker-protocol]] — file/tmux schema for mastermind↔worker sessions: status-file JSON contract, prompt/log paths, sentinels, signal priority. Read before building anything that renders or drives worker state.
- [[bazel-slot-shim-pathology]] — machine-wide bazel slot shim behavior, exit-75 deferral contract, JVM cleanup gotchas. Read before touching bazel invocation paths or worker test policy.
- [[agent-skills-and-plugins]] — living inventory of Claude/Codex skills + plugins, cleanse log, open items. Read/update on any skills or plugins question or change.
- [[exe-dev]] — external SSH-first cloud for persistent VMs, agent sandboxes, devboxes, pricing/security notes, and Phoebe fit.
- [[cliproxyapi]] — OAuth-subscription→API proxy evaluation: mechanics, use cases, ToS/account-risk flags. Read before routing any agent traffic through subscriptions.
- [[wiki-native-app-research]] — port wiki web app to native macOS: Tauri-sidecar verdict, product→stack table, effort/risks. Read before starting the native-app build.
- [[wiki-workspaces-design]] — WIKI-127 multi-workspace file browsing design: orchestrator-derived workspace allowlist, workspace param on file APIs, per-root containment. Read before touching workspace/file-API code.
- [[model-task-benchmarks]] — which model for which coding task — verified benchmark numbers + caveats; read before assigning models to workers
- [[harness]] — what makes agent harnesses great: verified cross-cutting principles + Claude Code/OpenHands/Codex CLI/Devin/SWE-agent design breakdowns
- [[conductor]] — Conductor desktop app stack: Tauri/Rust/WebKit + mostly TypeScript; local bundle evidence and public founder statements.
- [[mcp-vs-native-agent-tooling]] — Henry's position: MCP is generally a bad way to build agent tooling — prefer native, code-aware tools; Core MCP serves external clients only
- [[multi-account-auth-rotation]] — codex/claude multi-account auth mechanics + rotation design (WIKI-15 watchdog)
- [[instance-pinning-verification]] — Failure shape: verification targets a different instance than the consumer uses (main-vs-worktree, hot-vs-frozen backend, stale checkout); always pin the instance
- [[turbopuffer]] — Serverless vector+FTS search DB on object storage — what it is, when to use
- [[local-cloud]] — local-cloud: Henry's self-hosted service stack on GPU machine — direction, stack picks, pufferclone's role
- [[harness-best-of-breed]] — cross-harness feature comparison; picks the strongest bits of each agent harness
- [[harness-claude-agent-sdk]] — Claude Agent SDK harness breakdown: architecture, extension points, gotchas
- [[harness-codex-cli]] — Codex CLI harness breakdown: run model, tool surface, prompt style
- [[harness-gemini-cli]] — Gemini CLI harness breakdown: capabilities, sandboxing, limits
- [[harness-opencode]] — OpenCode harness breakdown: open-source stack, plugin model
- [[harness-pi-coding-agent]] — Pi coding agent harness breakdown: runtime, tool surface
- [[llm-rigorous-proving]] — LLMs on rigorous mathematical proving: current SOTA, failure modes, benchmarks
- [[graph-engineering]] — graph engineering as next layer after loop engineering; org-graph + work-graph, nodes/edges/shared-state, maps onto orchestrator/worker stack
- [[t3code]] — prior art for the wiki agent-control surface: theo/ping's open-source multi-provider harness controller

## Design

- [[laws-of-ux]] — Laws of UX field notes: which laws bite where in Wiki/Phoebe UI decisions

## Math

- [[jacobian-conjecture-fable-2026]] — Fable-model exploration of the Jacobian conjecture (2026); status and dead ends

## Phoebe

- [[repo-guide]] — phoebe monorepo 0→1 routing: read-first stack per task, cold-start mistake list, documented gaps. Start here for any phoebe repo work.
- [[admin-agent-dynamic-skills]] — Dynamic skill editing decision: DB overlay, draft→approve only, text-only boundary (PHO-13882); OpenClaw research notes
- [[admin-agent]] — Internal Admin Agent FULL architecture + state doc: code map, runtime, tool inventory, safety model, incidents, chronology, active work. THE one-read context for any admin-agent session.
- [[admin-agent-audit-2026-07-14]] — Full 15-domain e2e audit of admin agent @ origin/main af8173bbfb: 134 tools, 63 endpoints, maturity assessment, ranked gaps/risks (5 HIGH), half-finished arcs.
- [[recommendation-subagents]] — recommendation-subagents campaign (June–July 2026): changes, eval numbers, overfit arc.
- [[pr10475-parity-loop]] — PR-10475 parity iteration protocol: OFF frozen, no eval overfit, 6-step loop until ON hits OFF-vs-OFF noise floor
- [[datasource-sync-auto-disable]] — axiscare sync auto-disable: self-heals via 10:10Z daily forced sync; retry logs blind to status on result-based retries
- [[admin-agent-tool-discovery]] — native tool_search + defer_loading decision, shipping chain PHO-14285..88, go/no-go = 14286 eval, watch items
- [[admin-redesign]] — full /admin/* redesign decision (2026-07-27): Ramp×Linear×wiki direction, scrap 14461 primitives, signal-tier IA per laws-of-ux, one mega-PR after in-flight admin set lands

## Phoebe/til

- [[codebuild-secrets-json-key-drift]] — CodeBuild migrate failure: Secrets Manager JSON key missing during environment processing
- [[voice-qa-clock-writeback-action-family]] — Voice QA clock writeback can be blocked by QA_VOICE_AGENT_ENABLED_ACTION_FAMILIES vs call audit_context mismatch
- [[admin-call-search-hydrates-transcripts]] — Core semantic call search hydrates full transcript rows before list serialization; root cause for PHO-13225 ReadTimeouts
- [[admin-agent-chart-message-stream]] — Chart/visualization artifacts render from assistant_text `artifact_refs`; tool cards keep raw refs only for audit/JSON inspection (PHO-13277)
- [[buildbuddy-executeworkflow-pr-context]] — BuildBuddy ExecuteWorkflow on a PR branch runs branch-push workflow steps unless GitHub PR context is present

## Tools/til

- [[wiki-native-bundle-hardened-runtime-parent-watchdog]] — Tauri Wiki.app with a PyInstaller onefile sidecar needs hardenedRuntime false and a parent-pid watchdog on macOS.
- [[wiki-native-onedir-sidecar-startup]] — PyInstaller onedir sidecar with a bundled resource folder + exec wrapper drops steady-state Wiki backend startup to ~0.32-0.36s vs ~4.6-5.5s onefile; first post-build launch can still pay ~5.3s.
- [[wiki-native-tauri-dragdrop-html5]] — Tauri webview drag-drop handler kills HTML5 DnD — check before debugging dead drags in any Tauri app
- [[wkwebview-eval-defer-clobber]] — WKWebView defers window.eval past navigate() — injected loading page clobbers loaded app when backend boots fast
- [[vite-dist-root-owned]] — Vite ENOTEMPTY: root-owned frontend/dist artifacts block make native-build
- [[wiki-native-codesign-running]] — Wiki native build codesign can fail when wiki-native is still running from the target bundle
- [[wiki-headless-auto-resume-race]] — Headless supervisor recovery may reattach before an explicit /resume call; tests and operators must accept either valid path.
- [[wiki-headless-orphan-pid-identity]] — Detached orphan cleanup must verify PID start time before signaling and kill the provider process group to avoid PID-reuse and grandchild leaks.

## Phoebe/decisions

- [[admin-db-reads-generated-catalog]] — Admin agent DB access: schema-wide reads via migrate-apply generated catalog + secret deny-tier + cost guardrails; allow-list curation retired (PHO-13273)

## Phoebe/features

- [[meeting-booking-onboarding]] — Phoebe meeting booking: customer onboarding and Core/admin-agent seams

## Wiki

- [[polish-census]] — WIKI-150 systematic polish audit of Wiki.app surfaces (rubric + tags + census tables); fans out into 8-12 small polish tickets

## Mitm-inspector

- [[mitmweb-rebuild]] — Mitmweb rebuild campaign: local-only bounded traffic inspector, plan and ticket graph

## Mitm-inspector/decisions

- [[public-hook-adapter]] — Architecture: public mitmproxy hook addon and independent UI; private mitmweb API rejected

## Wiki-app

- [[supervisor-fingerprint-swap-wedge]] — dev-code clients kill the prod agent supervisor via fingerprint swap; shutdown wedges holding the lock

## Families (path patterns, not enumerated)

- `log/YYYY-MM-DD.md` — daily end-of-day changelogs, cross-project.
- `log/weekly-YYYY-MM-DD.md` — weekly shipped-work summaries (date = week end, Sunday); merged PRs by theme per repo.
- `<topic>/decisions/` — locked decisions + rationale.
- `<topic>/til/` — gotchas, tool quirks (greppable exact strings).
- `<topic>/features/` — feature breakdowns.
- `<topic>/specs/` — architecture specs.
