---
type: reference
tags: [wiki, ui, bb, parity, typography]
created: 2026-08-17
updated: 2026-08-17
---

# WIKI-317 formatting delta census

## Verdict

bb still looks cleaner for two main reasons.

1. bb gives prose, controls, and metadata distinct type ranks. Wiki often renders all three at 14.5px.
2. bb uses whitespace and flat rows for routine content. Wiki wraps too many routine rows in bordered, filled cards.

The highest-payoff work is not another palette pass. It is a typography ruling, transcript density work, and box removal.

All proposals below preserve current information. They can change rank, layout, truncation, hover behavior, or disclosure. They must not delete fields.

## Audit basis

- Audit checkout: `596cf2b47cb7a7a786cb970aba3a42bbdae75661` (`WIKI-316: Add bb themes (#254)`). This was `origin/main` when the build started.
- `origin/main` advanced during the audit to `379c9ea24066abca08ce0fb864e00cb5d6cb4c6a`. The added WIKI-315 commit changes expanded thought-summary cleanup. It does not change the structural findings below.
- WIKI-308 dialog work is still outside `origin/main`. The dialog rows below are verification criteria for that in-flight ticket, not a new ticket request.
- `npm ci` passed.
- `npm run build` passed. Vite built 4,923 modules. It only reported the existing large-chunk warning.
- The live app ran on `http://127.0.0.1:5175`. Ports 5173 and 5174 were occupied.
- The audit used real backend data from recent WIKI-316 and WIKI-317 sessions. The WIKI-316 session had 195 events. These included 81 thought events and 92 tool events.
- The browser viewport was 1440x900.
- The browser loaded Inter Variable through Wiki's `wiki-font`, `wiki-font-stack`, `wiki-font-weight`, and `wiki-font-size` settings. A computed-style and `document.fonts.check` check passed.
- Screenshots are in `.playwright-mcp/wiki-317/`. The set contains 30 images. It includes bb-dark and bb-light contact sheets.

## Severity rank

| Rank | Severity | Finding | Why it drives the gap |
|---:|---|---|---|
| 1 | S0 | Wiki has no durable type hierarchy | Prose, labels, paths, timestamps, and actions often have the same 14.5px size. The eye cannot find a stable reading order. |
| 2 | S0 | Long session input has no display envelope | The startup prompt rendered as a 659x1887 user card. It displaced the whole conversation before any assistant work appeared. |
| 3 | S1 | Agent groups contain nested cards | An orchestrator card contains worker cards. Each layer adds a border, fill, radius, and shadow. |
| 4 | S1 | Activity, Health, and Kanban overuse boxes | Routine rows become cards or pills. bb reserves a ring or fill for active and failed states. |
| 5 | S1 | Thought runs repeat the same row grammar | A real session showed 81 separate `Thought ... ENCRYPTED` rows. The repeated chrome becomes the primary visual object. |
| 6 | S2 | The session header uses two full chrome rows | Ticket, state, step, runtime fields, dividers, and actions take about 86px before transcript content. |
| 7 | S2 | Dashboard and Tokens give every region equal framing | Totals, tables, charts, summaries, and prompt panels compete as separate cards. |
| 8 | S3 | Neutral metadata still uses pills and persistent actions | Type labels, file paths, counts, and menu controls often look actionable or selected. |
| 9 | S3 | Some icons and baselines drift between surfaces | Several controls use local sizes and gaps instead of one 28px/14px optical contract. |
| 10 | S4 | Toast, sidebar geometry, tool rows, composer shell, and status strip are close | These surfaces need only the global type decision and small optical checks. They do not need another rebuild. |

## Per-surface delta table

The severity prefix is part of the `delta` cell.

