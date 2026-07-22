---
type: reference
tags: [wiki, ui, polish]
created: 2026-07-22
updated: 2026-07-22
---

# Wiki.app polish census (WIKI-150)

Systematic audit of every visible surface. Goal: move Wiki.app from "solid prototype" to "polished product" without adding features. Fans out into 8-12 small implementation tickets (WIKI-151+).

Related: [[wiki-app-ui-direction]] (Obsidian-clone direction), WIKI-143 (design tokens), WIKI-145 (chrome reduction), WIKI-144 (badge sweep, in flight).

Audit basis: source-only review of `frontend/src/` at `9399de9` on 2026-07-22. No app run or screenshots. WIKI-144 and WIKI-146–149 were still in flight, so their named surfaces are rated as they exist on this commit and carry the in-flight dependency where relevant.

## Rubric (polish 1-5)

| score | meaning |
|-------|---------|
| 5 | shippable-as-is; user reads as intentional; nothing to change |
| 4 | one small refinement away (spacing tweak, copy tweak) |
| 3 | acceptable but obviously prototype in a specific way (name / color / density) |
| 2 | actively looks like dev leftover; distracts from perceived quality |
| 1 | breaks the illusion; user assumes broken or half-built |

Baseline: anything <=3 gets a ticket. Anything 4 gets a follow-up note; anything 5 is done.

## Failure categories (tag each row)

- **COPY** — user-facing string reads like code jargon ("Provider stream", "sidecar", "artifact_refs", raw run_id). Rewrite from user's perspective.
- **CHROME** — decorative but noisy: divider, badge, border, background — reduce or kill.
- **INCONSISTENT** — deviates from tokens: padding, radius, color, weight, icon size, hover behavior.
- **DEBUG** — dev-only output leaking into product (raw JSON, timestamps that look like log lines, run IDs).
- **EMPTY** — first-run / zero-data state missing or ugly.
- **LOADING** — mid-fetch state missing, spinner-instead-of-skeleton, or content pop-in.
- **ERROR** — failure state undefined; blank screen, uncaught exception UI, or ambiguous toast.
- **HIERARCHY** — surface mixes unlike things without clear IA (e.g. left sidebar mixing workspace nav + agent list + tabs).
- **ACCESSIBILITY** — focus ring missing, contrast fails, keyboard nav dead-end.

## Census

### Global chrome

| surface | file(s) | polish | tags | issues | proposed action | ticket |
|---------|---------|--------|------|--------|----------------|--------|
| app ribbon | `frontend/src/App.tsx:3433` | 2 | HIERARCHY, CHROME | Eleven same-weight icons mix note creation, sidebar modes, terminal creation, analytics, fleet operations, settings, and theme; only one spacer expresses hierarchy. | Group content, run-management, and app controls; keep the three primary destinations visible and move secondary utilities into one labeled overflow. | WIKI-151 |
| left sidebar shell | `frontend/src/App.tsx:3557` | 3 | HIERARCHY, CHROME | Files, search, and agents replace the same unlabeled pane, while their controls and density differ sharply; there is no persistent section title. | Add a quiet mode title, align header/action spacing, and give files/search/sessions the same loading, empty, and error frame. | WIKI-151 |
| tab header | `frontend/src/App.tsx:3691` | 3 | CHROME, HIERARCHY | A bordered header repeats only the current title while actual window switching lives in the bottom rail; it looks like a nonfunctional tab. | Either turn it into the real active-window tab with close affordance or remove the extra bar and let the view header carry the title. | WIKI-151 |
| session header and "Provider stream" area | `frontend/src/agent-session-surface.tsx:477`, `frontend/src/session.tsx:838` | 2 | COPY, CHROME, DEBUG, HIERARCHY | Ticket, provider kind, role, model, actions, state strip, `Unknown 0`, and a second provider-inspector header stack before conversation content. "Provider stream" and raw/normalized counts are diagnostics. | Keep ticket/title plus one state and action menu in the header; move provider/model/normalization telemetry into a collapsed "Run details" inspector that auto-opens only for action-required states. | WIKI-152 |
| composer | `frontend/src/session.tsx:3097` | 3 | COPY, CHROME, HIERARCHY | The input, queued cards, attachment strip, skill menu, thinking label, subagent chips, Vim mode, and a long keyboard-instruction placeholder compete at once. | Use a short task-focused placeholder; show keyboard/Vim help on focus or in a help popover; reserve the status row for active send/queue state. Re-audit after WIKI-148. | WIKI-152 (after WIKI-148) |
| status bar / bottom rail | `frontend/src/App.tsx:3800` | 2 | COPY, DEBUG, HIERARCHY | `0:label` tmux notation and "No windows" expose the implementation model, while note metrics appear only in some modes and the top tab header duplicates active-window identity. | Rename the rail to user-facing workspace tabs, drop colon notation, preserve keyboard indices as muted shortcuts, and keep note metrics in a separate right-aligned status area. | WIKI-152 |
| notice / banner strip | `frontend/src/App.tsx:3744` | 3 | ERROR, CHROME | One global strip prints raw API error text with no operation label, recovery action, or persistence rule. | Standardize inline notices with a plain-language title, affected action, retry/dismiss controls, and mapped copy for network versus validation failures. | WIKI-158 |

