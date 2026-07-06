---
type: reference
tags: [meta]
created: 2026-07-06
updated: 2026-07-06
---

# Vault Conventions

Shared memory for Henry's agents (Claude Code, Codex, others); sometimes read by Henry in Obsidian. Write for a future agent finding and trusting the note.

## Structure

- One folder per topic (`phoebe/`, `tools/`, `meta/`, …). New folders as topics emerge.
- Filenames: stable kebab-case slugs; they are wikilink targets. Renaming breaks links.
- One note per topic — update, don't near-duplicate. Grep before writing.
- `map.md` (vault root) = topic → note index. Every note create/delete/rename updates its map line in the same action. Family notes are covered by their pattern line, not enumerated.

## Hierarchy

**Freeform** — distinct one-off topics: `<topic>/<slug>.md` at topic root. Campaign and reference notes live here.

**Families** — recurring notes with fixed paths (never invent variants like `decision/` or `daily/`):

| Family | Path | Content |
|---|---|---|
| Daily log | `log/YYYY-MM-DD.md` | End-of-day changelog, sections per project |
| Decisions | `<topic>/decisions/<slug>.md` | Choice + rationale + rejected alternatives |
| TILs | `<topic>/til/<slug>.md` | Gotchas, tool quirks |
| Features | `<topic>/features/<slug>.md` | How a feature works |
| Specs | `<topic>/specs/<slug>.md` | Architecture/design docs |

New recurring pattern → add it to this table first. A family not in this table doesn't exist.

## Frontmatter

```yaml
---
type: campaign | decision | til | reference | log
tags: [phoebe, evals]
created: 2026-07-06
updated: 2026-07-06
---
```

Reuse existing tags. Bump `updated` on every meaningful edit.

## Note types

| Type | What | Shape |
|---|---|---|
| `campaign` | Multi-day arc: what changed, what it did, numbers | The only long form; table + one through-line paragraph. Exemplar: [[recommendation-subagents]] |
| `decision` | Choice locked | ≤ 15 lines; the *why* and rejected paths are the payload |
| `til` | Gotcha/quirk | ≤ 10 lines; exact greppable strings required |
| `reference` | Evergreen topic page | Sections one screen each; updated in place |
| `log` | Daily changelog | ≤ 20 lines; the one append-style type |

## Writing rules

- Payload first: line one after the title = the answer/state/decision.
- Delete, don't compress. Over budget → cut content. Hurts to cut → second note.
- No throat-clearing: no "this note describes…", no restating the title, no unrequested background.
- Bullets/tables by default; prose must earn its place.
- Capture the *why* and rejected approaches — least recoverable information.
- Exact strings for anything greppable: errors, flags, paths, ticket/PR numbers.
- Wikilink liberally; an unresolved link marks a note worth writing.
- No secrets/PII — reference where credentials live, never values.
- Every fact traces to something observed or something Henry said.

## Templates

**decision**

```markdown
# <Title>
**Decided:** <choice, one sentence>
**Why:** <driving reason>
**Rejected:** <alternative — why not> (one line each)
**Revisit if:** <invalidating condition> (optional)
```

**til**

```markdown
# <Title>
**Symptom:** <exact error/behavior — greppable>
**Cause:** <mechanism>
**Fix:** <what works>
```

**log**

```markdown
# YYYY-MM-DD
## <project>
- <shipped / decided / learned — wikilink notes>
## Next
- <carry-forwards — mirror into [[todo]]>
```

**todo** (`todo.md` — strictest; Henry reads constantly)

```markdown
Todo:

- [P0|P1|P2] project: task sentence (ticket/PR links only if they exist)

Backlog:

- project: task sentence (links only if they exist)
```

- `Todo` = active working set, priority-sorted, keep ≤ ~10 items.
- `Backlog` = future/unscheduled/blocked; no priority tag — assign one on promotion to Todo.
- Promote/demote instead of letting Todo rot. Completed = deleted (and logged in `log/done.md`). No Done section, no prose.
- **Staleness check before writing todo.md:** Henry sometimes finishes work without telling agents. Cross-check every item against `log/done.md`; for items with ticket/PR links, verify status (gh / Linear) when in doubt. Remove finished items — don't re-list them.

**done** (`log/done.md`) — worldwide completion tally. Append a line whenever a PR merges or a ticket/task completes:

```markdown
- YYYY-MM-DD: project: one-line summary (ticket/PR links)
```

Append-only, newest last. This is the first place to check for todo staleness.

## Git

Repo is git-backed, private remote. After a vault writing session (no permission needed):

```bash
git -C ~/me/fun/wiki add vault && git -C ~/me/fun/wiki commit -m "vault: <what changed>" && git -C ~/me/fun/wiki push
```

## Gardening

On ask or ~weekly: (1) map completeness both directions, (2) dead wikilinks, (3) frontmatter validity, (4) family-path violations, (5) stale living notes vs reality. Fix, commit `vault: gardening sweep`.

## Not vault material

Session state (use handoffs), anything derivable from code/git, Claude-specific collaboration prefs (Claude memory).
