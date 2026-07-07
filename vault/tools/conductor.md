---
type: reference
tags: [tools, apps]
created: 2026-07-07
updated: 2026-07-07
---

# Conductor

Conductor desktop is not Electron; verified 2026-07-07 as a Tauri macOS app with Rust backend/native WebKit renderer and mostly TypeScript app code.

## Stack

- Desktop shell: Tauri 2.x on macOS, using the native Safari/WebKit renderer.
- Backend/shell code: Rust. Local binary strings include `tauri-plugin-http/2.4.3`, `tauri-plugin-updater/2.9.0`, `tauri_plugin_store`, `generated tauri context creation`, and `__TAURI_INTERNALS__`.
- App/frontend: mostly TypeScript. Charlie Holtz says the desktop app is roughly 90-95% TypeScript.
- Web/auth app: small Elixir/Phoenix app.
- Local state/tools: Conductor stores data under `~/Library/Application Support/com.conductor.app`; installed app 0.55.1 includes helper binaries under `/Applications/Conductor.app/Contents/Resources/bin`.

## Evidence

- Installed bundle `/Applications/Conductor.app` v0.55.1 has no `Electron Framework.framework`, `.asar`, or Node app package. `otool -L /Applications/Conductor.app/Contents/MacOS/conductor` links `WebKit.framework`, `AppKit.framework`, and related macOS system frameworks.
- Public source: Charlie Holtz thread says Conductor is built in Tauri with a Rust backend and native Mac renderer: https://threadreaderapp.com/thread/1945870105109246401.html
- Public source: June 2026 transcript says: Tauri app, native Safari renderer, Rust backend, mostly TypeScript desktop app, Elixir/Phoenix web app: https://you-tldr.com/transcript/fQmlML9Lay4