### Notes, files, and workspace views

| surface | file(s) | polish | tags | issues | proposed action | ticket |
|---------|---------|--------|------|--------|----------------|--------|
| file explorer tree | `frontend/src/App.tsx:1129`, `frontend/src/App.tsx:3607` | 3 | INCONSISTENT, ERROR, EMPTY | Inline indentation uses 17px increments, loading is defined, but file-tree fetch failures are silently converted back to an unloaded empty tree and can read as "No notes yet." | Move indentation to a token, distinguish no notes from workspace unavailable, and add an inline retry row without clearing the last successful tree. | WIKI-151 |
| workspace selector | `frontend/src/App.tsx:3591` | 3 | COPY, HIERARCHY, ERROR | Raw workspace IDs are the only labels; discovery failure silently falls back to `wiki`, making a missing workspace look like user choice. | Show a friendly label plus abbreviated path, mark unavailable roots, and display a recoverable discovery error instead of silently changing selection. | WIKI-151 |
| search panel | `frontend/src/App.tsx:3645` | 4 | EMPTY | Clear prompt and no-result state; only minor issue is generic `Search...` copy. | Change placeholder to "Search notes" and preserve the query when switching sidebar modes. | — |
| note reading view | `frontend/src/pane.tsx:357`, `frontend/src/markdown.tsx` | 4 | — | Restrained layout and renderer hierarchy are coherent after WIKI-141/143/145. | Keep current treatment; verify long tables and math in the next visual regression pass. | — |
| metadata / properties | `frontend/src/pane.tsx:361` | 4 | INCONSISTENT | Clean compact rows, but array pills inherit the broader radius-token ambiguity. | Converge pill radius with WIKI-160; no separate feature work. | WIKI-160 |
| source editor / new note | `frontend/src/App.tsx:3288`, `frontend/src/source-editor.tsx` | 4 | COPY | Editor is intentional; the optional raw `folder/note.md` field is technical but appropriate for the owner workflow. | Add a visible label to the path field and retain the current source-first behavior. | — |
| kanban board | `frontend/src/kanban.tsx:173`, `frontend/src/kanban.tsx:246` | 2 | ACCESSIBILITY, COPY, EMPTY | Moving/completing cards is drag-only, editing is undiscoverable double-click, and "logs to done.md" exposes storage instead of outcome. Empty state has no add path. | Add keyboard move/complete/edit actions and visible card menu; rewrite completion copy as "Mark complete" with destination detail in secondary text; let an empty board add its first lane/card. | WIKI-159 |
| backlinks | `frontend/src/pane.tsx:398` | 4 | — | Clear "Linked mentions" hierarchy, compact count, and direct navigation. | Keep; align spacing during token convergence only. | — |
| code-file pane | `frontend/src/code-file-pane.tsx:124` | 4 | ERROR | Find and code density are solid; errors are raw API strings and loading uses three periods. | Map file errors and normalize ellipsis during copy sweep. | — |
| terminal pane | `frontend/src/terminal-pane.tsx:155` | 4 | COPY | `terminal://id`, renderer, and connection state are technical, but this is explicitly an operator terminal and the restart failure state is defined. | Replace renderer chip with a tooltip-only diagnostic; retain terminal identity and connection state. | — |

### Chat / transcript

