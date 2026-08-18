from __future__ import annotations

import asyncio
import json
import os
import signal
import threading
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from backend.app.agent_runtime import wk_cli, wk_feature
from backend.app.agent_runtime.wk_cli import (
    default_model_id,
    one_shot,
    parse_cli,
    render_item,
    repl,
    run_turn,
    shutdown_lane,
    turn_end_status,
    wait_for_turn,
)

FIXTURE = Path(__file__).parent / "fixtures" / "agent_runtime" / "claude_sdk_lane_events.jsonl"


def _claude_item(raw: dict[str, Any]) -> dict[str, Any]:
    return {"raw": raw, "event": {"kind": f"claude.{raw.get('type')}", "provider": "claude"}}


def _codex_item(raw: dict[str, Any]) -> dict[str, Any]:
    return {"raw": raw, "event": {"kind": f"codex.{raw.get('method')}", "provider": "codex"}}


_OK_RESULT = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 5}
_ERROR_RESULT = {"type": "result", "subtype": "error_during_execution", "is_error": True, "duration_ms": 5}


class FakeLane:
    """Scripted lane: each start/send emits one prepared turn of events."""

    def __init__(self, turns: list[list[dict[str, Any]]]):
        self.turns = list(turns)
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.calls: list[tuple[str, str]] = []
        self.interrupts = 0
        self.closed = False

    async def _emit_next(self) -> None:
        for item in self.turns.pop(0) if self.turns else []:
            await self.queue.put(item)

    async def start(self, prompt: str) -> None:
        self.calls.append(("start", prompt))
        await self._emit_next()

    async def send_now(self, message: str) -> None:
        self.calls.append(("send", message))
        await self._emit_next()

    async def interrupt(self) -> None:
        self.interrupts += 1
        await self.queue.put(
            _claude_item({"type": "result", "subtype": "interrupted", "is_error": True})
        )

    async def close(self) -> None:
        self.closed = True
        await self.queue.put(None)

    async def events(self) -> AsyncIterator[Mapping[str, Any]]:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item


class _LoopThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro: Any, timeout: float = 10) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


@pytest.fixture
def loop_thread() -> Any:
    runner = _LoopThread()
    yield runner
    runner.stop()


def test_parse_defaults_claude_lane(tmp_path: Path) -> None:
    args = parse_cli(["--workdir", str(tmp_path)])
    assert args.lane == "claude"
    assert args.effort is None
    assert args.workdir == tmp_path.resolve()
    assert args.prompt is None


def test_parse_codex_lane_defaults_effort(tmp_path: Path) -> None:
    args = parse_cli(["--lane", "codex", "--workdir", str(tmp_path)])
    assert args.effort == "medium"


def test_parse_rejects_effort_on_claude_lane(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse_cli(["--lane", "claude", "--effort", "high", "--workdir", str(tmp_path)])
    assert excinfo.value.code == 2


def test_parse_rejects_missing_workdir(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        parse_cli(["--workdir", str(tmp_path / "missing")])


def test_default_models_come_from_wk_model_options() -> None:
    assert default_model_id("claude") == "sonnet"
    assert default_model_id("codex") == "gpt-5.6-sol"


def test_render_claude_fixture_covers_tools_and_lifecycle() -> None:
    lines: list[str] = []
    for raw_line in FIXTURE.read_text().splitlines():
        lines.extend(render_item(_claude_item(json.loads(raw_line))))
    text = "\n".join(lines)
    assert "[system] session sdk-session-1 started" in text
    assert "I will inspect the file." in text
    assert '[tool] mcp__wiki__read {"path": "README.md"}' in text
    assert "[tool result] ok file contents" in text
    assert "[tool result] error exit code 17" in text
    assert "[turn] success (12 ms)" in text
    assert "[turn] interrupted (25 ms)" in text


def test_render_codex_events_cover_tools_text_and_turns() -> None:
    cases = [
        ({"method": "thread/started", "params": {}}, ["[thread] started"]),
        (
            {
                "method": "item/tool/call",
                "params": {"namespace": "wiki", "tool": "read", "arguments": {"path": "README.md"}},
            },
            ['[tool] wiki.read {"path": "README.md"}'],
        ),
        (
            {
                "method": "item/completed",
                "params": {"item": {"type": "agentMessage", "text": "done"}},
            },
            ["done"],
        ),
        (
            {
                "method": "item/completed",
                "params": {"item": {"type": "dynamicToolCall", "tool": "read", "success": True}},
            },
            ["[tool result] read success=True"],
        ),
        (
            {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
            ["[turn] completed"],
        ),
        (
            {
                "method": "codex.provider_error",
                "params": {"error_class": "WkCodexError", "error": "boom"},
            },
            ["[error] WkCodexError: boom"],
        ),
    ]
    for raw, expected in cases:
        assert render_item(_codex_item(raw)) == expected


def test_render_skips_ledger_and_delta_events() -> None:
    assert render_item({"raw": {"type": "wk_ledger", "event_id": "e"}, "event": {}}) == []
    assert render_item(_codex_item({"method": "item/agentMessage/delta", "params": {}})) == []
    assert render_item(_claude_item({"type": "stream_event"})) == []


def test_turn_end_status_for_both_lanes() -> None:
    assert turn_end_status(_claude_item(dict(_OK_RESULT))) == "ok"
    assert turn_end_status(_claude_item(dict(_ERROR_RESULT))) == "error"
    assert (
        turn_end_status(
            _claude_item({"type": "result", "subtype": "interrupted", "is_error": True})
        )
        == "interrupted"
    )
    assert (
        turn_end_status(_codex_item({"method": "turn/completed", "params": {"turn": {"status": "completed"}}}))
        == "ok"
    )
    assert (
        turn_end_status(_codex_item({"method": "turn/completed", "params": {"turn": {"status": "failed"}}}))
        == "error"
    )
    assert turn_end_status(_claude_item({"type": "assistant", "message": {}})) is None
    error_item = {
        "raw": {"type": "provider_error", "error": "boom"},
        "event": {"kind": "claude.provider_error"},
    }
    assert turn_end_status(error_item) == "error"


def test_run_turn_streams_until_result() -> None:
    lane = FakeLane(
        [[
            _claude_item(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "hello back"}]},
                }
            ),
            _claude_item(dict(_OK_RESULT)),
        ]]
    )
    out: list[str] = []

    async def run() -> str:
        events = lane.events().__aiter__()
        return await run_turn(lane, events, "hello", first=True, out=out.append)

    assert asyncio.run(run()) == "ok"
    assert lane.calls == [("start", "hello")]
    assert out[0] == "[user] hello"
    assert "hello back" in out
    assert out[-1] == "[turn] success (5 ms)"


