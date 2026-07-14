---
type: reference
tags: [tools, agents]
created: 2026-07-07
updated: 2026-07-14
---

# Agent Harness Design

Harness design is a measurable independent variable, not a wrapper around a strong model: SWE-agent's agent-computer interface (ACI) beat a raw-shell baseline by ~10.7pp on SWE-bench Lite at fixed model (NeurIPS 2024, arxiv.org/abs/2405.15793); CodeAct's code-as-action space beat JSON tool-calling by up to 20% success across 17 LLMs (ICML 2024, arxiv.org/abs/2402.01030). Great harness = deliberate engineering of everything around the model: ACI, context economy, external state, independent verification.

## Good → great differentiators

1. **Canonical loop with verification as first-class phase**: gather context → act → verify → repeat. Verification is a phase, not an afterthought (Anthropic Agent SDK post).
2. **ACI treated as designed interface**: tool interfaces matter as much as human UIs — distinct purpose, clear description, usage heuristics per tool. Two proven action-space philosophies: small code-centric core (bash + Python + browser DSL; code subsumes bespoke tools — OpenHands/CodeAct) vs curated bespoke tool set (Claude Code). Both beat naive designs.
3. **Just-in-time agentic retrieval over pre-built indexes**: Claude Code = CLAUDE.md up-front + glob/grep on demand, no vector index. Anthropic verbatim: "Semantic search is usually faster than agentic search, but less accurate, more difficult to maintain, and less transparent." (Early RAG-based Claude Code was dropped for agentic search.)
4. **Context = depleting resource ("context rot")**: degradation is a gradient as context grows. Three canonical counters: compaction (auto-summarize near limit, preserving decisions/unresolved bugs), structured note-taking to external files, subagents returning 1-2k-token condensed summaries to a coordinator.
5. **Externalized state for cross-session work**: git commits + progress file read at startup; machine-readable feature registry (JSON with `passes` boolean) as ground truth — JSON because models overwrite it less casually than Markdown; explicit rule "unacceptable to remove or edit tests." Initializer agent (env scaffolding) split from coding agent.
6. **Narrow per-iteration scope**: one feature at a time fixed the one-shot-the-whole-app failure mode (incomplete impls, exhausted context). Single case study but corroborated by long-horizon degradation research.
7. **Generator/evaluator separation**: models self-praise mediocre work; tuning a standalone skeptical evaluator is more tractable than making a generator self-critical. Evaluator with direct environment perception (Playwright driving live app) caught bugs a code-reading QA pass rationalized away (Anthropic, Mar 2026).
8. **Sandboxed reproducible runtime with autonomy-inside-boundary**: hard technical boundary + approval policy as orthogonal layers — agent moves freely inside boundary, escalates outside (Codex CLI's design articulates this cleanest).
9. **Eval discipline**: benchmark harness in-repo; iterate against numbers, not vibes.

## Per-harness

### Claude Code / Claude Agent SDK
- Curated bespoke tools; agentic filesystem search (no index); auto-compaction; subagent fan-out with explicit structure (each subagent needs objective, output format, tool guidance, task boundaries — early versions spawned 50 subagents for simple queries); filesystem + bash as the universal substrate.
- Sources: anthropic.com/engineering/{building-agents-with-the-claude-agent-sdk, effective-context-engineering-for-ai-agents, effective-harnesses-for-long-running-agents, harness-design-long-running-apps, multi-agent-research-system}.

### OpenHands (ex-OpenDevin)
- Open-source Devin-style agent, All Hands AI, github.com/All-Hands-AI/OpenHands (ICLR 2025 paper arxiv.org/abs/2407.16741). Best readable reference for Devin-style internals.
- Small code-centric action space (IPython + bash + browsing DSL; CodeAct default agent — note: Nov 2025 SDK moved to typed tool schemas, kept small bash/code-centric set). Append-only event stream as state substrate (resumable, replayable, trajectories = eval artifacts). Per-session Docker sandbox: bash + Jupyter + Playwright Chromium (default; local/remote runtimes exist). Context condenser (docs.openhands.dev/sdk/arch/condenser). Explicit AgentDelegateAction for delegation. SWE-bench harness in-repo.

### Codex CLI (OpenAI)
- Rust CLI; shell-exec + apply-patch core actions, MCP support.
- Strongest published sandbox story: platform-native enforcement — macOS Seatbelt, Linux bubblewrap (`--ro-bind / /` read-only rootfs, `PR_SET_NO_NEW_PRIVS` + seccomp network filter; Landlock legacy fallback), Windows Sandbox. Modes: read-only / workspace-write (default) / danger-full-access. Network off by default; deny-wins allowlists. `.git`, `.agents`, `.codex` protected even in writable modes.
- Two orthogonal layers: sandbox mode (what agent CAN do) × approval policy (when it must ASK) — autonomy inside hard boundary. `approvals_reviewer = "auto_review"`: reviewer agent screens escalations for exfiltration/credential-probing/destructive actions.
- Sources: developers.openai.com/codex/concepts/sandboxing, /codex/agent-approvals-security, github.com/openai/codex.

### Devin (Cognition)
- Closed; philosophy public, harness proprietary. Shell + editor + browser in unspecified "sandboxed compute environment" (mechanism undisclosed — claim, not spec).
- Long-horizon iteration as differentiator: "72% of passing tests take over 10 minutes," 45-min runtime cap, "capability to run indefinitely" (cognition.com/blog/swe-bench-technical-report; 13.86% unassisted, mid-2024).
- "Don't Build Multi-Agents" (Walden Yan, 2025): single-threaded agent, share full traces not messages ("Actions carry implicit decisions, and conflicting decisions carry bad results"), dedicated compressor LLM for history compaction ("hard to get right," may need fine-tuning).

### Cursor (added 2026-07-14)
- Harness = evals-driven product, per-model: tool formats match training distribution (OpenAI = patch-based edits, Anthropic = string replacement); prompts tuned per family (OpenAI literal, Claude intuitive); quirks patched via prompts (one model's "context anxiety" — refused work as context filled).
- **Keep Rate**: fraction of agent-written code still in codebase after fixed intervals — ground-truth quality metric. Plus LLM-scored user-satisfaction from replies, latency/token/cache-hit ops metrics, Cursor Bench offline + A/B online.
- Tool-error discipline: unknown error = harness bug, always; expected-error taxonomy (`InvalidArguments`/`Timeout`/…); per-tool per-model anomaly alerts; sprint to "2-3 nines" tool-call reliability. Rationale: failed calls linger in context → context rot.
- Codex-model rework: shell-forward tool names; dropping Responses-API reasoning traces between tool calls = 30% drop on Cursor Bench (vs OpenAI's claimed 3%) — alerting guarantees trace flow; autonomy-biasing ("implement, don't propose"); token-thrift prompt lines made Codex refuse ambitious tasks.
- Static → dynamic context shift: dropped upfront dumps (folder layouts, semantic snippets) for on-demand fetch as models improved.
- "Automated software factory": weekly log-mining agents file Linear tickets on new/spiked issues.
- Sources: cursor.com/blog/{continually-improving-agent-harness, codex-model-harness} (fetched 2026-07-14, single-pass not adversarially verified).

### SWE-agent (Princeton)
- Origin of the ACI axis: purpose-built file-edit/repo-nav/test tools; removing ACI dropped resolve rate 12%→3% at fixed model. mini-swe-agent: ~100-line harness for eval baselines (github.com/SWE-agent/mini-swe-agent).

## Refuted — do not cite

3-vote adversarial verification killed these commonly-repeated claims:
- Anthropic multi-agent "90.2% uplift over single-agent Opus" — refuted 0-3.
- "Token usage explains 80% of variance on BrowseComp" — refuted 0-3.
- "Anthropic harness uses full context resets + handoff artifacts instead of compaction, to avoid context anxiety" — refuted 0-3.

## Caveats

- Verified evidence concentrates on three ecosystems (Anthropic, SWE-agent, OpenHands); Anthropic sources dominate the cross-cutting principles. Codex CLI/Devin sections from targeted primary-doc fetch (2026-07-07), single-pass not adversarially verified.
- Time-boxed numbers: SWE-agent 12.5% SOTA = mid-2024; CodeAct 20% gap measured on 2024 models, weakest on closed models tuned for native tool-calling.
- Anthropic long-running-harness findings (one-feature-per-iteration, initializer split, Playwright evaluator) = single internal case studies, not controlled evals.
- Open: does agentic-search-over-embeddings hold at large-monorepo scale; has RL-tuned native tool-calling closed the CodeAct gap.

Related: [[orchestrator-worker-protocol]], [[wiki-app-ui-direction]], [[model-task-benchmarks]].

Provenance: deep-research run 2026-07-07 (24 sources, 117 claims extracted, 25 verified 3-vote, 22 confirmed) + follow-up primary-doc fetch for Codex CLI/Devin.
