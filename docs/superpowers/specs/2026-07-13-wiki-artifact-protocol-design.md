# Wiki.app Native Artifact Protocol — Design

**Ticket:** WIKI-85 (to file)
**Date:** 2026-07-13
**Status:** Design approved, ready for implementation
**Author:** wiki orchestrator + Henry (brainstorm)

## Problem

Agents (cc/cdx workers, orchestrators) currently answer questions like "what is the architecture of this codebase?" by either:
1. Writing prose the user has to visualize themselves, or
2. Writing an SVG / mermaid file to `/tmp/dud_file.svg` and telling the user "it's at that path"

Neither ends inside Wiki.app. The `/tmp` path requires the user to leave the app, open a separate viewer, and lose the artifact when they close the session. The prose answer is inspectable but not visualizable.

## Goal

Give agents one MCP tool that emits typed artifacts (mermaid, SVG, raster image, table, plot, code/diff) which render inline in the Wiki.app session view, archive with the run, and stay inspectable (source, download, raw payload).

## Non-goals (v0)

- **Vault persistence** — no writing artifacts to `vault/artifacts/`. They live in the transcript. A future "pin to vault" affordance is possible but out of scope.
- **Artifact updates** — an agent cannot revise an existing artifact; each call produces a new inline artifact.
- **Interactive artifacts** — no bidirectional widgets (form inputs, drag-and-drop). Read-only rendering only.
- **Auto-detection of fenced blocks** — the trigger is exclusively the explicit MCP tool call. Existing `` ```mermaid `` fenced blocks in prose stay code fences.
- **Cross-run reuse** — an artifact belongs to its emitting run. No shared artifact registry.
- **Bytes-payload plot** (matplotlib-style PNG) — v0 accepts Vega-Lite JSON only. Agents that need PNG output emit `kind: "image"`.

## Architecture

```
[cc/cdx worker]
   │ MCP tool_use { name: "render_artifact", input: {kind, title?, caption?, payload} }
   ▼
[wiki-artifacts MCP server]  ← Python stdio, spawned per-run by supervisor alongside provider
   │ 1. validate against per-kind schema
   │ 2. reject if source > 100KB (text kinds) or image > 5MB
   │ 3. for image kind: write bytes to WIKI_AGENT_RUNTIME_DIR/runs/<run-id>/artifacts/<uuid>.<ext>
   │ 4. emit sentinel to stdout: <<wiki-artifact:v1>>{normalized-json}<<end>>
   │ 5. return tool_result: {artifact_id: "<uuid>", ok: true}
   ▼
[supervisor normalizer]  ← new event kind "artifact"
   │ 1. matches sentinel in MCP tool_use result stream
   │ 2. emits normalized event with full payload (text kinds) or ref (image kind)
   │ 3. persisted in raw NDJSON — WIKI-42 restart-safety
   ▼
[frontend session view]  ← existing VirtualSessionRow dispatch
   │ event.kind === "artifact" → <ArtifactBlock event={event} />
   ▼
[<ArtifactBlock />]  ← dispatch on artifact.kind
   │ mermaid → <MermaidRenderer />
   │ svg     → <SvgRenderer /> (DOMPurify-scrubbed)
   │ image   → <ImageRenderer /> (either data URL or backend-served)
   │ table   → <TableRenderer /> (virtualized, sortable, exportable)
   │ plot    → <PlotRenderer /> (vega-embed)
   │ code    → <CodeRenderer /> (Shiki, +diff view when diff_from present)
