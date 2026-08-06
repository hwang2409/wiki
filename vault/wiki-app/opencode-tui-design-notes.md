---
type: reference
tags: [wiki-app]
created: 2026-08-06
updated: 2026-08-06
---

# OpenCode TUI design notes

Source: `/tmp/opencode-ref/packages/tui/src`. All paths below are relative to that root. Line numbers from the cloned checkout.

## Semantic color roles

Full token set: `theme/index.ts:36-91`. Non-diff/non-syntax roles:

| role | usage rule (observed) |
|---|---|
| `primary` | THE selection color. Selected list rows (`ui/dialog-select.tsx:675`, `component/prompt/autocomplete.tsx:752`), selected dialog buttons (`ui/dialog-confirm.tsx:75`, `ui/dialog-alert.tsx:46`), focused footer actions (`ui/dialog-select.tsx:543`), cursor color (`ui/dialog-select.tsx:581`), "esc again to interrupt" armed state (`prompt/index.tsx:1584`). |
| `secondary` | Active option text in question prompts (`routes/session/question.tsx:384`), attachment "File" chip bg (`routes/session/index.tsx:1413`), pending editor-context label (`prompt/index.tsx:1656`), `@agent` extmark (`theme/index.ts:608-612`). |
| `accent` | Filter-prompt syntax (`theme/index.ts:595-598`), category headers in list dialogs (`ui/dialog-select.tsx:625`), question-prompt border + active tab (`question.tsx:292,310`), workspace notices and progress spinners (`prompt/index.tsx:1595,1603,1630`). |
| `error` / `warning` / `success` / `info` | Status only. Warning = permission borders/buttons and `△` glyph (`permission.tsx:635,653,682`); error = reject border (`permission.tsx:477`), retry text (`prompt/index.tsx:1577`); success = green status dots `•`/`⊙`/`✓` (`footer.tsx:70,79`, `question.tsx:384-389`); toasts map variant name directly to border color: `theme[current().variant]` (`ui/toast.tsx:35`). |
| `text` / `textMuted` | Exactly TWO text emphasis levels, plus bold as a third axis. See below. |
| `selectedListItemText` | Foreground on `primary`-filled selections; falls back to `background` or luminance-computed black/white (`theme/index.ts:95-111`). |
| `background` / `backgroundPanel` / `backgroundElement` / `backgroundMenu` | Four background elevations. Default theme maps them to Radix-style steps 1/2/3/3 (`theme/assets/opencode.json`; menu falls back to element, `theme/index.ts:285-289`). |
| `border` / `borderActive` / `borderSubtle` | Steps 7/8/6 of the same scale. `border` = neutral dividers (autocomplete frame `autocomplete.tsx:731`, idle prompt border `prompt/index.tsx:1288`); `borderActive` = scrollbar thumbs (`sidebar.tsx:44`), compaction rule (`session/index.tsx:1448`). |
| `thinkingOpacity` (default 0.6) | Reasoning text rendered at 60% alpha of normal syntax colors (`theme/index.ts:292`, `560-584`). |

### Text emphasis hierarchy

1. **`text` + bold** — titles, key labels of keybind hints, selected items (`dialog-select.tsx:562`, `subagent-footer.tsx:81`).
2. **`text`** — body content, primary values.
3. **`textMuted`** — everything secondary: descriptions, paths, keybind hint descriptions, placeholders, timestamps, metadata (`footer.tsx:54`, `dialog-select.tsx:593,700`).

The dominant micro-pattern is a two-tone pair inside one line: key in `text`, meaning in `textMuted` — `enter <muted>confirm</muted>` (`permission.tsx:706`, `question.tsx:500-509`, `prompt/index.tsx:1670-1677`). Color, not size, encodes hierarchy; there is no heading scale in chrome.

### Backgrounds / elevation

- `background` (step1): app canvas.
- `backgroundPanel` (step2): sidebar (`sidebar.tsx:29`), dialogs (`ui/dialog.tsx:60`), toasts (`toast.tsx:34`), user message cards (`session/index.tsx:1402`), permission/question panels (`permission.tsx:633`, `question.tsx:290`), subagent footer (`subagent-footer.tsx:76`).
- `backgroundElement` (step3): the composer input (`prompt/index.tsx:1364`), hover states (`subagent-footer.tsx:101`, `session/index.tsx:1402`), permission footer strip (`permission.tsx:672`), unselected question rows on hover, active question row bg (`question.tsx:378`).
- `backgroundMenu`: autocomplete popup (`autocomplete.tsx:735`) and unselected permission buttons (`permission.tsx:682`).

### Selection / hover states