| Surface | Delta | Dimension | bb reference (file:line) | Wiki location (file:line) | Proposed fix | Information-preservation note |
|---|---|---|---|---|---|---|
| Global | **S0:** `--font-ui-small`, `--font-ui-smaller`, prose, and mono aliases resolve to one 14.5px value | Type size and rank | `/tmp/bb-reference/apps/app/src/components/ui/theme.css:93` | `frontend/src/styles.css:93`; `frontend/src/settings.tsx:126` | Keep one base-size control, but derive 10px chrome, 13px controls, and 15px prose tiers from it. Use 400/500/600 weights only. This needs Henry's ruling first. | Keeps the font picker, weight picker, and size control. It changes how the chosen base size creates a scale. |
| Global code | bb uses Fira Code for code. Wiki assigns the selected UI family to `--font-monospace` | Type family and optical detail | `/tmp/bb-reference/apps/app/src/components/ui/theme.css:549` | `frontend/src/settings.tsx:159`; `frontend/src/styles.css:103` | Do not change this under the current WIKI-279 ruling. If Henry permits a code exception, restore a mono family for code and terminal output only. | All source text remains. Only its font changes. |
| Sidebar | **S3:** row geometry matches bb, but labels and `{N}m ago` can still inherit the 14.5px global alias | Type rank and chrome density | `/tmp/bb-reference/apps/app/src/components/ui/theme.css:130`; `/tmp/bb-reference/apps/app/src/components/ui/theme.css:553` | `frontend/src/styles.css:17669`; `frontend/src/styles.css:17826`; `frontend/src/styles.css:8819` | Keep 28px rows. Set primary row text to the 13px control tier. Set age and group metadata to the 10px chrome tier with tabular numerals. | Keep `{N}m ago` in its locked position. Do not hide or move it. |
| Sidebar | Persistent row actions would compete with labels if more actions are added | Chrome density | `/tmp/bb-reference/apps/app/src/components/ui/theme.css:248` | `frontend/src/styles.css:8773`; `frontend/src/styles.css:8804` | Use bb's hover/focus action reveal for optional row actions. Reserve always-visible space so text does not reflow. | All actions stay keyboard accessible. Labels and ages remain present. |
| Session user card | **S0:** long user messages have no height clamp or `Show more` control | Spacing rhythm and density | `/tmp/bb-reference/apps/app/src/components/thread/timeline/ConversationMessageContent.tsx:306` | `frontend/src/session.tsx:2545` | Clamp overflowing user content at 15 line-heights. Add a fade and `Show more`/`Show less`. Measure rendered height, not source lines. | Expansion must show the exact original text and attachments. Copy and search must still reach full content. |
| Session user card | The live startup message mixed plugin inventory, runtime metadata, and the human task into one 1,887px card | Chrome density and hierarchy | `/tmp/bb-reference/apps/app/src/components/thread/timeline/ConversationMessageContent.tsx:326` | `frontend/src/session.tsx:2550` | Detect known machine envelopes such as `<recommended_plugins>` and `<WIKI_RUNTIME_CARD>`. Render each as a quiet summary disclosure before the human task text. | The summary can show counts. Opening it must expose the exact raw envelope. Unknown tags remain unchanged. |
| Session user card | Wiki allows 85% width and 72ch. bb caps the user card at 70% | Spacing and alignment | `/tmp/bb-reference/apps/app/src/components/thread/timeline/ConversationMessageContent.tsx:475` | `frontend/src/styles.css:7577` | Use `max-width: 70%` on wide panes. Keep a responsive wider cap on narrow panes. | The text and attachments remain unchanged. Only wrapping changes. |
| Session assistant prose | The 15px/22px body treatment is close, but the chosen global weight can flatten heading and paragraph contrast | Type weight and line height | `/tmp/bb-reference/apps/app/src/components/thread/timeline/ConversationMessageContent.tsx:640` | `frontend/src/styles.css:7603` | Keep assistant prose at the 15px/22px body tier and 400 weight. Keep Markdown headings at 600. Do not let the global weight picker erase semantic weight differences. | All Obsidian Markdown features remain under W3. |
| Session thought rows | **S1:** 81 consecutive thought rows repeat `Thought`, title, duration, and `ENCRYPTED` with equal visual weight | Spacing rhythm and chrome density | `/tmp/bb-reference/packages/shared-ui/src/components/ui/activity-row-styles.ts:10`; `/tmp/bb-reference/apps/app/src/components/thread/timeline/ThreadTimelineSurface.tsx:342` | `frontend/src/session.tsx:1676`; `frontend/src/styles.css:8298` | Group consecutive settled thoughts into one disclosure: `N thoughts · total duration`. Show the latest or active thought as a live row. Expanded mode shows every current row in order. | Preserve every title, duration, encrypted marker, and expanded body. Do not infer private reasoning. |
| Session thought rows | The bordered uppercase `ENCRYPTED` chip repeats on every row | Hairline and box noise | `/tmp/bb-reference/packages/shared-ui/src/components/ui/activity-row-styles.ts:36` | `frontend/src/styles.css:8342` | Render `encrypted` as 10px subtle metadata without a border. In grouped mode, show it once in the group summary and on each expanded row. | The encrypted state stays visible. |
| Session tool rows | Settled tool rows are structurally close to bb, but their label, target, detail, and result can all render at 14.5px | Type rank | `/tmp/bb-reference/packages/shared-ui/src/components/ui/activity-row-styles.ts:27` | `frontend/src/styles.css:7823`; `frontend/src/styles.css:8163` | Keep the verb at 13px/500. Use 13px regular for the target. Use 10px subtle metadata for sizes, states, and result tails. | Full paths and details remain in the DOM and title. Existing output disclosure remains. |
| Session tool output | The left-rail disclosure is clean. No material box-noise delta remains | Hairline usage | `/tmp/bb-reference/apps/app/src/components/ui/expandable-line.tsx:36` | `frontend/src/styles.css:7926`; `frontend/src/styles.css:8012` | Keep this design. Only apply the global type tiers and optical icon contract. | Existing raw and polished output modes remain. |
| Session code blocks | Code uses Inter at 14.5px in the audited setup. bb uses 12px Fira Code with a tight line height | Type family, size, and line height | `/tmp/bb-reference/apps/app/src/components/ui/event-code-block.tsx:17` | `frontend/src/styles.css:8223`; `frontend/src/styles.css:11393` | Pending the WIKI-279 ruling, keep the current family. If approved, use mono 12px/tight for code and raw tool output. Keep prose at 15px/22px. | Code, gutters, folding, copy, and syntax colors remain. |
| Session header | **S2:** the header uses a 44px primary row plus a 40px secondary row | Spacing rhythm and chrome density | `/tmp/bb-reference/apps/app/src/components/layout/AppPageHeader.tsx:92` | `frontend/src/agent-session-surface.tsx:605`; `frontend/src/styles.css:15293` | Use one 40px primary row. Keep ticket, state, current step, and actions visible. Put kind, role, model, and loop metadata in a 10px inline cluster or a labeled disclosure. | Every runtime field remains reachable. Blocker text remains visible and cannot be hidden. |
| Session header | Runtime fields use repeated 14px vertical dividers and full-size text | Hairline, alignment, and rank | `/tmp/bb-reference/apps/app/src/components/layout/AppPageHeader.tsx:103` | `frontend/src/styles.css:15313`; `frontend/src/styles.css:15331` | Replace most dividers with gap and middle dots. Use one bottom seam for the header. Use tabular numerals for time data. | Kind, role, model, loop state, and actions remain. |
| Composer | The outer shell closely matches bb. The target label and placeholder remain too close to prose rank | Type rank | `/tmp/bb-reference/apps/app/src/components/promptbox/PromptBoxInternal.tsx:3067`; `/tmp/bb-reference/apps/app/src/components/promptbox/PromptBoxInternal.tsx:3249` | `frontend/src/session.tsx:4782`; `frontend/src/styles.css:16763` | Keep the current card. Set the target and editor control text to 13px. Keep the metadata footer at 10px. Keep the placeholder subtle. | Keep ticket target, shortcuts, model controls, thinking state, subagents, Vim state, and help text. W10 keeps the round send button. |
| Agents page | **S1:** an orchestrator `agent-activity-row` wraps nested worker `agent-activity-row` cards | Hairline and box noise | `/tmp/bb-reference/packages/shared-ui/src/components/ui/activity-row-styles.ts:10` | `frontend/src/agents.tsx:1837`; `frontend/src/agents.tsx:2111`; `frontend/src/styles.css:17585` | Make the orchestrator group flat. Use whitespace and one section seam. Give only active, selected, blocked, or failed worker rows a subtle state fill or ring. Make archived rows flat. | Keep ticket, state, step, blocker, age, owner, model, branch, PR, logs, and technical details. Existing disclosures remain. |
| Agents page | Ticket, step, age, state, and actions often share the same 14.5px rank | Type rank and chrome density | `/tmp/bb-reference/packages/shared-ui/src/components/ui/activity-row-styles.ts:19` | `frontend/src/styles.css:5170`; `frontend/src/styles.css:5240`; `frontend/src/styles.css:5247` | Use ticket at 13px/600, step at 13px/400, and age/status/owner at 10px. Limit normal weights to 400/500/600. | Keep `{N}m ago` placement. Do not remove fields. |
| Agents page | Menu and log controls remain visible even when they are not the scan target | Chrome density | `/tmp/bb-reference/apps/app/src/components/ui/theme.css:248` | `frontend/src/styles.css:5317`; `frontend/src/styles.css:5393` | Reveal optional actions on row hover, focus-within, or open state. Reserve their width to prevent reflow. Keep the primary status visible. | Keyboard focus must reveal each action. Touch layouts can keep actions visible. |
| Activity | **S1:** every commit is a raised bordered card, even when it is routine settled history | Hairline and box noise | `/tmp/bb-reference/packages/shared-ui/src/components/ui/activity-row-styles.ts:10` | `frontend/src/activity.tsx:263`; `frontend/src/styles.css:4217` | Use a flat commit row. Keep one day separator or group seam. Use fill/ring only for active or failed states. | Keep time, message, SHA, expansion, and file list. |
| Activity | Every changed file is a bordered rounded pill | Box noise and chrome density | `/tmp/bb-reference/packages/shared-ui/src/components/ui/activity-row-styles.ts:36` | `frontend/src/activity.tsx:285`; `frontend/src/styles.css:4261` | Render changed files as compact 10px metadata rows or a wrapped inline list. Use a plain status letter or word. Add hover fill only when the path is actionable. | Keep each path and status. Disabled paths remain visible. |
| Health | Three freshness values each use a bordered card. Counts use weight 700 | Box noise and weight discipline | `/tmp/bb-reference/apps/app/src/components/ui/detail-card.tsx:47`; `/tmp/bb-reference/apps/app/src/components/ui/theme.css:107` | `frontend/src/health.tsx:139`; `frontend/src/styles.css:4890`; `frontend/src/styles.css:4907` | Use one flat compact summary grid with internal spacing. Use 15px/600 for counts and 10px for labels. | Keep all three buckets, counts, and filter behavior. |
| Health | Every note type is a bordered pill. Name, type, path, and age are nearly one size | Chrome density and type rank | `/tmp/bb-reference/apps/app/src/components/ui/detail-card.tsx:31` | `frontend/src/health.tsx:178`; `frontend/src/styles.css:4945`; `frontend/src/styles.css:4971` | Use a 96px or content-fit 10px type column without a pill. Use 13px for the name and 10px for path and age. Keep state color only on stale/fresh age. | Keep basename, type, full path, and age. Keep path truncation with full title. |
| Dashboard | `Tickets` uses an 18px heading inside a page that already has a 40px app header | Type rank | `/tmp/bb-reference/apps/app/src/components/layout/AppPageHeader.tsx:92` | `frontend/src/styles.css:11532`; `frontend/src/styles.css:11539` | Treat `Tickets` as a 13px/600 section label. Keep the count at 10px. Keep the app header as the page title. | The `Tickets` label and count remain. |
| Dashboard | Autopilot, table, cost summary, cost grid, and prompt data each get separate borders | Hairline and box noise | `/tmp/bb-reference/apps/app/src/components/ui/detail-card.tsx:47` | `frontend/src/styles.css:11749`; `frontend/src/styles.css:11875`; `frontend/src/styles.css:11912`; `frontend/src/styles.css:13640` | Keep one frame for each semantic data group. Use flat detail rows and internal seams inside that frame. Remove redundant outer frames from summary and prompt subregions. | Keep all filters, ticket cells, costs, usage rows, and prompt bars. |
| Tokens | Totals and chart are two large, equally strong cards | Box noise and hierarchy | `/tmp/bb-reference/apps/app/src/components/ui/detail-card.tsx:47` | `frontend/src/tokens.tsx:411`; `frontend/src/styles.css:10013`; `frontend/src/styles.css:10047` | Make totals a flat summary strip above the chart. Keep one quiet chart boundary because the plot needs a frame. | Keep all totals, cached counts, filters, legend, chart, and tooltip. |
| Tokens | Data series colors are semantically valid. They are not non-accent decoration | Color usage | `/tmp/bb-reference/apps/app/src/components/ui/theme.css:667` | `frontend/src/tokens.tsx:460`; `frontend/src/styles.css:10116` | Keep series colors. Only reduce neutral frame and label contrast. | No series, legend item, or tooltip value changes. |
| Kanban | **S1:** a bordered lane contains a grid of bordered, filled, shadowed cards | Hairline and box noise | `/tmp/bb-reference/apps/app/src/components/ui/detail-card.tsx:47` | `frontend/src/kanban.tsx:282`; `frontend/src/kanban.tsx:312`; `frontend/src/styles.css:18095`; `frontend/src/styles.css:18101` | Make lanes flat scroll regions with a single header seam. Use either a quiet fill or one hairline on cards, not border, fill, and shadow together. Reserve the stronger frame for drag and selection states. | Keep the current multi-column lane layout, every card, drag behavior, editor, add action, and done zone. |
| Kanban | Lane title, count, and card content can all inherit 14.5px | Type rank | `/tmp/bb-reference/apps/app/src/components/ui/theme.css:107` | `frontend/src/styles.css:2973`; `frontend/src/styles.css:2983`; `frontend/src/styles.css:3008` | Use 13px/600 lane titles, 10px counts, and 13px/18px card content. Keep optional metadata at 10px. | Keep all rendered Markdown and card text. |
| Dialogs | The audited `origin/main` dialog is top-offset, square, borderless, and has weak rank | Shape, spacing, and hierarchy | `/tmp/bb-reference/packages/shared-ui/src/components/ui/dialog.tsx:270` | `frontend/src/styles.css:16289`; `frontend/src/settings.tsx:792` | Let in-flight WIKI-308 own this. Its final gate should center the desktop dialog, restore one border and rounded shape, and use 15/13/10 ranks. | Keep every field, hint, validation state, and action. Do not open a duplicate ticket before WIKI-308 lands. |
| Settings dialog | Fifteen theme choices each use a filled bordered card, and the active choice adds a second inset border | Box noise and selection | `/tmp/bb-reference/apps/app/src/components/ui/detail-card.tsx:47` | `frontend/src/settings.tsx:810`; `frontend/src/styles.css:2111` | Gate WIKI-308 on a quieter theme picker. Use a flat grid with one selected fill or ring. Reduce each tile to swatches plus a 10px label. | Keep all 15 choices and all four swatches per choice. |
| Start-run dialog | `THIS WILL CREATE` and field hints compete with form labels at the same size | Type rank and chrome density | `/tmp/bb-reference/packages/shared-ui/src/components/ui/dialog.tsx:277` | `frontend/src/agents.tsx:705`; `frontend/src/styles.css:16289` | Gate WIKI-308 on 13px labels, 10px hints and preview metadata, and 15px title text. | Keep the full creation preview and all advanced fields. |
| Toast | No material residual delta. Width, single border, title/description rank, and hover close match bb | Shape and hierarchy | `/tmp/bb-reference/apps/app/src/components/ui/app-toast.tsx:132` | `frontend/src/styles.css:17884`; `frontend/src/styles.css:17942` | Keep the current toast. Add only regression coverage in both bb themes. | Title, description, tone, actions, and close control remain. |
| Status strip | Exact bb parity is not possible because Wiki must keep the tmux strip. Its 28px pills and 11px rank are already calm | Standing ruling and chrome density | `/tmp/bb-reference/apps/app/src/components/ui/tab-pill.tsx:68` | `frontend/src/styles.css:16893`; `frontend/src/styles.css:16906` | Keep the strip under W2. Do not spend a new ticket on it. Verify truncation, tabular metrics, keyboard focus, and both themes. | Keep all windows and metrics. Do not restore a top tab strip. |
| Cross-theme color | No broad non-accent color defect appeared in bb-dark or bb-light. The remaining noise comes from surface count, not palette hue | Color usage | `/tmp/bb-reference/apps/app/src/components/ui/theme.css:641` | `frontend/src/themes.css:1`; `frontend/src/styles.css:17585` | Do not start another theme-copy ticket. Fix surface count and type rank first. Keep accent colors for state, selection, danger, and chart data only. | All state and data meaning remains. |