def test_run_turn_reports_closed_stream() -> None:
    lane = FakeLane([[]])

    async def run() -> str:
        events = lane.events().__aiter__()
        await lane.close()
        return await run_turn(lane, events, "hi", first=True, out=lambda _line: None)

    assert asyncio.run(run()) == "closed"


def test_one_shot_exit_codes(loop_thread: _LoopThread) -> None:
    ok_lane = FakeLane([[_claude_item(dict(_OK_RESULT))]])
    ok_events = ok_lane.events().__aiter__()
    assert one_shot(ok_lane, ok_events, loop_thread.loop, "go", out=lambda _line: None) == 0

    error_lane = FakeLane([[_claude_item(dict(_ERROR_RESULT))]])
    error_events = error_lane.events().__aiter__()
    assert one_shot(error_lane, error_events, loop_thread.loop, "go", out=lambda _line: None) == 1


def test_one_shot_start_failure_exits_nonzero(loop_thread: _LoopThread) -> None:
    class BrokenLane(FakeLane):
        async def start(self, prompt: str) -> None:
            raise RuntimeError("no provider")

    lane = BrokenLane([])
    out: list[str] = []
    events = lane.events().__aiter__()
    assert one_shot(lane, events, loop_thread.loop, "go", out=out.append) == 1
    assert any("no provider" in line for line in out)


def test_repl_dispatches_start_then_send_and_exits_on_eof(loop_thread: _LoopThread) -> None:
    lane = FakeLane(
        [
            [_claude_item(dict(_OK_RESULT))],
            [_claude_item(dict(_OK_RESULT))],
        ]
    )
    events = lane.events().__aiter__()
    lines = iter(["first question", "", "second question"])

    def input_fn(_prompt: str) -> str:
        try:
            return next(lines)
        except StopIteration:
            raise EOFError

    out: list[str] = []
    repl(lane, events, loop_thread.loop, input_fn=input_fn, out=out.append)
    assert lane.calls == [("start", "first question"), ("send", "second question")]
    loop_thread.run(lane.close())
    assert lane.closed


def test_repl_turn_error_keeps_the_repl_alive(loop_thread: _LoopThread) -> None:
    class FlakyLane(FakeLane):
        async def send_now(self, message: str) -> None:
            self.calls.append(("send", message))
            raise RuntimeError("turn failed")

    lane = FlakyLane([[_claude_item(dict(_OK_RESULT))], []])
    events = lane.events().__aiter__()
    lines = iter(["one", "two"])

    def input_fn(_prompt: str) -> str:
        try:
            return next(lines)
        except StopIteration:
            raise EOFError

    out: list[str] = []
    repl(lane, events, loop_thread.loop, input_fn=input_fn, out=out.append)
    assert lane.calls == [("start", "one"), ("send", "two")]
    assert any("turn failed" in line for line in out)


