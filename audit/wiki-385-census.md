# WIKI-385 Wiki.app UI/UX census

## Verdict

Wiki.app now has a strong, flat shell. The main gaps are interaction quality, dense graph views, media controls, and small targets.

The highest-impact defect is terminal focus transfer. `C-a j` and `C-a k` select a terminal pane but leave DOM focus on the pane frame. The selected terminal cannot accept input until the user clicks it.

The video defect is confirmed. It uses native WebKit controls and an off-center frame. WIKI-383 already owns that fix.

## Audit basis

- source: `1836d7feaec0d3c6c4a4bb95a4dc0cae715e1c3d`, equal to `origin/main` at audit start
- change census: walked `frontend/src/` and reviewed `git log --since=2026-06-25 --oneline -- frontend/`
- build: `npm ci` passed; `npm run build` passed; Vite built 4,927 modules with the existing large-chunk warning
- runtime: worktree Vite server on `http://127.0.0.1:5178`, with read-only API proxy to `http://127.0.0.1:8213`
- viewport: 1440x900, `macos-dark`
- live data: current fleet, dashboard, token, activity, health, graph, and WIKI-385-AUDIT1 transcript data
- screenshots: `audit/screenshots/`
- interaction checks: command palette, settings, font picker, pane leader navigation, composer, graph mode, and source inspection for each artifact renderer
- fixture limit: legacy artifact Playwright fixtures no longer match the current run-backed session lookup. Artifact rows without live payloads are marked `source-only`.
- prior audits: WIKI-150, WIKI-317, and WIKI-329 findings were treated as regression checks. Closed transcript type and framing work was not reopened.

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

- **COPY** — user-facing string reads like code jargon ("Provider stream", "sidecar", `artifact_refs`, raw `run_id`). Rewrite from the user's view.
- **CHROME** — decorative but noisy: divider, badge, border, background. Reduce or remove it.
- **INCONSISTENT** — deviates from tokens: padding, radius, color, weight, icon size, hover behavior.
- **DEBUG** — dev-only output leaks into the product: raw JSON, timestamps that look like log lines, run IDs.
- **EMPTY** — first-run or zero-data state is missing or poor.
- **LOADING** — mid-fetch state is missing, uses text or a spinner instead of a skeleton, or causes content pop-in.
- **ERROR** — failure state is undefined, blank, raw, or has no recovery action.
- **HIERARCHY** — a surface mixes unlike things without clear information structure.
- **ACCESSIBILITY** — focus ring is missing, contrast fails, or keyboard navigation has a dead end.
- **INTERACTION** — a flow needs extra steps or ends in a dead state. Examples include focus not following selection and state that needs a reload.

## Census

### Global shell and pane navigation

| surface | file(s) | polish | tags | issues | proposed action | ticket-suggestion |
|---------|---------|--------|------|--------|----------------|-------------------|
| worktree build and app shell | `frontend/src/App.tsx:3721`, `frontend/src/styles.css:17663` | 4 | — | The flat panel, page header, sidebar, and bottom strip read as one deliberate system. | Keep. Add the screenshots to the standing shell regression set. | — |
| command palette | `frontend/src/command-palette.tsx:75`, `frontend/src/command-palette.tsx:176` | 4 | — | Search, modes, inert background, focus trap, keyboard movement, Escape, and focus return form one complete flow. | Keep. | — |
| sidebar view list | `frontend/src/App.tsx:3708` | 2 | HIERARCHY, COPY, INTERACTION | `Agent list` selects the sidebar run list. `Agents` opens the Runs page. They sit together with similar icons and names, so the distinction is not clear. | At `App.tsx:3709-3711`, rename the sidebar mode to `Runs sidebar` or remove it from Views. Keep one `Runs` destination and expose the sidebar mode from that page. | navigation naming ticket |
| bottom window strip | `frontend/src/App.tsx:4330`, `frontend/src/styles.css:16812` | 4 | — | The strip is calm and keeps window identity visible. Long fleets still depend on horizontal clipping. | Keep. Add a 20-window truncation regression. | — |
| leader pane navigation | `frontend/src/App.tsx:2121`, `frontend/src/App.tsx:2735`, `frontend/src/terminal-pane.tsx:141` | 1 | INTERACTION, ACCESSIBILITY | Confirmed seeded defect. `cyclePaneFocus` changes pane state. `focusWindowPane` then focuses `.pane-frame` in a requestAnimationFrame. `TerminalPane` focuses xterm during the render effect first, so the later frame focus steals input. The live check ended with `focusedPane=pane-terminal`, `activeTag=DIV`, and no terminal input marker. | Add `focus()` to `TerminalPaneController` at `terminal-pane.tsx:145`. At `App.tsx:2136`, focus the selected pane's primary target after frame activation. Use xterm input for terminals, composer only when explicitly requested, and pane frame otherwise. Test `C-a j/k`, window switch, and pane close. | WIKI-384 |
| terminal pane shell | `frontend/src/terminal-pane.tsx:60`, `frontend/src/styles.css:17529` | 4 | — | The xterm surface, connection state, and flat frame match the app shell. The defect is focus transfer, not terminal chrome. | Keep the visual shell. | — |
| focus after pane close or window switch | `frontend/src/App.tsx:2783`, `frontend/src/App.tsx:2793` | 2 | INTERACTION, ACCESSIBILITY | The same frame-focus path affects a remaining terminal after close and a terminal selected by window navigation. | At `App.tsx:2783-2799`, cover pane close and window navigation in the WIKI-384 focus fix. Restore focus to the active surface, not only its container. | WIKI-384 |
| compact shell action targets | `frontend/src/styles.css:15584`, `frontend/src/styles.css:17063`, `frontend/src/styles.css:17132` | 3 | ACCESSIBILITY, INCONSISTENT | Common icon and tab-close controls are 24-32px. This is below the 40px target from the audit skill. | At `styles.css:15584,17063,17132`, keep 14px glyphs but expand hit areas to 40px where targets do not overlap. Use pseudo-elements in dense strips. | target-size ticket |

