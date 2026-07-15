#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_dir="${WIKI_AGENT_RUNTIME_DIR:-${HOME}/.wiki/agent-runtime}"
force_stage_only="${FORCE_STAGE_ONLY:-0}"
guard_args=()
if [[ "${ALLOW_MISSING_APP_LOCK:-${WIKI_NATIVE_ALLOW_MISSING_APP_LOCK:-}}" == 1 ]]; then
  guard_args+=(--allow-missing-app-lock)
fi

if ! python3 "$ROOT/scripts/native_build_guard.py" --runtime-dir "$runtime_dir" "${guard_args[@]}"; then
  if [[ "$force_stage_only" != 1 ]]; then
    exit 1
  fi
  echo "live Wiki supervisor detected; FORCE_STAGE_ONLY=1 will build without swapping" >&2
fi

stage_parent="${WIKI_NATIVE_STAGE_PARENT:-$ROOT/.native-build-staging}"
mkdir -p "$stage_parent"
shopt -s nullglob
for completed_stage in "$stage_parent"/*/.swap-complete; do
  rm -rf "$(dirname "$completed_stage")"
done
shopt -u nullglob
if [[ -n "${WIKI_NATIVE_STAGE_ROOT:-}" ]]; then
  stage_root="$(cd "$(dirname "$WIKI_NATIVE_STAGE_ROOT")" && pwd)/$(basename "$WIKI_NATIVE_STAGE_ROOT")"
else
  stage_root="$stage_parent/$(date +%Y%m%d-%H%M%S)-$$"
fi
if [[ -e "$stage_root" ]]; then
  echo "staging directory already exists: $stage_root" >&2
  exit 1
fi

preserve_stage=0
cleanup() {
  if [[ "$preserve_stage" == 1 ]]; then
    echo "staged build preserved at $stage_root" >&2
  else
    rm -rf "$stage_root"
  fi
}
trap cleanup EXIT

mkdir -p "$stage_root"
frontend_dist="$stage_root/frontend-dist"
pyinstaller_dist="$stage_root/dist"
pyinstaller_work="$stage_root/build"
backend_output_dir="$stage_root/backend-binaries"

env \
  WIKI_FRONTEND_DIST="$frontend_dist" \
  WIKI_PYINSTALLER_DIST="$pyinstaller_dist" \
  WIKI_PYINSTALLER_WORK="$pyinstaller_work" \
  WIKI_NATIVE_BACKEND_OUTPUT_DIR="$backend_output_dir" \
  "$ROOT/scripts/build-native-backend.sh"

mkdir -p "$stage_root/src-tauri"
if command -v rsync >/dev/null 2>&1; then
  rsync -a \
    --exclude '/target' \
    --exclude '/binaries' \
    --exclude '/.native-build-staging' \
    "$ROOT/src-tauri/" "$stage_root/src-tauri/"
else
  cp -R "$ROOT/src-tauri/." "$stage_root/src-tauri/"
  rm -rf \
    "$stage_root/src-tauri/target" \
    "$stage_root/src-tauri/binaries" \
    "$stage_root/src-tauri/.native-build-staging"
fi
mkdir -p "$stage_root/src-tauri/binaries"
cp "$backend_output_dir"/* "$stage_root/src-tauri/binaries/"

python3 - "$stage_root/src-tauri/tauri.conf.json" <<'PY'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
config = json.loads(config_path.read_text(encoding="utf-8"))
config["build"]["frontendDist"] = "../frontend-dist"
config["bundle"]["externalBin"] = ["binaries/wiki-backend"]
config["bundle"]["resources"] = {
    "../dist/wiki-backend-sidecar/": "wiki-backend-sidecar/"
}
config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
PY

(
  cd "$stage_root/src-tauri"
  CARGO_TARGET_DIR="${WIKI_NATIVE_CARGO_TARGET_DIR:-$ROOT/.native-cargo-target}" \
    cargo tauri build --bundles app
)

shared_bundle="${WIKI_NATIVE_CARGO_TARGET_DIR:-$ROOT/.native-cargo-target}/release/bundle/macos/Wiki.app"
staged_bundle="$stage_root/target/release/bundle/macos/Wiki.app"
if [[ ! -d "$shared_bundle" ]]; then
  echo "Tauri build did not produce $shared_bundle" >&2
  exit 1
fi
mkdir -p "$(dirname "$staged_bundle")"
cp -R "$shared_bundle" "$staged_bundle"

if [[ "$force_stage_only" == 1 ]]; then
  preserve_stage=1
  echo "staged native build at $stage_root"
  echo "swap later with: ./scripts/swap-native-app.sh $stage_root"
  exit 0
fi

if ! "$ROOT/scripts/swap-native-app.sh" "$stage_root"; then
  preserve_stage=1
  echo "native build was staged but not swapped; quit Wiki.app and run:" >&2
  echo "./scripts/swap-native-app.sh $stage_root" >&2
  exit 1
fi