## Proposed ticket ladder

### WIKI-317 — Publish the formatting delta census

Scope: this audit only.

Exit criteria:

- Store both-theme screenshots for every requested surface at 1440x900.
- Record exact bb and Wiki source locations.
- Rank the visual causes.
- Propose follow-up tickets that preserve all information.
- Flag conflicts with standing rulings.

### WIKI-318 — Restore a three-tier type hierarchy

Scope: one dimension across the app. Start only after Henry rules on WIKI-279.

Exit criteria:

- Keep one font-size setting, if Henry approves that interpretation.
- Derive a 15px/22px prose tier, a 13px control tier, and a 10px/14px chrome tier at the default setting.
- Scale all three tiers together when the user changes the base size.
- Limit semantic weights to 400, 500, and 600. Do not use 700 for routine UI.
- Keep the selected family on all surfaces unless Henry separately permits a mono code exception.
- Verify computed sizes on session prose, tool rows, sidebar rows, page headers, metadata, code, dialogs, and utility pages.
- Pass both-theme 1440x900 screenshots without lost or clipped information.

### WIKI-319 — Compress the session input envelope

Scope: session user turns and thought runs.

Exit criteria:

- Clamp any overflowing user message at 15 line-heights.
- Add accessible `Show more` and `Show less` controls based on rendered overflow.
- Render known plugin and runtime envelopes as labeled disclosures with exact raw content inside.
- Use a 70% user-card cap on wide panes and a responsive cap on narrow panes.
- Group consecutive settled thoughts into one summary disclosure.
- Keep active thought work visible.
- Preserve every thought title, duration, encrypted state, body, and original order when expanded.
- Test the archived WIKI-316 transcript and one current long-output transcript in both themes.