class _InterruptOnce:
    """Future wrapper: the first result() call raises KeyboardInterrupt."""

    def __init__(self, fut: Any):
        self.fut = fut
        self.raised = False

    def result(self, timeout: float | None = None) -> Any:
        if not self.raised:
            self.raised = True
            raise KeyboardInterrupt
        return self.fut.result(timeout)

    def cancel(self) -> bool:
        return self.fut.cancel()


def test_wait_for_turn_interrupt_stops_the_turn(loop_thread: _LoopThread) -> None:
    lane = FakeLane([[]])  # the turn emits nothing until interrupted
    events = lane.events().__aiter__()
    out: list[str] = []
    fut = asyncio.run_coroutine_threadsafe(
        run_turn(lane, events, "long task", first=True, out=out.append),
        loop_thread.loop,
    )

    def request_interrupt() -> None:
        asyncio.run_coroutine_threadsafe(lane.interrupt(), loop_thread.loop).result(5)

    status = wait_for_turn(_InterruptOnce(fut), request_interrupt, out.append)
    assert status == "interrupted"
    assert lane.interrupts == 1
    assert any(line.startswith("[interrupt]") for line in out)


def test_wait_for_turn_reports_exceptions() -> None:
    class FailedFuture:
        def result(self, timeout: float | None = None) -> Any:
            raise RuntimeError("lane exploded")

    out: list[str] = []
    assert wait_for_turn(FailedFuture(), lambda: None, out.append) is None
    assert any("lane exploded" in line for line in out)


def test_sigint_during_turn_closes_lane_and_reaps_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.terminated = False
            self.killed = False

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> None:
            if self.returncode is None:
                await asyncio.Event().wait()

    class SignalLane:
        def __init__(self) -> None:
            self.process = FakeProcess()
            self._process = self.process
            self.closed = False
            self.interrupts = 0
            self.turn_cancelled = False

        async def start(self, _prompt: str) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.turn_cancelled = True
                raise

        async def interrupt(self) -> None:
            self.interrupts += 1

        async def close(self) -> None:
            self.closed = True

        async def events(self) -> AsyncIterator[Mapping[str, Any]]:
            if False:
                yield {}

    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    lane = SignalLane()
    monkeypatch.setattr(wk_cli, "build_lane", lambda **_kwargs: lane)

    def interrupt_process() -> None:
        os.kill(os.getpid(), signal.SIGINT)

    timer = threading.Timer(0.1, interrupt_process)
    timer.start()
    try:
        assert wk_cli.main(["--workdir", str(tmp_path), "-p", "hello"]) == 130
    finally:
        timer.cancel()

    assert lane.closed
    assert lane.turn_cancelled
    assert lane.interrupts == 0
    assert lane.process.terminated
    assert lane.process.killed


def test_repeated_signal_during_close_uses_one_shutdown_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.terminate_calls = 0
            self.wait_calls = 0

        def terminate(self) -> None:
            self.terminate_calls += 1
            self.returncode = 0

        async def wait(self) -> None:
            self.wait_calls += 1

    class SignalLane:
        def __init__(self) -> None:
            self.process = FakeProcess()
            self._process = self.process
            self.close_started = threading.Event()
            self.release_close = threading.Event()
            self.close_calls = 0

        async def start(self, _prompt: str) -> None:
            await asyncio.Event().wait()

        async def events(self) -> AsyncIterator[Mapping[str, Any]]:
            if False:
                yield {}

        async def close(self) -> None:
            self.close_calls += 1
            self.close_started.set()
            await asyncio.to_thread(self.release_close.wait)

    monkeypatch.setattr(wk_feature, "_WK_ENABLED", True)
    lane = SignalLane()
    monkeypatch.setattr(wk_cli, "build_lane", lambda **_kwargs: lane)

    def interrupt_process() -> None:
        os.kill(os.getpid(), signal.SIGINT)
        assert lane.close_started.wait(2)
        os.kill(os.getpid(), signal.SIGTERM)
        threading.Event().wait(0.1)
        lane.release_close.set()

    timer = threading.Timer(0.1, interrupt_process)
    timer.start()
    try:
        assert wk_cli.main(["--workdir", str(tmp_path), "-p", "hello"]) == 130
    finally:
        timer.cancel()

    assert lane.close_calls == 1
    assert lane.process.terminate_calls == 1
    assert lane.process.wait_calls == 1


def test_shutdown_cancels_receive_task_before_close() -> None:
    class HangingLane:
        def __init__(self) -> None:
            self.close_called = False
            self.receive_task = asyncio.create_task(self._wait())
            self._receive_task = self.receive_task

        async def _wait(self) -> None:
            await asyncio.Event().wait()

        async def close(self) -> None:
            self.close_called = True

    async def run() -> HangingLane:
        lane = HangingLane()
        await shutdown_lane(lane, process_timeout=0.01)
        return lane

    lane = asyncio.run(run())
    assert lane.close_called
    assert lane.receive_task.cancelled()
