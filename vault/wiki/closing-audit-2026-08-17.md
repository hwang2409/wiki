---
type: reference
tags: [wiki, ui, bb, parity, audit]
created: 2026-08-17
updated: 2026-08-17
---

# WIKI-329 bb-parity closing audit

**Overall verdict: PUNCH LIST REQUIRED**

The program is not complete. Five root defects remain. Three defects break explicit contract rulings. One defect can cause accidental deletion.

## Audit basis

- Source: `8e7ed10a6b2b1cd0558f975f176ea197b6b24a84`, which matched `origin/main` at audit start.
- Build: `npm ci` and `npm run build` passed in an isolated copy. Vite served the build on free port 5175.
- Data: the audit used the read-only backend on port 8213. An isolated rich fixture covered transcript edge cases.
- Viewport: every requested surface was captured at 1440x900 in bb-dark and macos-dark. bb-light spot checks were also captured.
- Evidence: 63 screenshots are in `/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/`.
- Type baseline: the semantic tokens compute to 15px prose, 13px controls, and 10px chrome at the default base.
- Weight baseline: no sampled visible text exceeded weight 600.
- Information checks: the audit opened transcript, tool, agent-detail, and dialog disclosures. Exact user and envelope text remained available.

## Surface verdicts

| Surface | Verdict | Evidence screenshot | Gap detail | Suggested fix size |
|---|---|---|---|---|
| Theme and type foundation | PASS | [bb-dark session](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-dark-session-user-envelope.png) · [macos-dark session](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-session-user-envelope.png) | The root tokens compute to 15/13/10. Sampled weights use 400, 500, or 600. Surface overrides still bypass these tokens, as listed below. | — |
| Shell and standing rulings | PASS | [bb-dark shell](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/00-recon-session-bb-dark.png) · [macos-dark shell](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-session-user-envelope.png) | W2 keeps the tmux strip. W9 keeps the 12px rounded panel. W10 keeps the 34px round send button. The selected font family drives the semantic family tokens. | — |
| Session assistant prose and code fences | PASS | [bb-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-dark-assistant-code.png) · [macos-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-assistant-code.png) | Prose computes at 15px. The fixture code fence computes at 13px and uses the selected family. Code content remains exact. | — |
| Session user cards and envelopes | FAIL | [bb-dark open envelope](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-dark-session-envelope-open.png) · [macos-dark card](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-session-user-envelope.png) · [bb-light card](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-light-session-user-envelope.png) | The 15-line clamp works. `Show more` reveals all 40 fixture lines. Both known envelopes expose their exact raw text. However, `.session-envelope-raw` is a `pre` leaf that falls back to generic `monospace`. This breaks R2's one-family ruling. | S |
| Session thought groups | FAIL | [bb-dark expanded](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-dark-thought-group-expanded.png) · [macos-dark expanded](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-thought-group-expanded.png) · [bb-light expanded](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-light-thought-group-expanded.png) | The collapsed summary preserves count, total duration, and encrypted state. Expansion preserves all titles, durations, markers, bodies, and order. But the virtual row keeps a short height. Its 477px content overlaps the next row by 450px. Tool output, assistant prose, and the composer become unreadable. | M |
| Session tool rows and output | FAIL | [bb-dark expanded](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-dark-tool-row-expanded.png) · [macos-dark expanded](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-tool-row-expanded.png) | Row shape, target, result tail, full command, and output disclosure pass. The command `code.codex-stream-command-input` falls back to generic `monospace`. This breaks R2. | S |
| Composer | PASS | [bb-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-dark-assistant-code.png) · [macos-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-assistant-code.png) | Control text computes at 13px. Footer metadata computes at 10px. Ticket, model, state, shortcuts, Vim state, and help remain visible. The send button stays round. | — |
| Session header | PASS | [bb-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-dark-session-user-envelope.png) · [macos-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-session-user-envelope.png) | The header is one 40px row. Ticket, state, kind, role, model, graph, replay, and close actions remain reachable. The row uses one bottom seam. | — |
| Sidebar | PASS | [bb-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-dark-sidebar.png) · [macos-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-sidebar.png) | Rows keep the 28px grammar. Primary text and metadata use the correct tiers. Optional actions reserve width. Labels do not reflow. `{N}m ago` stays in its locked position. | — |
| Agents page | FAIL | [bb-dark hover](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-dark-agents-hover.png) · [macos-dark groups](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-agents-groups.png) · [macos-dark details](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-agents-details.png) | Group wrappers are flat. Only active states get a frame. Hover actions reveal without reflow. Details preserve provider, role, model, PR, branch, run, worktree, and log. However, 29 visible status labels compute at 11px instead of 10px. | M |
| Settings dialog | FAIL | [bb-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-dark-settings-dialog.png) · [macos-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-settings-dialog.png) · [bb-light](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-light-settings-dialog.png) | Centering, scrim, rounded border, sticky footer, focus trap, Escape, and focus return pass. The title computes at 14px. Descriptions, rail labels, and buttons compute at 12px. The theme picker still gives every tile a fill and border. Selection adds a second inset ring. Both points break the formatting contract. | M |
| Spawn dialog | FAIL | [bb-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-dark-spawn-dialog-advanced.png) · [macos-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-spawn-dialog-advanced.png) · [bb-light](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-light-spawn-dialog-advanced.png) | Shape, fields, advanced disclosure, trap, Escape, and focus return pass. The title computes at 14px. Description and footer controls compute at 12px. The dialog misses the 15/13/10 ranks. | M |
| Destructive dialogs | FAIL | [bb-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-dark-destructive-dialog.png) · [macos-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-destructive-dialog.png) · [bb-light](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-light-destructive-dialog.png) | Shape, warning text, Escape, and focus return pass. Initial focus lands on `Delete`. An immediate Enter can delete the item. Title and control sizes also inherit the 14px and 12px dialog stragglers. | S |
| Dashboard | FAIL | [bb-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-dark-dashboard.png) · [macos-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-dashboard.png) · [bb-light](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-light-dashboard.png) | The loaded state has 178 ticket rows and no skeleton. Semantic groups use one frame with flat internal rows. All ticket and cost fields remain. Yet 641 visible text leaves compute at 12px and 186 status labels compute at 11px. | M |
| Tokens | FAIL | [bb-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-dark-tokens.png) · [macos-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-tokens.png) | Totals form a flat strip. The chart keeps one useful boundary. Totals, cache counts, filters, legend, and series remain. Eleven controls compute at 12px. Eight filter or legend labels compute at 11px. | M |
| Activity | PASS | [bb-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-dark-activity.png) · [macos-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-activity.png) | Commit rows and changed files are flat. Time, message, SHA, disclosure, file path, and status remain. All sampled visible text uses 15px, 13px, or 10px. | — |
| Health | PASS | [bb-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-dark-health.png) · [macos-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-health.png) | The summary grid and note rows are flat. All buckets, counts, filters, names, types, paths, and ages remain. All sampled visible text uses 15px, 13px, or 10px. | — |
| Kanban | FAIL | [bb-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-dark-kanban.png) · [macos-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-kanban.png) | Lanes are flat. Cards use one quiet fill without a border and shadow stack. Columns, cards, add controls, and the done zone remain. Sixty-eight priority labels compute at 9.36px. One loading label computes at 11px. | M |
| Toasts | FAIL | [bb-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-dark-toast.png) · [macos-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-toast.png) · [bb-light](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-light-toast.png) | Width, one border, tone, action, and close control pass. The title computes at 12px. The description computes at 11px. These sizes bypass the 13px and 10px tiers. | M |
| Status strip | FAIL | [bb-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/bb-dark-status-strip.png) · [macos-dark](/Users/henry/me/fun/wiki/.worktrees/wiki-329-audit/.playwright-mcp/wiki-329/macos-dark-status-strip.png) | W2 shape passes. The strip stays quiet, keeps all windows and metrics, and uses tabular data. Its labels still compute at 11px through `--fs-xs`. The later R1 amendment and this audit gate require 10px chrome. | M |

