---
type: reference
tags: [wiki-app, macos, tauri]
created: 2026-07-07
updated: 2026-07-07
---

# Wiki Native macOS App Research

Researched 2026-07-07 by a codex research worker (WIKI-1, gpt-5.5 --search); gated by wiki-dev orchestrator. Question: port the wiki web app (FastAPI + React/Vite) to a native macOS app?

## Verdict

Porting the wiki app to a “real native” macOS app is viable only if “native” means a native shell around the existing web UI and Python backend. A rewrite to Swift or Rust is not justified: the hard part is not rendering notes, it is the backend’s privileged local integration with `~/.claude`, `~/.codex`, `/tmp/agent-*`, git, and `tmux capture-pane/send-keys`. Keep that Python surface intact.

Primary recommendation: Tauri 2.x shell, React/Vite production build in WKWebView, Python FastAPI packaged as a sidecar binary and spawned/stopped by the app. Runner-up: a very thin Swift/AppKit or SwiftUI app with `WKWebView` that starts the same packaged backend. Electron works and is easiest for Python spawning, but it is the least attractive for a personal always-on fleet monitor because it adds a full Chromium/Node runtime to an app whose UI is already lightweight.

## Comparable Products

| Product | Shipping stack | Source |
|---|---|---|
| Conductor | Tauri 2 / Rust shell / WebKit renderer; local bundle links AppKit+WebKit, no Electron | https://threadreaderapp.com/thread/1945870105109246401.html plus local `/Applications/Conductor.app` teardown |
| Cursor | VS Code fork, now moving more custom; local bundle is Electron | https://cursor.com/blog/cursor-3 |
| Windsurf / Devin Desktop | VS Code OSS-style desktop IDE with local agent harness; exact mac bundle not locally verified | https://docs.devin.ai/desktop/getting-started |
| Zed | Native Rust editor, GPU-oriented | https://zed.dev/ |
| Warp | Terminal built entirely in Rust | https://www.warp.dev/blog/how-warp-works |
| Obsidian | Electron desktop app; official changelog references Electron versions; local bundle has Electron Framework and ASARs | https://obsidian.md/changelog/ |
| Linear desktop | Same JS/React web app wrapped in Electron for notifications, dock badge, always-on presence | https://linear.app/changelog/2019-04-25-linear-desktop-app |
| Claude Desktop | Desktop app on macOS/Windows/Linux; local mac bundle is Electron | https://support.claude.com/en/articles/10065433-install-claude-desktop |
| Raycast | Native macOS app built with Swift/AppKit; extensions use React/TypeScript/Node but render native UI | https://www.raycast.com/blog/a-technical-deep-dive-into-the-new-raycast and https://www.raycast.com/blog/how-raycast-api-extensions-work |

## Recommended Architecture

Use Tauri as the app wrapper, not as a backend rewrite. Build the frontend with Vite (`npm run build`) and serve static assets through Tauri’s app protocol in production. Package the FastAPI app as a self-contained sidecar, probably via PyInstaller or a similar frozen-Python build, and run `uvicorn` bound to `127.0.0.1` on a chosen free port. The Tauri Rust side owns lifecycle: allocate port, spawn backend, wait on `/health`, load the WKWebView to the local URL, terminate the child on quit, and surface backend failure in a small native error window. In dev, keep the current `:8011` + `:5173` flow.

Tauri specifically fits because its sidecar model is designed for embedded external binaries and can spawn them with explicit permissions. It also keeps the UI close to Conductor’s architecture and avoids Electron’s memory floor. The important packaging choice is “bundled Python binary,” not “requires system Python”: Henry’s app should start from Finder without shell activation, pyenv state, or virtualenv drift. Rewriting the backend in Rust is a non-goal unless the Python API becomes stable enough to split into a smaller privileged daemon later.

Expected effort: 3-5 focused days for a usable personal app, 1-2 weeks for a polished one with updater, settings, login item, crash handling, and signed/notarized distribution. Top risks: PyInstaller packaging; WKWebView differences from Chromium for CSS, keyboard handling, drag/drop, SSE, and local URL/CORS behavior; lifecycle/security around a backend with intentionally unrestricted filesystem and `tmux` access.

## Option Evaluation

Tauri 2.x: best default. React/Vite is supported directly, and Tauri can bundle sidecars: https://v2.tauri.app/develop/sidecar/ and https://v2.tauri.app/start/frontend/vite/. Gotchas: production is not Vite dev server; asset paths/CSP/custom protocol need testing; WKWebView is Safari, so do not assume Chromium behavior.

Electron: lowest integration risk for web behavior and Python child processes. Node’s process APIs and Electron’s process model fit this: https://electronjs.org/docs/latest/tutorial/process-model. Cost: app size, memory, and a heavier always-on runtime.

Swift/WKWebView shell: smallest conceptual surface for a mac-only personal app: https://developer.apple.com/documentation/webkit/wkwebview. It can spawn the same packaged backend and use Apple APIs directly. Cost: hand-rolled lifecycle, menus, permissions, updates, bridge messaging, and WebView edge cases.

PWA/site-specific browser: useful baseline, not enough. Chrome install and Safari Add to Dock give a Dock icon, standalone window, notifications, badges, and login item behavior: https://support.google.com/chrome/answer/9658361 and https://support.apple.com/en-us/104996. They do not own backend startup, tmux access, sidecar packaging, or privileged local process lifecycle.

## Native Features Ranked

1. Native notifications for merge-ready/blocked workers: high value. Tauri notification plugin, Electron Notification, Swift UserNotifications, and Safari/Chrome web notifications all support this, but PWA only works while the web service already exists.
2. Menubar/tray fleet status: high value for an always-on monitor. Tauri tray and Electron Tray are straightforward; Swift `NSStatusItem` is native but manual; PWA cannot do this.
3. Launch at login: high value because the monitor should be ambient. Tauri autostart, Electron auto-launch/LoginItem helpers, and Swift `SMAppService` all work. Safari web apps can be login items, but again cannot launch the backend.
4. Dock badge counts: medium-high. Electron has `app.setBadgeCount`; Safari web apps support unread badges; Swift can set badges via notification APIs; Tauri may need macOS-specific plugin/Rust glue.
5. Global hotkey: medium. Tauri and Electron have global shortcut APIs; Swift can do it, with more native-code work; PWA cannot.
6. Multiple windows: medium. All wrappers support it; the current app already has split panes, so native multi-window is nice but not first-order.

## Non-goals

Do not pursue App Store sandboxing; unrestricted filesystem and subprocess access are core requirements. Do not rewrite the backend in Rust or Swift. Do not require system Python. Do not ship the dev servers as-is. Do not choose Electron just because Claude/Cursor/Linear use it; Henry’s app is personal, mac-only, and always-on, so Tauri or Swift has a better cost profile.
