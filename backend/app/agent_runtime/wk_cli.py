"""Terminal driver for the wk provider lanes.

Run ``./wk`` from the repo root to chat with a lane directly, without the
backend or supervisor.  The launcher exports ``WIKI_ENABLE_WK=1`` before
Python starts because the flag is frozen at ``wk_feature`` import time.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import tempfile
import threading
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .wk_tui.engine import (
    EFFORT_LEVELS,
    LANE_KINDS,
    MAX_LINE,  # noqa: F401 - preserve the legacy module export
    SHUTDOWN_CLOSE_TIMEOUT,  # noqa: F401 - preserve the legacy module export
    SHUTDOWN_PROCESS_TIMEOUT,
    SHUTDOWN_WAIT_TIMEOUT,
    _force_kill_provider_process,
    _provider_process,
    _run_on_loop,
    _wait_for_process,
    build_lane,
    default_model_id,
    render_item,  # noqa: F401 - preserve the legacy module export
    run_turn,
    shutdown_lane,
    turn_end_status,  # noqa: F401 - preserve the legacy module export
    wait_for_turn,
)

_INPUT_SHUTDOWN = object()


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

        readers_ready: Callable[[], None] | None = None
        if self._readers_ready is not None:
            readers_ready = self._readers_ready
            self._readers_ready = None
        try:
            loop.add_reader(self._fd, read_ready)
            loop.add_reader(self._shutdown_fd, shutdown_ready)
            if readers_ready is not None:
                readers_ready()
            sys.stdout.write(prompt)
            sys.stdout.flush()
            line = self._take_line()
            if line is not None:
                return line
            return await line_future
        finally:
            loop.remove_reader(self._fd)
            loop.remove_reader(self._shutdown_fd)


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
    parser.add_argument("--p", dest="prompt", help=argparse.SUPPRESS)
    parser.add_argument("--plain", action="store_true", help="use the plain REPL instead of the TUI")
    args = parser.parse_args(argv)
    if args.lane == "claude" and args.effort is not None:
        parser.error("wk-claude workers do not accept reasoning effort")
    if args.lane == "codex" and args.effort is None:
        args.effort = "medium"
    args.workdir = args.workdir.resolve()
    if not args.workdir.is_dir():
        parser.error(f"workdir is not a directory: {args.workdir}")
    return args


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
    shutdown_event: asyncio.Event | None = None,
) -> int:
    async def guarded_turn() -> str:
        if shutdown_event is not None and shutdown_event.is_set():
            return "shutdown"
        return await run_turn(lane, events, prompt, first=True, out=out)

    async def register_turn() -> asyncio.Task[str] | None:
        if shutdown_event is not None and shutdown_event.is_set():
            return None
        turn = asyncio.create_task(guarded_turn())
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


def _run_tui(args: argparse.Namespace) -> int | None:
    try:
        from .wk_tui.app import main as tui_main
    except ImportError:
        return None
    return tui_main(args)


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
    if sys.stdout.isatty() and args.prompt is None and not args.plain:
        tui_code = _run_tui(args)
        if tui_code is not None:
            return tui_code
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
                await shutdown_lane(current_lane, active_turn=active_turn)

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
