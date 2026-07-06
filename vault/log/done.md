---
type: log
tags: [log, done]
created: 2026-07-06
updated: 2026-07-06
---

# Done

## 2026-07-06

- **cleanup** — cancelled 11 implemented/stale Internal Admin Agent tickets with evidence comments (PHO-11231, 11268, 11273, 11461, 11462, 11464, 11465, 11466, 11540, 11727, 12424)
- **admin-agent** — PHO-12937 type-aware tool-output renderers — diff/table/code/JSON + markdown fallback, bounded ([#10614](https://github.com/phoebe-health/phoebe/pull/10614) merged 1049b2646f)
- **admin-agent** — PHO-12306 leg B GitHub webhook ingestion — endpoint, HMAC fail-closed, event persistence + dedupe ([#10622](https://github.com/phoebe-health/phoebe/pull/10622) merged dc5d04d5c0)
- **admin-agent** — PHO-13042 read-only GitHub repo primitives + deploy doctrine skill; fixed missing actions:read App permission in review→fix loop ([#10552](https://github.com/phoebe-health/phoebe/pull/10552) merged 0aad4a0378)
- **admin-agent** — PHO-12980 core accounts API — owner filter, pagination, projection, evidence opt-in ([#10611](https://github.com/phoebe-health/phoebe/pull/10611) merged f9f1587f24)
- **cleanup** — PHO-12658, PHO-10748, PHO-11305, PHO-11344, PHO-10498 closed by Henry (completed in Linear, pruned from todo)
- **tools** — codex skills cleanse — 14 stock samples + orphans removed; wiki-vault/handoff/codex-goal-loop ported to Codex
- **tools** — vault system stood up — conventions, map, families, templates, todo, git+remote backup

## 2026-07-05

- **admin-agent** — PHO-12640 core-accounts read access closed as superseded by PHO-12921 (archived in Linear)

## 2026-07-03

- **phoebe** — PHO-12949 + PHO-12962 expected-failure alerting (monitors 288625692 / 302175306 live)
- **phoebe** — PHO-12955 `:eyes:` trace diagnosis
- **phoebe** — PHO-12952 report_missing_tool
- **phoebe** — PHO-12951 tool telemetry dashboard
- **phoebe** — PHO-12939 external tool specs / identifier resolution ([#10474](https://github.com/phoebe-health/phoebe/pull/10474))
- **phoebe** — PHO-12938 tool-output tiering
- **phoebe** — PHO-12931 Linear-style trace filters
- **phoebe** — eval harness fidelity + runner robustness — scheduler pass-budget fix, tree-scoped claims, answer-key leak removal ([#10480](https://github.com/phoebe-health/phoebe/pull/10480))

## 2026-07-01

- **admin-agent** — PHO-12830 Linear default project-scope resolution fix (Linear: Merged)
- **admin-agent** — PHO-12826 codebase tools no longer starve run lease heartbeats (Linear: Merged)