## Ranked punch list

### 1. Fix thought-group virtual row measurement

- Priority: P0
- Size: M
- Problem: expanded thought groups use their estimated virtual height. They overlap all later transcript rows.
- Likely cause: `session-layout.ts:getRowHeight` validates a measurement against `[row.event]`. Group measurements contain all thought event references, so validation always fails.
- Scope: `frontend/src/session.tsx`, `frontend/src/session-layout.ts`, and virtual-layout tests.
- Acceptance:
  - Collapsed and expanded groups update `totalHeight` and every later row top.
  - No group overlaps a tool row, assistant row, timestamp, or composer.
  - Expansion preserves every title, duration, encrypted state, body, and original order.
  - Add 1440x900 regression checks in bb-dark, macos-dark, and bb-light.

### 2. Put safe initial focus in destructive dialogs

- Priority: P0
- Size: S
- Problem: `Dialog` assigns `initialFocusRef` to the confirm button when no input exists. A danger dialog therefore opens on `Delete`.
- Scope: `frontend/src/App.tsx` and dialog focus tests.
- Acceptance:
  - A danger alert dialog opens on `Cancel` or the close control.
  - Enter immediately after open cannot confirm deletion.
  - Tab stays trapped. Escape closes the dialog and restores the file-row origin.

### 3. Finish the 15/13/10 type-tier migration

