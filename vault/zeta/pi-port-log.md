---
type: reference
tags: [zeta, agents]
created: 2026-09-28
updated: 2026-09-28
---

# Pi to Zeta port log

Pi returned a 53.6% input-token cache hit rate across 51 turns in this task mix. Longer follow-up work reused more context. Short chats returned no cache reads.

## Benchmark: 2026-09-28

- Pi 0.87.1, `openai-codex/gpt-6-sol`, three sessions per workflow. Each session kept one Pi RPC process and one conversation. Programming tasks used read-only tools; other tasks used no tools. Main runs used `--thinking off` and disabled context files, extensions, skills, prompt templates, and themes to control prompt size. Zeta source comparison: `origin/main` at `ccdcca35`.
- Rate = `cacheRead / (input + cacheRead + cacheWrite)`, summed over provider requests. Pi's Codex backend reported zero `cacheWrite` tokens. This measures the share of input tokens served from cache, not the share of requests with a hit.

| Workflow | Turns | Provider requests | All input | Follow-up input |
|---|---:|---:|---:|---:|
| Fresh quick asks | 3 | 3 | 0.0% | n/a |
| Short follow-up chats | 12 | 12 | 0.0% | 0.0% |
| Long everyday documents | 9 | 9 | 48.3% | 70.9% |
| Repository programming | 9 | 21 | 61.6% | 70.0% |
| Code review | 9 | 9 | 50.9% | 74.9% |
| Technical planning | 9 | 10 | 48.4% | 55.6% |
| **Task mix total** | **51** | **64** | **53.6%** | — |

- Total: 75,008 cache-read tokens of 139,814 input tokens. Follow-up excludes the first user turn in each session. One planning request failed before usage and retried; the zero-token failure counts as a provider request.
- Short chats ended below 600 input tokens per request. Longer sessions often read 1,400–6,500 cached tokens on follow-ups, but some follow-ups missed. Prompt length and stable history mattered more than the task label in this sample.
- Normal startup spot check: four everyday chat turns returned 0% of 1,910 input tokens; three programming turns returned 74.2% of 35,542 input tokens. Earlier benchmark runs may have warmed the programming prefix, so this is not a clean cold start.
- These are small synthetic runs with one model and one provider path. They do not give a universal Pi rate. Anthropic auth was unavailable, and this test did not cover idle expiry, long sessions, or compaction.

## Port candidates

| Priority | Candidate | Evidence and next check | Status |
|---|---|---|---|
| High | Cost-gated cache warming | Pi replays a tiny request near expiry when predicted savings exceed $0.05. Zeta has no cache warmer on `origin/main`. Check Zeta traces for costly idle misses before adding background requests. | Candidate |
| High | Missed-token and missed-cost diagnostics | Pi reports avoidable misses above a 1,024-token noise floor and excludes expected compaction rebuilds. Zeta already has `/cost` trends and `ZETA_CACHE_TRACE=1`; add miss reasons and cost to those paths. | Candidate |
| High | Skip cache writes for one-off compaction summaries | Pi disables writes for compaction and branch summaries. Zeta's compaction call uses the regular Anthropic payload, which marks blocks for one-hour caching. Confirm the wire payload and costs before changing it. | Candidate |
| Medium | Configurable Anthropic cache lifetime | Zeta sets `ttl: "1h"` in `src/zeta/providers/anthropic_payload.py`. Pi exposes short or long retention. Compare real burst and break patterns before selecting a default. | Candidate |
| Explore | Preserve initial prompt/tool prefix when dynamic tools change | Pi records prompt and tool changes as transcript deltas where supported. Check whether future Zeta dynamic-tool work breaks its stable prefix before porting this approach. | Candidate |

Already present in Zeta: Anthropic cache markers, Codex cache affinity, `/cost` cache trends, opt-in cache traces, branch history, and JSON-RPC. Pi's Codex cache key does not need a direct port; Zeta already computes a static-prefix key and handles GPT-5.6 separately.

## Sources

- Pi RPC and usage: https://pi.dev/docs/latest/rpc and https://pi.dev/docs/latest/json
- Pi settings, models, and compaction: https://pi.dev/docs/latest/settings, https://pi.dev/docs/latest/models, https://pi.dev/docs/latest/compaction
- Pi cache implementation: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/cache-stats.ts and https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/cache-warmer.ts
- Zeta `origin/main` at `ccdcca35`: `src/zeta/providers/anthropic_payload.py`, `src/zeta/providers/codex.py`, `src/zeta/core/slash.py`, `src/zeta/runtime/loop/cache_trace.py`, `src/zeta/core/context.py`
- OpenAI prompt caching behavior: https://developers.openai.com/api/docs/guides/prompt-caching