```

Four new surfaces: MCP server (~150 LOC Python), one supervisor normalizer branch, one backend artifact-serve route, one React component tree. No vault write, no cleanup daemon.

## Protocol

### MCP tool schema

```json
{
  "name": "render_artifact",
  "description": "Render a typed artifact inline in the Wiki.app session view. Prefer this over dumping /tmp/file.svg paths in prose — the artifact is inspectable, downloadable, and lives with the transcript. For prose comparison tables (<= 6 rows, <= 3 columns), write a plain markdown table instead — this tool is for rich or large data.",
  "input_schema": {
    "type": "object",
    "required": ["kind", "payload"],
    "properties": {
      "kind": {
        "enum": ["mermaid", "svg", "image", "table", "plot", "code"]
      },
      "title": {
        "type": "string",
        "maxLength": 200,
        "description": "Short caption shown above the artifact"
      },
      "caption": {
        "type": "string",
        "maxLength": 500,
        "description": "One-line context under the artifact"
      },
      "payload": {
        "description": "Kind-specific payload. See per-kind schemas below."
      }
    }
  }
}
```

### Per-kind payload schemas

**`mermaid`**
```json
{"source": "<mermaid DSL, <=100KB>"}
```

**`svg`**
```json
{"source": "<svg xmlns=... ></svg>  <=100KB"}
```

**`image`**
```json
{"data_base64": "<base64-encoded bytes, decoded <=5MB>", "mime": "image/png|image/jpeg|image/webp"}
```

**`table`**
```json
{
  "columns": [{"key": "id", "label": "ID", "type": "string|number|date|link"}, ...],
  "rows": [[<cell>, ...], ...]
}
```
Column `type` drives rendering (right-align numbers, format dates, wrap links). Rows serialized as arrays for compactness.

Tool description explicitly steers agents: *"Use this ONLY for tabular data with 20+ rows OR data the user will want to sort, filter, export, or inspect. For small comparison tables (≤ 6 rows, ≤ 3 columns), use a plain markdown table in prose."*

**`plot`**
```json
{"spec_vega_lite": {<Vega-Lite JSON, <=100KB>}}
```

**`code`**
```json
{
  "language": "<Shiki-supported id>",
  "filename": "<optional display path>",
  "source": "<code, <=100KB>",
  "diff_from": "<optional; if present, render as unified diff against this>"
}
```

### Normalized event shape

```json
{
  "kind": "artifact",
  "id": "<uuid>",
  "title": "<from tool_use>",
  "caption": "<from tool_use>",
  "artifact": {
    "kind": "mermaid|svg|image|table|plot|code",
    // text kinds: full payload inlined here
    "source": "...",
    // image: ref only
    "ref": "artifact://<uuid>",
    "mime": "image/png",
    "byte_size": 42893
  },
  "ts": "<ISO 8601>"
}
```

### Backend serve route

```
GET /api/agents/{ticket}/artifact/{uuid}
```

Resolves `WIKI_AGENT_RUNTIME_DIR/runs/<run-id>/artifacts/<uuid>.<ext>`, sends with correct `Content-Type`, `Cache-Control: private, max-age=3600`. Returns 404 if artifact doesn't exist. No auth beyond backend's existing localhost-bind (same posture as other agent routes).

## Renderers

Every artifact carries the same chrome:

```
┌───────────────────────────────────────────┐
│  [icon] {title}    [Copy] [Download] [ⓘ]  │
│  {optional caption}                       │
├───────────────────────────────────────────┤
│                                           │
│           <kind-specific renderer>        │
│                                           │
└───────────────────────────────────────────┘
```

- **[Copy]** — copies raw payload (source text for text kinds, TSV for table, base64 for image)
- **[Download]** — triggers `<a download>` with the right extension per kind: `.mmd | .svg | .png | .csv | .json | .<lang>`
- **[ⓘ Inspect]** — opens a side panel with the raw normalized event JSON (payload + metadata). Useful when a render is wrong and you want to see what the agent actually sent.

Per-kind renderer notes:

| Kind | Renderer | Notes |
|---|---|---|
| `mermaid` | `mermaid.js` in a themed container | Dynamic import ~200KB. Re-render on `document.documentElement[data-theme]` change (mirror the WIKI-59 Shiki theme observer). |
| `svg` | inline via `dangerouslySetInnerHTML` after `DOMPurify` scrub | Allowlist: `svg, g, path, rect, circle, ellipse, line, polyline, polygon, text, tspan, defs, use, symbol, title, desc, style, linearGradient, radialGradient, stop, pattern, clipPath, mask, image, marker`. Block: `script, foreignObject, iframe, a[href^="javascript:"]`. +14KB DOMPurify. |
| `image` | `<img src>` — data URL for inline, `/api/agents/<ticket>/artifact/<uuid>` for stored | No new dep. Native `<img>` handles decode + async. |
| `table` | virtualized rows via existing session-scroll pattern; sticky header; click-column sort; row-count badge; Copy-as-TSV / Copy-as-CSV / Copy-as-JSON menu on `[Copy]` | No new dep. Reuse tabular-nums CSS. |
| `plot` | `vega-embed` with our theme colors injected via config | +vega-embed ~300KB. Config: pass current `--font-monospace`, theme accent, background. |
| `code` | Shiki-highlighted via existing WIKI-59 pipeline | `diff_from` triggers unified-diff view (compute diff client-side via a tiny diff lib, e.g. `diff` on npm, ~10KB). Language falls back to plaintext if unknown. |

## Storage

- **Text kinds** (mermaid, svg, table, plot, code) — payload inlined in raw NDJSON event. Cap 100KB per artifact enforced at the MCP server, reject with clear error over 100KB.
- **`image`** — bytes decoded from base64 written to `WIKI_AGENT_RUNTIME_DIR/runs/<run-id>/artifacts/<uuid>.<ext>` (extension inferred from `mime`). Cap 5MB. Event carries only `{ref, mime, byte_size}`. Backend serves via the route above.

Everything archives with the run: WIKI-42 supervisor archive already sweeps `runs/<run-id>/*` including the new `artifacts/` subdir. Reading an archived session → artifact route still resolves. No separate cleanup, no orphan risk.

## Discovery

Wiki-supervisor spawns `wiki-artifacts` as an MCP stdio server as part of the same `spawn` code path that starts the provider CLI. Wiring:

- **Codex**: MCP servers registered via `~/.codex/config.toml` OR provided at `codex app-server --stdio` startup. Since the supervisor already owns codex spawn args (WIKI-42), it adds the artifact server there. Zero user-side config.
- **Claude**: MCP servers registered via `claudeMcpServers`. Same story — supervisor adds it to the claude CLI args.

The MCP server binary ships inside the wiki-backend sidecar (same bundle strategy as `wiki-backend`). It reads `WIKI_AGENT_RUNTIME_DIR` and `WIKI_RUN_ID` from env at startup so it knows where to write image bytes.

## Testing

Backend:
- `test_wiki_artifacts_server.py` — one test per kind: valid payload accepted + returns tool_result, oversized payload rejected with clear error, malformed shape rejected.
- `test_supervisor_normalizer_artifact.py` — raw MCP tool_use sentinel → normalized `artifact` event, one case per kind.
- `test_artifact_serve.py` — `GET /api/agents/{ticket}/artifact/{uuid}` returns bytes with correct Content-Type; 404 for unknown UUID; scope check that a bad ticket ID can't grab another ticket's artifact.

Frontend (Playwright):
- `wiki-86-artifacts.playwright.mjs` — fixture session with one artifact per kind → each renders visible, `[Copy]` puts source on the clipboard, `[Inspect]` opens the side panel with the raw payload.
- XSS regression: fixture SVG with `<script>alert(1)</script>` and `<a href="javascript:alert(1)">` → sanitizer strips, no alert.

## Rollout

Single PR closes WIKI-85. No feature flag — the tool is additive; agents that don't call it see zero behavior change. Prompt updates for cc/cdx worker skills happen in a follow-up (the tool description alone should be enough for a well-behaved agent).

## Follow-ups (out of scope for WIKI-85)

- Pin-to-vault affordance (turn ephemeral artifact into vault-persistent note attachment)
- Bytes-payload `plot` variant (matplotlib PNG straight into a Plot artifact so agents don't have to encode as `image`)
- Cross-artifact ref (agent A emits artifact X, agent B references it in a later run)
- Interactive widgets (form inputs, drag-to-explore plot)
- Skill files documenting the tool for user-invoked agents (Codex `~/.codex/skills/`, Claude `~/.claude/skills/`)