### WIKI-320 — Remove nested framing from the Agents page

Scope: the Agents page only.

Exit criteria:

- Remove card chrome from orchestrator group wrappers.
- Render archived and routine settled workers as flat rows.
- Use a quiet fill or ring only for active, selected, blocked, or failed rows.
- Apply the ticket, step, and metadata type ranks from WIKI-318.
- Reveal optional row actions on hover, focus-within, open state, and touch layouts.
- Preserve every current field and disclosure.
- Verify long tickets, long steps, blockers, missing metadata, multiple workers, and empty groups.

### WIKI-321 — Flatten Activity and Health lists

Scope: the Activity and Health utility cluster.

Exit criteria:

- Replace per-commit cards with flat rows and one day-group seam.
- Replace file pills with compact path/status metadata.
- Replace three Health cards with one flat summary grid.
- Replace note-type pills with a compact text column.
- Keep every time, SHA, path, status, type, age, count, filter, and expansion.
- Verify truncation exposes full values through titles or disclosure.
- Capture loaded, empty, error, and stale states in bb-dark and bb-light.

### WIKI-322 — Reduce Kanban box noise

Scope: Kanban only.

Exit criteria:

- Make each lane a flat scroll region with one header seam.
- Give routine cards one surface treatment: fill or hairline, not both plus shadow.
- Reserve stronger treatment for drag, drop, edit, and selection states.
- Apply 13px/18px card text, 13px/600 lane titles, and 10px counts.
- Keep multi-column lanes, horizontal scroll, every card, Markdown, drag behavior, editing, adding, and the done zone.
- Verify dense four-column content in both themes at 1440x900.