| surface | file(s) | polish | tags | issues | proposed action | ticket |
|---------|---------|--------|------|--------|----------------|--------|
| user message | `frontend/src/session.tsx:1458` | 4 | — | Clear right-aligned message treatment; pending-send states are visible and actionable. | Keep; token-align pending-state spacing in WIKI-160. | — |
| assistant message | `frontend/src/session.tsx:1509` | 4 | — | Plain prose after WIKI-145 reads intentionally and uses the full markdown pipeline. | Keep; no new ticket. | — |
| tool call block | `frontend/src/session.tsx:972` | 3 | DEBUG, COPY | Collapsed summaries are useful, but `err` is cryptic and expansion immediately exposes unbounded raw input/output without copy or wrapping controls. | Keep the human summary row; label failure as "failed"; add bounded preview, copy-full, and open-full controls for raw input/output. | WIKI-153 |
| bash / tool result block | `frontend/src/session.tsx:1182` | 2 | DEBUG, COPY | stdout and stderr are visually dumped with no labels, copy action, size cap, or line-count summary; the only affordance is "expand output." | Separate command, output, and error; default to a two-to-six-line preview with line/byte count; add copy-full and preserve ANSI in expanded view. | WIKI-153 |
| system / hook / marker messages | `frontend/src/session.tsx:1270`, `frontend/src/session.tsx:1475` | 3 | DEBUG, COPY, CHROME | Normalized text is rendered verbatim in bordered rows; internal hook or API wording can become product copy and marker families do not share a single severity grammar. | Whitelist user-meaningful event families, map them to short verbs, hide internal-only events in Run details, and use one info/warn/error row style. | WIKI-153 |
| provider action-required card | `frontend/src/session.tsx:405` | 2 | DEBUG, COPY, HIERARCHY | Header prints request kind and request ID; fallback asks users for "Provider response JSON" and exposes a "Raw request" dump. | Present only the question and choices by default; rename fallback to "Advanced response" and put request kind/ID/payload under a guarded Details disclosure. | WIKI-152 |
| loading / streaming state | `frontend/src/session.tsx:2363`, `frontend/src/session.tsx:3309` | 3 | LOADING | Initial skeleton is good, but live work is a small `thinking...` label detached from the last assistant turn; streamed content has no dedicated stable placeholder. | Anchor a restrained working row at transcript tail, keep its height stable through first token, and expose cancel/interrupt nearby when available. | WIKI-155 |
| error message | `frontend/src/session.tsx:2363`, `frontend/src/session.tsx:3172` | 2 | ERROR | Session and composer errors are raw strings in otherwise empty containers, with no retry or distinction between unavailable transcript, disconnected agent, and rejected message. | Define three mapped error states with retry/reconnect/edit controls and retain the last good transcript. | WIKI-155 |
| empty session state | `frontend/src/session.tsx:2384` | 1 | EMPTY | A successfully loaded session with zero events renders an empty scroll area and composer; it is indistinguishable from a broken transcript parser. | Add an explicit state for ready-to-start, waiting for first response, archived with no transcript, and unsupported/corrupt transcript. | WIKI-155 |
| timestamps | `frontend/src/timestamp.tsx:52`, `frontend/src/session.tsx:1619` | 4 | — | Sparse per-turn timestamps with absolute hover avoid log-line density. | Keep. | — |
| model / format / disposition footer | `frontend/src/session.tsx:2380`, `frontend/src/session.tsx:2463` | 2 | DEBUG, COPY, CHROME | Format, model ID, tokens, and `Unknown N` are always-present telemetry; `Unknown 0` is also repeated above the transcript. | Keep model switching as an action; move format/token/disposition telemetry to Run details and suppress zero-value unknown counts entirely. | WIKI-152 |

### Agent management

| surface | file(s) | polish | tags | issues | proposed action | ticket |
|---------|---------|--------|------|--------|----------------|--------|
| agents page toolbar / hierarchy | `frontend/src/agents.tsx:1355` | 2 | HIERARCHY, COPY, CHROME | "Workers" heads a page that contains orchestrators, workers, archives, account events, spawn controls, and a session preview; unlike information has equal visual weight. | Rename the page "Runs," split Active and History, make start-run the primary action, and move provider/account health to a compact contextual banner. | WIKI-154 |
| live worker card | `frontend/src/agents.tsx:1082` | 2 | DEBUG, HIERARCHY, CHROME | Ticket, kind, role, model, state, age, step, blocker, health, tmux window, run ID, runtime state, handoff chain, worktree, PR, and six actions can appear in one card. | Default card to title, state, current step, age, and one primary action; move runtime IDs, process/window details, history, and worktree into an expandable technical details section. | WIKI-154 |
| archived run card | `frontend/src/agents.tsx:1293` | 3 | HIERARCHY, COPY | Cards reuse active-run visual grammar and the action remains "log" even when the preview is a normalized transcript. | Use a quieter history row, label action "View transcript," and group by completion date/outcome. | WIKI-154 |
| account / auth banner | `frontend/src/agents.tsx:730` | 2 | DEBUG, COPY, ERROR | Copy exposes "codex auth-dead," account rotation internals, revival caps, ticket arrays, and raw failure reasons. | Translate to user outcome and next action; keep account/provider diagnostics behind Details; persist actionable failures until resolved. | WIKI-154 |
| lifecycle controls | `frontend/src/agents.tsx:1014` | 3 | COPY, HIERARCHY | "Interrupt," "Revive," "Complete," "Stop," "Replace," "Archive," "Review," and "log" can coexist without a clear primary/secondary hierarchy. | Show one context-sensitive primary action and put destructive/technical actions in a menu with consistent confirmation copy. | WIKI-154 |
| session preview sidebar | `frontend/src/session.tsx:3393` | 3 | HIERARCHY, CHROME | Preview duplicates session header metadata and can open/review elsewhere, creating a third navigation layer inside the already dense agents page. | Make the row click open the full transcript; reserve preview for a lightweight recent-message peek with one "Open transcript" action. | WIKI-154 |
| PR review panel | `frontend/src/agent-pr-review.tsx:115` | 4 | INCONSISTENT | Checks, threads, diff, confirmation, and empty states are complete; lowercase action labels and badge sweep remain minor. | Re-audit badge density after WIKI-144 and normalize action casing. | WIKI-144 |

### Dashboard

