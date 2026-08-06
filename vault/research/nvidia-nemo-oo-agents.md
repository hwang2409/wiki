---
type: reference
tags: [research, agents, frameworks]
created: 2026-08-03
updated: 2026-08-03
---

# nvidia nemo oo agents

## summary

nvidia-labs oo agents (nooa) is an alpha, model-agnostic Python agent framework.
It targets developers who want typed Python objects to define state, prompts, actions, and outputs.

Its main runtime lets models write Python in a Jupyter-style loop.
The framework records events, validates code, retries failures, and persists agent state.

source: [repository](https://github.com/NVIDIA-NeMo/labs-OO-Agents), inspected at commit `0cda041`.

## repo signals

- stars: 724.
- last push: 2026-08-03.
- contributors: 12, from the GitHub contributors API.
- license: Apache-2.0. `gh repo view` reports `Other` for `licenseInfo`.
- activity: the latest commit merged pull request #81 on 2026-08-03.

## core abstractions

- `Agent` is a Python object. Typed fields hold state, methods expose capabilities, and docstrings guide the model.
- An async method with an ellipsis body becomes a generation method through `AgentMeta`.
- Ordinary Python methods stay deterministic and become visible to generated code as callable helpers.
- `GenerationStrategy` separates the method contract from execution style. `CodeActStrategy` is the default.
- `RuntimeServices` gives strategies controlled access to generation, validated code execution, events, and nested calls.
- `EventManager` and event backends provide append-style conversation state, filtering, collapse, and persistence.
- `Skill` and `SkillRegistry` add Python or `SKILL.md` capabilities with attach and detach hooks.
- `MemoryManager` adds persistent memories, retrieval, graph links, reflection, forgetting, and live references.

Top-level layout centers on `src/nooa`, `packages/nooa-*`, `skills`, `examples`, `tests`, and `util/eval_pipeline`.
Entrypoints include `Agent`, `@strategy`, `get_llm_client`, `nooa` CLI commands, and `trace-explorer`.

## agent lifecycle

1. A developer defines an `Agent` subclass and chooses an LLM at class, instance, or method scope.
2. `AgentMeta` detects async ellipsis methods and wraps them with the selected strategy.
3. A call creates or reuses a serialized actor runtime and records a task event.
4. The runtime renders context blocks and event history into provider messages.
5. The strategy calls the model. CodeAct receives Python tool calls and executes validated code.
6. Generated code can call `self` methods, skills, context APIs, event APIs, and nested generation methods.
7. Execution emits output or error events. Validation, typed-return checks, and retries feed failures back to the model.
8. The runtime returns a typed result, records traces and metrics, and can save events and snapshots.

The runtime serializes generation access and supports nested calls without deadlocking.
Context blocks can be static, dynamic, scoped, cached, or filtered by event query.

## provider + tool surface

`UnifiedLLM` exposes sync `call` and async `acall`, normalized responses, tool calls, structured outputs, retries, and token usage.
`CompletionClient` uses LiteLLM Chat Completions; `ResponsesClient` supports the OpenAI Responses shape.

LiteLLM routes OpenAI, Anthropic, Google, NVIDIA NIM, Ollama, vLLM, and other compatible providers.
YAML aliases can set model names, API bases, environment-key names, context windows, and client type.
The provider layer builds per-client HTTPX transports and provider-specific wrappers.

Tools use normalized `Tool` and `ToolCall` objects. The framework converts typed Python signatures into provider schemas.
CodeAct usually exposes one `execute_python` tool, so methods and object attributes act as the model's tool surface.
MCP tools are also supported through an optional client package.

LiteLLM streaming responses are collected into normalized `LLMResponse` values.
The public `UnifiedLLM` interface is request/response based, not a caller-facing token stream.

Generated Python has AST validation, blocked-module and call checks, timeouts, and optional sandbox execution.
The repository warns that in-process checks are defense-in-depth, not a containment boundary.

## evals / testing

The repository has a broad pytest suite for metaclass wiring, events, runtime behavior, sandboxing, storage, providers, MCP, and traces.
It includes provider-compatibility tests, model-onboarding capability tests, and performance tests.

`util/eval_pipeline` runs configured agents in subprocesses, records traces, applies scorers, and supports parallel experiments.
`nooa-bench` provides a `BenchAgent` and Harbor benchmark runner.
The paper and examples report capability tests plus SWE-bench Verified and Terminal-Bench 2.0 results.

## compare vs phoebe-v3 and wiki-supervisor

Nooa duplicates phoebe-v3's provider-neutral LLM adapter, event history, tool-call retry loop, artifact-capable execution, and typed result validation.
It also duplicates wiki-supervisor's event-driven execution, persistent status, tracing, and explicit worker isolation at a broader framework level.

Worth stealing:

- typed Python methods as the agent contract, with deterministic helpers exposed without separate tool registration;
- per-method strategy selection, including late-bound model selection and nested strategy composition;
- context blocks with dynamic expressions, scoped event views, progressive `doc()` disclosure, and explicit event queries;
- a narrow `RuntimeServices` protocol that keeps strategies above the full runtime implementation;
- normalized tool schemas and provider formatters behind one adapter;
- memory as an attachable skill with recall, reflection, references, and event hooks;
- the test and eval split between unit coverage, provider checks, onboarding, benchmarks, and trace scoring.

Nooa's generated-code REPL is not a fit for phoebe-v3's least-surface design.
Phoebe's four doors (`retrieve`, `run_bash`, `write`, `confirm_write`) keep authority at explicit boundaries.
Nooa instead makes the Python object graph the broad capability surface.

Wiki-supervisor already has the supervisor/worker split and machine-readable worker protocol.
Nooa's actor runtime, nested generation IDs, event backend, and trace model could improve worker internals.
They do not replace supervisor policy, status files, merge gates, or worker lifecycle control.

## verdict

Henry should not adopt nooa as a framework.
It is alpha research software, adds a large code-as-action runtime, and overlaps existing Phoebe and wiki architecture.

Henry should borrow the typed method contract, strategy protocol, dynamic context blocks, normalized provider boundary, and attachable memory skill.
These ideas can reduce tool-schema drift and improve composability without changing Phoebe's authority model.
The eval taxonomy is also useful for adding provider, capability, and trace-contract checks to existing harnesses.
