#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/python ]]; then
  echo "missing .venv; create it first with python3 -m venv .venv" >&2
  exit 1
fi

.venv/bin/pip install -r backend/requirements-native.txt

(
  cd frontend
  npm install
  npm run build
)

target_triple="$(rustc --print host-tuple 2>/dev/null || rustc -Vv | awk '/host:/ {print $2}')"

.venv/bin/pyinstaller --clean --noconfirm packaging/wiki-backend.spec
mkdir -p src-tauri/binaries
cp dist/wiki-backend "src-tauri/binaries/wiki-backend-${target_triple}"
chmod +x "src-tauri/binaries/wiki-backend-${target_triple}"

echo "built src-tauri/binaries/wiki-backend-${target_triple}"