- Selection = **background fill shift** (`primary` fill + contrast fg, bold), never an outline (`dialog-select.tsx:671-676,768`; `autocomplete.tsx:752,767`).
- Hover = one elevation step up (`backgroundPanel` → `backgroundElement`), no color (`subagent-footer.tsx:101-121`, `question.tsx:311-313`).
- "Current" (already-active item, not cursor) = `primary` foreground + `●` gutter dot, no fill (`dialog-select.tsx:749-758`).
- When focus moves to footer actions, the list selection demotes to `backgroundElement` fill with muted text (`dialog-select.tsx:673-674,748`).

## Layout + spacing

Implicit scale from grepping all `padding*`/`gap` values: **1 (137×), 2 (66×), 3 (34×), 4 (11×)** cells; gaps are 1 (87×) or 2 (16×). Rules:

- 1 = default gap between sibling texts and stacked blocks.
- 2 = container inset (message list `paddingLeft/Right={2}` `session/index.tsx:1166`; sidebar `sidebar.tsx:34-35`; card text inset).
- 3 = indent for continuation/detail lines under an item (`dialog-select.tsx:699`, `question.tsx:393,426`) and hanging indent after a marker.
- 4 = dialog header/footer inset only (`dialog-select.tsx:559,717`).
- 2 = gap between footer keybind-hint groups (`dialog-select.tsx:718`, `question.tsx:489`, `prompt/index.tsx:1653`); 1 = gap inside a group.

Width strategy:
- Chat content: full width minus fixed insets — `dimensions().width - (sidebar ? 42 : 0) - 4` (`session/index.tsx:271`). No max content width for the transcript.
- Sidebar: fixed 42 cells (`sidebar.tsx:30`); below a width threshold it becomes an overlay over a `rgba(0,0,0,70/255)` scrim (`session/index.tsx:1330-1340`).
- Home composer: `maxWidth = max(75, 70% of viewport)` or configured (`routes/home.tsx:33-37`).
- Dialogs: fixed widths 60 / 88 / 116 (medium/large/xlarge), clamped to viewport-2 (`ui/dialog.tsx:22-26,59`).
- Toast: `maxWidth min(60, w-6)`, top-right at (2,2) (`toast.tsx:27-29`).

Borders vs whitespace:
- There are effectively **no boxes**. `ui/border.ts` defines only `EmptyBorder` (all blank) and `SplitBorder` — a single heavy vertical bar `┃` used as a LEFT edge accent (`border.ts:15-21`). Full 4-side borders never appear; the one `border={["top"]}` is the "Compaction" horizontal rule with centered title (`session/index.tsx:1443-1449`).
- The left bar is the signature element: user messages (agent color, `session/index.tsx:1386-1388`), composer (agent color, `prompt/index.tsx:1352-1357`), permission (warning), question (accent), reject (error), subagent footer (`border`), toasts (variant color, left+right, `toast.tsx:36-37`).
- Everything else separates by background elevation change + 1-cell padding. Dialogs have no frame at all — just a panel fill on a dark scrim.

## Dialog anatomy

Overlay: full-screen scrim `rgba(0,0,0,150/255)`; dialog top edge sits at 1/4 viewport height, horizontally centered (`ui/dialog.tsx:44-48`). Panel: `backgroundPanel`, `paddingTop 1`, no border, fixed width (`dialog.tsx:58-62`). Click outside closes. One dialog at a time (stack replaces).

List dialog (`ui/dialog-select.tsx:557-727`):

```
  ┌ scrim ──────────────────────────────────────┐
  │    ░░░░░░░░░░░░ backgroundPanel ░░░░░░░░░░  │  width 60/88/116
  │    Title (bold)                        esc  │  padding-x 4; "esc" muted, clickable
  │                                             │
  │    Search…                                  │  filter input, muted placeholder
  │                                             │  (gap 1)
  │   CATEGORY (accent, bold)                   │  padding-left 3
  │   ███ selected row ████████████ footer ███  │  primary fill, contrast fg, bold
  │     other row      muted description        │  padding-left 3
  │   ● current row (primary fg, dot gutter)    │
  │                                             │
  │    Action ctrl+x     Action2 ctrl+y    esc  │  footer: key=text, label=muted; gap 2
  └─────────────────────────────────────────────┘
```

- Title row: bold title left, muted `esc` right (`dialog-select.tsx:560-569`).
- Filter input is a plain line (no box), placeholder muted, cursor `primary` (`:570-596`).
- List max height = half viewport minus 6 (`:213`).
- Empty state = muted "No results found" (`:602-606`).
- Confirm/alert dialogs: bold title + esc, muted message, right-aligned button row; buttons are text chips with `primary` fill when active, padding-x 1-3 (`dialog-confirm.tsx:57-88`, `dialog-alert.tsx:30-55`).

## Status bar / footer anatomy

Session footer (`routes/session/footer.tsx:52-90`): ONE line, no background, no separators except spacing.

```
~/code/project                    △ 2 Permissions  • 3 LSP  ⊙ 2 MCP  /status
└ muted cwd, left                 └ right cluster, gap 2 ────────────────────┘
```