- Priority: P1
- Size: M
- Problem: legacy `--fs-xs`, fixed pixel sizes, and old component tokens bypass the semantic tiers.
- Scope: Agents status labels; session state metadata; Settings, Spawn, and destructive dialogs; Dashboard; Tokens; Kanban priority labels; Toasts; tmux status strip.
- Measured stragglers:
  - Agents: 29 status labels at 11px.
  - Session state: two labels at 11px.
  - Dialogs: titles at 14px; descriptions and controls at 12px.
  - Dashboard: 641 text leaves at 12px; 186 status labels at 11px.
  - Tokens: 11 controls at 12px; eight filter or legend labels at 11px.
  - Kanban: 68 priority labels at 9.36px; one loading label at 11px.
  - Toasts: title at 12px; description at 11px.
  - Status strip: window labels at 11px.
- Acceptance:
  - At the default base, visible prose, control, and chrome text computes only at 15px, 13px, or 10px.
  - Normal text weights stay at or below 600.
  - W2 strip shape, R4 age placement, dialog focus, and all fields remain unchanged.
  - Add a computed-style regression test for each affected surface.

### 4. Apply the selected family to transcript `pre` and `code` leaves

- Priority: P1
- Size: S
- Problem: browser defaults override inherited family on raw envelope and command leaves.
- Scope: `.session-envelope-raw`, `.codex-stream-command-input`, and focused tests.
- Acceptance:
  - Both leaves compute to the selected family, not generic `monospace`.
  - Markdown code fences keep the same selected family.
  - Raw bytes, wrapping, copy, and output disclosures remain unchanged.

### 5. Flatten the Settings theme grid

- Priority: P2
- Size: S
- Problem: every theme tile has a fill and border. The selected tile adds a second inset border.
- Scope: `.theme-choice`, `.theme-choice.is-active`, and Settings visual tests.
- Acceptance:
  - Unselected tiles are flat.
  - Selection uses one fill or one ring, not both.
  - All 15 choices and all four swatches per choice remain.
  - Verify bb-dark, macos-dark, and bb-light at 1440x900.

## Ruling check

No finding needs a new Henry ruling. All five fixes are mechanical under W2, W9, W10, amended R1, and declined R2.

## Resolution (2026-08-17 ~23:15)

Punch list fully closed; the bb parity contract is met.

| # | Item | Fixed by |
|---|---|---|
| 1 | Thought-group virtual row measurement | #266 |
| 2 | Safe initial focus in destructive dialogs | #267 |
| 3 | 15/13/10 type-tier migration | #268 (legacy `--fs-*` scale now aliases the semantic tiers; kanban priority badge off `0.72em`; per-class probe matrix in wiki-143-tokens) |
| 4 | Selected family on transcript `pre`/`code` leaves | #267 |
| 5 | Flatten the Settings theme grid | #267 |

Follow-up ratchet #269: live tier sweep (`wiki-332-tier-sweep`) walks every visible text leaf on the session surface, status strip, and Settings dialog — off-tier text on classes the audit never sampled fails the suite. Also registered `wiki-331-closing-fixes` in the stage runner (it was never wired in).

Remaining out of scope: WIKI-313 graph view (P3, W4 ruling). Note for future gates: the full legacy stage chain has pre-existing reds on main (wiki-145-noise `.notice` assert, wiki-237 pre-W10 send shape, wiki-105 font enumeration, terminal-pane); the operative merge gate tonight was build + vitest + affected suites.
