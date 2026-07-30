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
swap_intent="$stage_root/.swap-intent"
if [[ ! -d "$staged_bundle" && ! -f "$swap_intent" ]]; then
  echo "missing staged Wiki.app at $staged_bundle" >&2
  exit 1
fi

python3 "$ROOT/scripts/atomic_swap.py" \
  "$staged_bundle" \
  "$live_bundle" \
  --runtime-dir "$runtime_dir" \
  --success-sentinel "$stage_root/.swap-complete" \
  --swap-intent "$swap_intent" \
  "${allow_missing_args[@]}"

if ! python3 "$ROOT/scripts/native_daemon_restart.py" \
  "$live_bundle" \
  --runtime-dir "$runtime_dir" \
  --repo-root "$ROOT"; then
  echo "new daemon failed health or fingerprint verification; restoring old bundle" >&2
  if ! python3 "$ROOT/scripts/atomic_swap.py" \
    "$staged_bundle" \
    "$live_bundle" \
    --runtime-dir "$runtime_dir" \
    --success-sentinel "$stage_root/.swap-complete" \
    --swap-intent "$swap_intent" \
    --rollback \
    "${allow_missing_args[@]}"; then
    echo "cannot restore the old native bundle" >&2
    exit 1
  fi
  if ! python3 "$ROOT/scripts/native_daemon_restart.py" \
    "$live_bundle" \
    --runtime-dir "$runtime_dir" \
    --repo-root "$ROOT"; then
    echo "old daemon did not become healthy after bundle rollback; unloading it" >&2
    WIKI_APP_PATH="$live_bundle" WIKI_AGENT_RUNTIME_DIR="$runtime_dir" \
      python3 "$ROOT/wiki" daemon uninstall --json || true
    exit 1
  fi
  echo "restored old bundle and verified old daemon health" >&2
  exit 1
fi

rm -rf "$stage_root"

echo "swapped staged Wiki.app into $live_bundle"