- Left: cwd in `textMuted`.
- Right: glyph-prefixed counts; glyph carries status color (`success` dot when up, `error` when failed, `warning` △ for pending permissions), count in `text`, trailing `/status` hint in `textMuted`.
- Subagent footer is heavier: `backgroundPanel` strip, left `┃` border, padding y 1: left = bold label + muted "(1 of 3)" + muted "tokens · cost" joined by `·`; right = clickable "Parent ^p / Prev / Next" pairs with hover elevation (`subagent-footer.tsx:66-128`).

## Composer / prompt dock anatomy

(`component/prompt/index.tsx:1347-1687`)

```
┃ ░░░░ backgroundElement ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
┃ Ask anything... "Fix a TODO in the codebase"            ← textarea, muted placeholder
┃ ░░░░                                                    ← padding-x 2, padding-top 1
┃ Build auto · big-pickle opencode                        ← meta row: agent (agent color),
╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀  ← half-block bottom fade
 ■■■⬝⬝⬝⬝⬝  esc interrupt          … or when idle:
 ~/dir                       12.3K (4%) · $0.02  tab agents  ctrl+p commands
```

- Frame: LEFT bar only (`┃`, terminating `╹`), colored by **agent color** — each agent gets a deterministic color from a 7-color theme cycle (`context/local.tsx:83-91,119-130`); shell mode switches it to `primary` (`prompt/index.tsx:1287-1293`). Border tint fades in over 160 ms (`:1308`, `util/signal.ts:40-44`).
- Input bg `backgroundElement`; bottom edge is a `▀` half-block row in the same bg — a soft fade instead of a hard border (`:1484-1509`).
- Meta row inside the box, below text: agent name (agent color) `· model provider` with muted separators (`:1441-1481`).
- Below the box, one hint line, space-between: left = spinner + status/retry or cwd; right = context% · cost, then `key muted-label` hints (`:1510-1686`).
- Attachments render as inline chips in text: `[Image 1]` with `warning` bg extmark (`theme/index.ts:614-621`); file chips = colored label segment + muted filename segment, both as bg spans (`session/index.tsx:1412-1417`).
- Busy state: knight-rider block spinner `■■⬝⬝⬝⬝` in agent color, 40 ms interval (`:1521-1523`), plus `esc interrupt` on the right; pressing esc once arms it — text turns `primary` "esc again to interrupt" (`:1584-1589`).

## Inline interruptions (permission / question)

Both render **inline in the flow above the composer**, not as modal dialogs.

- Permission (`permission.tsx:631-710`): panel = `backgroundPanel` + warning `┃` left border, maxHeight 15 (expandable to fullscreen). Header: `△ Permission required` then indented `→ Edit path/file` (icon muted, per-tool glyphs: `→ ✱ # % ◈ ⚙`, `permission.tsx:203-380`). Body: diff or muted key-value lines, padding-left 1. Footer strip: `backgroundElement`, left = button chips (`Allow once / Allow always / Reject`) padding-x 1, selected chip = `warning` fill + contrast fg, unselected = `backgroundMenu` + muted (`:676-694`); right = keybind hints (`⇆ select  enter confirm`).
- Question (`question.tsx:288-513`): same skeleton with `accent` border. Multi-question = tab chips (active = accent fill, answered = text, unanswered = muted). Options are numbered rows `1. label` with muted description indented 3; active row = `backgroundElement` fill + `secondary` text; picked = `success` + `✓`. Footer hints only, no buttons.
- Reject sub-stage swaps border to `error` and embeds a one-line textarea in the footer strip (`permission.tsx:473-520`).

## Motion language

- Generic spinner: 10-frame braille, **80 ms** interval, muted by default; when animations are disabled it degrades to a static `⋯` (`component/spinner.tsx:10-25`).
- Working spinner: bidirectional knight-rider trail of blocks `■`/`⬝` in agent color, **40 ms** interval, holds at each end (holdStart 30 / holdEnd 9 frames), trail via alpha falloff 0.65^n, minAlpha 0.3 (`ui/spinner.ts:272-329`, `prompt/index.tsx:1321-1343`); static fallback `[⋯]` (`:1521`).
- Fade-in: single easing used anywhere — 160 ms smoothstep at 16 ms ticks, for prompt meta text and border tint (`util/signal.ts:36-44`); nothing fades out.
- Toast: appears instantly top-right, auto-dismisses after **5000 ms** default (3000 for minor warnings, `prompt/index.tsx:221`), one at a time — new replaces old (`ui/toast.tsx:61-67`). No slide/fade.
- Startup loading: only shows if startup exceeds **500 ms**, then stays a minimum **3000 ms** to avoid flicker; small centered pill at bottom (`startup-loading.tsx:42-46,22`).
- Ambient bg pulse exists only on one upsell screen, capped to 30 fps (`bg-pulse.tsx:77-82`).
- Global kill switch: `animations_enabled` KV; every animation has a static fallback.
- Restraint summary: no layout animation, no easing on movement, no exit transitions. Motion = spinners + one 160 ms opacity fade + dot ellipsis ticks (`prompt/index.tsx:1610,1632`).