### WIKI-323 — Compress the session header and finish composer rank

Scope: session chrome only.

Exit criteria:

- Fit the normal desktop session header into one 40px row.
- Keep ticket, state, current step, blocker, and primary actions visible.
- Place kind, role, model, and loop details in a 10px inline cluster or labeled disclosure.
- Remove redundant runtime dividers. Keep one bottom seam.
- Keep the existing composer card and 10px metadata footer.
- Set composer target and editor control text to the control tier.
- Keep the W10 round send button.
- Test long tickets, long steps, blockers, all runtime fields, narrow panes, and keyboard focus.

### WIKI-324 — Simplify Dashboard and Tokens framing

Scope: data-heavy Dashboard and Tokens surfaces.

Exit criteria:

- Use one frame per semantic data group.
- Use internal seams or whitespace for subregions.
- Make the Tokens totals strip flat and keep one quiet chart boundary.
- Demote the Dashboard `Tickets` section heading to control rank.
- Keep all filters, ticket data, cost data, prompt bars, totals, chart series, legend items, and tooltips.
- Preserve data-series and state colors.
- Verify zero, loading, stale, error, and populated states in both themes.

### WIKI-325 — Normalize optical chrome details

Scope: one cross-app dimension after WIKI-318 through WIKI-324.

Exit criteria:

