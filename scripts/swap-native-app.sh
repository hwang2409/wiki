#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ $# -ne 1 ]]; then
  echo "usage: $0 STAGE_ROOT" >&2
  exit 2
fi

stage_root="$(cd "$1" && pwd)"
runtime_dir="${WIKI_AGENT_RUNTIME_DIR:-${HOME}/.wiki/agent-runtime}"
allow_missing_args=()
if [[ "${ALLOW_MISSING_APP_LOCK:-${WIKI_NATIVE_ALLOW_MISSING_APP_LOCK:-}}" == 1 ]]; then
  allow_missing_args+=(--allow-missing-app-lock)
fi

staged_bundle="$stage_root/target/release/bundle/macos/Wiki.app"
live_bundle="$ROOT/src-tauri/target/release/bundle/macos/Wiki.app"
if [[ ! -d "$staged_bundle" ]]; then
  echo "missing staged Wiki.app at $staged_bundle" >&2
  exit 1
fi

python3 "$ROOT/scripts/atomic_swap.py" \
  "$staged_bundle" \
  "$live_bundle" \
  --runtime-dir "$runtime_dir" \
  "${allow_missing_args[@]}"

touch "$stage_root/.swap-complete"
rm -rf "$stage_root"

echo "swapped staged Wiki.app into $live_bundle"
