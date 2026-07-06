---
type: reference
tags: [meta]
created: 2026-07-06
updated: 2026-07-06
---

# Vault Conventions

This vault is shared memory for Henry's agents (Claude Code, Codex, others), sometimes read by Henry directly in Obsidian. Notes are written primarily for agent retrieval and reuse; optimize for a future agent finding and trusting the note, not for prose polish.

## Structure

- One folder per topic area (`phoebe/`, `games/`, `tools/`, `meta/`, …). Create new topic folders as they emerge; don't force-fit.
- Filenames are kebab-case slugs and double as wikilink targets — pick stable, descriptive names (`recommendation-subagents.md`, not `notes-july.md`). Renaming breaks links; avoid it.
- One note per topic. Update and accrete rather than creating near-duplicates. Grep the vault before writing a new note.
- `map.md` at the vault root is the topic → note index. Writing a note is a two-step action: (1) write the note, (2) add/update its one-line entry in `map.md` (link + hook). Deletes and renames update the map too. Family notes are covered by their pattern line — do not enumerate them individually.

## Hierarchy: freeform notes vs note families

Two tiers. Decide the tier first; it determines the path.

**Freeform** — distinct, infrequent topics (a game scrapbook, a one-off writeup). Live at the topic-folder root: `<topic>/<slug>.md` (e.g. `games/ssbu-yoshi-0-to-1.md`). Slug is a judgment call; no ceremony.

**Families** — recurring structured notes. Deterministic paths so every agent, in any session, computes the same location without guessing:

| Family | Path pattern | Content |
|---|---|---|
| Daily log | `log/YYYY-MM-DD.md` | Personal end-of-day changelog, cross-project; one file per day, sections per project |
| Decisions | `<topic>/decisions/<slug>.md` | Choice locked + rationale + rejected alternatives |
| TILs | `<topic>/til/<slug>.md` | Gotchas, tool quirks, non-obvious mechanisms |
| Feature breakdowns | `<topic>/features/<slug>.md` | How a shipped/planned feature works |
| Architecture specs | `<topic>/specs/<slug>.md` | System/subsystem design documents |

Rules:
- Dates always `YYYY-MM-DD`. Slugs always kebab-case.
- Family folder names are fixed (plural for decisions/features/specs, singular `til`, root `log/`). Never invent variants (`decision/`, `feature-notes/`, `daily/`).
- Campaign and reference notes stay freeform at topic root — they are landmark notes, not families.
- New recurring pattern emerging? Add the family to this table FIRST, then write the note. A family not in this table doesn't exist.

## Frontmatter

```yaml
---
type: campaign | decision | til | reference | log
tags: [phoebe, evals]
created: 2026-07-06
updated: 2026-07-06
---
```

- `type` — the note archetype (see below).
- `tags` — topic tags for retrieval; reuse existing tags before inventing new ones.
- Bump `updated` on every meaningful edit.

## Note types

| Type | What | Shape |
|---|---|---|
| `campaign` | Multi-day work arc synthesized: what changed, what it did, numbers, through-line | Long, tables, written at milestones; exemplar: [[recommendation-subagents]] |
| `decision` | Choice locked with rationale: what was chosen, alternatives rejected, why | Short; the *why* and the rejected paths are the payload |
| `til` | Non-obvious mechanism, gotcha, tool quirk discovered during work | Small; MUST include exact greppable strings (error messages, flag names) |
| `reference` | Evergreen topic page that accretes: tool cheat-sheets, org context, game notes | Updated in place, never append-only logs |
| `log` | Personal end-of-day changelog | One per day at `log/YYYY-MM-DD.md`; sections per project; the one append-style type |

## Writing rules

- Dense over polished. Tables over prose. No fluff, no restating what code/git already says.
- Capture the *why* and the rejected approaches — that's the least recoverable information.
- Exact strings for anything an agent might grep: error messages, command flags, file paths, ticket/PR numbers.
- Wikilink liberally (`[[note-slug]]`) — links are the retrieval graph. An unresolved link marks a note worth writing.
- No secrets, keys, or PII. Reference where credentials live, never values.
- Facts must trace to something observed or something Henry said. No invented boilerplate.

## What does NOT belong here

- Routine task detail or session state (use handoffs for that).
- Anything trivially derivable from code, git history, or project config files.
- Claude-specific collaboration preferences (those live in Claude's own memory).
