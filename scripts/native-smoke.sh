#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

bundle_binary="src-tauri/target/release/bundle/macos/Wiki.app/Contents/MacOS/wiki-backend"
if [[ ! -x "$bundle_binary" ]]; then
  echo "missing bundled backend $bundle_binary; run make native-build first" >&2
  exit 1
fi
binary="$bundle_binary"

repo_dir="$ROOT"
case "$repo_dir" in
  */.codex/worktrees/*)
    repo_dir="${repo_dir%%/.codex/worktrees/*}"
    ;;
esac
vault_dir="${repo_dir}/vault"
port="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"

cleanup() {
  if [[ -n "${pid:-}" ]]; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

"$binary" \
  --host 127.0.0.1 \
  --port "$port" \
  --repo-dir "$repo_dir" \
  --vault-dir "$vault_dir" &
pid=$!

for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${port}/health" >/dev/null; then
    break
  fi
  sleep 0.25
done

curl -fsS "http://127.0.0.1:${port}/health"
printf '\n---\n'
curl -fsS "http://127.0.0.1:${port}/" | head -n 5
