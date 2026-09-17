---
type: reference
tags: [design]
created: 2026-09-10
updated: 2026-09-10
---

# Zeta GUI: Wiki run-UI parity design handoff

Extracted 2026-09-10 by the zeta orch (Explore agent over ~/me/fun/wiki/frontend) for the ZETA-106+ arc: make the zeta GPUI GUI look like the wiki AGENT RUN UI (Henry directive, scoped same day: run surfaces only, not the whole app). Zeta-specific adaptations at the end override the source where they conflict.

## Zeta adaptations (override the extraction)

- THINKING CHROME IS HEADER-ONLY until server protocol work lands (orch decision, ZETA-107 round 4, 2026-09-10): zeta `Thinking` blocks mix raw reasoning with summaries (`providers/codex.py` Thinking assembly; Anthropic raw thinking stored the same way), so no display-safe body source exists. The GUI renders a generic collapsed "+ Thought" header and never renders Thinking content. The wiki's title/duration/expanded-body design is deferred (bullet in zeta docs/design.md deferred section).

- ALL-MONO stays (Henry 2026-09-10): zeta keeps JetBrains Mono for every surface INCLUDING assistant prose. The wiki's Inter-for-prose split does NOT carry over. Adopt the wiki's prose line-height (1.65) for assistant turns in mono.
- Default palette target: the `opencode` theme values (the shipped wiki default). Mono-light/mono-dark ramps are reference only.
- Radii: zero/1-2px everywhere, flat elevation by tint step, no shadows — matches wiki exactly.
- gpui-kit is the theming layer; map wiki CSS tokens onto Kit semantic theme slots rather than hardcoding hex in components.

## Extraction report (agent run surfaces)

### 0. Where everything lives

| File | Role |
|---|---|
| `~/me/fun/wiki/frontend/src/styles.css` | Whole app stylesheet, 18,623 lines. Base token block at :100-247. |
| `~/me/fun/wiki/frontend/src/themes.css` | 17 theme palettes. `[data-theme="opencode"]` at :125 is the shipped default. |
| `~/me/fun/wiki/frontend/src/themes.ts` | `DEFAULT_THEME = "opencode"` (:44). |
| `~/me/fun/wiki/frontend/src/settings.tsx` | Font tokens written inline on `<html>` at boot (:407-441). Defaults: chrome Inter Variable, mono Fira Code, size 15px. |
| `~/me/fun/wiki/frontend/src/session-layout.ts:3` | `VIRTUAL_ROW_GAP = 14` — transcript row rhythm. |
| `styles.css:17170-17530` | `bb-*` primitives: Button, IconButton, Chip, DetailCard. |

Run-surface CSS clusters in styles.css: agents list 5230-5850, session state strip 5851-6400, transcript 7681-8600, sidebar agent rows 8758-9000, composer 9103-9430, full-screen session 9960-10000, status badges 12166-12460, transcript preview 12910-13010, run header 15536-15800, opencode chrome overrides 16512-16920 (this block wins — read it last).

### 1. Color palette (opencode, dark, all literal hex)

| Token | Value | Used for |
|---|---|---|
| background-primary | `#1e1e17` | app canvas, transcript bg |
| background-panel | `#24241b` | sidebar, tool-output interiors |
| background-element | `#2c2c21` | user turn panels, composer strip, cards, chips |
| border | `#35352a` | all hairlines |
| hover | `rgba(236,233,216,0.06)` | row hover |
| active | `rgba(177,139,244,0.14)` | pressed |
| text-normal | `#ece9d8` | warm off-white primary text |
| text-muted | `#a19e88` | secondary/metadata |
| text-faint | `#716f5e` | timestamps, hints, labels |
| accent (single) | `#b18bf4` violet | the ONE accent |
| success | `#a9c957` | |
| warning | `#d5d878` | |
| danger | `#e2685c` | |
| selection | `rgba(236,233,216,0.16)` | |
| overlay-strong | `rgba(0,0,0,0.45)` | modal backdrop |

Derived: border-subtle ~`#2f2f25`; border-active ~`#706f62`; canvas-bg ~`#26261d`; sidebar ~`#22221b`; surface-selected `rgba(177,139,244,0.16)`; ring = accent.
Diff: add bg = success 16% tint, remove bg = danger 14% tint, remove fg `#e2685c`.
Syntax: text `#ece9d8`, comment `#716f5e`, keyword `#b18bf4`, function `#d5d878`, string `#a9c957`, number `#e29a5c`, type `#7fc9b8`.
Run status: working = accent 14% tint bg + accent border/text; merge-ready = solid `#a9c957` bg + `#1e1e17` text; blocked = transparent bg + `#e2685c` border/text.

### 2. Typography

- ONE size drives everything: 15px (user-adjustable 11-22). Every role token aliases it.
- Line height: fixed 22px rhythm (22/15, snapped to whole px). Exception: assistant prose 1.65; thinking body 1.6.
- Weights: 400 / 500 / 600 only. 600 = tickets, verbs, section labels, selected rows.
- Wiki mono usage: sidebar agent rows, user turns, timestamps, tool rows/bodies, composer, footer, badges, pills. Wiki prose = Inter. ZETA OVERRIDE: everything mono.
- Letter spacing: uppercase labels get 0.04-0.14em tracking (section labels 0.14em); body text none. Status badges/state words forced lowercase.
- tabular-nums on ages, badges, chips, output-peek meta.

### 3. Spacing and shape

