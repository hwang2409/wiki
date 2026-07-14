# Knowledge Layer: SQLite-Indexed Search over Vault + Fleet History

Date: 2026-07-14
Status: approved (Henry, 2026-07-14 brainstorm)
Ticket: WIKI-100

## Goal

Any agent in any session can ask "what do we know about X" and get ranked,
cited answers spanning vault notes AND wiki-managed run history in one tool
call. Retrieval becomes a query, not the map.md-walk + grep ritual.

Motivating incident: a worker transcript held a complete Modal-deploy
diagnosis that was never filed as a note; a teammate re-derived it from
scratch. Unfiled knowledge must still be findable.

## Non-goals

- Replacing markdown/git/Obsidian as vault source of truth (files stay truth).
- Multi-device sync.
- UI surfaces (v1 is agent-facing only; Wiki.app panes come later).
- Indexing personal CLI sessions outside the fleet (`~/.codex/sessions`,
  `~/.claude/projects` are NOT crawled).
- Embeddings/semantic search (v2; schema leaves room, v1 ships FTS only).

## Decisions (locked)

| Question | Decision |
|---|---|
| Driver | Search/query power |
| Vault truth | Files remain source of truth; DB is a derived, rebuildable index |
| Transcript scope | Wiki-managed runs only: supervisor runs + archives (~205M) |
| Capabilities | v1: FTS + link graph. v1.5: structured analytics. v2: embeddings |
| Consumers | Agents first: `wiki search` CLI + `search_knowledge` MCP tool |
| Engine | Single SQLite file (stdlib `sqlite3`, FTS5), no daemon, no new infra |

## Architecture

One SQLite database at `~/.wiki/knowledge.db` (path env-overridable,
`WIKI_KNOWLEDGE_DB_PATH`, tests use temp DBs — never the live file).

### Schema (v1)

- `meta(schema_version, built_at)` — version bump ⇒ drop + full rebuild. No
  migrations, ever.
- `notes(path, title, type, tags, created, updated, mtime, content_hash)`
- `chunks(id, source_kind {note|event}, source_id, ticket, heading, text,
  pos)` — heading-bounded chunks for notes; per-event text for transcripts
- `chunks_fts` — FTS5 virtual table (content= external content mode) over
  `chunks.text` + title/heading, `porter` tokenizer
- `links(src_note, dst_name, resolved_path)` — wikilink graph: backlinks,
  orphans, unresolved
- `runs(run_id, ticket, provider, model, role, spawned_at, ended_at,
  outcome)` — from registry snapshots + archive meta
- `events(run_id, seq, type, ts, text_excerpt)` — normalized events.jsonl
  rows; text goes into `chunks` for FTS

### Ingest (never blocks writers)

- **Vault**: mtime + content-hash delta scan. Triggers: backend startup,
  after each `wiki` CLI transactional op (enqueue only — op returns first),
  and `wiki index rebuild`. 46 files/612K ⇒ full scan <100ms; no fs watchers.
- **Runs**: hook at archive time in `store.py` (after events.jsonl persist,
  same lifecycle step) — parse normalized events, insert rows. Live runs:
  lazily indexed on first query hit (delta by seq), not per-event.
- All ingest runs in a background task inside the existing FastAPI backend;
  CLI ops and supervisor operations gain 0ms.

### Query surface

- `wiki search "<query>" [--ticket T] [--kind note|run] [--type <event>]
  [--since DATE] [--limit N]` — ranked results with source path (note) or
  run_id+seq (event), snippet, score. JSON output mode for agents.
- `search_knowledge` MCP tool on the existing wiki-artifacts server pattern
  (registered for workers; orchestrator registration ships with WIKI-88).
  Same params/results as CLI.
- Graph queries: `wiki links backlinks <note>`, `wiki links orphans`,
  `wiki links unresolved` (replaces part of `wiki lint`'s scan).

### Rebuild invariant

`rm knowledge.db && wiki index rebuild` restores everything from files.
DB is cache, never hostage. Rebuild is the only migration mechanism.

## Performance budgets (acceptance criteria)

- `wiki` CLI op added latency: **0ms** (enqueue only)
- Archive-time ingest hook: **<1s** per run
- Query p95: **<50ms**
- Full rebuild (vault + all archived runs): **<60s**
- Index staleness: seconds acceptable; reads never block writes

## Phasing

1. **v1 (this ticket, WIKI-100)**: schema, ingest, FTS, link graph, CLI +
   MCP tool, rebuild command.
2. **v1.5**: analytics views/queries (tokens per ticket, cost per merged PR,
   spawn→merge time) — `runs`/`events` tables already carry the data.
3. **v2**: embeddings via sqlite-vec on `chunks`, model TBD
   (local vs API decided then).

## Error handling

- Index corruption / schema mismatch ⇒ log, drop, rebuild in background;
  queries during rebuild return partial results with a `rebuilding: true`
  flag rather than erroring.
- Malformed events.jsonl lines skipped with counter (mirrors normalizer's
  unknown-count pattern).
- DB unavailable ⇒ CLI/MCP degrade with explicit error; never blocks vault
  writes or supervisor lifecycle.

## Testing

- Unit: chunker (heading bounds, fence-safety), wikilink resolver, delta
  scan (mtime + hash), FTS ranking smoke, rebuild idempotence
  (rebuild twice ⇒ identical row counts/hashes).
- Integration: temp vault + fake archived run ⇒ ingest ⇒ query returns both
  note and event hits with correct citations; archive hook fires through
  real store.py path against isolated registry/runtime dirs (WIKI-42 test
  conventions — never the live `/tmp/agent-registry.json`).
- Budgets asserted: rebuild timing on fixture corpus, query latency bound.
