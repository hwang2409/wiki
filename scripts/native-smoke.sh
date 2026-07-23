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

pyinstaller_sidecar="${WIKI_PYINSTALLER_DIST:-$ROOT/dist}/wiki-backend-sidecar"
if [[ -d "$pyinstaller_sidecar" ]]; then
  packaged_sidecar="$pyinstaller_sidecar"
else
  packaged_sidecar="$ROOT/src-tauri/target/release/bundle/macos/Wiki.app/Contents/Resources/wiki-backend-sidecar"
fi
packaged_binary="$packaged_sidecar/wiki-backend"
if [[ ! -x "$packaged_binary" ]]; then
  echo "missing PyInstaller sidecar $packaged_binary; run make native-backend or make native-build" >&2
  exit 1
fi

schema_dir="$packaged_sidecar/_internal/schemas"
expected_schemas=(
  edge.schema.json
  finding.schema.json
  steer.schema.json
  verdict.schema.json
  workgraph.schema.json
)
for schema in "${expected_schemas[@]}"; do
  if [[ ! -f "$schema_dir/$schema" ]]; then
    echo "missing packaged schema $schema_dir/$schema" >&2
    exit 1
  fi
done

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
  if [[ -n "${capture_pid:-}" ]]; then
    kill "$capture_pid" 2>/dev/null || true
    wait "$capture_pid" 2>/dev/null || true
  fi
  if [[ -n "${capture_dir:-}" ]]; then
    rm -rf "$capture_dir"
  fi
}
trap cleanup EXIT

capture_dir="$(mktemp -d "${TMPDIR:-/tmp}/wiki-native-smoke.XXXXXX")"
capture_port_file="$capture_dir/port"
capture_path_file="$capture_dir/path"
capture_body_file="$capture_dir/body"
capture_count_file="$capture_dir/count"
mcp_output_file="$capture_dir/mcp-output"

python3 - "$capture_port_file" "$capture_path_file" "$capture_body_file" "$capture_count_file" <<'PY' &
import http.server
import pathlib
import sys


port_path, request_path, request_body, request_count = map(pathlib.Path, sys.argv[1:])


class CaptureHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        count = int(request_count.read_text(encoding="utf-8")) if request_count.exists() else 0
        request_count.write_text(str(count + 1), encoding="utf-8")
        request_path.write_text(self.path, encoding="utf-8")
        request_body.write_bytes(self.rfile.read(length))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, _format, *_args):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
server.timeout = 0.25
port_path.write_text(str(server.server_port), encoding="utf-8")
for _ in range(8):
    server.handle_request()
server.server_close()
PY
capture_pid=$!

for _ in $(seq 1 60); do
  if [[ -s "$capture_port_file" ]]; then
    break
  fi
  if ! kill -0 "$capture_pid" 2>/dev/null; then
    echo "packaged MCP capture server exited before becoming ready" >&2
    exit 1
  fi
  sleep 0.1
done
if [[ ! -s "$capture_port_file" ]]; then
  echo "timed out waiting for packaged MCP capture server" >&2
  exit 1
fi

capture_port="$(<"$capture_port_file")"
set +e
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"steer_agent","arguments":{"id":"WIKI-162","message":"preserve this exact native steer body","mode":"on-idle","request_id":"native-steer-1","source":"mastermind"}}}' \
  | WIKI_AGENT_ROLE=orchestrator WIKI_BACKEND_URL="http://127.0.0.1:${capture_port}" \
    "$packaged_binary" --wiki-artifacts-mcp >"$mcp_output_file"
mcp_status=$?
set -e

for _ in $(seq 1 60); do
  if [[ -s "$capture_body_file" ]]; then
    break
  fi
  if ! kill -0 "$capture_pid" 2>/dev/null; then
    break
  fi
  sleep 0.1
done
if kill -0 "$capture_pid" 2>/dev/null; then
  echo "packaged MCP steer did not reach the capture backend" >&2
  exit 1
fi
wait "$capture_pid" 2>/dev/null || true
if [[ "$mcp_status" -ne 0 ]]; then
  echo "packaged MCP steer failed:" >&2
  cat "$mcp_output_file" >&2
  exit "$mcp_status"
fi

python3 - "$capture_path_file" "$capture_body_file" "$capture_count_file" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
payload = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
count = int(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))
expected_path = "/api/agents/WIKI-162/message"
expected_payload = {
    "text": "preserve this exact native steer body",
    "mode": "on-idle",
    "request_id": "native-steer-1",
    "source": "mastermind",
}
if path != expected_path:
    raise SystemExit(f"unexpected packaged MCP request path: {path!r}")
if payload != expected_payload:
    raise SystemExit(f"unexpected packaged MCP request body: {payload!r}")
if count != 1:
    raise SystemExit(f"expected one packaged MCP request, got {count}")
print("packaged MCP steer smoke passed: schemas present, graph_lint loaded, one unchanged backend request")
PY

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
