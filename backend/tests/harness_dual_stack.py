"""Real HTTP harness for legacy and SQLite session reads."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.agent_runtime.client import SupervisorClient
from backend.app.agent_runtime.event_store import (
    EventReducerAdapter,
    SQLiteEventStore,
    replay_raw_jsonl,
    runtime_event_db_path,
)
from backend.app.agent_runtime.fake import FixtureAdapterFactory
from backend.app.agent_runtime.normalizer import normalize_provider_event
from backend.app.agent_runtime.provider import ProviderEvent
from backend.app.agent_runtime.protocol import UnixSupervisorServer
from backend.app.agent_runtime.store import RunStore, RuntimePaths
from backend.app.agent_runtime.supervisor import Supervisor
from backend.app.agent_runtime.types import ProviderKind, RunRecord


FIXTURE_DIR = Path(__file__).parent / "fixtures"
RAW_FIXTURE = FIXTURE_DIR / "headless_pending_ask_raw.jsonl"
TRANSCRIPT_FIXTURE = FIXTURE_DIR / "claude_native_surfaces.jsonl"
CODEX_ARTIFACT_FIXTURE = FIXTURE_DIR / "agent_runtime" / "codex_render_artifact_completed.jsonl"
ADAPTER_FIXTURES = FIXTURE_DIR / "agent_runtime"


class _SupervisorThread:
    """Run the real Unix supervisor server beside TestClient."""

    def __init__(self, supervisor: Supervisor, socket_path: Path):
        self.supervisor = supervisor
        self.server = UnixSupervisorServer(supervisor, socket_path)
        self.ready = threading.Event()
        self.finished = threading.Event()
        self.failure: BaseException | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event: asyncio.Event | None = None
        self.thread = threading.Thread(target=self._run, name="wiki-282-supervisor")

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(timeout=10):
            raise RuntimeError("dual-stack supervisor did not start")
        if self.failure is not None:
            raise RuntimeError("dual-stack supervisor failed") from self.failure

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        self.stop_event = asyncio.Event()

        async def serve() -> None:
            try:
                await self.server.start()
                self.ready.set()
                await self.stop_event.wait()
            except BaseException as exc:
                self.failure = exc
                self.ready.set()
            finally:
                await self.server.close()
                await self.supervisor.close()

        try:
            loop.run_until_complete(serve())
        finally:
            loop.close()
            self.finished.set()

    def stop(self) -> None:
        if self.loop is not None and self.stop_event is not None:
            self.loop.call_soon_threadsafe(self.stop_event.set)
        if not self.finished.wait(timeout=10):
            raise RuntimeError("dual-stack supervisor did not stop")
        self.thread.join(timeout=1)


class DualStackHarness:
    """Boot a real app against a real legacy transcript and SQLite store."""

    ticket = "WIKI-282-HARNESS"

    def __init__(
        self,
        *,
        include_pending_overlay: bool = True,
        include_index_artifact: bool = False,
        include_legacy_artifacts: bool = True,
    ) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="wiki-282-dual-stack-")
        self.root = Path(self.temp.name)
        self.run_id = str(uuid4())
        self.include_pending_overlay = include_pending_overlay
        self.include_index_artifact = include_index_artifact
        self.include_legacy_artifacts = include_legacy_artifacts
        self.paths = RuntimePaths(
            runtime_dir=self.root / "runtime",
            socket_path=self.root / "runtime" / "supervisor.sock",
            registry_path=self.root / "registry.json",
            archive_dir=self.root / "archive",
            status_dir=self.root / "status",
        )
        self.transcript_path = self.root / "legacy" / "session.jsonl"
        self.subagent_id = "abc12345"
        self.subagent_path = (
            self.transcript_path.parent
            / self.transcript_path.stem
            / "subagents"
            / f"agent-{self.subagent_id}.jsonl"
        )
        self.raw_path = self.root / "legacy" / "raw.jsonl"
        self.artifact_raw_path = self.root / "legacy" / "artifact-raw.jsonl"
        self.sqlite_path = runtime_event_db_path(self.root / "runtime", self.run_id)
        self.store: RunStore | None = None
        self.supervisor: Supervisor | None = None
        self.supervisor_thread: _SupervisorThread | None = None
        self.client: TestClient | None = None
        self._saved_globals: dict[str, Any] = {}
        self._saved_flags: dict[str, str | None] = {}
        self._saved_env: dict[str, str | None] = {}
        self._saved_session_paths: dict[str, tuple[str, Path]] = {}

    def __enter__(self) -> DualStackHarness:
        self._prepare_fixture_files()
        self._save_main_state()
        self._configure_main_paths()
        self._seed_real_runtime()
        assert self.supervisor is not None
        self.supervisor_thread = _SupervisorThread(
            self.supervisor,
            self.paths.socket_path,
        )
        self.supervisor_thread.start()
        self._set_runtime_env()
        self._set_flag_env(())
        self.client = TestClient(main.app, base_url="http://127.0.0.1")
        self.client.__enter__()
        self._trigger_parent_ingest()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.client is not None:
            self.client.__exit__(exc_type, exc, traceback)
        if self.supervisor_thread is not None:
            self.supervisor_thread.stop()
        self._restore_main_state()
        self.temp.cleanup()

    def _prepare_fixture_files(self) -> None:
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_rows = [
            json.loads(line)
            for line in TRANSCRIPT_FIXTURE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        transcript_rows.append(
            {
                "type": "assistant",
                "timestamp": "2026-07-10T16:00:00.300Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_agent_282",
                            "name": "Agent",
                            "input": {
                                "description": "Delegate child fixture",
                                "prompt": "Inspect child-only branch for WIKI-282.",
                                "subagent_type": "Explore",
                            },
                        }
                    ],
                },
            }
        )
        self.transcript_path.write_text(
            "".join(
                json.dumps(row, separators=(",", ":")) + "\n"
                for row in transcript_rows
            ),
            encoding="utf-8",
        )
        self.subagent_path.parent.mkdir(parents=True, exist_ok=True)
        child_transcript_rows = [
            {
                "type": "user",
                "timestamp": "2026-07-10T16:00:00Z",
                "message": {
                    "role": "user",
                    "content": "Inspect child-only branch for WIKI-282.",
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-07-10T16:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "child-only event"}],
                },
            },
        ]
        self.subagent_path.write_text(
            "".join(
                json.dumps(row, separators=(",", ":")) + "\n"
                for row in child_transcript_rows
            ),
            encoding="utf-8",
        )
        rows = [
            {
                "seq": index,
                "received_at": str(row.get("timestamp") or ""),
                "provider": "claude",
                "direction": "stdout",
                "generation": 1,
                "payload": row,
            }
            for index, row in enumerate(transcript_rows, start=1)
        ]
        pending_rows = (
            [
                json.loads(line)
                for line in RAW_FIXTURE.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if self.include_pending_overlay
            else []
        )
        next_seq = len(rows) + 1
        rows.extend(
            {**row, "seq": next_seq + int(row["seq"]) - 1}
            for row in pending_rows
        )
        next_seq = len(rows) + 1
        rows.extend(
            [
                {
                    "seq": next_seq,
                    "received_at": "2026-07-10T16:00:01Z",
                    "provider": "claude",
                    "direction": "stdout",
                    "generation": 1,
                    "payload": {
                        "type": "mode",
                        "tool": {"agent_id": "child-agent-282"},
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "provider inspector fixture",
                                }
                            ]
                        },
                    },
                },
                {
                    "seq": next_seq + 1,
                    "received_at": "2026-07-10T16:00:02Z",
                    "provider": "claude",
                    "direction": "client",
                    "generation": 1,
                    "payload": {
                        "type": "user",
                        "pending_id": "composer-282",
                        "composer_text": "composer fixture message",
                        "composer_sent_at": "2026-07-10T16:00:02Z",
                    },
                },
            ]
        )
        self.raw_path.write_text(
            "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
            encoding="utf-8",
        )
        if self.include_index_artifact:
            source = json.loads(CODEX_ARTIFACT_FIXTURE.read_text(encoding="utf-8"))
            artifact_row = {
                "seq": 1,
                "received_at": "2026-07-14T22:09:40.564269+00:00",
                "provider": "codex",
                "direction": str(source["direction"]),
                "generation": 1,
                "payload": dict(source["message"]),
            }
            self.artifact_raw_path.write_text(
                json.dumps(artifact_row, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
    def _save_main_state(self) -> None:
        names = (
            "AGENT_REGISTRY_PATH",
            "AGENT_STATUS_DIR",
            "AGENT_RUNTIME_DIR",
            "AGENT_RUNS_DIR",
            "AGENT_ARCHIVE_DIR",
            "AGENT_TMP_DIR",
            "MSG_QUEUE_PATH",
            "SUPERVISOR_CLIENT",
        )
        self._saved_globals = {name: getattr(main, name) for name in names}
        self._saved_globals["SNAPSHOT_DIR"] = main.workgraph.SNAPSHOT_DIR
        self._saved_session_paths = dict(main._session_paths)
        self._saved_flags = {
            flag: os.environ.get(flag)
            for flag, _adapter in main._SQLITE_READ_ROUTES.values()
        }
        self._saved_env = {
            name: os.environ.get(name)
            for name in (
                "WIKI_AGENT_RUNTIME_DIR",
                "WIKI_AGENT_REGISTRY_PATH",
                "WIKI_AGENT_ARCHIVE_DIR",
                "WIKI_AGENT_STATUS_DIR",
                "WIKI_AGENT_RUNS_DIR",
                "WIKI_AGENT_TMP_DIR",
                "WIKI_MSG_QUEUE_PATH",
                "WIKI_SQLITE_SHADOW_SAMPLE_RATE",
            )
        }

    def _set_runtime_env(self) -> None:
        values = {
            "WIKI_AGENT_RUNTIME_DIR": str(self.paths.runtime_dir),
            "WIKI_AGENT_REGISTRY_PATH": str(self.paths.registry_path),
            "WIKI_AGENT_ARCHIVE_DIR": str(self.paths.archive_dir),
            "WIKI_AGENT_STATUS_DIR": str(self.paths.status_dir),
            "WIKI_AGENT_RUNS_DIR": str(self.paths.runs_dir),
            "WIKI_AGENT_TMP_DIR": str(self.root / "tmp"),
            "WIKI_MSG_QUEUE_PATH": str(self.root / "queue.json"),
            "WIKI_SQLITE_SHADOW_SAMPLE_RATE": "1",
        }
        os.environ.update(values)

    def _configure_main_paths(self) -> None:
        main.AGENT_REGISTRY_PATH = self.paths.registry_path
        main.AGENT_STATUS_DIR = self.paths.status_dir
        main.AGENT_RUNTIME_DIR = self.paths.runtime_dir
        main.AGENT_RUNS_DIR = self.paths.runs_dir
        main.AGENT_ARCHIVE_DIR = self.paths.archive_dir
        main.AGENT_TMP_DIR = self.root / "tmp"
        main.MSG_QUEUE_PATH = self.root / "queue.json"
        main.workgraph.SNAPSHOT_DIR = self.root / "workgraphs"
        main._session_paths.clear()

    def _seed_real_runtime(self) -> None:
        self.store = RunStore(self.paths)
        record = self.store.create(
            RunRecord.new(
                agent_id=self.ticket,
                provider=ProviderKind.CLAUDE,
                role="implement",
                model="fixture-model",
                worktree=str(self.root),
                prompt="dual stack fixture",
                run_id=self.run_id,
            )
        )
        record.transcript_path = str(self.transcript_path)
        self.store._write_record(record)  # noqa: SLF001
        legacy_artifacts = [
            {
                "kind": "artifact",
                "artifact_id": "legacy-fixture-mermaid",
                "payload": {
                    "kind": "artifact",
                    "id": "legacy-fixture-mermaid",
                    "title": "Legacy fixture mermaid",
                    "caption": "legacy palette fixture",
                    "artifact": {"kind": "mermaid", "source": "graph TD; A-->B"},
                    "ts": "2026-07-14T22:09:40+00:00",
                },
            },
            {
                "kind": "artifact",
                "artifact_id": "legacy-fixture-table",
                "payload": {
                    "kind": "artifact",
                    "id": "legacy-fixture-table",
                    "title": "Legacy fixture table",
                    "caption": "legacy palette fixture",
                    "artifact": {
                        "kind": "table",
                        "columns": [{"key": "name", "label": "Name", "type": "string"}],
                        "rows": [["fixture"]],
                    },
                    "ts": "2026-07-14T22:09:41+00:00",
                },
            },
        ]
        if self.include_legacy_artifacts:
            (self.paths.runs_dir / self.run_id / "events.jsonl").write_text(
                "".join(
                    json.dumps({"seq": index, **event}, separators=(",", ":")) + "\n"
                    for index, event in enumerate(legacy_artifacts, start=1)
                ),
                encoding="utf-8",
            )
        registry = json.loads(self.paths.registry_path.read_text(encoding="utf-8"))
        registry[self.ticket]["current"]["transcript"] = str(self.transcript_path)
        self.paths.registry_path.write_text(
            json.dumps(registry, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        replay_raw_jsonl(
            self.raw_path,
            self.sqlite_path,
            run_id=self.run_id,
            agent_id=self.ticket,
            provider=ProviderKind.CLAUDE,
        )
        if self.include_index_artifact:
            artifact_run_id = str(uuid4())
            replay_raw_jsonl(
                self.artifact_raw_path,
                runtime_event_db_path(self.root / "runtime", artifact_run_id),
                run_id=artifact_run_id,
                agent_id="WIKI-282-artifacts",
                provider=ProviderKind.CODEX,
            )
        for row in self._raw_rows():
            envelope = self.store.append_raw(
                self.run_id,
                provider=str(row["provider"]),
                direction=str(row["direction"]),
                payload=dict(row["payload"]),
                generation=int(row.get("generation", 1)),
                received_at=str(row["received_at"]),
            )
            normalized = normalize_provider_event(
                ProviderKind.CLAUDE,
                dict(row["payload"]),
                direction=str(row["direction"]),
            )
            self.store.append_normalized(
                self.run_id,
                raw_seq=int(envelope["seq"]),
                disposition=normalized.disposition,
                kind=normalized.kind,
                payload=normalized.payload,
                lifecycle_state=normalized.lifecycle_state,
                normalized_at=str(row.get("received_at") or ""),
            )
        self.adapter_factory = FixtureAdapterFactory(ADAPTER_FIXTURES, pid=os.getpid())
        self.supervisor = Supervisor(
            self.store,
            self.adapter_factory,
        )
        main.SUPERVISOR_CLIENT = SupervisorClient(self.paths, timeout=3)

    def _trigger_parent_ingest(self) -> None:
        """Use the real provider ingest path to discover child transcripts."""

        assert self.supervisor is not None
        assert self.supervisor_thread is not None
        assert self.supervisor_thread.loop is not None
        assert self.store is not None
        adapter = self.adapter_factory(self.store.get(self.run_id))
        event = ProviderEvent(
            ProviderKind.CLAUDE,
            {"type": "mode", "mode": "fixture-child-discovery"},
            direction="stdout",
            generation=1,
            received_at="2026-07-10T16:00:04Z",
        )
        future = asyncio.run_coroutine_threadsafe(
            self.supervisor._handle_provider_event_without_admission(  # noqa: SLF001
                self.run_id,
                adapter,
                event,
            ),
            self.supervisor_thread.loop,
        )
        future.result(timeout=10)

    def _raw_rows(self) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in self.raw_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _restore_main_state(self) -> None:
        for name, value in self._saved_globals.items():
            if name == "SNAPSHOT_DIR":
                main.workgraph.SNAPSHOT_DIR = value
            else:
                setattr(main, name, value)
        main._session_paths.clear()
        main._session_paths.update(self._saved_session_paths)
        for flag, value in self._saved_flags.items():
            if value is None:
                os.environ.pop(flag, None)
            else:
                os.environ[flag] = value
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _set_flag_env(
        self, enabled: tuple[str, ...], *, defaults: bool = False
    ) -> None:
        flags = {
            **{
                route: flag
                for route, (flag, _adapter) in main._SQLITE_READ_ROUTES.items()
            },
        }
        for route, flag in flags.items():
            if defaults:
                os.environ.pop(flag, None)
            elif route in enabled:
                os.environ[flag] = "1"
            else:
                os.environ[flag] = "0"

    def request(
        self,
        method: str,
        path: str,
        *,
        flags: tuple[str, ...] = (),
        defaults: bool = False,
        **kwargs: Any,
    ):
        if self.client is None:
            raise RuntimeError("harness is not active")
        self._set_flag_env(flags, defaults=defaults)
        return self.client.request(method, path, **kwargs)

    def palette(self, query: str, *, limit: int = 30):
        return self.request(
            "GET",
            "/api/palette/search",
            params={"q": query, "limit": limit, "mode": "lexical"},
        )

    def mark_materializer_not_ready(self) -> None:
        store = SQLiteEventStore(self.sqlite_path, migrate=False)
        with store.connection() as connection:
            connection.execute(
                "UPDATE run_cursors SET rebuild_state = 'needed' WHERE run_id = ?",
                (self.run_id,),
            )

    def make_non_headless(self) -> None:
        registry = json.loads(self.paths.registry_path.read_text(encoding="utf-8"))
        current = registry[self.ticket]["current"]
        current.pop("run_id", None)
        registry[self.ticket]["current"] = current
        self.paths.registry_path.write_text(
            json.dumps(registry, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        main._session_paths[self.ticket] = ("claude", self.transcript_path)

    def delete_native_transcripts(self) -> None:
        self.transcript_path.unlink()
        self.subagent_path.unlink()

    def corrupt_sqlite_session_event(self) -> None:
        store = SQLiteEventStore(self.sqlite_path, migrate=False)
        with store.connection() as connection:
            connection.execute(
                "UPDATE run_projections SET tokens_json = ? WHERE run_id = ?",
                (json.dumps({"shadow": "corruption"}), self.run_id),
            )

    def corrupt_sqlite_projection(self) -> None:
        store = SQLiteEventStore(self.sqlite_path, migrate=False)
        with store.connection() as connection:
            connection.execute(
                "UPDATE run_projections SET tokens_json = ? WHERE run_id = ?",
                ("{not-json", self.run_id),
            )

    def append_tool_result_patch(self) -> None:
        store = SQLiteEventStore(self.sqlite_path, migrate=False)
        reducer = EventReducerAdapter(ProviderKind.CLAUDE)
        for row in store.read_normalized_events(self.run_id):
            reducer.apply_normalized_row(row)
        current_state = store.cursor(self.run_id)
        raw = {
            "seq": current_state.raw_seq + 1,
            "received_at": "2026-07-14T22:09:42+00:00",
            "provider": "claude",
            "direction": "stdout",
            "generation": 1,
            "payload": {
                "type": "user",
                "timestamp": "2026-07-14T22:09:42+00:00",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_agent_282",
                            "content": "patched child output",
                        }
                    ],
                },
            },
        }
        store.materialize(self.run_id, raw, reducer)
        with self.raw_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(raw, separators=(",", ":")) + "\n")

    def session(
        self,
        *,
        flags: tuple[str, ...] = (),
        defaults: bool = False,
        cursor: int = 0,
        client_path: str | None = None,
    ):
        params: dict[str, Any] = {"cursor": cursor}
        if client_path is not None:
            params["path"] = client_path
        return self.request(
            "GET",
            f"/api/agents/{self.ticket}/session",
            flags=flags,
            defaults=defaults,
            params=params,
        )

    def delta(
        self,
        *,
        flags: tuple[str, ...] = (),
        defaults: bool = False,
        cursor: int = 0,
        client_path: str | None = None,
    ):
        params: dict[str, Any] = {"cursor": cursor}
        if client_path is not None:
            params["path"] = client_path
        return self.request(
            "GET",
            f"/api/agents/{self.ticket}/subagents/{self.subagent_id}/session",
            flags=flags,
            defaults=defaults,
            params=params,
        )

    def older(
        self,
        *,
        flags: tuple[str, ...] = (),
        defaults: bool = False,
        before: int = 1000,
        count: int = 500,
    ):
        return self.request(
            "GET",
            f"/api/agents/{self.ticket}/session/older",
            flags=flags,
            defaults=defaults,
            params={"before": before, "count": count},
        )

    def provider_events(
        self,
        *,
        flags: tuple[str, ...] = (),
        defaults: bool = False,
        after_seq: int = 0,
        limit: int = 200,
    ):
        return self.request(
            "GET",
            f"/api/agents/{self.ticket}/events",
            flags=flags,
            defaults=defaults,
            params={"after_seq": after_seq, "limit": limit},
        )

    def sse_session_event(
        self, *, flags: tuple[str, ...] = (), defaults: bool = False
    ) -> dict[str, Any]:
        self._set_flag_env(flags, defaults=defaults)
        subscriber = main._subscribe_agent_events()
        for _ in range(10):
            while not subscriber.empty():
                subscriber.get_nowait()
            time.sleep(0.02)
        gate: asyncio.Event | None = None
        future = None
        try:
            assert self.supervisor_thread is not None
            assert self.supervisor_thread.loop is not None
            assert self.supervisor is not None
            assert self.store is not None
            before_cursor = SQLiteEventStore(self.sqlite_path, migrate=False).cursor(
                self.run_id
            ).change_cursor
            gate = asyncio.Event()
            self.supervisor.materializer_gate = gate
            adapter = self.adapter_factory(self.store.get(self.run_id))
            event = ProviderEvent(
                ProviderKind.CLAUDE,
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "sse ingest"}]}},
                direction="stdout",
                generation=1,
                received_at="2026-07-10T16:00:05Z",
            )
            future = asyncio.run_coroutine_threadsafe(
                self.supervisor._handle_provider_event_without_admission(  # noqa: SLF001
                    self.run_id,
                    adapter,
                    event,
                ),
                self.supervisor_thread.loop,
            )
            time.sleep(0.05)
            assert not future.done()
            assert subscriber.empty()
            self.supervisor_thread.loop.call_soon_threadsafe(gate.set)
            future.result(timeout=10)
            after_cursor = SQLiteEventStore(self.sqlite_path, migrate=False).cursor(
                self.run_id
            ).change_cursor
            assert after_cursor > before_cursor
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    return subscriber.get_nowait()
                except asyncio.QueueEmpty:
                    time.sleep(0.01)
            raise AssertionError("SSE event did not reach the real bridge")
        finally:
            if gate is not None and future is not None and not future.done():
                self.supervisor_thread.loop.call_soon_threadsafe(gate.set)
            self.supervisor.materializer_gate = None
            main._event_subscribers.discard(subscriber)

    def append_child_event_after_ingest(self) -> None:
        with self.subagent_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-07-10T16:00:03Z",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "child grew after ingest"}],
                        },
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )

    def mark_child_stale(self) -> None:
        old_time = self.subagent_path.stat().st_mtime - 120
        os.utime(self.subagent_path, (old_time, old_time))

    def rebuild_swap(self) -> None:
        self.rebuild_swap_variant(False)

    def rebuild_swap_variant(self, extra_event: bool) -> None:
        variant_raw = self.root / (
            "raw-with-extra.jsonl" if extra_event else "raw-base.jsonl"
        )
        rows = self._raw_rows()
        if extra_event:
            rows.append(
                {
                    "seq": len(rows) + 1,
                    "received_at": "2026-07-10T16:00:03Z",
                    "provider": "claude",
                    "direction": "stdout",
                    "generation": 1,
                    "payload": {
                        "type": "assistant",
                        "message": {
                            "content": [{"type": "text", "text": "variant"}]
                        },
                    },
                }
            )
        variant_raw.write_text(
            "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
            encoding="utf-8",
        )
        replacement = self.root / (
            "replacement-with-extra.sqlite3"
            if extra_event
            else "replacement-base.sqlite3"
        )
        replacement.unlink(missing_ok=True)
        replay_raw_jsonl(
            variant_raw,
            replacement,
            run_id=self.run_id,
            agent_id=self.ticket,
            provider=ProviderKind.CLAUDE,
        )
        from backend.app.agent_runtime.event_store import SQLiteEventStore

        SQLiteEventStore(self.sqlite_path, migrate=False).replace_run_from(
            replacement,
            self.run_id,
        )