- Radii: opencode zeroes ALL radii; run surfaces independently pin 1-2px. Effectively square everywhere.
- Borders: 1px solid `#35352a` everywhere. Exceptions: badges/agent-state 1.5px; left accent rails 2-3px; blocked = 1.5px dashed; pending/queued = 1px dashed.
- Padding/gap scale: 1,2,3,4,6,7,8,10,12,14,16,20,24. Rows 5px 8px; chips 1px 6px / 3px 9px; panels 8px 10px - 12px 14px; transcript column 16px 16px 12px.
- Shadows: NONE in run chrome (opencode sets box-shadow: none on modals). Elevation = one tint step (`#1e1e17` -> `#24241b` -> `#2c2c21`), 4 levels total.
- Motion: single pair — 120ms cubic-bezier(0.2,0,0,1); transitions limited to background-color/color; press = scale(0.96).
- Focus: outline 2px ring (accent) offset 1px; composer suppresses ring, signals focus via left-rail brighten.

### 4. Component shapes

SIDEBAR: 216px fixed; bg `#24241b`; border-right 1px subtle. Agent rows: min-height 40px, padding 5px 8px, mono; ticket 600; meta faint, state-colored (blocked=danger, merge-ready=success at 500); age right-aligned tabular faint. Hover = flat `#2c2c21` fill. CURRENT item = NO fill: transparent bg, accent text at 600, plus a small dot in the gutter (left 4px, 0.58em). Keyboard cursor = solid accent fill. Workers nest via margin-left 40px, no guide rail; nested rows 32px. Attention rail: 2px accent (danger when failed) pinned left.

RUN HEADER: two stacked bands. Band 1: min-height 44px, padding 7px 14px — ticket (mono 600) + state pill + step/blocker. Band 2: min-height 40px — runtime metadata, separated by 1x14px vertical rules, margin 0 10px. State pill: padding 3px 9px, near-square, mono 600 lowercase 0.03em; positive=solid success fill+canvas text, negative=solid danger, neutral=solid accent. Blocker row: border-left 2px danger + danger 10% tint bg.

TRANSCRIPT: 1024px readable column centered, padding 16px 16px 12px; 14px row gap (0 between adjacent tool rows). User turn: flat rectangle, padding 8px 12px, border-left 3px accent, bg `#2c2c21`, mono. Assistant turn: NAKED — padding 2px 0, no bg/border, line-height 1.65. Tool row: one flat line, min-height 20px, mono; state by COLOR ONLY (running=normal, done=muted, failed=danger); verb 600; detail opacity 0.78. Tool body: indent rail — margin 3px 0 5px, padding 2px 0 2px 8px, border-left 1px (danger on error), bg `#24241b`. Output peek collapsed: chevron/preview/size + hover-fade "show output" hint. Thinking: header `+ Thought: title · 463ms`; expanded header opacity 0.6; body indent 2ch, no chrome. Running indicator: 7px circle pulsing opacity 0.25-1 over 1.2s. Code fences: padding 12px 16px, 1px border, soft-wrap, NO horizontal scroll. Footer strip: mono faint, mode word in accent 500, keybind hints right-aligned.

COMPOSER: grid minmax(0,1fr) auto x auto minmax(40px,auto); min-height 64px; padding 8px 10px; bg `#2c2c21`; border-left 3px accent-at-62%; square; no other borders. Focus promotes rail to border-active + fill lightens 6%; NO focus ring. Textarea transparent borderless mono, min-height 44px, caret accent. Target line above input: mono muted with target name accent 600. SEND: 40px tall, min-width 82, mono 600, square; enabled = INVERTED (text-normal bg, background-primary text), hover fades to muted; disabled = outline, opacity 0.55. Queued strip 1px dashed; pending user turns dashed -> solid on send, danger on fail.

BADGES/CHIPS: status-badge padding 2px 8px, 1.5px border, mono 500 lowercase tabular; pattern = outline (accent 45% mix) + 10% tint fill + accent text; only merge-ready/completed invert to solid fill; blocked dashed; abandoned/closed line-through. Chip: 20px tall, padding 0 8px, surface-raised bg, 11px 500. Branch pill: 1px border, 4% wash, mono 500, max-width 220px.

MODALS: flat panel on scrim — border 0, radius 0, bg `#24241b`, NO shadow; top 25vh; width min(480px, 100vw-32px); padding 12px 16px 14px. Title 15px 600 with plain-text `esc` at right. Inputs: no box, border-bottom 1px only, focus promotes underline. Buttons: min-height 30, borderless, menu bg, muted 500; confirm = solid accent.

SCROLLBARS: thin, 8px, thumb = text-normal 20% mix on transparent, hover 40%, no arrows. Composer hides its scrollbar.

### 5. Overall character

Terminal-first (OpenCode TUI reference). Square rectangles, never rounded cards. Compact but breathing: 28-40px rows, small padding, airy transcript (22px rhythm, 14px gaps, 1024px column, no h-scroll). Low-mid warm contrast: `#ece9d8` on `#1e1e17`. Color scarce and load-bearing: ONE violet accent carries current-item, composer rail, mode word, target name, selection, attention rail; semantic colors only as 10-16% tints + colored text + 45%-mixed borders.

Hierarchy tools, in order: (1) LEFT RAILS 2-3px — the structural device ("semantic left rails replace framed chrome"); (2) text color tier normal->muted->faint; (3) weight 400/500/600; (4) indentation (workers 40px, tool bodies 8px, thinking 2ch); (5) uppercase+tracking for labels, lowercase for state words. Size is NOT a hierarchy tool — one size for the whole app by design.

