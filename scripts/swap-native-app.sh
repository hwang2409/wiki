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

# The swap transaction imports the agent runtime (pydantic, PIL, ...),
# so it must run under the repo venv, not the system interpreter.
python_bin="python3"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  python_bin="$ROOT/.venv/bin/python"
fi

"$python_bin" "$ROOT/scripts/native_swap_transaction.py" \
  "$stage_root" \
  --runtime-dir "$runtime_dir" \
  --repo-root "$ROOT" \
  "${allow_missing_args[@]}"
