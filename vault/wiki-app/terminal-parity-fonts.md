---
type: decision
tags: [wiki-app, fonts, terminal-parity]
created: 2026-08-18
updated: 2026-08-18
---

# Terminal.app font-rendering parity

Wiki.app renders text via WKWebView (Tauri). Terminal.app draws with Core Text directly. Same font file rasterizes differently: sub-pixel positioning, hinting, and AA differ. This note tracks a layered approach to close the gap.

## Root causes (2026-08-18 investigation)

1. **Global grayscale AA override.** `frontend/src/styles.css:240-241` forces `-webkit-font-smoothing: antialiased` + `-moz-osx-font-smoothing: grayscale`. Terminal.app uses macOS default (subpixel-hinted). Grayscale renders visibly thinner.
2. **Font resolution gap.** `frontend/src/settings.tsx:30-68` (MONO_FONTS) only lists `"JetBrains Mono"`. Henry's installed variants are `JetBrainsMonoNL Nerd Font Mono` / `Propo` (Nerd Font patched, not vanilla NL). If the picker persists the label `JetBrains Mono NL` and no installed family matches that exact string, WKWebView silently falls through to the `ui-monospace, SFMono-Regular, "SF Mono", ...` tail — user is looking at SF Mono, not any NL variant. No JetBrains Mono .ttf is bundled.
3. **GPU-composited layers under text.** `styles.css` has `backdrop-filter: blur(...)` at :1568/:2623 and many `transform:` / `opacity < 1` / `filter: blur(4px)` sites. Text descendants get rasterized on separate compositor layers with different sub-pixel positioning than the primary layer.
4. **WKWebView vs Core Text pipeline.** Structural: DOM text → Core Graphics/Core Animation compositing ≠ Terminal.app's direct Core Text draw. Different hinting, different subpixel positioning, different baseline snap.

## Layered fix plan

Each layer closes a chunk of the visible gap. Take them in order; measure after each.

### Layer 1 — WIKI-340 CSS-only Terminal AA parity (P2, small)

Ship as one PR. All CSS toggles on code/mono surfaces (`code`, `pre`, `.font-monospace`, terminal panes). Leave prose alone.

- Remove `-webkit-font-smoothing: antialiased` + `-moz-osx-font-smoothing: grayscale` (line 240-241 in styles.css). The main lever.
- `font-synthesis: none` — kill browser-fabricated bold/italic.
- `font-variant-ligatures: none` — Terminal doesn't ligature.
- `font-kerning: none` — monospace terminals don't kern.
- `font-feature-settings: normal` — no calt/liga/opsz surprises.
- `font-optical-sizing: none` — kill size-driven glyph swaps in variable fonts.
- `text-rendering: geometricPrecision` — closer to Core Text's straight raster than default `optimizeLegibility`.

Verify with a side-by-side screenshot: same JetBrains Mono NL string in Wiki.app and Terminal.app. If the family didn't resolve, Layer 2 is the next step.

### Layer 2 — WIKI-341 bundle real JetBrains Mono NL + picker fixes (P2, medium)

- Bundle `JetBrainsMonoNL-Regular.ttf` (and needed weights) into `frontend/public/fonts/`. Load via `@font-face { font-family: "JetBrains Mono NL"; src: url(...) format("truetype"); font-display: block; }`. TTF preserves the hinting Terminal.app draws from; @fontsource woff2 subsets don't.
- Add curated entries to `MONO_FONTS` for `JetBrainsMonoNL Nerd Font Mono` / `Propo` so the picker labels match Henry's real installed family names.
- Optional: rename picker label "JetBrains Mono NL" to a stack that lists both the bundled family and the installed Nerd Font family in order.

### Layer 3 — WIKI-342 pixel discipline (P2, medium)

- Integer line-heights on all mono/code surfaces (`line-height: <integer>px`, not `1.5`).
- Kill compositor layers on text ancestors: audit `styles.css` for `transform`, `filter`, `backdrop-filter`, `opacity < 1`, `will-change: transform`, `contain: paint` above code blocks; remove or scope tighter.
- Integer `padding`/`margin` on code containers (no fractional px).
- Verify `body { zoom: 1 }`, no dynamic scale factor.

### Layer 4 — WIKI-343 native Core Text render path (P3, large)

Only path to strict pixel identity. Two options:

- Render code blocks via `<canvas>` measured with `CanvasRenderingContext2D.font` + Core Text (WKWebView exposes CT via canvas text APIs). Bypass DOM text.
- Embed a native NSView from Tauri and draw with TextKit / Core Text. Real bridge work.

Only worth it if layers 1-3 leave a visible gap Henry cares about.

## Expected outcome after each layer

- Layer 1 alone: close to Terminal.app for most surfaces. Grayscale removal is the biggest single delta.
- Layer 1 + 2: font actually resolves to the intended file; rendering identical to Terminal.app apart from WKWebView's compositor.
- Layer 1 + 2 + 3: difference visible only in A/B side by side.
- Layer 4: indistinguishable.

## References

- Investigation session 2026-08-18: `system_profiler SPFontsDataType | grep -iE 'jetbrains|nl'` confirmed installed family names.
- `frontend/src/styles.css:240-241` — the AA overrides to remove.
- `frontend/src/settings.tsx:30-68` — MONO_FONTS curated list.
- `frontend/src/main.tsx:3-4` — @fontsource imports (Inter + Fira Code only; no JetBrains Mono).
- `frontend/public/fonts/` — only Consolas for Powerline + Moxy Static bundled today.
