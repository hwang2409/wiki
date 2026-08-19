"""Provider lane lifecycle and turn helpers shared by wk drivers."""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any

LANE_KINDS = {"claude": "wk-claude", "codex": "wk-codex"}
EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh")
MAX_LINE = 240
SHUTDOWN_CLOSE_TIMEOUT = 30
SHUTDOWN_PROCESS_TIMEOUT = 5
SHUTDOWN_WAIT_TIMEOUT = SHUTDOWN_CLOSE_TIMEOUT + (2 * SHUTDOWN_PROCESS_TIMEOUT) + 5


def default_model_id(lane: str) -> str:
    from ...agent_models import WK_MODEL_OPTIONS

    for option in WK_MODEL_OPTIONS:
        if option.kind == LANE_KINDS[lane] and option.default_worker:
            return option.id
    raise ValueError(f"no default wk model for lane: {lane}")


def build_lane(*, lane: str, model: str, workdir: Path, effort: str | None, state_dir: Path) -> Any:
    from ..wk_core import WkLoop, WkRunMetadata

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
        from ..wk_codex import WkCodexLane

        return WkCodexLane(**kwargs, effort=effort)
    from ..wk_claude import WkClaudeLane

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
