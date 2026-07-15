#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/python ]]; then
  echo "missing .venv; create it first with python3 -m venv .venv" >&2
  exit 1
fi

.venv/bin/pip install -r backend/requirements-native.txt

frontend_dist="${WIKI_FRONTEND_DIST:-$ROOT/frontend/dist}"
(
  cd frontend
  npm install
  npm run build -- --outDir "$frontend_dist"
)

target_triple="$(rustc --print host-tuple 2>/dev/null || rustc -Vv | awk '/host:/ {print $2}')"

pyinstaller_dist="${WIKI_PYINSTALLER_DIST:-$ROOT/dist}"
pyinstaller_work="${WIKI_PYINSTALLER_WORK:-$ROOT/build}"
backend_output_dir="${WIKI_NATIVE_BACKEND_OUTPUT_DIR:-$ROOT/src-tauri/binaries}"
.venv/bin/pyinstaller \
  --clean \
  --noconfirm \
  --distpath "$pyinstaller_dist" \
  --workpath "$pyinstaller_work" \
  packaging/wiki-backend.spec
mkdir -p "$backend_output_dir"
cp "$pyinstaller_dist/wiki-backend" \
  "$backend_output_dir/wiki-backend-${target_triple}"
chmod +x "$backend_output_dir/wiki-backend-${target_triple}"

echo "built $backend_output_dir/wiki-backend-${target_triple}"