| surface | file(s) | polish | tags | issues | proposed action | ticket |
|---------|---------|--------|------|--------|----------------|--------|
| dashboard tickets grid | `frontend/src/dashboard.tsx:144` | 4 | — | Compact sortable table with good count and persistence; reads as intentional. | Keep. | — |
| PR row | `frontend/src/dashboard.tsx:196` | 4 | COPY | PR number and link are clear; missing PR is an intentional dash. | Keep. | — |
| worker / status row | `frontend/src/dashboard.tsx:205` | 3 | INCONSISTENT, COPY | Backend state strings flow directly into a broad badge state map and can fall through to `unknown`; styling overlaps WIKI-144. | Normalize status vocabulary to queued/working/needs attention/ready/done and use the shared badge component after WIKI-144. | WIKI-160 (after WIKI-144) |
| empty state | `frontend/src/dashboard.tsx:171` | 3 | EMPTY | Copy explains absence but provides no route to start a run or clear active filters from within the empty panel. | Add one contextual action: "Start a run" for zero tickets, "Clear filters" for zero matches. | WIKI-155 |
| loading skeleton | `frontend/src/dashboard.tsx:144` | 1 | LOADING | When `tickets` is null the header renders with no count, filter, skeleton, or loading message; the page appears empty. | Render a table-shaped skeleton with stable column widths and replace it in place. | WIKI-155 |
| filters row | `frontend/src/dashboard.tsx:236` | 4 | ACCESSIBILITY | Clear filters and date/multi-select controls are coherent; custom listbox keyboard semantics are incomplete but native checkboxes remain operable. | Add roving focus/arrow support during WIKI-159 accessibility pass. | WIKI-159 |

### Session list

| surface | file(s) | polish | tags | issues | proposed action | ticket |
|---------|---------|--------|------|--------|----------------|--------|
| session row | `frontend/src/agents.tsx:1562` | 3 | HIERARCHY, INCONSISTENT | Ticket, terse raw state, and age are compressed into one row; provider groups indent differently and active/archived rows use separate dot semantics. | Standardize row anatomy to name, human state, relative activity, and optional unread marker; preserve provider/model in tooltip/details. | WIKI-151 (after WIKI-147) |
| unread indicator | `frontend/src/agents.tsx:1562` | 3 | INCONSISTENT | No current unread state; WIKI-147 is in flight and must coexist with state dots without making a two-dot row. | Let WIKI-147 own a single unread marker; fold runtime state into text rather than adding a second competing dot. | WIKI-147, then WIKI-151 |
| empty state | `frontend/src/agents.tsx:1545` | 2 | EMPTY | Sidebar says only "No workers" despite also listing orchestrators and archived runs; no action or explanation. | Say "No runs yet" and offer "Start a run" plus a secondary link to the Agents/Runs page. | WIKI-151 |
| grouping / sort headers | `frontend/src/agents.tsx:1577` | 3 | HIERARCHY, COPY | Raw orchestrator IDs act as group labels; archived is a divider at the bottom; no sort or live/history count is stated. | Label Active and History explicitly, sort active by attention then recency, and show ownership as secondary metadata rather than the primary hierarchy. | WIKI-151 |

### Artifacts

| surface | file(s) | polish | tags | issues | proposed action | ticket |
|---------|---------|--------|------|--------|----------------|--------|
| artifact list / transcript block | `frontend/src/artifact-block.tsx:870` | 3 | CHROME, INCONSISTENT | A border, left bar, tinted header, icon, title, caption, count badge, and up to four actions make every artifact heavier than neighboring assistant content. | Keep the left alignment cue and title; reveal secondary actions on hover/focus, remove redundant header background, and apply WIKI-144 badge result. | WIKI-156 (after WIKI-144) |
| artifact viewer chrome | `frontend/src/artifact-panel.tsx:83` | 2 | CHROME, COPY, DEBUG | Tabs show raw kind, fallback titles expose artifact ID prefixes, "Recently closed" adds a second tab strip, and a disabled "Pin to vault — Coming soon" control ships unfinished UI. | Remove the coming-soon control, hide IDs/kinds unless no title exists, move recently closed into the tab menu, and add a clear viewer title/action hierarchy. | WIKI-156 |
| table renderer | `frontend/src/artifact-detail/table.tsx:88` | 4 | — | Filter, sort, row/column copy, counts, and scroll state are complete. | Keep; align toolbar spacing in WIKI-160. | WIKI-160 |
| mermaid renderer | `frontend/src/artifact-block.tsx:210` | 4 | ERROR | Sanitized render, compact preview, pan/zoom, and render-failure feedback are complete. | Replace raw Mermaid error details with a short mapped message plus Details. | — |
| plot renderer | `frontend/src/artifact-block.tsx:428` | 3 | LOADING, ERROR | The plot container is blank while Vega loads; render errors print library text, and no retry exists beyond opening the detail reset control. | Add plot skeleton, friendly failure with Details, and retry/reset action. | WIKI-156 |
| SVG renderer | `frontend/src/artifact-block.tsx:258` | 3 | COPY, LOADING, ERROR | "Sanitizing SVG…" describes implementation, and raw sanitizer/render errors are shown as primary copy. | Use "Preparing image…" and a shared visual-artifact error component with retry and technical Details. | WIKI-156 |
| image renderer | `frontend/src/artifact-block.tsx:304` | 4 | ERROR | Intentional lazy rendering and descriptive fallback alt; broken-image recovery is the remaining minor gap. | Add a compact failed-image placeholder in WIKI-156. | WIKI-156 |
| code renderer | `frontend/src/artifact-detail/code.tsx:105` | 3 | INCONSISTENT, ACCESSIBILITY | Detail view has find/fold controls but no copy action on this commit, heuristic folding has no language awareness, and fold state is button-only in a narrow gutter. | Land WIKI-149 copy/diff work, then add a toolbar copy control, keyboard fold action, and shared code typography. | WIKI-156 (after WIKI-149) |
| diff renderer | `frontend/src/artifact-detail/diff.tsx:4` | 4 | — | Split detail and unified compact views have clear empty state. | Re-audit after WIKI-149; no new batch. | WIKI-149 |
| artifact inspector | `frontend/src/artifact-block.tsx:941` | 2 | DEBUG, COPY | "Artifact event" opens the entire normalized event as raw JSON beside the artifact, including IDs and transport metadata. | Rename to "Details," show title/type/size/source status as structured rows, and put raw JSON behind a second explicit "View raw event" disclosure with copy. | WIKI-153 |
| unknown / unsupported / missing payload | `frontend/src/artifact-block.tsx:828`, `frontend/src/artifact-panel.tsx:131` | 2 | ERROR, EMPTY | Missing payload becomes a terse error; viewer says payload unavailable but gives no close, retry, or raw-download recovery; unsupported kinds rely on trusted typing. | Add a shared fallback with kind/title, reason, copy/download raw payload when present, retry, and close-panel action. | WIKI-156 |

