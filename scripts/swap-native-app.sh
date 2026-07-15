#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ $# -ne 1 ]]; then
  echo "usage: $0 STAGE_ROOT" >&2
  exit 2
fi

stage_root="$(cd "$1" && pwd)"
runtime_dir="${WIKI_AGENT_RUNTIME_DIR:-${HOME}/.wiki/agent-runtime}"
python3 "$ROOT/scripts/native_build_guard.py" --runtime-dir "$runtime_dir"

staged_bundle="$stage_root/target/release/bundle/macos/Wiki.app"
live_bundle="$ROOT/src-tauri/target/release/bundle/macos/Wiki.app"
if [[ ! -d "$staged_bundle" ]]; then
  echo "missing staged Wiki.app at $staged_bundle" >&2
  exit 1
fi

python3 "$ROOT/scripts/atomic_swap.py" "$staged_bundle" "$live_bundle"

echo "swapped staged Wiki.app into $live_bundle"
