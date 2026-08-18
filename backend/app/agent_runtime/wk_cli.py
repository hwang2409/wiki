"""Terminal driver for the wk provider lanes.

Run ``./wk`` from the repo root to chat with a lane directly, without the
backend or supervisor.  The launcher exports ``WIKI_ENABLE_WK=1`` before
Python starts because the flag is frozen at ``wk_feature`` import time.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import signal
import sys
import tempfile
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

LANE_KINDS = {"claude": "wk-claude", "codex": "wk-codex"}
EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh")
MAX_LINE = 240
_INPUT_SHUTDOWN = object()
SHUTDOWN_CLOSE_TIMEOUT = 30
SHUTDOWN_PROCESS_TIMEOUT = 5
SHUTDOWN_WAIT_TIMEOUT = SHUTDOWN_CLOSE_TIMEOUT + (2 * SHUTDOWN_PROCESS_TIMEOUT) + 5


class _InputShutdown(Exception):
    pass


class _StdinReader:
    def __init__(self, shutdown_fd: int, readers_ready: Callable[[], None] | None = None) -> None:
        self._fd = sys.stdin.fileno()
        self._shutdown_fd = shutdown_fd
        self._readers_ready = readers_ready
        self._buffer = bytearray()

    def _take_line(self) -> str | None:
        newline = self._buffer.find(b"\n")
        if newline < 0:
            return None
        line = bytes(self._buffer[:newline])
        del self._buffer[: newline + 1]
        return line.decode(errors="replace")

    async def read_line(self, prompt: str) -> str:
        loop = asyncio.get_running_loop()
        sys.stdout.write(prompt)
        sys.stdout.flush()
        line = self._take_line()
        if line is not None:
            return line

        line_future: asyncio.Future[str] = loop.create_future()

        def read_ready() -> None:
            try:
                chunk = os.read(self._fd, 4096)
            except OSError as exc:
                if not line_future.done():
                    line_future.set_exception(exc)
                return
            if not chunk:
                if not line_future.done():
                    line_future.set_exception(EOFError)
                return
            self._buffer.extend(chunk)
            line = self._take_line()
            if line is not None and not line_future.done():
                line_future.set_result(line)

        def shutdown_ready() -> None:
            try:
                os.read(self._shutdown_fd, 4096)
            except OSError:
                pass
            if not line_future.done():
                line_future.set_exception(_InputShutdown)

        loop.add_reader(self._fd, read_ready)
        loop.add_reader(self._shutdown_fd, shutdown_ready)
        if self._readers_ready is not None:
            self._readers_ready()
        try:
            return await line_future
        finally:
            loop.remove_reader(self._fd)
            loop.remove_reader(self._shutdown_fd)


def default_model_id(lane: str) -> str:
    from ..agent_models import WK_MODEL_OPTIONS

    for option in WK_MODEL_OPTIONS:
        if option.kind == LANE_KINDS[lane] and option.default_worker:
            return option.id
    raise ValueError(f"no default wk model for lane: {lane}")


def parse_cli(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wk",
        description="Chat with a wk provider lane from a plain terminal.",
    )
    parser.add_argument("--lane", choices=tuple(LANE_KINDS), default="claude")
    parser.add_argument("--model", help="model id (defaults per lane from the wk model options)")
    parser.add_argument("--effort", choices=EFFORT_LEVELS, help="reasoning effort (codex lane only)")
    parser.add_argument("--workdir", type=Path, default=Path.cwd(), help="lane worktree (default: current directory)")
    parser.add_argument("-p", "--prompt", help="one-shot mode: run a single turn and exit")
    args = parser.parse_args(argv)
    if args.lane == "claude" and args.effort is not None:
        parser.error("wk-claude workers do not accept reasoning effort")
    if args.lane == "codex" and args.effort is None:
        args.effort = "medium"
    args.workdir = args.workdir.resolve()
    if not args.workdir.is_dir():
        parser.error(f"workdir is not a directory: {args.workdir}")
    return args


def build_lane(*, lane: str, model: str, workdir: Path, effort: str | None, state_dir: Path) -> Any:
    from .wk_core import WkLoop, WkRunMetadata

    agent_id = f"wk-cli-{uuid.uuid4().hex[:8]}"
    kwargs: dict[str, Any] = {
        "metadata": WkRunMetadata.from_kind(LANE_KINDS[lane]),
        "run_id": agent_id,
        "agent_id": agent_id,
        "worktree": workdir,
        "model": model,
        "loop": WkLoop(status_path=state_dir / "status.json", worktree=workdir),
        "steering_path": state_dir / "steering.json",
    }
    if lane == "codex":
        from .wk_codex import WkCodexLane

        return WkCodexLane(**kwargs, effort=effort)
    from .wk_claude import WkClaudeLane

    return WkClaudeLane(**kwargs)


def _one_line(value: object, limit: int = MAX_LINE) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, sort_keys=True)
    text = " ".join(text.split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _render_claude(raw: Mapping[str, Any]) -> list[str]:
    kind = str(raw.get("type") or "")
    if kind == "system":
        subtype = str(raw.get("subtype") or "")
        if subtype == "init":
            return [f"[system] session {raw.get('session_id') or '?'} started"]
        return [f"[system] {_one_line(subtype or dict(raw))}"]
    if kind == "assistant":
        lines: list[str] = []
        content = (raw.get("message") or {}).get("content") or []
        for block in content:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type == "text":
                lines.append(str(block.get("text") or ""))
            elif block_type == "thinking":
                lines.append(f"[thinking] {_one_line(block.get('thinking') or '')}")
            elif block_type == "tool_use":
                lines.append(f"[tool] {block.get('name')} {_one_line(block.get('input') or {})}")
        return lines
    if kind == "user":
        lines = []
        content = (raw.get("message") or {}).get("content") or []
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "tool_result":
                status = "error" if block.get("is_error") else "ok"
                lines.append(f"[tool result] {status} {_one_line(block.get('content') or '')}")
        return lines
    if kind == "result":
        status = str(raw.get("subtype") or ("error" if raw.get("is_error") else "ok"))
        return [f"[turn] {status} ({raw.get('duration_ms', '?')} ms)"]
    if kind == "control_request":
        return [f"[approval] {_one_line(raw.get('request') or {})}"]
    if kind == "provider_error":
        return [f"[error] {raw.get('error_class')}: {_one_line(raw.get('error') or '')}"]
    if kind == "stream_event":
        return []
    return [f"[event] {_one_line(kind or dict(raw))}"]


_CODEX_ERROR_METHODS = {"error", "codex.provider_error", "wk.codex.tool_policy_unavailable"}


def _render_codex(raw: Mapping[str, Any]) -> list[str]:
    method = str(raw.get("method") or "")
    params = raw.get("params") if isinstance(raw.get("params"), Mapping) else {}
    if method in _CODEX_ERROR_METHODS:
        detail = params.get("error") or params
        prefix = params.get("error_class") or "error"
        return [f"[error] {prefix}: {_one_line(detail)}"]
    if method == "thread/started":
        return ["[thread] started"]
    if method == "turn/started":
        return ["[turn] started"]
    if method == "turn/completed":
        turn = params.get("turn") if isinstance(params.get("turn"), Mapping) else {}
        return [f"[turn] {turn.get('status') or '?'}"]
    if method == "item/tool/call":
        return [f"[tool] {params.get('namespace')}.{params.get('tool')} {_one_line(params.get('arguments') or {})}"]
    if method == "item/completed":
        item = params.get("item") if isinstance(params.get("item"), Mapping) else {}
        item_type = item.get("type")
        if item_type == "agentMessage":
            text = str(item.get("text") or "")
            return [text] if text else []
        if item_type == "reasoning":
            summary = " ".join(str(part) for part in item.get("summary") or [])
            return [f"[thinking] {_one_line(summary)}"] if summary else []
        if item_type == "dynamicToolCall":
            return [f"[tool result] {item.get('tool')} success={item.get('success')}"]
        if item_type == "userMessage":
            return []
        return [f"[item] {_one_line(item_type or dict(item))}"]
    if method == "thread/status/changed":
        status = params.get("status") if isinstance(params.get("status"), Mapping) else {}
        return [f"[status] {status.get('type') or '?'}"]
    if method.startswith("item/") or method.startswith("thread/"):
        return []
    return [f"[event] {_one_line(method or dict(raw))}"]


def render_item(item: Mapping[str, Any]) -> list[str]:
    raw = item.get("raw")
    if not isinstance(raw, Mapping) or raw.get("type") == "wk_ledger":
        return []
    if "method" in raw:
        return _render_codex(raw)
    return _render_claude(raw)


def turn_end_status(item: Mapping[str, Any]) -> str | None:
    event = item.get("event")
    if isinstance(event, Mapping):
        kind = str(event.get("kind") or "")
        if kind.endswith("provider_error") or kind == "wk.codex.tool_policy_unavailable":
            return "error"
    raw = item.get("raw")
    if not isinstance(raw, Mapping):
        return None
    if raw.get("type") == "result":
        if raw.get("subtype") == "interrupted":
            return "interrupted"
        return "error" if raw.get("is_error") else "ok"
    if raw.get("method") == "turn/completed":
        params = raw.get("params")
        turn = params.get("turn") if isinstance(params, Mapping) else None
        status = str(turn.get("status") or "") if isinstance(turn, Mapping) else ""
        if status == "completed":
            return "ok"
        return "interrupted" if status == "interrupted" else "error"
    return None


async def run_turn(
    lane: Any,
    events: AsyncIterator[Mapping[str, Any]],
    prompt: str,
    *,
    first: bool,
    out: Callable[[str], None],
) -> str:
    out(f"[user] {_one_line(prompt)}")
    if first:
        await lane.start(prompt)
    else:
        await lane.send_now(prompt)
    while True:
        try:
            item = await events.__anext__()
        except StopAsyncIteration:
            return "closed"
        for line in render_item(item):
            out(line)
        status = turn_end_status(item)
        if status is not None:
            return status


def wait_for_turn(
    fut: Any,
    request_interrupt: Callable[[], None],
    out: Callable[[str], None],
) -> str | None:
    """Join a turn future on the main thread; Ctrl-C interrupts the turn."""

    interrupts = 0
    while True:
        try:
            return fut.result()
        except KeyboardInterrupt:
            interrupts += 1
            if interrupts > 2:
                fut.cancel()
                out("[interrupt] gave up waiting for the turn")
                return None
            out("[interrupt] stopping the current turn")
            try:
                request_interrupt()
            except Exception as exc:
                out(f"[error] interrupt failed: {exc}")
        except asyncio.CancelledError:
            return None
        except Exception as exc:
            out(f"[error] {type(exc).__name__}: {exc}")
            return None


def _run_on_loop(loop: asyncio.AbstractEventLoop, coro: Any, timeout: float | None = None) -> Any:
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)


def _provider_process(lane: Any) -> Any | None:
    pending = [lane]
    seen: set[int] = set()
    while pending:
        owner = pending.pop()
        if owner is None or id(owner) in seen:
            continue
        seen.add(id(owner))
        process = getattr(owner, "_process", None)
        if process is not None:
            return process
        for name in ("_client", "_adapter", "_transport", "transport"):
            child = getattr(owner, name, None)
            if child is not None:
                pending.append(child)
    return None


async def _wait_for_process(process: Any, timeout: float) -> None:
    wait = getattr(process, "wait", None)
    if not callable(wait):
        return
    result = wait()
    if inspect.isawaitable(result):
        await asyncio.wait_for(result, timeout)


async def _reap_provider_process(process: Any | None, timeout: float = 5) -> None:
    if process is None:
        return

    try:
        if getattr(process, "returncode", None) is None:
            terminate = getattr(process, "terminate", None)
            if callable(terminate):
                result = terminate()
                if inspect.isawaitable(result):
                    await result
    except BaseException:
        pass

    timed_out = False
    try:
        await _wait_for_process(process, timeout)
    except TimeoutError:
        timed_out = True
    except ProcessLookupError:
        return
    except BaseException:
        timed_out = True

    try:
        if not timed_out and getattr(process, "returncode", None) is not None:
            return
    except BaseException:
        pass

    try:
        kill = getattr(process, "kill", None)
        if callable(kill):
            result = kill()
            if inspect.isawaitable(result):
                await result
    except BaseException:
        pass

    try:
        await _wait_for_process(process, timeout)
    except BaseException:
        pass


def _force_kill_provider_process(process: Any | None) -> bool:
    if process is None:
        return False
    try:
        if getattr(process, "returncode", None) is not None:
            return False
        kill = getattr(process, "kill", None)
        if not callable(kill):
            return False
        kill()
        return True
    except BaseException:
        return True


async def shutdown_lane(
    lane: Any,
    *,
    active_turn: Any = None,
    close_timeout: float = SHUTDOWN_CLOSE_TIMEOUT,
    process_timeout: float = SHUTDOWN_PROCESS_TIMEOUT,
) -> None:
    """Cancel CLI work, close the lane, and reap its provider child."""

    if active_turn is not None and not active_turn.done():
        active_turn.cancel()
    process = _provider_process(lane)
    for name in ("_receive_task", "_pump_task"):
        task = getattr(lane, name, None)
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
    close_error: BaseException | None = None
    try:
        await asyncio.wait_for(lane.close(), close_timeout)
    except BaseException as exc:
        close_error = exc
    reap_error: BaseException | None = None
    try:
        await _reap_provider_process(process, timeout=process_timeout)
    except BaseException as exc:
        reap_error = exc
    if close_error is not None:
        raise close_error
    if reap_error is not None:
        raise reap_error


async def _read_input_or_shutdown(
    input_fn: Callable[[str], str] | None,
    prompt: str,
    stdin_reader: _StdinReader | None,
) -> str | object:
    if input_fn is not None:
        return input_fn(prompt)
    if stdin_reader is None:
        raise RuntimeError("stdin reader is not configured")
    try:
        return await stdin_reader.read_line(prompt)
    except _InputShutdown:
        return _INPUT_SHUTDOWN


def repl(
    lane: Any,
    events: AsyncIterator[Mapping[str, Any]],
    loop: asyncio.AbstractEventLoop,
    *,
    input_fn: Callable[[str], str] | None = None,
    out: Callable[[str], None] = print,
    turn_state: Callable[[Any], None] | None = None,
    should_shutdown: Callable[[], bool] | None = None,
    shutdown_fd: int | None = None,
    readers_ready: Callable[[], None] | None = None,
) -> None:
    stdin_reader = (
        _StdinReader(shutdown_fd, readers_ready)
        if input_fn is None and shutdown_fd is not None
        else None
    )
    started = False
    while True:
        if should_shutdown is not None and should_shutdown():
            return
        try:
            line = _run_on_loop(
                loop,
                _read_input_or_shutdown(input_fn, "wk> ", stdin_reader),
            )
            if line is _INPUT_SHUTDOWN:
                return
        except EOFError:
            out("")
            return
        except KeyboardInterrupt:
            out("")
            if should_shutdown is not None and should_shutdown():
                return
            continue
        text = line.strip()
        if not text:
            continue
        fut = asyncio.run_coroutine_threadsafe(
            run_turn(lane, events, text, first=not started, out=out), loop
        )
        if turn_state is not None:
            turn_state(fut)
        try:
            status = wait_for_turn(
                fut, lambda: _run_on_loop(loop, lane.interrupt(), 10), out
            )
        finally:
            if turn_state is not None:
                turn_state(None)
        started = started or status is not None
        if should_shutdown is not None and should_shutdown():
            return
        if status == "closed":
            out("[session] event stream ended")
            return


def one_shot(
    lane: Any,
    events: AsyncIterator[Mapping[str, Any]],
    loop: asyncio.AbstractEventLoop,
    prompt: str,
    *,
    out: Callable[[str], None] = print,
    turn_state: Callable[[Any], None] | None = None,
    turn_gate: asyncio.Lock | None = None,
    shutdown_event: asyncio.Event | None = None,
) -> int:
    async def guarded_turn() -> str:
        if shutdown_event is not None and shutdown_event.is_set():
            return "shutdown"
        return await run_turn(lane, events, prompt, first=True, out=out)

    async def register_turn() -> asyncio.Task[str] | None:
        if turn_gate is None:
            if shutdown_event is not None and shutdown_event.is_set():
                return None
            turn = asyncio.create_task(guarded_turn())
        else:
            async with turn_gate:
                if shutdown_event is not None and shutdown_event.is_set():
                    return None
                turn = asyncio.create_task(guarded_turn())
                if turn_state is not None:
                    turn_state(turn)
                return turn
        if turn_state is not None:
            turn_state(turn)
        return turn

    turn = _run_on_loop(loop, register_turn())
    if turn is None:
        return 1
    fut = asyncio.run_coroutine_threadsafe(_await_task(turn), loop)
    try:
        status = wait_for_turn(fut, lambda: _run_on_loop(loop, lane.interrupt(), 10), out)
    finally:
        if turn_state is not None:
            turn_state(None)
    return 0 if status == "ok" else 1


async def _await_task(task: asyncio.Task[str]) -> str:
    return await task


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_cli(argv)
    os.environ.setdefault("WIKI_ENABLE_WK", "1")
    from .wk_feature import wk_enabled

    if not wk_enabled():
        print(
            "error: WIKI_ENABLE_WK=1 was not set before Python imported the backend; "
            "run this tool through ./wk",
            file=sys.stderr,
        )
        return 2
    model = args.model or default_model_id(args.lane)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="wk-cli-loop", daemon=True)
    thread.start()
    code = 1
    lane = None
    active_turn: Any = None
    shutdown_future: Any = None
    shutdown_requested = threading.Event()
    shutdown_event = asyncio.Event()
    turn_gate = asyncio.Lock()
    shutdown_lock = threading.Lock()
    shutdown_read_fd, shutdown_write_fd = os.pipe()
    os.set_blocking(shutdown_read_fd, False)
    os.set_blocking(shutdown_write_fd, False)

    def set_active_turn(value: Any) -> None:
        nonlocal active_turn
        active_turn = value

    def schedule_shutdown() -> None:
        nonlocal shutdown_future
        with shutdown_lock:
            if shutdown_future is not None or lane is None:
                return
            current_lane = lane

            async def shutdown_current_lane() -> None:
                async with turn_gate:
                    current_turn = active_turn
                await shutdown_lane(current_lane, active_turn=current_turn)

            shutdown_future = asyncio.run_coroutine_threadsafe(shutdown_current_lane(), loop)

    def handle_signal() -> None:
        shutdown_requested.set()
        shutdown_event.set()
        try:
            os.write(shutdown_write_fd, b"\0")
        except OSError:
            pass
        schedule_shutdown()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, handle_signal)
        except (OSError, ValueError):
            pass
    with tempfile.TemporaryDirectory(prefix="wk-cli-") as state:

        async def make_lane() -> Any:
            built_lane = build_lane(
                lane=args.lane,
                model=model,
                workdir=args.workdir,
                effort=args.effort,
                state_dir=Path(state),
            )
            await asyncio.sleep(0)
            return built_lane

        try:
            lane = _run_on_loop(loop, make_lane(), 60)
            if shutdown_requested.is_set():
                schedule_shutdown()
            else:
                events = lane.events().__aiter__()
                if args.prompt is not None:
                    code = one_shot(
                        lane,
                        events,
                        loop,
                        args.prompt,
                        turn_state=set_active_turn,
                        turn_gate=turn_gate,
                        shutdown_event=shutdown_event,
                    )
                else:
                    print(f"wk {args.lane} lane | model {model} | workdir {args.workdir}")
                    print("Ctrl-C exits. Ctrl-D exits.")
                    print("[wk] ready", file=sys.stderr, flush=True)
                    repl(
                        lane,
                        events,
                        loop,
                        turn_state=set_active_turn,
                        should_shutdown=shutdown_requested.is_set,
                        shutdown_fd=shutdown_read_fd,
                        readers_ready=lambda: print(
                            "[wk] readers-ready", file=sys.stderr, flush=True
                        ),
                    )
                    code = 0
            if shutdown_requested.is_set():
                code = 130
        except KeyboardInterrupt:
            code = 130
        except Exception as exc:
            print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
            code = 1
        finally:
            try:
                schedule_shutdown()
                if shutdown_future is not None:
                    shutdown_future.result(SHUTDOWN_WAIT_TIMEOUT)
            except BaseException as exc:
                print(f"[warn] lane shutdown failed: {exc}", file=sys.stderr)
            try:
                process = _provider_process(lane)
                if _force_kill_provider_process(process):
                    _run_on_loop(
                        loop,
                        _wait_for_process(process, SHUTDOWN_PROCESS_TIMEOUT),
                        SHUTDOWN_PROCESS_TIMEOUT + 1,
                    )
            except BaseException as exc:
                print(f"[warn] forced provider reap failed: {exc}", file=sys.stderr)
            try:
                for signum in (signal.SIGINT, signal.SIGTERM):
                    try:
                        loop.remove_signal_handler(signum)
                    except (OSError, ValueError):
                        pass
            finally:
                try:
                    loop.call_soon_threadsafe(loop.stop)
                finally:
                    thread.join(timeout=5)
                    if not thread.is_alive():
                        loop.close()
                    os.close(shutdown_read_fd)
                    os.close(shutdown_write_fd)
    return code


if __name__ == "__main__":
    sys.exit(main())