### Command palette and switchers

WIKI-146 had not shipped into the audited commit, so there is no separate command-palette surface to rate. The existing quick and fleet switchers remain visible and were audited.

| surface | file(s) | polish | tags | issues | proposed action | ticket |
|---------|---------|--------|------|--------|----------------|--------|
| quick switcher | `frontend/src/switcher.tsx:398` | 4 | COPY | Keyboard behavior, grouping, loading, and no-results are defined; session results still expose raw role/provider metadata. | Let WIKI-146 absorb command actions and replace raw session metadata with friendly state labels. | WIKI-146 |
| fleet switchers | `frontend/src/switcher.tsx:489` | 4 | — | Arrow/j-k navigation, active state, empty state, and disabled rows are coherent. | Keep; align padding during WIKI-160. | WIKI-160 |

### Utility pages

| surface | file(s) | polish | tags | issues | proposed action | ticket |
|---------|---------|--------|------|--------|----------------|--------|
| activity feed | `frontend/src/activity.tsx:61` | 3 | COPY, DEBUG | Raw commit messages, one-letter file statuses, ISO day headings, and clock times make this read like git plumbing; error text lacks retry. | Use friendly day labels, expand file-status words, retain commit metadata in Details, and add retry without discarding loaded commits. | WIKI-157 |
| graph view | `frontend/src/graph.tsx:440` | 1 | ACCESSIBILITY, EMPTY, LOADING, ERROR | Canvas-only output has no visible loading, empty, error, legend, keyboard focus model, or nonvisual node list. | Add explicit states, keyboard-selectable companion list/search, canvas instructions, focus-visible node selection, and accessible summary. | WIKI-157 |
| vault health | `frontend/src/health.tsx:55` | 3 | COPY, DEBUG, HIERARCHY | "agent memory rots" and `wiki lint` are internal operator guidance; freshness buckets lack a page title/definition and empty state has no filter reset. | Rename to "Note freshness," explain thresholds in secondary help, move CLI advice to Details, and provide "Show all types" in empty state. | WIKI-157 |
| token usage | `frontend/src/tokens.tsx:93` | 2 | COPY, LOADING, ERROR | Initial fetch renders zero totals and an empty chart instead of loading; copy exposes `cli`, "sessions scanned," and `refreshing...`; errors are raw and visually last. | Add stable skeleton, page title/metric definitions, friendly source labels, top-level error/retry, and proper ellipsis. | WIKI-157 |

### Modals and menus

| surface | file(s) | polish | tags | issues | proposed action | ticket |
|---------|---------|--------|------|--------|----------------|--------|
| settings | `frontend/src/settings.tsx:713` | 3 | COPY, INCONSISTENT, ACCESSIBILITY | Copy says "UI chrome" and uses a `wiki agent register` specimen; modal has Escape/backdrop close but no focus trap or focus restoration. | Rewrite labels around appearance/readability, use neutral preview text, trap focus, restore opener focus, and token-align rows/controls. | WIKI-160 |
| spawn worker / orchestrator dialogs | `frontend/src/agents.tsx:250`, `frontend/src/agents.tsx:526` | 2 | COPY, DEBUG, HIERARCHY | Raw `cc`/`cdx`, role enums, reasoning effort, working directory, orchestrator ID, byte counts, and kickoff prompt are presented with equal weight. | Make provider/model an Advanced section, label the primary task/name inputs plainly, use friendly provider names, and show byte limits only near the limit. | WIKI-154 |
| replace dialog | `frontend/src/replace-agent-modal.tsx:110` | 2 | COPY, DEBUG | "Provider kind," "active provider process," and "durable context" explain machinery rather than user impact. | Lead with "Restart this run?" and preserved/ended effects; put provider/model overrides under Advanced. | WIKI-154 |
| confirm-destructive dialogs | `frontend/src/App.tsx:976`, `frontend/src/App.tsx:2706` | 3 | COPY, ACCESSIBILITY | "Git is the undo" is technical reassurance; generic dialog lacks `aria-modal`, labelled title association, focus trap, and focus restoration. | Say the note is recoverable from version history, include full target path as secondary text, and implement shared accessible dialog behavior. | WIKI-159 |
| context menu | `frontend/src/App.tsx:3830` | 4 | ACCESSIBILITY | Clear concise actions, but it is a positioned div without menu role, arrow navigation, or focus management. | Add menu semantics and keyboard navigation in WIKI-159. | WIKI-159 |