## Empty states

- Home (`routes/home.tsx:70-93`): vertically centered column — flexible spacer, logo, 1-row gap, composer (maxWidth 75/70%), flexible spacer. No feature tour, no cards. The composer placeholder does the onboarding: random rotating example, `Ask anything... "Fix broken tests"` (`prompt/index.tsx:1310-1319`, examples `home.tsx:17-20`).
- Logo: two-tone text-art — left word muted, right word bold `text`, with a shadow tint at 0.25 alpha of bg→fg (`component/logo.tsx:10,53-55`).
- List empty state: one muted line ("No results found" / "No matching items"), same padding as rows (`dialog-select.tsx:602-606`, `autocomplete.tsx:743-745`).

## Transferable rules for a web app

1. Use exactly four background elevations: canvas, panel, element, menu — mapped to steps 1/2/3/3 of a Radix-style 12-step neutral scale; borders from steps 6/7/8, muted text 11, text 12 (`theme/assets/opencode.json`).
2. Two text colors only (`text`, `textMuted`); bold is the only extra emphasis. No font-size hierarchy in chrome.
3. Write hints as pairs: keybind/verb in `text`, explanation in `textMuted`, e.g. "enter confirm" (`permission.tsx:702-707`).
4. Selection = solid `primary` background fill with auto-contrast foreground + bold. Never a border or outline (`dialog-select.tsx:671-676`).
5. Hover = one background elevation step up (panel→element), no color change (`subagent-footer.tsx:101`).
6. "Currently active" (vs "cursor is here") = `primary` text color + a `●` gutter dot, no fill (`dialog-select.tsx:755-758`).
7. Never draw 4-sided boxes. Panels are background-fill rectangles; the only border is a thick left edge bar (~3-4px web equivalent) whose color encodes semantics (`ui/border.ts:15-21`).
8. Left-bar color code: agent color = messages/composer, warning = permission, accent = question, error = reject/danger, status color = toast (`session/index.tsx:1387`, `permission.tsx:635`, `question.tsx:292`, `toast.tsx:35`).
9. Give each agent a deterministic identity color cycled from [secondary, accent, success, warning, primary, error, info]; reuse it for its message bar, composer bar, and spinner (`context/local.tsx:83-91`).
10. Spacing scale: 1 unit (~8px) default gap; 2 units container inset; 3 units detail-line indent; 4 units dialog header inset. Keybind-hint groups separate by 2 units (`grep counts; dialog-select.tsx:559,717,718`).
11. Dialogs: fixed widths (3 sizes), top-anchored at 25% viewport height on a ~60% black scrim, no frame, click-outside closes, one at a time (`ui/dialog.tsx:22-62`).
12. Every list dialog = title row (bold title + muted "esc" top-right) / plain filter input / grouped rows (accent bold category headers) / footer action row with key labels (`dialog-select.tsx:557-727`).
13. Buttons are text chips: padding-x 1, background fill when selected (primary or the panel's semantic color), `backgroundMenu` + muted when not. No borders, no rounding language (`permission.tsx:679-694`, `dialog-confirm.tsx:70-87`).
14. Status bar: single line, transparent, muted path left; right cluster of `glyph + count` items where only the glyph is status-colored, gap 2 (`footer.tsx:52-90`).
15. Metadata joins with a muted `·` separator: "12.3K (4%) · $0.02", "model · provider" (`prompt/index.tsx:1665`, `subagent-footer.tsx:91`).
16. Interruptions (approvals/questions) render inline in the conversation flow above the composer, not as modals; footer strip inside the panel holds the choices (`permission.tsx:631-710`).
17. Motion budget: spinners for progress, one 160 ms smoothstep opacity fade-in for appearing metadata, nothing else. No movement easing, no exit animations; every animation needs a static fallback and a global disable flag (`util/signal.ts:36-44`, `spinner.tsx:17`).
18. Loading indicators appear only after 500 ms and then persist at least 3 s (`startup-loading.tsx:42-46`).
19. Toasts: one at a time, top-right, panel bg with variant-colored edge, plain text, 5 s auto-dismiss (`toast.tsx:20-67`).
20. Render "thinking"/secondary AI output at ~60% opacity of its normal colors instead of a different palette (`theme/index.ts:292,560-584`).
21. Empty states are one muted sentence with normal row padding; the home empty state is just a centered logo + composer with a rotating example placeholder (`home.tsx:70-93`, `prompt/index.tsx:1310-1319`).
