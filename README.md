# Wiki

A local Markdown note vault with a Python API and a React web app.

## Project Layout

```text
backend/     FastAPI app that reads and writes Markdown notes
frontend/    Vite + React note browser and editor
vault/       Plain Markdown files, safe to edit directly
```

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements.txt

cd frontend
npm install
```

## Run

Start the backend:

```bash
. .venv/bin/activate
uvicorn backend.app.main:app --reload --port 8011
```

Start the frontend in another terminal:

```bash
cd frontend
npm run dev
```

Open `http://localhost:5173`.

If you run the backend on another port, start the frontend with:

```bash
WIKI_API_TARGET=http://127.0.0.1:8011 npm run dev
```

## Agent Fleet Leader Keys

The agent monitor supports a tmux-style leader on `Ctrl+A`. The prefix only arms when focus is not inside an `input`, `textarea`, or `contenteditable` surface, times out after about 1.5 seconds, and shows a temporary `C-a` chip while armed.

| Chord | Action |
| --- | --- |
| `C-a j` / `C-a k` | Cycle pane focus inside the current tmux window |
| `C-a h` / `C-a l` | Previous / next tmux window |
| `C-a 0-9` | Jump to tmux window slot `0-9` |
| `C-a w` | Open the run / open-note chooser and move the focused pane slot |
| `C-a x` | Close the focused pane (`agent` panes solo out unless already solo) |
| `C-a z` | Zoom/unzoom the focused pane |
| `C-a ,` | Open settings |

## Native macOS App

The native app is a Tauri 2 shell around the existing built frontend plus a frozen Python sidecar. The web app remains the source of truth: `make dev`, `frontend/src/api.ts`, and the Vite proxy flow stay unchanged.

### Prerequisites

- Rust with the `cargo tauri` subcommand available on `PATH`
- Node dependencies installed under `frontend/`
- A build-only Python venv at `.venv/`

Build-only setup:

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements-native.txt

cd frontend
npm install
```

This implementation uses the existing `cargo tauri` command on the machine. An extra `@tauri-apps/cli` npm dependency is not required.

### Native Commands

```bash
make native-backend   # build frontend + freeze wiki-backend sidecar
make native-smoke     # run the built sidecar directly and curl /health + /
make native-dev       # launch the Tauri shell with the packaged sidecar
make native-build     # build the signed .app bundle (no DMG)
```

`make native-dev` and `make native-build` both depend on `make native-backend`, so the sidecar binary is rebuilt first.

### Native Dev Modes

Web dev remains unchanged:

```bash
make dev
```

Manual-backend native mode is still useful for route-targeted WKWebView testing:

```bash
WIKI_REPO_DIR=$PWD \
WIKI_VAULT_DIR=$PWD/vault \
WIKI_FRONTEND_DIST=$PWD/frontend/dist \
.venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port 8811

WIKI_NATIVE_BACKEND_URL='http://127.0.0.1:8811/#/graph' cargo tauri dev --no-watch
```

Packaged sidecar mode needs no manual uvicorn process:

```bash
make native-dev
```

### Runtime Notes

- The Tauri app picks a dynamic `127.0.0.1` port for the packaged backend.
- The sidecar sets a Finder-safe `PATH` so backend subprocesses can find `git` and `tmux`.
- Sidecar logs are written to `~/Library/Logs/Wiki/`.
- The frozen backend respects `WIKI_REPO_DIR` and `WIKI_VAULT_DIR` so it targets the real vault instead of the PyInstaller extraction directory.

### Build Output

`make native-build` produces the macOS bundle at:

```text
src-tauri/target/release/bundle/macos/Wiki.app
```

The target intentionally uses `cargo tauri build --bundles app`, so it produces the validated `.app` bundle without also trying to build a DMG.

Open it with:

```bash
open src-tauri/target/release/bundle/macos/Wiki.app
```

### Gatekeeper and Signing

- The app is ad-hoc signed with `signingIdentity: "-"`.
- It is not notarized and is not App Store sandboxed.
- A locally built app should launch with `open .../Wiki.app`; a transferred or quarantined build may still require right-click Open or approval in Privacy & Security.

### Troubleshooting

- If the packaged backend fails to start, inspect `~/Library/Logs/Wiki/*.log`.
- If `git` or `tmux` work in Terminal but not from Finder, verify the launched app is using the bundled sidecar and not a stale manual backend URL.
- If PyInstaller misses imports or dylibs, rebuild with `./scripts/build-native-backend.sh` and inspect `build/wiki-backend/warn-*.txt`.
- If a sidecar process appears stuck, quit the app first and then check `pgrep -fl wiki-backend`; the native shell should clean it up on exit.

## API

- `GET /api/notes` lists notes in `vault/`
- `GET /api/notes/{path}` returns one note
- `POST /api/notes` creates a note
- `PUT /api/notes/{path}` updates a note

Set `WIKI_VAULT_DIR=/path/to/vault` to point the API at a different note directory.