### Runs, fleet, workgraph, replay, and autopilot

| surface | file(s) | polish | tags | issues | proposed action | ticket-suggestion |
|---------|---------|--------|------|--------|----------------|-------------------|
| Runs page hierarchy | `frontend/src/agents.tsx:1360`, `frontend/src/agents.tsx:2280` | 4 | — | Active and history rows are flat and the start action is clear. Current empty and loading frames are complete. | Keep. | — |
| run row action menus (source-only) | `frontend/src/agents.tsx:1260`, `frontend/src/agents.tsx:1880` | 3 | ACCESSIBILITY, INTERACTION | Menus close on Escape and outside click, but they do not implement arrow-key movement or initial item focus. | At `agents.tsx:1270-1302`, focus the first menu item on open. Add Up, Down, Home, End, Enter, and focus return. | menu keyboard ticket |
| start-run dialog (source-only) | `frontend/src/agents.tsx:450`, `frontend/src/agents.tsx:510` | 3 | HIERARCHY, COPY | The base form and advanced form can expose many provider, role, model, effort, path, and prompt choices. This asks the user to understand runtime structure before the task starts. | At `agents.tsx:450-690`, keep ticket, task, and project in the first step. Derive provider defaults. Put runtime overrides in a clearly named `Runtime overrides` disclosure. | run-start flow ticket |
| fleet graph loading | `frontend/src/fleet-graph.tsx:188` | 2 | LOADING, HIERARCHY | The page renders `loading` in the header and `loading fleet graph` in the body. The large canvas stays blank with no group-shaped skeleton or progress. | Replace `fleet-graph.tsx:197,224` with stable group and screencast skeletons. Keep the header timestamp blank until data exists. | fleet graph ticket |
| fleet graph filters | `frontend/src/fleet-graph.tsx:64` | 3 | ACCESSIBILITY, INTERACTION | Active filter chips use only class styling. They do not expose `aria-pressed`, so assistive tools cannot read selected state. | Add `aria-pressed` at `fleet-graph.tsx:80-94`. Add one Clear filters action when any filter is active. | fleet graph ticket |
| fleet graph group cards | `frontend/src/fleet-graph.tsx:101` | 3 | HIERARCHY, CHROME | Each orchestrator stacks a DAG and one screencast card per live worker. Large fleets create a long wall of repeated boxes. | At `fleet-graph.tsx:108-143`, default each group to one compact DAG and a selected worker strip. Expand the full screencast list on demand. | fleet graph ticket |
| screencast strip (source-only) | `frontend/src/screencast-strip.tsx:249` | 2 | INTERACTION, ACCESSIBILITY | Every frame update forces `scrollTop=scrollHeight`. A user cannot hold an earlier line for review. The full tape is also one polite live region, which can announce high-rate output. | At `screencast-strip.tsx:253-270`, auto-pin only while the user is already at the end. Add `Jump to latest`. Announce a short summary, not every frame. | screencast control ticket |
| ticket workgraph panel (source-only) | `frontend/src/workgraph-panel.tsx:375`, `frontend/src/workgraph-panel.tsx:483` | 3 | LOADING, ERROR | Initial load is one text line. Failure and no-data share the same visual state, and no retry is offered. | Replace `workgraph-panel.tsx:483-485` with separate skeleton, empty, and error states. Add retry without closing the panel. | graph state ticket |
| replay run picker and transport (source-only) | `frontend/src/replay-scrubber-panel.tsx:470`, `frontend/src/replay-scrubber-panel.tsx:647` | 4 | — | Run selection, range input, bookmarks, stepping, speed, and paging are explicit and keyboard reachable. | Keep transport behavior. | — |
| replay event body (source-only) | `frontend/src/replay-scrubber-panel.tsx:742` | 2 | DEBUG, HIERARCHY, COPY | Raw JSON is always shown below every event. `seq`, raw kind, disposition, lifecycle enum, and bookmark enum dominate a user-facing replay. | At `replay-scrubber-panel.tsx:743-763`, make the human summary primary. Move raw JSON and sequence values into a closed `Event details` disclosure with copy. Map enums to plain labels. | replay readability ticket |
| replay panel focus (source-only) | `frontend/src/agent-session-surface.tsx:663`, `frontend/src/agent-session-surface.tsx:728` | 3 | INTERACTION, ACCESSIBILITY | Clicking Replay mounts a side panel but leaves focus on the header button. Keyboard users must tab through the session header again to reach the new panel. Graph and Review share this pattern. | At `agent-session-surface.tsx:643-733`, focus each mounted panel heading or close button. On close, return focus to its trigger. | side-panel focus ticket |
| autopilot detail popover (source-only) | `frontend/src/loop-state-chrome.tsx:278` | 3 | ACCESSIBILITY, COPY | The detail uses `role="dialog"` but does not move or trap focus. Terms such as `plateau` and `unrouted` appear without explanation. | At `loop-state-chrome.tsx:321-333`, use a non-modal popover role with Escape and focus return, or implement a true dialog. Add plain-language tooltips for loop terms. | autopilot clarity ticket |

