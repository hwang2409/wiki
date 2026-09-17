---
type: til
tags: [phoebe, evals]
created: 2026-09-17
updated: 2026-09-17
---

# Bash golden eval trajectory gotchas

reference trajectories and paired inverse cases are required before treating a live Bash golden failure as agent evidence.

- PHO-17602 added six cases across five behaviors, including both query-source directions.
- The repair case needed four calls: failed attempt, repair, inspection, and final repair.
- Read failed transcripts first. A grader expecting `id` rejected equivalent `target_id` evidence, and an unclear proof-copy prompt caused a false signal.
- Structured JSON evidence and tree-sitter syntax shape avoid lexical grading.
