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

## API

- `GET /api/notes` lists notes in `vault/`
- `GET /api/notes/{path}` returns one note
- `POST /api/notes` creates a note
- `PUT /api/notes/{path}` updates a note

Set `WIKI_VAULT_DIR=/path/to/vault` to point the API at a different note directory.