### Global states

| surface | polish | tags | issues | proposed action | ticket |
|---------|--------|------|--------|-----------------|--------|
| app first-run (no workspaces / notes) | 2 | EMPTY, HIERARCHY | The shell opens to "No file is open" while the sidebar separately says "No notes yet"; there is no guided distinction between an empty vault and an undiscovered workspace. | Show one centered first-run state with detected vault/workspace status, "Create first note," and "Choose workspace" actions; suppress duplicate sidebar empty copy. | WIKI-158 |
| offline / backend-down | 1 | ERROR | Boot eventually leaves a normal-looking empty shell plus a raw notice; workspace discovery failures are swallowed, SSE disconnect has no visible state, and there is no retry/reconnect state. | Replace the main pane with a persistent reconnecting state, preserve cached navigation if available, show retry and diagnostic Details, and announce recovery. | WIKI-158 |
| provider-auth failure | 2 | ERROR, COPY, DEBUG | No contextual session-level state exists; only transient Agents-page banners such as "codex auth-dead" or "usage limit hit" surface, and the prior ribbon badge was removed. | Show a persistent, scoped "Provider sign-in required" state on affected runs with re-auth instructions/action; keep rotation/revival internals in Details, not global ribbon chrome. | WIKI-158 |
| update-available banner | 1 | EMPTY | No update-available surface was found in the frontend, so native updates can be invisible or rely on out-of-app behavior. | Add a low-noise, dismissible update notice with version, "Restart to update," "Later," and failure/retry states; show only when an update is actually staged. | WIKI-158 |

### Copy audit (single sweep)

Every visible dev-jargon string found in the audited source, with the replacement or disposition:

