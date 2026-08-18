"""Terminal driver for the wk provider lanes.

Run ``./wk`` from the repo root to chat with a lane directly, without the
backend or supervisor.  The launcher exports ``WIKI_ENABLE_WK=1`` before
Python starts because the flag is frozen at ``wk_feature`` import time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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


def repl(
    lane: Any,
    events: AsyncIterator[Mapping[str, Any]],
    loop: asyncio.AbstractEventLoop,
    *,
    input_fn: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> None:
    started = False
    while True:
        try:
            line = input_fn("wk> ")
        except EOFError:
            out("")
            return
        except KeyboardInterrupt:
            out("")
            continue
        text = line.strip()
        if not text:
            continue
        fut = asyncio.run_coroutine_threadsafe(
            run_turn(lane, events, text, first=not started, out=out), loop
        )
        status = wait_for_turn(
            fut, lambda: _run_on_loop(loop, lane.interrupt(), 10), out
        )
        started = started or status is not None
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
) -> int:
    fut = asyncio.run_coroutine_threadsafe(
        run_turn(lane, events, prompt, first=True, out=out), loop
    )
    status = wait_for_turn(fut, lambda: _run_on_loop(loop, lane.interrupt(), 10), out)
    return 0 if status == "ok" else 1


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
    with tempfile.TemporaryDirectory(prefix="wk-cli-") as state:

        async def make_lane() -> Any:
            return build_lane(
                lane=args.lane,
                model=model,
                workdir=args.workdir,
                effort=args.effort,
                state_dir=Path(state),
            )

        lane = None
        try:
            lane = _run_on_loop(loop, make_lane(), 60)
            events = lane.events().__aiter__()
            if args.prompt is not None:
                code = one_shot(lane, events, loop, args.prompt)
            else:
                print(f"wk {args.lane} lane | model {model} | workdir {args.workdir}")
                print("Ctrl-C interrupts the current turn. Ctrl-D exits.")
                repl(lane, events, loop)
                code = 0
        except Exception as exc:
            print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
            code = 1
        finally:
            if lane is not None:
                try:
                    _run_on_loop(loop, lane.close(), 30)
                except Exception as exc:
                    print(f"[warn] lane close failed: {exc}", file=sys.stderr)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            if not thread.is_alive():
                loop.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