- Use one 28px desktop action box and one 14px normal glyph size.
- Use reduced glyph sizing only for optically dense icons.
- Use the bb 1.75 icon stroke where the icon library permits it.
- Align labels, counts, timestamps, and icons on stable baselines.
- Reserve action width before hover reveal so labels do not move.
- Keep full text through truncation titles or disclosure.
- Add screenshot checks for sidebar, app header, session header, Agents, Activity, and dialogs.

## Deltas that need a ruling or must remain

> **Rulings resolved (Henry, 2026-08-17):** R1 AMENDED — one user-facing base size derives the 15/13/10 tiers; all tiers scale together (WIKI-318 unblocked; WIKI-279 reinterpreted, settings copy to be updated). R2 DECLINED — one family everywhere stands; no mono exception for code/terminal (code keeps the chosen family; only its size moves to the control tier via WIKI-318). NEW DIRECTION — macos-native layered on the ladder: WIKI-326 SF-Symbols-style icon pass + WIKI-327 macos-light/macos-dark theme pair, run alongside WIKI-318-325.

### R1 — bb's type scale conflicts with WIKI-279

WIKI-279 says one pixel size for prose, chat, code, and chrome. The live settings copy repeats that rule at `frontend/src/settings.tsx:878`.

bb's design language depends on three sizes: 15px body, 13px controls, and 10px chrome. Full type parity is impossible under a literal one-pixel-size rule.

