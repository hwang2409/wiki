---
type: reference
tags: [phoebe, agents, reading]
created: 2026-08-11
updated: 2026-08-11
---

# agent engineering reading against phoebe v3

v3 has the right minimal harness shape. Its main unfinished problem is durable
execution across side effects, automation wakes, and long episodes.

## 1. sources

- [Anthropic: building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
  - Start with the smallest composable loop. Add workflow complexity only when evals prove its value.
  - Tool design is ACI design: clear arguments, examples, error boundaries, and poka-yoke beat a clever prompt.
  - Agents need ground truth after each action, explicit stop conditions, and human checkpoints for blockers.
- [Anthropic: effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
  - Compaction alone does not preserve progress. Fresh sessions need an initializer, a feature ledger, and clean handoffs.
  - One-feature increments and end-to-end checks prevent both half-finished work and premature victory.
- [Anthropic: effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
  - Context is a finite attention budget. The target is the smallest high-signal token set, not the largest window.
  - Just-in-time references plus progressive disclosure beat eagerly loading every record.
  - Compaction, structured notes, and subagents are separate tools for long horizons; each trades latency for focus.
- [OpenAI: practical guide to building agents](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/)
  - Baseline with the strongest model, then replace calls with cheaper models only where evals hold.
  - Maximize one agent first. Split only when tool overlap or conditional prompt logic remains a measured failure source.
  - Guardrails need layers: deterministic limits, risk-rated tools, output checks, and human escalation for high-risk actions.
- [Inngest: durable AI agents](https://www.inngest.com/blog/ai-agents-inngest-durable-steps)
  - Model calls, tools, and saves should be distinct durable steps. Resume from the last completed step, not loop start.
  - `invoke` gives synchronous subagents without polling; events give asynchronous and scheduled work.
  - Step traces make each input, output, retry, cost, and duration inspectable.
- [Inngest: durable execution in production](https://www.inngest.com/blog/durable-execution-key-to-harnessing-ai-agents)
  - Probabilistic, compositional, stateful agents make whole-function retries unsafe and expensive.
  - Durable checkpoints map naturally to approval waits and external API backoff.
  - Exactly-once effects still require application idempotency; durability does not create it automatically.
- [Temporal: idempotency and durable execution](https://temporal.io/blog/idempotency-and-durable-execution)
  - An idempotency key must stay constant across retries and unique across workflows.
  - Use a business ID when one exists; otherwise derive a stable `(workflow, activity)` key near request origin.
  - Record-and-check keys in one transaction, then prune them only after duplicate-arrival risk expires.
- [Trigger.dev: effective AI agents](https://trigger.dev/blog/ai-agents-with-trigger)
  - Fixed chains need programmatic gates, not another model paragraph.
  - Parallel workers suit independent checks; orchestrator-workers suit dynamic decomposition and typed fan-in.
  - Evaluator-optimizer loops need bounded iterations and explicit approval criteria.
- [LangChain: agent development lifecycle](https://www.langchain.com/blog/the-agent-development-lifecycle)
  - Build, test, deploy, and monitor is a loop. Production traces should become the next eval dataset.
  - Multi-turn simulations matter for state, ambiguity, tool choice, and escalation; single-turn scores miss these.
  - Governance is cost, tool access, auditability, human review, and discoverability—not just uptime.
- [Fowler: humans and agents](https://www.martinfowler.com/articles/exploring-gen-ai/humans-and-agents.html)
  - Humans should stay “on the loop”: improve the harness that produces outcomes instead of inspecting every artifact.
  - External quality drives the loop, but clean internal structure still improves agent speed, cost, and recovery.
  - A flywheel connects evals, production signals, harness changes, and a controlled backlog.

## 2. synthesis for v3

### already do

- **Minimal composition:** `phoebe_v3_agent/sidebar/bundle.py` mounts one agent with bash, query, interaction, and poke. `ORGANIZATION.md` bans surface predicates and sideways imports. This matches Anthropic/OpenAI’s “single agent first” advice.
- **Context discipline:** `prompts/v3_agent.md` gives stable domain rules; `core/prompt.py` freezes the prompt per conversation; `PROMPTING.md` enforces one rule home, cache-safe prefixes, and just-in-time data. This is unusually close to Anthropic’s context guidance.
- **Progressive disclosure:** `skills/` keeps EHR refresh behind `load_skill`; query uses a generated catalog shared by prompt and validator; `middleware/workspace_refs.py` loads `ws://` artifacts only when a tool asks for them.
- **Bounded evidence:** `middleware/output_contract.py` emits compact previews, schema hints, artifact refs, and bounded envelopes. Query auto-spills large results. `agent_sandbox/durable.py` rehydrates run files and caps files at 10 MiB and runs at 100 MiB.
- **Grounded execution:** `llm_framework/base/runner.py` persists turns, compacts before overflow, retries provider failures, pauses for approval or user input, and stops at iteration/cost/truncation boundaries. Sandbox commands have cgroup CPU, memory, PID caps and telemetry.
- **Eval intent:** `docs/testing.md` requires code-aware golden cases tied to real failures. The runner and sandbox expose traces, usage, resource, register, and recovery signals.

### gaps worth acting on

- **Make tool effects idempotent before v3 grows writes.** `V3Agent` persists turns, but `process_claimed_agent_run` is a leased event loop, not a memoized step ledger. Add a tool-call receipt keyed by `(run_id, call_id, tool_name)` and require external write tools to use it. Apply the same rule to `agent_automations/scheduled_triggers.py` and `executions.py`, where a claimed trigger can outlive a worker failure. Driven by [Inngest](https://www.inngest.com/blog/durable-execution-key-to-harnessing-ai-agents) and [Temporal](https://temporal.io/blog/idempotency-and-durable-execution).
- **Give long v3 episodes a durable checkpoint model.** The current handler has a 30-minute timeout, 30-second lease heartbeat, 200-iteration cap, three continuation passes, and abandoned-run recovery. That is good recovery, not step-level replay: a crash after an external effect but before its result can still need reconciliation. Ticket: persist an explicit per-iteration state machine and replay-safe tool result before adding parked subagents (#13746/#13745). Driven by [Inngest](https://www.inngest.com/blog/ai-agents-inngest-durable-steps).
- **Route coordinator automations deliberately.** `origin/main` has `agent_automations` and `automations_sweep.py`; it has no exact `coordinator_automations` symbol. Automation runs are `AUTOMATION`, while `_run_routes_to_v3` only accepts stamped `GENERAL` runs and `build_v3_run_bundle` requires a conversation. Decide whether scheduled runs get a v3 bundle, a separate automation bundle, or stay legacy. If v3 wins, add durable suspend/resume approval semantics; today `_on_pending_approval_wait` skips automation runs. Driven by [Inngest](https://www.inngest.com/blog/durable-execution-key-to-harnessing-ai-agents) and [LangChain](https://www.langchain.com/blog/the-agent-development-lifecycle).
- **Measure context, not only output size.** The prompt is frozen and tool outputs spill, but there is no contract for total system-prompt, tool-schema, history, compaction, and artifact-reference budgets. Add per-turn context composition metrics and golden regressions for prompt growth. Compare the static home-care prompt, generated query catalog, loaded skills, and recent items. Driven by [Anthropic](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents).
- **Close the trace-to-eval loop.** `docs/testing.md` says real failures become goldens, but v3 telemetry does not itself promote a trace, tool path, or sandbox receipt into a replay fixture. Add a redacted “failure packet” with model, prompt contract, tool calls, refs, approvals, and outcome, then require a human to accept it into the golden set. Driven by [LangChain](https://www.langchain.com/blog/the-agent-development-lifecycle) and [Fowler](https://www.martinfowler.com/articles/exploring-gen-ai/humans-and-agents.html).
- **Add risk-based human checkpoints with durable waits.** `build_v3_run_bundle` sets `tool_approval_config=None`; this is safe while v3 is read-only plus check-ins and EHR refresh. Future write tools need a capability risk class, approval event, expiry, and idempotency key. Do not make approval a prompt-only rule. Driven by [OpenAI](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/) and [Inngest](https://www.inngest.com/blog/durable-execution-key-to-harnessing-ai-agents).

### deliberately skip

- **Adopt Temporal for every interactive v3 turn:** Phoebe already has a DB-backed event loop, lease fencing, recovery, and low-latency streaming. Use Temporal for genuinely long callout workflows, not as a second runtime for chat.
- **Build multi-agent orchestration now:** v3 has one narrow surface and parked capability/subagent drafts. Add it only after tool overload or context isolation appears in golden failures.
- **Copy Anthropic’s initializer plus feature ledger literally:** that pattern targets coding projects across fresh sessions. Phoebe’s durable conversation, run metadata, frozen prompt, and golden cases cover the current chat horizon.
- **Buy a general agent framework or no-code hub:** v3 already owns the required thin seams. Another abstraction would hide the prompts, persistence, and tool contracts that need audit.

## 3. top five actions

1. **Tool-call idempotency ledger.** Ticket: add durable `(run_id, call_id, tool)` receipts and a write-tool contract, then test crash-after-effect recovery.
2. **Automation execution state machine.** Ticket: choose the v3 automation bundle boundary and make trigger claim, run creation, wake, approval, and completion replay-safe.
3. **Context budget telemetry.** Ticket: emit per-turn token shares for prompt, schemas, history, compaction, and refs; add a prompt-growth golden.
4. **Trace-to-golden failure packets.** Ticket: export redacted v3 trajectories with tool and sandbox evidence into reviewable replay fixtures.
5. **Risk-based approval waits.** Ticket: add capability risk metadata and durable approval events before the first irreversible v3 tool ships.