### Dashboard and utility views

| surface | file(s) | polish | tags | issues | proposed action | ticket-suggestion |
|---------|---------|--------|------|--------|----------------|-------------------|
| ticket dashboard table | `frontend/src/dashboard.tsx:230`, `frontend/src/dashboard.tsx:283` | 4 | — | Skeleton, zero-ticket, filtered-empty, stale, and error states exist in source. The loaded table stays dense and clear. | Keep. | — |
| dashboard cost summary | `frontend/src/dashboard.tsx:344` | 4 | — | The summary uses one frame with flat internal groups. Dynamic totals use tabular numerals. | Keep. | — |
| token usage | `frontend/src/tokens.tsx:390` | 4 | — | Time range, agent, model, totals, chart, legend, and cached toggle form a coherent data view. | Keep. Add a narrow-width legend check. | — |
| activity feed | `frontend/src/activity.tsx:230` | 4 | — | Day groups, commit rows, and changed paths scan well after the flattening pass. | Keep. | — |
| note freshness | `frontend/src/health.tsx:100` | 4 | — | Summary counts and rows are flat, aligned, and readable. | Keep. | — |
| note graph canvas | `frontend/src/graph.tsx:110`, `frontend/src/graph.tsx:342` | 2 | HIERARCHY, ACCESSIBILITY | With 124 notes, labels overlap into an unreadable center mass. The keyboard model works, but the default visual mode does not show useful structure. | At `graph.tsx:172-181`, default to List above a density threshold. In canvas mode, label only the focused node and its neighbors. Add zoom controls and a `Fit graph` action. | note graph ticket |
| note graph list alternative | `frontend/src/graph.tsx:187` | 4 | — | The list gives a keyboard-first alternative with outgoing and incoming links. | Keep. Make the mode choice persist. | — |

### Artifacts and media