- [ ] `Provider stream` (`session.tsx:860`) → `Run details`; collapsed unless action is required.
- [ ] `raw N → normalized N` (`session.tsx:861`) → hide from default chrome; `N events processed` inside technical Details.
- [ ] provider plus raw runtime state (`session.tsx:870`) → friendly run state; provider name remains secondary.
- [ ] `Provider response JSON` / `Send JSON` (`session.tsx:513`, `:537`) → `Advanced response` / `Send response`.
- [ ] `Raw request` (`session.tsx:527`) → `Request details`; payload behind a second explicit raw disclosure.
- [ ] raw request kind and `request <id>` (`session.tsx:465`) → omit from card header; show only in Details.
- [ ] `No normalized provider events yet.` (`session.tsx:901`) → `No run details available.`
- [ ] `Unknown N` disposition (`session.tsx:374`) → suppress at zero; `N unrecognized events` in Run details when nonzero.
- [ ] raw format/model/token footer (`session.tsx:2463`) → model action only in footer; remaining telemetry in Run details.
- [ ] `cc` / `cdx` (`agents.tsx:298`, `:589`) → `Claude` / `Codex`; keep IDs in Advanced details.
- [ ] `Provider kind` (`replace-agent-modal.tsx:140`) → `Provider`; move under Advanced.
- [ ] `Reasoning effort` (`agents.tsx:346`, `:620`) → `Response depth` or retain only as an Advanced model control.
- [ ] `Working dir` / `Project directory` (`agents.tsx:371`, `:566`) → `Project folder`.
- [ ] `Orchestrator id` (`agents.tsx:384`, `:549`) → `Coordinator` on worker form; `Run group name` on orchestrator form.
- [ ] `Kickoff prompt` (`agents.tsx:403`) → `Task`.
- [ ] prompt/goal byte counts (`agents.tsx:415`, `:656`) → hide until 80% of limit, then show friendly remaining size.
- [ ] `run <id> · <runtime_state>` (`agents.tsx:1131`, `:1241`) → move into Technical details; default shows friendly state only.
- [ ] `tmux <window>` (`agents.tsx:1125`, `:1424`) → move into Technical details.
- [ ] `session N · prev:` handoff chain (`agents.tsx:1136`) → `Run history` disclosure with dated entries.
- [ ] spawn/replace notices containing run IDs, log paths, and tmux windows (`agents.tsx:1394`) → `Run started` / `Run restarted`; technical receipt in Details.
- [ ] `register from the terminal with wiki agent register` (`agents.tsx:1212`) → remove from primary empty state; put CLI setup in help.
- [ ] `log` action (`agents.tsx:1192`, `:1273`, `:1343`) → `View transcript`.
- [ ] `orchestrator` / `worker` → keep in technical run-management Details; use `coordinator` / `run` in navigation and primary page hierarchy.
- [ ] `codex auth-dead`, rotation, revival cap (`agents.tsx:730`) → `Provider sign-in required` / `Usage limit reached`; internals in Details.
- [ ] `Artifact event` (`artifact-block.tsx:944`) → `Details`; raw event becomes a nested opt-in.
- [ ] fallback `artifact <id-prefix>` (`artifact-panel.tsx:17`) → `Untitled artifact`; ID only in Details.
- [ ] `Pin to vault` + `Coming soon` (`artifact-panel.tsx:113`) → remove until the action works.
- [ ] `Sanitizing SVG…` (`artifact-block.tsx:300`) → `Preparing image…`.
- [ ] `diagnostic queued/sent/deduplicated for agent` (`artifact-block.tsx:927`) → `Couldn’t render. Asking the run to correct it…`; delivery mechanics in Details.
- [ ] `agent unavailable` (`artifact-block.tsx:930`) → `Couldn’t render, and this run is no longer active.`
- [ ] `terminal://<id>` and renderer chip (`terminal-pane.tsx:164`) → keep terminal name; move renderer name to tooltip-only diagnostics.
- [ ] `agent memory rots` / `wiki lint` (`health.tsx:74`) → `Living notes become less reliable when they are not reviewed`; move CLI to help.
- [ ] `cli`, `sessions scanned`, `refreshing...` (`tokens.tsx:233`, `:239`, `:309`) → `Source`, `Runs included`, `Refreshing…`.
- [ ] `Drop to complete — logs to done.md` (`kanban.tsx:269`) → `Mark complete`; explain history destination in secondary text.
- [ ] `-- INSERT --`, `-- VISUAL --`, `-- PANE --`, `-- NORMAL --` (`session.tsx:3326`) → compact `Vim: Insert/Normal/Visual`; hide pane mode unless keyboard help is open.
- [x] `sidecar` → no visible frontend occurrence found.
- [x] `artifact_refs` → no visible frontend occurrence found.
- [x] raw run ID in URLs → no user-visible route occurrence found; run IDs do leak in Agents-page text and notices above.
- [x] `gate` / `gate check` → no visible frontend occurrence found; dashboard uses check/status language.

## Micro-consistency sweep

The WIKI-143 variable layer exists, but component CSS has not converged on the census rubric.

| area | evidence | worst offenders | proposed action | ticket |
|------|----------|-----------------|-----------------|--------|
| spacing | `styles.css` contains 247 `padding:` declarations; many use 3, 5, 7, 9, 10, 14, and 18px rather than the stated 4/8/12/16/24 scale. | Session notifications `5px 12px 5px 10px` (`styles.css:4090`), composer `14px 16px 8px` (`:6109`), artifact header `7px 8px 7px 10px` (`:6917`), artifact tabs `6px 5px 5px 9px` (`:7423`). | Add semantic space tokens and convert shell/header/control/card primitives first; allow 1–2px only for optical border/glyph corrections with comments. | WIKI-160 |
| radii | `styles.css` has 165 radius declarations. Root defines duplicate aliases `--radius-s/m` = 4/8px and another six-step family 4/6/8/10/12/999px (`styles.css:63`, `:84`), conflicting with the census target 2/6/10. | Full pills remain on leader, badges, compact summaries, recent artifacts, and dots; agent-session doctrine calls for 0–2px rectangles but most session controls use 4px. | Choose one three-level radius family, retain 50% only for status dots, map every component to element/chip/card, and delete duplicate aliases. | WIKI-160 |
| hover / motion | Thirteen shorthand transitions plus separate property/duration declarations use 100ms and 120ms with `ease`, `ease-out`, and `ease-in-out`. | Folder chevrons (`styles.css:491`), badges (`:2199`), agent controls (`:4008`), composer (`:6133`), artifact tab actions (`:7448`). | Add `--motion-fast` and `--ease-ui`; use one color/background transition and one transform transition; honor `prefers-reduced-motion`. | WIKI-160 |
| icons / control boxes | JSX uses 10, 11, 12, 13, 14, 15, 16, and 18px icons; CSS control boxes span 18, 20, 22, 24, 25, 26, 28, 30, 31, and 32px. | Agent actions are mainly 11–13px (`agents.tsx:1008`, `:1111`, `:1191`), ribbon is 18px (`App.tsx:3441`), artifact toolbar is 11–12px (`artifact-panel.tsx:110`). | Standardize to 14 inline, 16 button, 20 surface-header icons and 28/32px compact/standard hit boxes; keep smaller glyphs only inside data-dense tables. | WIKI-160 |
| colors / shadows | Theme colors are correctly centralized, but `styles.css` still has 32 direct color literals, largely ANSI plus three floating shadows outside theme tokens. | Leader shadow (`styles.css:2256`), model menu shadow (`:4964`), queued tooltip shadow (`:6269`); ANSI palette (`:5750`) is a justified exception. | Replace floating shadows with theme shadow tokens; document ANSI literals as terminal-content exceptions; forbid new component-level hex/rgb values. | WIKI-160 |
| weights | Root exposes the desired 400/500/600 plus `--fw-display: 700` (`styles.css:79`); some headings use the display exception. | Display weight is reasonable for note titles but should not leak into compact chrome. | Scope 700 to document titles only and use 600 as maximum for UI controls and surface headers. | WIKI-160 |

