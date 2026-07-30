"""Wiki CLI regression tests."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID


REPO_ROOT = Path(__file__).resolve().parents[2]
WIKI_CLI = REPO_ROOT / "wiki"


class AgentUpdateSessionTests(unittest.TestCase):
    def _run(self, args: list[str], env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(WIKI_CLI), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )

    def test_session_id_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "agent-registry.json"
            worktree = Path(tmp) / "wt-15"
            worktree.mkdir()
            registry_path.write_text(json.dumps({
                "WIKI-15": {
                    "current": {
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "worktree": str(worktree),
                        "log": "/tmp/cdx-WIKI-15.log",
                        "session": 1,
                    }
                }
            }))
            env = {**os.environ, "WIKI_AGENT_REGISTRY_PATH": str(registry_path)}
            proc = self._run(
                ["agent", "update", "WIKI-15", "--session", "sess-abc-123"],
                env=env,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("session_id=sess-abc-123", proc.stdout)
            reloaded = json.loads(registry_path.read_text())
            self.assertEqual(
                reloaded["WIKI-15"]["current"]["session_id"], "sess-abc-123"
            )
            # Doesn't clobber the integer session-count field.
            self.assertEqual(reloaded["WIKI-15"]["current"]["session"], 1)

    def test_update_without_session_still_works(self) -> None:
        """Backwards compat: existing fields still round-trip; --session is opt-in."""
        with TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "agent-registry.json"
            registry_path.write_text(json.dumps({
                "WIKI-15": {
                    "current": {
                        "window": "@42",
                        "kind": "cdx",
                        "role": "implement",
                        "session": 1,
                    }
                }
            }))
            env = {**os.environ, "WIKI_AGENT_REGISTRY_PATH": str(registry_path)}
            proc = self._run(["agent", "update", "WIKI-15", "--window", "@99"], env=env)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            reloaded = json.loads(registry_path.read_text())
            self.assertEqual(reloaded["WIKI-15"]["current"]["window"], "@99")
            self.assertNotIn("session_id", reloaded["WIKI-15"]["current"])

    def test_orchestrator_registration_persists_model(self) -> None:
        with TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "agent-registry.json"
            env = {
                **os.environ,
                "WIKI_AGENT_REGISTRY_PATH": str(registry_path),
                "CLAUDE_CODE_SESSION_ID": "claude-session-1",
            }
            proc = self._run(["agent", "orch", "wiki", "--window", "@9", "--model", "sonnet"], env=env)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            reloaded = json.loads(registry_path.read_text())
            orch = reloaded["_orchestrators"]["wiki"]
            self.assertEqual(orch["window"], "@9")
            self.assertEqual(orch["model"], "sonnet")
            self.assertEqual(orch["session_id"], "claude-session-1")

    def test_update_rejects_supervisor_owned_run(self) -> None:
        with TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "agent-registry.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "WIKI-15": {
                            "current": {
                                "run_id": "00000000-0000-4000-8000-000000000015",
                                "window": None,
                                "kind": "cdx",
                                "role": "implement",
                            }
                        }
                    }
                )
            )
            env = {**os.environ, "WIKI_AGENT_REGISTRY_PATH": str(registry_path)}
            proc = self._run(["agent", "update", "WIKI-15", "--window", "@99"], env=env)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("supervisor-owned headless run WIKI-15", proc.stderr)
            reloaded = json.loads(registry_path.read_text())
            self.assertIsNone(reloaded["WIKI-15"]["current"]["window"])

    def test_register_rejects_supervisor_owned_run(self) -> None:
        with TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "agent-registry.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "WIKI-15": {
                            "current": {
                                "run_id": "00000000-0000-4000-8000-000000000015",
                                "window": None,
                                "kind": "cdx",
                                "role": "implement",
                            }
                        }
                    }
                )
            )
            env = {**os.environ, "WIKI_AGENT_REGISTRY_PATH": str(registry_path)}
            proc = self._run(
                [
                    "agent",
                    "register",
                    "WIKI-15",
                    "--window",
                    "@42",
                    "--kind",
                    "cdx",
                    "--role",
                    "implement",
                ],
                env=env,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("supervisor-owned headless run WIKI-15", proc.stderr)

    def test_orchestrator_registration_rejects_supervisor_owned_id(self) -> None:
        with TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "agent-registry.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "wiki": {
                            "current": {
                                "run_id": "00000000-0000-4000-8000-000000000099",
                                "window": None,
                                "kind": "cc",
                                "role": "orchestrator",
                            }
                        }
                    }
                )
            )
            env = {
                **os.environ,
                "WIKI_AGENT_REGISTRY_PATH": str(registry_path),
                "CLAUDE_CODE_SESSION_ID": "claude-session-1",
            }
            proc = self._run(["agent", "orch", "wiki", "--window", "@9"], env=env)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("supervisor-owned headless run wiki", proc.stderr)


class AgentDoneWindowCleanupTests(unittest.TestCase):
    def _run(self, args: list[str], env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(WIKI_CLI), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )

    def _write_registry(self, path: Path, ticket: str, window: str = "@12") -> None:
        path.write_text(
            json.dumps(
                {
                    ticket: {
                        "current": {
                            "window": window,
                            "kind": "cdx",
                            "role": "implement",
                            "log": f"/tmp/cdx-{ticket}.log",
                        },
                        "history": [],
                    }
                }
            ),
            encoding="utf-8",
        )

    def _write_archive(self, home: Path, ticket: str) -> Path:
        session_dir = home / "me" / "fun" / "agent-archive" / ticket / "20260708-183300"
        session_dir.mkdir(parents=True)
        meta_path = session_dir / "meta.json"
        meta_path.write_text("{}\n", encoding="utf-8")
        return meta_path

    def _install_tmux_stub(self, bin_dir: Path) -> Path:
        script = bin_dir / "tmux"
        script.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                if args == ["list-windows", "-a", "-F", "#{window_id}"]:
                    sys.stdout.write(os.environ.get("TMUX_WINDOWS", ""))
                    sys.exit(int(os.environ.get("TMUX_LIST_EXIT", "0")))
                if len(args) == 3 and args[:2] == ["kill-window", "-t"]:
                    log_path = os.environ.get("TMUX_KILL_LOG")
                    if log_path:
                        with Path(log_path).open("a", encoding="utf-8") as handle:
                            handle.write(args[2] + "\\n")
                    sys.exit(int(os.environ.get("TMUX_KILL_EXIT", "0")))
                sys.exit(97)
                """
            ),
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    def test_done_kills_exact_recorded_window(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            registry_path = tmp_path / "agent-registry.json"
            kill_log = tmp_path / "killed.log"
            self._write_registry(registry_path, "WIKI-26", window="@12")
            meta_path = self._write_archive(tmp_path, "WIKI-26")
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            self._install_tmux_stub(bin_dir)
            env = {
                **os.environ,
                "HOME": str(tmp_path),
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "TMUX_WINDOWS": "@12\n@999\n",
                "TMUX_KILL_LOG": str(kill_log),
                "WIKI_AGENT_REGISTRY_PATH": str(registry_path),
            }

            proc = self._run(["agent", "done", "WIKI-26", "--outcome", "merged"], env=env)

            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("deregistered WIKI-26 (outcome: merged)", proc.stdout)
            self.assertIn("killed window @12", proc.stdout)
            self.assertEqual(kill_log.read_text(encoding="utf-8"), "@12\n")
            self.assertEqual(json.loads(registry_path.read_text(encoding="utf-8")), {})
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["outcome"], "merged")
            self.assertEqual(meta["worker"]["window"], "@12")
            self.assertIn("ended_at", meta)

    def test_done_keep_window_skips_tmux_calls(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            registry_path = tmp_path / "agent-registry.json"
            kill_log = tmp_path / "killed.log"
            self._write_registry(registry_path, "WIKI-26", window="@12")
            self._write_archive(tmp_path, "WIKI-26")
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            self._install_tmux_stub(bin_dir)
            env = {
                **os.environ,
                "HOME": str(tmp_path),
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "TMUX_WINDOWS": "@12\n",
                "TMUX_KILL_LOG": str(kill_log),
                "WIKI_AGENT_REGISTRY_PATH": str(registry_path),
            }

            proc = self._run(
                ["agent", "done", "WIKI-26", "--outcome", "merged", "--keep-window"],
                env=env,
            )

            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("kept window @12", proc.stdout)
            self.assertFalse(kill_log.exists())

    def test_done_reports_window_already_gone(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            registry_path = tmp_path / "agent-registry.json"
            kill_log = tmp_path / "killed.log"
            self._write_registry(registry_path, "WIKI-26", window="@12")
            self._write_archive(tmp_path, "WIKI-26")
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            self._install_tmux_stub(bin_dir)
            env = {
                **os.environ,
                "HOME": str(tmp_path),
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "TMUX_WINDOWS": "@999\n",
                "TMUX_KILL_LOG": str(kill_log),
                "WIKI_AGENT_REGISTRY_PATH": str(registry_path),
            }

            proc = self._run(["agent", "done", "WIKI-26", "--outcome", "closed"], env=env)

            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("window already gone: @12", proc.stdout)
            self.assertFalse(kill_log.exists())

    def test_done_tolerates_tmux_absent(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            registry_path = tmp_path / "agent-registry.json"
            self._write_registry(registry_path, "WIKI-26", window="@12")
            self._write_archive(tmp_path, "WIKI-26")
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            env = {
                **os.environ,
                "HOME": str(tmp_path),
                "PATH": str(bin_dir),
                "WIKI_AGENT_REGISTRY_PATH": str(registry_path),
            }

            proc = self._run(["agent", "done", "WIKI-26", "--outcome", "abandoned"], env=env)

            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("window already gone: @12", proc.stdout)
            self.assertEqual(json.loads(registry_path.read_text(encoding="utf-8")), {})

    def test_done_rejects_supervisor_owned_run(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            registry_path = tmp_path / "agent-registry.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "WIKI-26": {
                            "current": {
                                "run_id": "00000000-0000-4000-8000-000000000026",
                                "window": None,
                                "kind": "cdx",
                                "role": "implement",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            env = {**os.environ, "WIKI_AGENT_REGISTRY_PATH": str(registry_path)}

            proc = self._run(["agent", "done", "WIKI-26", "--outcome", "merged"], env=env)

            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("supervisor-owned headless run WIKI-26", proc.stderr)
            self.assertIn("WIKI-26", json.loads(registry_path.read_text(encoding="utf-8")))


class _WatchApi:
    def __init__(self, responder):
        self.responder = responder
        self.requests: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
                owner.requests.append(self.path)
                status, payload = owner.responder(self.path)
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"


class _AgentControlApi:
    def __init__(self):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.current: dict[str, dict] = {}
        self.spawn_results: dict[str, dict] = {}
        self.provider_starts = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, status: int, payload: dict) -> None:
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
                owner.requests.append(("GET", self.path, None))
                if self.path == "/health":
                    self._reply(200, {"status": "ok"})
                    return
                if self.path == "/api/agents":
                    workers = [
                        {
                            "ticket": ticket,
                            "run_id": row["run_id"],
                            "runtime_state": row["state"],
                            "state": row["state"],
                            "role": row["role"],
                            "kind": row["kind"],
                        }
                        for ticket, row in sorted(owner.current.items())
                    ]
                    self._reply(200, {"workers": workers, "orchestrators": []})
                    return
                self._reply(404, {"detail": "not found"})

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                owner.requests.append(("POST", self.path, payload))
                if self.path == "/api/agents/spawn":
                    request_id = payload["request_id"]
                    result = owner.spawn_results.get(request_id)
                    if result is None:
                        owner.provider_starts += 1
                        result = {
                            "run_id": f"00000000-0000-4000-8000-{owner.provider_starts:012d}",
                            "window": None,
                        }
                        owner.spawn_results[request_id] = result
                        owner.current[payload["ticket"]] = {
                            "run_id": result["run_id"],
                            "state": "working",
                            "role": payload["role"],
                            "kind": payload["kind"],
                        }
                    self._reply(200, result)
                    return
                if self.path == "/api/agents/spawn-orchestrator":
                    request_id = payload["request_id"]
                    result = owner.spawn_results.get(request_id)
                    if result is None:
                        owner.provider_starts += 1
                        result = {
                            "run_id": f"00000000-0000-4000-8000-{owner.provider_starts:012d}",
                            "window": None,
                        }
                        owner.spawn_results[request_id] = result
                        owner.current[payload["id"]] = {
                            "run_id": result["run_id"],
                            "state": "working",
                            "role": "orchestrator",
                            "kind": payload["kind"],
                        }
                    self._reply(200, result)
                    return
                match = re.fullmatch(r"/api/agents/([^/]+)/(message|replace|archive)", self.path)
                if match:
                    agent_id, action = match.groups()
                    row = owner.current[agent_id]
                    if action == "message":
                        self._reply(200, {"status": "sent", "request_id": payload["request_id"]})
                    elif action == "replace":
                        row["run_id"] = "00000000-0000-4000-8000-000000000099"
                        self._reply(200, {"run_id": row["run_id"], "model": payload.get("model")})
                    else:
                        row["state"] = "completed"
                        self._reply(200, {"state": "completed", "outcome": payload["outcome"]})
                    return
                self._reply(404, {"detail": "not found"})

            def log_message(self, _format, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"


class AgentControlCliTests(unittest.TestCase):
    def _run(self, args: list[str], env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(WIKI_CLI), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )

    def test_spawn_status_steer_replace_archive_round_trip_and_idempotency(self) -> None:
        with TemporaryDirectory() as tmp, _AgentControlApi() as api:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text("Implement ticket WIKI-200", encoding="utf-8")
            worktree = root / "worktree"
            worktree.mkdir()
            env = {**os.environ, "WIKI_BACKEND_URL": api.url}
            spawn_args = [
                "agent",
                "spawn",
                "WIKI-200",
                "--kind",
                "cdx",
                "--role",
                "implement",
                "--model",
                "gpt-5.4",
                "--effort",
                "high",
                "--prompt-file",
                str(prompt),
                "--workdir",
                str(worktree),
                "--orch",
                "wiki",
                "--request-id",
                "cli-spawn-1",
            ]
            first = self._run(spawn_args, env)
            replay = self._run(spawn_args, env)
            status = self._run(["agent", "status", "WIKI-200"], env)
            steer = self._run(
                [
                    "agent",
                    "steer",
                    "WIKI-200",
                    "--message",
                    "Run the backend suite",
                    "--mode",
                    "on-idle",
                    "--request-id",
                    "cli-steer-1",
                ],
                env,
            )
            replace = self._run(
                ["agent", "replace", "WIKI-200", "--model", "gpt-5.5"],
                env,
            )
            archive = self._run(
                ["agent", "archive", "WIKI-200", "--outcome", "merged"],
                env,
            )

            for process in (first, replay, status, steer, replace, archive):
                self.assertEqual(process.returncode, 0, msg=process.stderr)
            self.assertEqual(json.loads(first.stdout), json.loads(replay.stdout))
            self.assertEqual(api.provider_starts, 1)
            self.assertEqual(json.loads(status.stdout)["run_id"], json.loads(first.stdout)["run_id"])
            self.assertEqual(json.loads(steer.stdout)["status"], "sent")
            self.assertEqual(json.loads(archive.stdout)["outcome"], "merged")

            # CLI steers are orchestrator-originated, not typed by Henry — they
            # must carry a synthetic source tag so the receiving session
            # renders them as system markers instead of user bubbles.
            message_requests = [
                payload
                for method, path, payload in api.requests
                if method == "POST"
                and path.endswith("/message")
                and payload.get("request_id") == "cli-steer-1"
            ]
            self.assertEqual(len(message_requests), 1)
            self.assertEqual(message_requests[0].get("source"), "supervisor-steer")

            spawn_requests = [
                payload
                for method, path, payload in api.requests
                if method == "POST" and path == "/api/agents/spawn"
            ]
            self.assertEqual(
                [payload["request_id"] for payload in spawn_requests],
                ["cli-spawn-1", "cli-spawn-1"],
            )

    def test_generated_request_id_and_discovery_file_find_live_backend(self) -> None:
        with TemporaryDirectory() as tmp, _AgentControlApi() as api:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text("Review ticket WIKI-201", encoding="utf-8")
            worktree = root / "worktree"
            worktree.mkdir()
            discovery = root / "backend-url"
            discovery.write_text(api.url + "\n", encoding="utf-8")
            env = {
                key: value
                for key, value in os.environ.items()
                if key not in {"WIKI_BACKEND_URL", "WIKI_BACKEND_PORT"}
            }
            env["WIKI_BACKEND_DISCOVERY_FILE"] = str(discovery)
            spawned = self._run(
                [
                    "agent",
                    "spawn",
                    "WIKI-201",
                    "--kind",
                    "cc",
                    "--role",
                    "review",
                    "--model",
                    "sonnet",
                    "--prompt-file",
                    str(prompt),
                    "--workdir",
                    str(worktree),
                ],
                env,
            )
            status = self._run(["agent", "status", "WIKI-201"], env)
            orchestrator = self._run(
                [
                    "agent",
                    "spawn",
                    "WIKI-ORCH",
                    "--kind",
                    "cc",
                    "--role",
                    "orchestrator",
                    "--model",
                    "opus",
                    "--prompt-file",
                    str(prompt),
                    "--workdir",
                    str(worktree),
                ],
                env,
            )
            self.assertEqual(spawned.returncode, 0, msg=spawned.stderr)
            self.assertEqual(status.returncode, 0, msg=status.stderr)
            self.assertEqual(orchestrator.returncode, 0, msg=orchestrator.stderr)
            request = next(
                payload
                for method, path, payload in api.requests
                if method == "POST" and path == "/api/agents/spawn"
            )
            UUID(request["request_id"])
            self.assertTrue(
                any(
                    method == "POST" and path == "/api/agents/spawn-orchestrator"
                    for method, path, _ in api.requests
                )
            )
            self.assertTrue(
                any(method == "GET" and path == "/health" for method, path, _ in api.requests)
            )


class AgentWatchTests(unittest.TestCase):
    def _run(self, args: list[str], env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(WIKI_CLI), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )

    @staticmethod
    def _worker(*, state: str = "working", last_event_at: str | None = None) -> dict:
        worker = {
            "ticket": "WIKI-103",
            "runtime_state": state,
            "state_reason": None,
            "provider_pid": 4242,
        }
        if last_event_at is not None:
            worker["lastEventAt"] = last_event_at
        return worker

    def test_watch_dedupes_status_and_emits_each_changed_content_once(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status_dir = tmp_path / "status"
            status_dir.mkdir()
            status_path = status_dir / "WIKI-103.json"
            status_path.write_text(json.dumps({
                "state": "working", "pr": None, "step": "first", "blocker": None,
            }))
            polls = 0

            def responder(path: str) -> tuple[int, dict]:
                nonlocal polls
                if path.startswith("/api/agents/WIKI-103/events"):
                    return 200, {"events": []}
                self.assertEqual(path, "/api/agents")
                polls += 1
                if polls == 6:
                    status_path.write_text(json.dumps({
                        "state": "working", "pr": None, "step": "second", "blocker": None,
                    }))
                elif polls == 7:
                    status_path.write_text(json.dumps({
                        "state": "merge-ready", "pr": None, "step": "ready", "blocker": None,
                    }))
                return 200, {"workers": [self._worker()]}

            with _WatchApi(responder) as api:
                env = {
                    **os.environ,
                    "WIKI_AGENT_STATUS_DIR": str(status_dir),
                    "WIKI_BACKEND_URL": api.url,
                    "WIKI_AGENT_RUNTIME_DIR": str(tmp_path / "runtime"),
                }
                proc = self._run(
                    ["agent", "watch", "WIKI-103", "--json", "--interval", "0", "--until", "merge-ready"],
                    env,
                )

            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            events = [json.loads(line) for line in proc.stdout.splitlines()]
            statuses = [event for event in events if event["kind"] == "status"]
            self.assertEqual([event["step"] for event in statuses], ["first", "second", "ready"])
            self.assertEqual(sum(event["merge_ready"] for event in statuses), 1)
            self.assertEqual(polls, 7)

    def test_watch_uses_remote_status_after_first_poll_when_no_local_file_exists(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status_dir = tmp_path / "status"
            status_dir.mkdir()
            polls = 0

            def responder(path: str) -> tuple[int, dict]:
                nonlocal polls
                if path.startswith("/api/agents/WIKI-103/events"):
                    return 200, {"events": []}
                polls += 1
                worker = self._worker()
                if polls == 1:
                    worker.update({"state": "working", "step": "first"})
                else:
                    worker.update({"state": "merge-ready", "step": "ready"})
                return 200, {"workers": [worker]}

            with _WatchApi(responder) as api:
                proc = self._run(
                    ["agent", "watch", "WIKI-103", "--json", "--interval", "0", "--until", "merge-ready"],
                    {**os.environ, "WIKI_AGENT_STATUS_DIR": str(status_dir), "WIKI_BACKEND_URL": api.url},
                )

            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            statuses = [json.loads(line) for line in proc.stdout.splitlines() if json.loads(line)["kind"] == "status"]
            self.assertEqual([event["step"] for event in statuses], ["first", "ready"])
            self.assertTrue(statuses[-1]["merge_ready"])
            self.assertEqual(polls, 2)

    def test_watch_until_terminal_and_stall(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status_dir = tmp_path / "status"
            status_dir.mkdir()
            (status_dir / "WIKI-103.json").write_text(json.dumps({
                "state": "working", "pr": None, "step": "waiting", "blocker": None,
            }))

            def responder(path: str) -> tuple[int, dict]:
                if path.startswith("/api/agents/WIKI-103/events"):
                    return 200, {"events": []}
                return 200, {"workers": [self._worker(state="dead", last_event_at="2000-01-01T00:00:00Z")]}

            with _WatchApi(responder) as api:
                env = {
                    **os.environ,
                    "WIKI_AGENT_STATUS_DIR": str(status_dir),
                    "WIKI_BACKEND_URL": api.url,
                }
                proc = self._run(
                    ["agent", "watch", "WIKI-103", "--json", "--until", "terminal", "--stall-secs", "1"],
                    env,
                )

            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            events = [json.loads(line) for line in proc.stdout.splitlines()]
            self.assertTrue(any(event["kind"] == "terminal" and event["terminal"] == "dead" for event in events))
            self.assertFalse(any(event["kind"] == "stall" and event.get("active") for event in events))

    def test_watch_emits_stall_from_last_event_at(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status_dir = tmp_path / "status"
            status_dir.mkdir()
            (status_dir / "WIKI-103.json").write_text(json.dumps({
                "state": "merge-ready", "pr": None, "step": "ready", "blocker": None,
            }))

            def responder(path: str) -> tuple[int, dict]:
                if path.startswith("/api/agents/WIKI-103/events"):
                    return 200, {"events": []}
                return 200, {"workers": [self._worker(last_event_at="2000-01-01T00:00:00Z")]}

            with _WatchApi(responder) as api:
                proc = self._run(
                    ["agent", "watch", "WIKI-103", "--json", "--until", "merge-ready", "--stall-secs", "1"],
                    {**os.environ, "WIKI_AGENT_STATUS_DIR": str(status_dir), "WIKI_BACKEND_URL": api.url},
                )

            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            events = [json.loads(line) for line in proc.stdout.splitlines()]
            self.assertTrue(any(event["kind"] == "stall" and event.get("active") for event in events))

    def test_watch_until_merged_and_backend_down_exit_codes(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status_dir = tmp_path / "status"
            status_dir.mkdir()
            (status_dir / "WIKI-103.json").write_text(json.dumps({
                "state": "working", "pr": "https://github.com/example/wiki/pull/103", "step": "waiting", "blocker": None,
            }))
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            self._write_fake_gh(bin_dir)

            def responder(path: str) -> tuple[int, dict]:
                if path.startswith("/api/agents/WIKI-103/events"):
                    return 200, {"events": []}
                return 200, {"workers": [self._worker()]}

            with _WatchApi(responder) as api:
                env = {
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                    "FAKE_GH_SCENARIO": "merged",
                    "WIKI_AGENT_STATUS_DIR": str(status_dir),
                    "WIKI_BACKEND_URL": api.url,
                }
                merged = self._run(["agent", "watch", "WIKI-103", "--json", "--until", "merged"], env)

            self.assertEqual(merged.returncode, 0, msg=merged.stderr)
            self.assertTrue(any(json.loads(line)["kind"] == "pr" for line in merged.stdout.splitlines()))
            down = self._run(
                ["agent", "watch", "WIKI-103", "--json", "--no-retry"],
                {**env, "WIKI_BACKEND_URL": "http://127.0.0.1:1"},
            )
            self.assertEqual(down.returncode, 3)
            self.assertTrue(any(json.loads(line)["kind"] == "warning" for line in down.stdout.splitlines()))

    def test_watch_until_merged_retains_pr_after_status_and_worker_disappear(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status_dir = tmp_path / "status"
            status_dir.mkdir()
            status_path = status_dir / "WIKI-103.json"
            status_path.write_text(json.dumps({
                "state": "working", "pr": "https://github.com/example/wiki/pull/103", "step": "waiting", "blocker": None,
            }))
            state_path = tmp_path / "pr-state"
            state_path.write_text("OPEN", encoding="utf-8")
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            self._write_fake_gh(bin_dir)
            polls = 0

            def responder(path: str) -> tuple[int, dict]:
                nonlocal polls
                if path.startswith("/api/agents/WIKI-103/events"):
                    return 200, {"events": []}
                polls += 1
                if polls == 2:
                    status_path.unlink()
                    state_path.write_text("MERGED", encoding="utf-8")
                    return 200, {"workers": []}
                return 200, {"workers": [self._worker()]}

            with _WatchApi(responder) as api:
                proc = self._run(
                    ["agent", "watch", "WIKI-103", "--json", "--interval", "0", "--until", "merged"],
                    {
                        **os.environ,
                        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                        "FAKE_GH_STATE_FILE": str(state_path),
                        "WIKI_AGENT_STATUS_DIR": str(status_dir),
                        "WIKI_BACKEND_URL": api.url,
                    },
                )

            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            pr_events = [json.loads(line) for line in proc.stdout.splitlines() if json.loads(line)["kind"] == "pr"]
            self.assertEqual([event["state"] for event in pr_events], ["OPEN", "MERGED"])
            self.assertEqual(polls, 2)

    def test_watch_keeps_identical_merge_ready_status_across_missing_gap(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status_dir = tmp_path / "status"
            status_dir.mkdir()
            status_path = status_dir / "WIKI-103.json"
            status = {"state": "merge-ready", "pr": None, "step": "ready", "blocker": None}
            status_path.write_text(json.dumps(status))
            polls = 0

            def responder(path: str) -> tuple[int, dict]:
                nonlocal polls
                if path.startswith("/api/agents/WIKI-103/events"):
                    return 200, {"events": []}
                polls += 1
                if polls == 2:
                    status_path.unlink()
                elif polls == 3:
                    status_path.write_text(json.dumps(status))
                if polls == 4:
                    return 200, {"workers": [self._worker(state="dead")]}
                return 200, {"workers": [self._worker()]}

            with _WatchApi(responder) as api:
                proc = self._run(
                    ["agent", "watch", "WIKI-103", "--json", "--interval", "0", "--until", "terminal"],
                    {**os.environ, "WIKI_AGENT_STATUS_DIR": str(status_dir), "WIKI_BACKEND_URL": api.url},
                )

            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            statuses = [json.loads(line) for line in proc.stdout.splitlines() if json.loads(line)["kind"] == "status"]
            self.assertEqual(len(statuses), 1)
            self.assertTrue(statuses[0]["merge_ready"])

    def test_watch_handles_truncated_status_and_emits_one_stall_alert(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status_dir = tmp_path / "status"
            status_dir.mkdir()
            status_path = status_dir / "WIKI-103.json"
            status_path.write_text('{"state":')
            polls = 0

            def responder(path: str) -> tuple[int, dict]:
                nonlocal polls
                if path.startswith("/api/agents/WIKI-103/events"):
                    return 200, {"events": []}
                polls += 1
                if polls == 3:
                    status_path.write_text(json.dumps({
                        "state": "merge-ready", "pr": None, "step": "ready", "blocker": None,
                    }))
                return 200, {"workers": [self._worker(last_event_at="2000-01-01T00:00:00Z")]}

            with _WatchApi(responder) as api:
                proc = self._run(
                    ["agent", "watch", "WIKI-103", "--json", "--interval", "0", "--until", "merge-ready", "--stall-secs", "1"],
                    {**os.environ, "WIKI_AGENT_STATUS_DIR": str(status_dir), "WIKI_BACKEND_URL": api.url},
                )

            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            events = [json.loads(line) for line in proc.stdout.splitlines()]
            statuses = [event for event in events if event["kind"] == "status"]
            self.assertIn("error", statuses[0])
            self.assertEqual(sum(event["kind"] == "stall" and event.get("active") for event in events), 1)

    def test_watch_warns_on_backend_down_then_recovery(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status_dir = tmp_path / "status"
            status_dir.mkdir()
            (status_dir / "WIKI-103.json").write_text(json.dumps({
                "state": "merge-ready", "pr": None, "step": "ready", "blocker": None,
            }))
            polls = 0

            def responder(path: str) -> tuple[int, dict]:
                nonlocal polls
                if path.startswith("/api/agents/WIKI-103/events"):
                    return 200, {"events": []}
                polls += 1
                if polls == 1:
                    return 503, {"detail": "starting"}
                return 200, {"workers": [self._worker()]}

            with _WatchApi(responder) as api:
                proc = self._run(
                    ["agent", "watch", "WIKI-103", "--json", "--interval", "0", "--until", "merge-ready"],
                    {**os.environ, "WIKI_AGENT_STATUS_DIR": str(status_dir), "WIKI_BACKEND_URL": api.url},
                )

            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            warnings = [json.loads(line)["message"] for line in proc.stdout.splitlines() if json.loads(line)["kind"] == "warning"]
            self.assertEqual(len(warnings), 2)
            self.assertTrue(warnings[0].startswith("backend unavailable; retrying:"))
            self.assertEqual(warnings[1], "backend recovered")

    def test_watch_rejects_unknown_worker(self) -> None:
        with _WatchApi(lambda _path: (200, {"workers": []})) as api:
            proc = self._run(
                ["agent", "watch", "WIKI-103", "--no-retry"],
                {**os.environ, "WIKI_BACKEND_URL": api.url},
            )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("WIKI-103 was not found", proc.stderr)

    def test_watch_and_gate_reject_invalid_identifiers(self) -> None:
        watch = self._run(
            ["agent", "watch", "../outside", "--no-retry"],
            {**os.environ, "WIKI_BACKEND_URL": "http://127.0.0.1:1"},
        )
        self.assertEqual(watch.returncode, 2)
        self.assertIn("ticket must match", watch.stderr)
        gate = self._run(["gate", "../outside"], os.environ.copy())
        self.assertEqual(gate.returncode, 2)
        self.assertIn("PR must be a number", gate.stderr)

    def _write_fake_gh(self, bin_dir: Path) -> None:
        script = bin_dir / "gh"
        script.write_text(textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            import sys

            scenario = os.environ.get("FAKE_GH_SCENARIO", "ready")
            args = sys.argv[1:]
            if args[:2] == ["pr", "view"]:
                if scenario == "not-found":
                    sys.stderr.write("pull request not found\\n")
                    raise SystemExit(1)
                state_file = os.environ.get("FAKE_GH_STATE_FILE")
                state = open(state_file).read().strip() if state_file else ("MERGED" if scenario == "merged" else "OPEN")
                print(json.dumps({
                    "state": state,
                    "isDraft": scenario == "draft",
                    "mergeable": "CONFLICTING" if scenario == "not-mergeable" else "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "headRefOid": "abcdef0123456789",
                    "url": "https://github.com/example/wiki/pull/103",
                }))
            elif args[:2] == ["pr", "checks"]:
                if scenario == "failing-checks":
                    print(json.dumps([{ "name": "test", "state": "FAILURE" }]))
                    raise SystemExit(8)
                if scenario == "no-checks":
                    print("[]")
                elif scenario == "empty-checks-failure":
                    raise SystemExit(1)
                else:
                    print(json.dumps([{ "name": "test", "state": "SUCCESS" }]))
            elif args[:2] == ["api", "graphql"]:
                unresolved = [] if scenario != "unresolved" else [{ "isResolved": False }, { "isResolved": False }]
                print(json.dumps({"data": {"resource": {"reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": unresolved
                }}}}))
            else:
                sys.stderr.write("unexpected gh call: " + repr(args) + "\\n")
                raise SystemExit(97)
            """
        ), encoding="utf-8")
        script.chmod(0o755)


class GateTests(unittest.TestCase):
    _run = AgentWatchTests._run
    _write_fake_gh = AgentWatchTests._write_fake_gh

    def test_gate_json_verdicts_and_exit_codes(self) -> None:
        cases = {
            "ready": (0, []),
            "draft": (1, ["draft"]),
            "failing-checks": (1, ["checks-failing"]),
            "no-checks": (0, []),
            "empty-checks-failure": (2, []),
            "unresolved": (1, ["unresolved-threads:2"]),
            "not-mergeable": (1, ["not-mergeable"]),
        }
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            self._write_fake_gh(bin_dir)
            for scenario, (code, reasons) in cases.items():
                with self.subTest(scenario=scenario):
                    proc = self._run(
                        ["gate", "103", "--repo", "example/wiki", "--expect-sha", "abcdef", "--json"],
                        {
                            **os.environ,
                            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                            "FAKE_GH_SCENARIO": scenario,
                        },
                    )
                    self.assertEqual(proc.returncode, code, msg=proc.stderr)
                    if code == 2:
                        self.assertIn("gh pr checks failed", proc.stderr)
                        continue
                    payload = json.loads(proc.stdout)
                    self.assertEqual(payload["ready"], code == 0)
                    self.assertEqual(payload["reasons"], reasons)
                    if scenario == "no-checks":
                        self.assertEqual(payload["notes"], ["no-checks-reported"])

    def test_gate_reports_sha_mismatch_and_pr_not_found_as_usage_error(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            self._write_fake_gh(bin_dir)
            mismatch = self._run(
                ["gate", "103", "--repo", "example/wiki", "--expect-sha", "wrong", "--json"],
                {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"},
            )
            self.assertEqual(mismatch.returncode, 1)
            self.assertEqual(json.loads(mismatch.stdout)["reasons"], ["sha-mismatch"])
            missing = self._run(
                ["gate", "103", "--repo", "example/wiki", "--json"],
                {
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                    "FAKE_GH_SCENARIO": "not-found",
                },
            )
            self.assertEqual(missing.returncode, 2)
            self.assertIn("pull request not found", missing.stderr)


class TodoCompleteTests(unittest.TestCase):
    def _run(self, args: list[str], env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(WIKI_CLI), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )

    def _vault(self, root: Path) -> Path:
        vault = root / "vault"
        (vault / "log").mkdir(parents=True, exist_ok=True)
        (vault / "todo.md").write_text(textwrap.dedent("""\
            ---
            type: reference
            tags: [meta]
            created: 2026-07-14
            updated: 2026-07-14
            ---

            Todo:

            - [P1] WIKI-103: concise item

            In Progress:

            Backlog:
        """), encoding="utf-8")
        (vault / "log" / "done.md").write_text(textwrap.dedent("""\
            ---
            type: log
            tags: [log, done]
            created: 2026-07-14
            updated: 2026-07-14
            ---

            # Done
        """), encoding="utf-8")
        (vault / "map.md").write_text("# Map\n\n- [[todo]] — tasks\n", encoding="utf-8")
        return vault

    def test_default_removes_only_and_done_line_is_opt_in(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            vault = self._vault(tmp_path)
            env = {
                **os.environ,
                "WIKI_VAULT_DIR": str(vault),
                "WIKI_AGENT_RUNTIME_DIR": str(tmp_path / "runtime"),
            }
            done_path = vault / "log" / "done.md"
            before = done_path.read_text(encoding="utf-8")
            default = self._run(["todo", "complete", "WIKI-103"], env)
            self.assertEqual(default.returncode, 0, msg=default.stderr)
            self.assertIn("no done.md line written", default.stdout)
            self.assertNotIn("WIKI-103", (vault / "todo.md").read_text(encoding="utf-8"))
            self.assertEqual(done_path.read_text(encoding="utf-8"), before)

            self._vault(tmp_path)
            with_line = self._run(["todo", "complete", "WIKI-103", "--done-line", "wiki: terse completion"], env)
            self.assertEqual(with_line.returncode, 0, msg=with_line.stderr)
            done = done_path.read_text(encoding="utf-8")
            self.assertEqual(done.count("- **wiki** — terse completion"), 1)
            lint = self._run(["lint"], env)
            self.assertEqual(lint.returncode, 0, msg=lint.stdout + lint.stderr)


if __name__ == "__main__":
    unittest.main()