| surface | file(s) | polish | tags | issues | proposed action | ticket-suggestion |
|---------|---------|--------|------|--------|----------------|-------------------|
| inline artifact shell | `frontend/src/artifact-block.tsx:850` | 4 | — | The current shell is quiet and consistent with transcript rows. | Keep. | — |
| artifact side panel (source-only) | `frontend/src/artifact-panel.tsx:105` | 3 | ACCESSIBILITY, INTERACTION | Closing the focused tab can unmount the focused close button without moving focus. The overflow `role=menu` has no arrow-key model or Escape handler. | At `artifact-panel.tsx:55-81,115-195`, implement roving tab focus, menu keyboard behavior, focus the next tab after close, and return focus to the transcript trigger when the panel closes. | artifact panel keyboard ticket |
| fullscreen artifact inspector (source-only) | `frontend/src/artifact-inspector.tsx:400`, `frontend/src/styles.css:15299` | 3 | ACCESSIBILITY | Focus handling is complete, but primary icon targets remain 32px. | Expand inspector action hit areas to 40px at `styles.css:15299-15325` without increasing glyph size. | target-size ticket |
| video renderer (source-only) | `frontend/src/artifact-media-renderers.tsx:217` | 1 | CHROME, INCONSISTENT | Confirmed seeded defect. Native WebKit video controls add glass chrome. The video sits at the frame origin instead of being optically centered. | WIKI-383 owns this. Replace `video controls` at `artifact-media-renderers.tsx:220-253` with themed controls and center the media inside its reserved frame. | WIKI-383, no new ticket |
| audio renderer (source-only) | `frontend/src/artifact-media-renderers.tsx:321` | 2 | CHROME, INCONSISTENT | Audio uses the same native WebKit controls family. It sits beside a custom waveform and custom speed row, so the mixed chrome is more visible. | At `artifact-media-renderers.tsx:326-365`, build one themed play, time, seek, speed, and transcript bar. Keep the native element hidden but accessible. | media controls ticket |
| PDF renderer (source-only) | `frontend/src/artifact-detail/pdf.tsx:360` | 3 | HIERARCHY, ACCESSIBILITY | Page, fit, zoom, reset, and find controls create three dense groups. Several buttons use 11-14px glyphs in sub-40px targets. | At `pdf.tsx:360-445`, keep page navigation visible. Move fit/reset into one View menu. Expand isolated targets and preserve shortcuts. | artifact toolbar ticket |
| plot renderer (source-only) | `frontend/src/artifact-detail/plot.tsx:150` | 3 | HIERARCHY, ACCESSIBILITY | Interactive plots can show reset, two zoom buttons, four pan buttons, save, status, and a long help sentence in one row. | At `plot.tsx:150-197`, group pan into one directional control or menu. Keep Reset, Zoom, and Save visible. Put interaction help behind Help. | artifact toolbar ticket |
| table renderer (source-only) | `frontend/src/artifact-detail/table.tsx:88` | 4 | — | Filter, sort, result count, format, visible copy, full copy, row copy, and column copy are explicit. | Keep. | — |
| Mermaid renderer (source-only) | `frontend/src/artifact-detail/mermaid.tsx:1` | 4 | — | Pan, zoom, reset, and failure handling are mature. | Keep. | — |
| SVG renderer (source-only) | `frontend/src/artifact-detail/svg.tsx:1` | 4 | — | Sanitized rendering and view controls are consistent with Mermaid and images. | Keep. | — |
| image detail and lightbox (source-only) | `frontend/src/artifact-detail/lightbox.tsx:336`, `frontend/src/styles.css:15061` | 3 | ACCESSIBILITY | Focus trap and return are correct. Zoom and close controls use 32px hit areas. | At `styles.css:15061-15180`, expand non-overlapping lightbox targets to 40px. Keep current focus and drag behavior. | target-size ticket |
| visual diff (source-only) | `frontend/src/visual-diff-renderer.tsx:1` | 4 | — | Before, after, overlay, and comparison modes use a clear artifact frame. | Keep. | — |
| diff artifact (source-only) | `frontend/src/artifact-detail/diff.tsx:1` | 4 | — | Split/unified rendering, wrapping, and copy behavior are coherent. | Keep. | — |
| code artifact (source-only) | `frontend/src/artifact-detail/code.tsx:90` | 4 | — | Find, highlighted source, filename, and copy are complete. | Keep. | — |
| JSON artifact (source-only) | `frontend/src/artifact-detail/json.tsx:1` | 4 | — | Structured JSON stays bounded inside an explicit artifact surface. | Keep. | — |
| file-list artifact (source-only) | `frontend/src/artifact-detail/file-list.tsx:1` | 4 | — | Files scan as one list without card noise. | Keep. | — |

### Composer, settings, notes, and images

