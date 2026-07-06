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

**Freeform** — distinct, infrequent topics (a one-off writeup, a landmark campaign note). Live at the topic-folder root: `<topic>/<slug>.md` (e.g. `phoebe/recommendation-subagents.md`). Slug is a judgment call; no ceremony.

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

- **Payload first.** First line after the title is the answer/state/decision. Context after, only if needed.
- **Length budgets:** til ≤ 10 lines, decision ≤ 15, daily log ≤ 20, reference sections one screen each. Campaign is the only long form, and even it leads with a table + one through-line paragraph, not narrative.
- **Delete, don't compress.** Over budget → cut content, not squeeze wording. Hurts to cut → it's a second note.
- **No throat-clearing.** No "this note describes…", no restating the title, no unrequested background.
- Bullets/tables by default; a prose paragraph must earn its place.
- Dense over polished. No fluff, no restating what code/git already says.
- Capture the *why* and the rejected approaches — that's the least recoverable information.
- Exact strings for anything an agent might grep: error messages, command flags, file paths, ticket/PR numbers.
- Wikilink liberally (`[[note-slug]]`) — links are the retrieval graph. An unresolved link marks a note worth writing.
- No secrets, keys, or PII. Reference where credentials live, never values.
- Facts must trace to something observed or something Henry said. No invented boilerplate.

## Templates

Copy the skeleton for family notes; don't improvise structure.

**decision** (`<topic>/decisions/<slug>.md`)

```markdown
---
type: decision
tags: []
created: YYYY-MM-DD
updated: YYYY-MM-DD
---
# <Decision title>
**Decided:** <what was chosen, one sentence>
**Why:** <the driving reason/constraint>
**Rejected:** <alternative — why not> (one line each)
**Revisit if:** <condition that invalidates this> (optional)
```

**til** (`<topic>/til/<slug>.md`)

```markdown
---
type: til
tags: []
created: YYYY-MM-DD
updated: YYYY-MM-DD
---
# <Gotcha title>
**Symptom:** <exact error string / observable behavior — greppable>
**Cause:** <mechanism>
**Fix:** <what works>
```

**log** (`log/YYYY-MM-DD.md`)

```markdown
---
type: log
tags: [log]
created: YYYY-MM-DD
updated: YYYY-MM-DD
---
# YYYY-MM-DD
## <project>
- <what happened / shipped / decided — link notes with [[slug]]>
## Next
- <carry-forward items — also mirror into [[todo]]>
```

**todo** (`todo.md`, Henry reads constantly — strictest format)

```markdown
Todo:

- [P0|P1|P2] project: sentence describing task (ticket link, PR link — only if they exist)
```

Nothing else in the file: no Done section (completed items are deleted), no agent instructions, no prose. Sorted by priority.

## Git

The wiki repo is git-backed (private remote `origin`). After any vault writing session, commit and push:

```bash
git -C ~/me/fun/wiki add vault && git -C ~/me/fun/wiki commit -m "vault: <what changed>" && git -C ~/me/fun/wiki push
```

One commit per session is fine; don't ask permission for vault commits.

## Gardening

Periodic integrity sweep (any agent, when asked or ~weekly). Checks:

1. **Map completeness** — every non-family note has a `map.md` line; no map lines pointing at deleted notes.
2. **Dead wikilinks** — `[[slugs]]` that resolve to nothing; fix or mark as intentionally-unwritten.
3. **Frontmatter validity** — every note has `type/tags/created/updated`; `type` from the allowed set.
4. **Family-path violations** — decisions/til/features/specs/log notes outside their pattern paths.
5. **Stale living notes** — `reference` notes whose content contradicts current reality (spot-check the load-bearing ones, e.g. [[agent-skills-and-plugins]]).

Fix mechanically, commit as `vault: gardening sweep`.

## What does NOT belong here

- Routine task detail or session state (use handoffs for that).
- Anything trivially derivable from code, git history, or project config files.
- Claude-specific collaboration preferences (those live in Claude's own memory).
