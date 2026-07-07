---
type: reference
tags: [meta, agents]
created: 2026-07-06
updated: 2026-07-06
---

# Agent Vault Patterns (Field Survey)

Deep-research survey 2026-07-06 (21 sources fetched, 25 claims adversarially verified 3-0). Seven high-quality sources; all techniques verified against primary code/specs.

## Sources & their techniques

| Source | Core technique |
|---|---|
| [obsidian-mind](https://github.com/breferrari/obsidian-mind) (3.3k★) | 5 Claude Code lifecycle hooks (SessionStart re-index/inject, UserPromptSubmit classify, PostToolUse frontmatter+wikilink validation, PreCompact backup, Stop checklist); tiered token loading (~2K always-loaded → 100-token routing hints → semantic search → full reads); scripts own environment, agent owns content; MEMORY.md = pointers only. Ships Codex + Gemini hook configs too. |
| [claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) (8.9k★) | `hot.md` recent-context cache (~500 words) — Stop hook overwrites it, SessionStart cats it back; retrieval order hot → index → pages to cap token cost; fixed scaffold: index.md, log.md, hot.md, overview.md, CLAUDE.md. |
| [Stefan Imhoff](https://www.stefanimhoff.de/agentic-note-taking-obsidian-claude-code/) (6k-note vault) | Lightweight path: `/init` → CLAUDE.md; retrieval via [qmd](https://github.com/tobi/qmd) (BM25+vector markdown search); writes via official `obsidian` CLI; MOCs as navigation; recurring maintenance packaged as [skills](https://github.com/kogakure/skills). |
| [OKF spec](https://alexop.dev/posts/open-knowledge-format-markdown-frontmatter-agent-knowledge/) (Google Cloud v0.1) | One required frontmatter field (`type`) → filter without reading bodies; reserved index.md (read-first per folder) + log.md (episodic append); read-before-work / write-back-after loop. |
| [obsidian-memory-for-ai](https://github.com/jrcruciani/obsidian-memory-for-ai) | "One fact, one file": facts/{entity}/{predicate}.md, path = primary key, frontmatter = schema, lint.py = constraint engine, regenerated _views/ = materialized read models; prose layer deliberately separated from typed-fact layer. No DB/daemon/embeddings. |
| [Eric J. Ma](https://ericmjl.github.io/blog/2026/3/6/mastering-personal-knowledge-management-with-obsidian-and-ai/) | Conventions-over-tooling: AGENTS.md documents the system; project notes as "control towers"; agent-run sweeps on context gaps; **provenance rule: derived notes must quote source notes** (hallucination ~1 per 4-5 sweeps, usually bad transcripts). |
| [Karpathy LLM Wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) | Reframe: replace query-time RAG ("rediscovering knowledge from scratch on every question") with an incrementally maintained interlinked wiki — synthesis and cross-links compound across sessions; Obsidian as browsing IDE. |

## Convergent stack (all 7 agree)

Small always-loaded manual (CLAUDE.md/AGENTS.md) → index/map read first, progressive disclosure → append-only episodic log → read-at-start / write-back-at-end ritual → typed frontmatter for body-free filtering → harness memory as pointers, never store → validation (lint/provenance) against drift.

## Gap analysis vs this vault

Already have: map.md read-first, conventions.md manual, CLAUDE.md-as-pointers, typed frontmatter, log/done.md + daily logs, `wiki lint`, git-backed, wikilinks.

Worth considering:
- **hot.md-style recent-context cache** — cheapest high-leverage add; Stop/SessionStart hooks maintain a ~500-word rolling state file.
- **Session-lifecycle hooks** — ours is prompt-convention (like claude-obsidian); obsidian-mind mechanizes read/validate/write via hooks incl. PostToolUse frontmatter+wikilink validation (our lint, but inline at write time).
- **Provenance rule** — derived/synthesized notes quote their sources (ericmjl); cheap convention, direct anti-hallucination.
- **Semantic search** — qmd if grep/full-text stops sufficing at scale.

Skipped deliberately: one-fact-one-file (too heavy for this vault's size), embeddings/DB (everyone agrees: no).