| surface | file(s) | polish | tags | issues | proposed action | ticket-suggestion |
|---------|---------|--------|------|--------|----------------|-------------------|
| composer core | `frontend/src/session.tsx:4920` | 4 | — | Ask/steer context, queue semantics, Vim mode, slash menu, and send state are visible without excess chrome. | Keep. | — |
| skill and slash menu | `frontend/src/session.tsx:5008`, `frontend/src/composer-slash-menu.tsx:70` | 4 | — | Arrow, Ctrl+j/k, Tab, Enter, and Escape behavior is explicit. | Keep. | — |
| image uploads (source-only) | `frontend/src/session.tsx:4384`, `frontend/src/session.tsx:4861` | 3 | ERROR, ACCESSIBILITY, INTERACTION | Upload failure collapses to `Image upload failed` with no file name or retry. The remove control is a small 11px icon. | At `session.tsx:4384-4407`, keep failed attachments with file name and Retry/Remove. At `session.tsx:4861-4877`, use a 40px non-overlapping remove hit area and a descriptive alt label. | upload recovery ticket |
| transcript image chips | `frontend/src/session.tsx:2148` | 4 | — | Images open with an explicit chip and remain separate from prose. | Keep. | — |
| Markdown image frame (source-only) | `frontend/src/markdown-image.tsx:366` | 4 | — | Ratio reservation, thumbnail fade, error copy, and lightbox launch prevent layout shift. | Keep. | — |
| settings theme picker | `frontend/src/settings.tsx:591` | 4 | — | Fifteen themes remain scan-friendly because each option is flat and selection uses one fill. | Keep. | — |
| font picker | `frontend/src/font-settings.tsx:130` | 4 | — | Search, sample text, selected state, keyboard movement, and focus return are complete. | Keep. | — |
| source editor (source-only) | `frontend/src/source-editor.tsx:1` | 4 | — | CodeMirror editing, save state, and the read-first product direction remain coherent. | Keep. | — |
| note reading view | `frontend/src/pane.tsx:300`, `frontend/src/markdown.tsx:1` | 4 | — | Markdown hierarchy and Obsidian features remain stable after the prior audit work. | Keep. | — |

## Ranked TOP-20 defects

1. **Terminal pane selection does not transfer input focus.** `frontend/src/App.tsx:2121-2137`, `frontend/src/terminal-pane.tsx:141-143`. This blocks typing after `C-a j/k` and affects pane close and window switch.
2. **Dense note graphs are unreadable by default.** `frontend/src/graph.tsx:172-181`, `frontend/src/graph.tsx:342-380`. The live 124-note canvas produced a central label mass.
3. **Replay exposes raw event JSON as primary content.** `frontend/src/replay-scrubber-panel.tsx:742-763`. Common use becomes a debug inspection task.
4. **Audio leaks native WebKit chrome.** `frontend/src/artifact-media-renderers.tsx:321-365`. It clashes with the custom waveform and shell.
5. **Video leaks native WebKit chrome and mis-centers its frame.** `frontend/src/artifact-media-renderers.tsx:217-255`. WIKI-383 already owns this.
6. **Sidebar has two near-duplicate run destinations.** `frontend/src/App.tsx:3708-3711`. `Agent list` and `Agents` require internal product knowledge.
7. **Artifact tab close and overflow menu lose keyboard continuity.** `frontend/src/artifact-panel.tsx:55-81`, `frontend/src/agent-session-surface.tsx:400-415`.
8. **Common icon controls use sub-40px hit targets.** `frontend/src/styles.css:15584`, `frontend/src/styles.css:17063`, `frontend/src/styles.css:17132`.
9. **Fleet graph uses text-only loading on a large page.** `frontend/src/fleet-graph.tsx:188-225`.
10. **Screencast updates force scroll to the end.** `frontend/src/screencast-strip.tsx:253-270`. Review of earlier output cannot hold position.
11. **Workgraph failure and no-data states share one dead-end frame.** `frontend/src/workgraph-panel.tsx:483-485`.
12. **Graph, Replay, and Review panels do not receive focus when opened.** `frontend/src/agent-session-surface.tsx:639-733`.
13. **Fleet graph filter state is not exposed to assistive tools.** `frontend/src/fleet-graph.tsx:64-96`.
14. **Start-run asks for runtime choices too early.** `frontend/src/agents.tsx:450-690`.
15. **PDF toolbar has too many equal-weight controls.** `frontend/src/artifact-detail/pdf.tsx:360-445`.
16. **Plot toolbar can expand to eight equal-weight actions plus help.** `frontend/src/artifact-detail/plot.tsx:150-197`.
17. **Image upload failure has no item-level retry.** `frontend/src/session.tsx:4384-4407`.
18. **Run action menus lack standard arrow navigation.** `frontend/src/agents.tsx:1260-1302`.
19. **Autopilot detail has dialog semantics without dialog focus behavior.** `frontend/src/loop-state-chrome.tsx:278-333`.
20. **Fleet graph repeats a screencast card for every live worker.** `frontend/src/fleet-graph.tsx:101-143`. Large fleets become a wall of boxes.