Recommended ruling: keep one user control, but make it a base size that derives all semantic tiers. If Henry rejects this, skip WIKI-318 and accept the largest remaining visual gap.

### R2 — bb's code family conflicts with WIKI-279

bb uses Inter Variable for UI and Fira Code for code. Wiki's current one-family rule assigns the chosen font to code too.

Recommended ruling: do not change the family in this ladder. Ask separately whether code and terminal output can use one fixed mono exception.

### R3 — Keep the tmux status strip

bb has no equivalent bottom tmux strip. W1 and W2 removed the old top tab strip and retained the bottom strip. Exact shell parity is therefore not a goal.

The current strip is already close to bb's tab-pill grammar. Keep it.

### R4 — Keep `{N}m ago` placement

Do not remove or move the relative age. Only demote it to the chrome tier and use tabular numerals.

### R5 — Keep the rounded outer panel

W9 keeps Wiki's rounded outer panel. bb's shell can feel flatter. Do not remove the panel to chase exact parity.

### R6 — Keep the round send button

W10 keeps the round send button. Do not copy bb's send shape.

### R7 — Keep Obsidian rendering and Kanban structure

W3 keeps all Obsidian Markdown features. The multi-column Kanban is also a Wiki product surface with no direct bb equivalent.

Restyle these surfaces. Do not remove Markdown features, card content, columns, or plugin behavior.

### R8 — Keep the Agents route

W7 keeps the Agents route. bb has no exact route equivalent. Use bb's activity-row grammar without deleting the route or its operational fields.

### R9 — Keep the 11-theme architecture

W5 keeps all themes. This audit only validates bb-dark and bb-light because they are the direct reference themes. Semantic changes must still compose in the other themes.

### R10 — Do not duplicate WIKI-308

The screenshots show old dialog chrome because WIKI-308 is not on `origin/main`. Apply the dialog criteria above to WIKI-308's final gate. Open a new dialog ticket only if a residual remains after it merges.

## Work that does not need a new ticket

- Toasts already match bb's width, single border, title rank, description rank, and hover close behavior.
- The composer shell already matches bb's raised rounded form.
- Tool rows and tool-output disclosure are already flat and information-rich.
- The sidebar already uses bb's 28px row and sticky-tier geometry.
- The status strip already uses a quiet 28px pill grammar.
- The bb-dark and bb-light palette copy is not the current problem.

The implementation ladder should stop when these targeted changes close the visual gap. It should not reopen completed parity work without new screenshot evidence.

## Amendment — 2026-08-17 late (Henry, live review)

- **R1 SUPERSEDED: one size, no tiers.** After seeing the 15/13/10 tiers live, Henry ruled all text renders at the single picker size — prose, controls, and chrome alike. `--font-prose/control/chrome-size` all alias `--font-single-size`; tier token names survive as aliases. WIKI-334, direct to main (7809927).
- **Composer flattened (supersedes the WIKI-303 card shape):** square corners (`--composer-radius: 0`), transparent fill flush with the pane, no `--shadow-lift`; focus shifts the hairline only. Round send stays (W10 holds).
- Delivery-mode change for this arc: Henry works interactively, direct commits to main, no PRs.