## Proposed polish tickets

Ten batches cover every row rated 1–3. Each batch is sized around one coherent visual/interaction system rather than a route.

| suggested ticket | scope | priority | estimated effort | concrete treatment / exit criteria |
|------------------|-------|----------|------------------|------------------------------------|
| WIKI-151 | Navigation and sidebar IA: ribbon, sidebar shell, files/workspace states, tab header, session list | P1 | large | Three clearly grouped ribbon zones; one sidebar frame across modes; explicit Active/History run grouping; no silent workspace fallback; no fake tab bar. Depends on WIKI-147 for unread anatomy. |
| WIKI-152 | Agent-session chrome: header, provider inspector, action-required card, composer help, footer/status rail | P1 | large | Conversation starts near top; "Provider stream," raw/normalized counts, request IDs, `Unknown 0`, format/token telemetry, and tmux punctuation are absent from default chrome; diagnostics remain reachable in Run details. Rebase after WIKI-148. |
| WIKI-153 | Transcript/debug-detail treatment: tools, bash results, hook/marker messages, artifact inspector | P1 | medium | Human summaries are primary; raw input/output/event JSON are bounded, labelled, copyable, and opt-in; internal-only hooks do not appear as product messages. |
| WIKI-154 | Runs management productization: Agents page/cards/banners/actions, session preview, spawn/replace dialogs | P1 | large | Active/History hierarchy; cards show only decision-relevant fields; run IDs/tmux/log paths/role enums live in Technical details; provider/auth notices state user impact and next action. |
| WIKI-155 | Session and dashboard state completeness | P1 | medium | Explicit session zero-event/working/error variants with recovery; dashboard table skeleton; both dashboard empty variants have correct contextual actions; last good content survives refresh failure. |
| WIKI-156 | Artifact shell and renderer-state polish | P2 | large | Quiet inline artifact header; no coming-soon controls or ID-prefix titles; shared loading/error/fallback component; plot/SVG/code states actionable; unsupported payloads recoverable. Depends on WIKI-144 and WIKI-149. |
| WIKI-157 | Utility-page refinement: activity, graph, health, token usage | P2 | large | Every page has title, loading, empty, error, and retry; graph has keyboard/noncanvas access; git/CLI/provider terminology is secondary; token fetch never shows false zero data. |
| WIKI-158 | Global resilience and lifecycle states: first run, backend down, provider auth, update available, notices | P1 | large | One coherent first-run path; backend outage cannot masquerade as an empty vault; affected runs show persistent sign-in state; update notice exists only for a staged update; all states recover and announce success. |
| WIKI-159 | Keyboard and dialog accessibility: kanban, dashboard filters, destructive dialog, context menu | P2 | medium | All drag/double-click actions have keyboard/menu equivalents; listbox/menu/dialog semantics are complete; focus is trapped/restored; destructive copy describes outcome and recovery. |
| WIKI-160 | Design-token convergence and shared controls: spacing, radii, motion, icons, shadows, settings/status components | P2 | large | One spacing/radius/motion vocabulary, no duplicate radius aliases, component shadows use theme tokens, UI weights cap at 600, and settings/shared controls use the primitives. Rebase after WIKI-144. |

## V2 polish

Rows rated 4 should not block the first pass. Revisit after WIKI-151–160:

- Search: task-specific placeholder and preserved query across modes.
- Reading/editor/backlinks/code file/terminal: visual-regression pass only; retain existing IA.
- PR review: normalize casing and verify badge density after WIKI-144.
- Quick/fleet switchers: verify WIKI-146 command additions preserve grouping, loading, and keyboard behavior.
- Mermaid/image/table/diff artifacts: map raw library failures and inherit shared WIKI-156/WIKI-160 primitives without redesign.

## Process

1. Walk the app surface-by-surface, fill polish + tags + issues.
2. Group <=3-rated surfaces into 8-12 batches by theme (chrome, chat, dashboard, states, copy, etc.).
3. File one ticket per batch (WIKI-151 + …). Each ticket sized to one worker.
4. Sequence: highest-frustration batches first (per Henry: sidebar, session header "Provider stream", DEBUG leakage in chat).
5. Track completion by marking rows polish=5 as tickets land.

## Meta

- Update `updated:` on every sweep pass.
- New surfaces added post-WIKI-146/148 get their own rows.
- Deferred rows (polish=4, want but not now) accumulate in a "V2 polish" section at bottom.
