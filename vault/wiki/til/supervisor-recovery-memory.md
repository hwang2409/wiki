---
type: til
tags: [wiki, agent-runtime]
created: 2026-08-02
updated: 2026-08-03
---

# Supervisor recovery memory spike

**Symptom:** Before WIKI-243, `wiki-backend --supervisor` reached more than 2 GiB RSS after startup.
**Cause:** Recovery loaded whole `raw.jsonl` and `events.jsonl` files with `Path.read_text().splitlines()` and `json.loads`.
**Evidence:** On 2026-08-02, `~/.wiki/agent-runtime/runs` held 552 JSONL files totaling 5.5 GiB; the supervisor peaked at 3.1 GiB.
**Fix:** WIKI-243 / PR #175 (`16a3b2a`) streams startup recovery through `_iter_json_lines`. Do not restore unbounded log loads.