## Proposed ticket decomposition

| order | suggested ticket | scope | effort | exit criteria |
|------:|------------------|-------|--------|---------------|
| 1 | WIKI-384 | Terminal focus transfer | 1 day | `C-a j/k`, window switch, and pane close focus xterm when the destination is a terminal. Typing works with no click. Add active-element Playwright checks. |
| 2 | Note graph density | Dense graph defaults and controls | 1-2 days | Default to List above a tested node threshold. Canvas labels only the focused neighborhood. Add zoom and fit controls. |
| 3 | Replay readability | Replay event information hierarchy | 1-2 days | Human summary is primary. Raw JSON, sequence, and provider enums sit in a closed, copyable details section. |
| 4 | Audio control chrome | Themed audio transport | 1-2 days | Replace visible native audio controls with one themed accessible transport. Keep waveform, speed, transcript, and native media semantics. Do not duplicate WIKI-383. |
| 5 | Sidebar run naming | Run destination labels and entry points | 1 day | One clear Runs destination. Sidebar mode and full page no longer appear as peer actions with near-identical names. |
| 6 | Artifact and run menu keyboard model | Tabs, overflow, and action menus | 1-2 days | Artifact tabs, artifact overflow, and run menus support initial focus, arrows, Home/End, Escape, close focus transfer, and trigger focus return. |
| 7 | Side-panel focus contract | Open and close focus transfer | 1 day | Graph, Replay, Review, artifact, and subagent panels receive useful focus on open and return it on close. |
| 8 | Compact target-size pass | Dense icon and close controls | 1-2 days | Expand non-overlapping icon, close, inspector, lightbox, PDF, and tab targets toward 40px. Keep the current visible geometry. |
| 9 | Fleet graph state and density | Loading, filters, and worker layout | 1-2 days | Add group-shaped skeletons, `aria-pressed` filter state, clear filters, and compact selected-worker screencast mode. |
| 10 | Screencast review controls | Scroll pinning and live announcements | 1 day | Auto-pin only at the end. Add Jump to latest. Reduce live announcements to a bounded summary. |
| 11 | Artifact toolbar hierarchy | PDF and plot controls | 1-2 days | PDF and plot keep primary controls visible. Secondary fit, pan, and help actions move into grouped menus without losing keyboard use. |
| 12 | Upload recovery | Failed image attachment actions | 1 day | Failed image uploads keep filename, state, Retry, and Remove. Attachment controls meet target size and have descriptive labels. |
| 13 | Run-start progressive disclosure | Basic and advanced run options | 1-2 days | First view asks for task identity and project only. Runtime overrides stay available in one advanced section with stable defaults. |
| 14 | Workgraph and autopilot state polish | State frames and popover semantics | 1 day | Workgraph has distinct skeleton, empty, error, and retry states. Autopilot uses correct popover focus semantics and plain term help. |

## Not audited

- Artifact binary payload rendering was `source-only`. The old PDF, media, and inspector fixtures now fail current run-backed session lookup. They returned 503 for session data or 404 for artifact bytes. No product score depends on those fixture failures.
- WIKI-383's in-flight custom video implementation was not audited. This branch uses `origin/main`, where native video controls remain.
- Actual playback on WebKit was not run. Chromium confirmed the DOM and focus model. Native macOS control leakage was confirmed from source and the seeded report.
- Live visual-diff payloads were not available. The renderer and tests were reviewed from source.
- Live gallery, JSON, file-list, and large-table payloads were not available. These rows are source-only.
- Destructive run actions were not executed against live fleet state. Dialog source and prior focus tests were reviewed.
- Autopilot enable/disable was not changed. The audit remained read-only against live state.
- Note writes, card moves, file rename/delete, message send, upload, spawn, replace, archive, approve, and replay rewind were not executed because they change live state.
- Mobile and touch layouts were not run. The target-size findings are based on computed CSS geometry and the 1440x900 desktop pass.
