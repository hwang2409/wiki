"""Provider-neutral Wiki tool implementations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from .wk_core import WkLoop, WkMutationClass, WkToolRegistry, WkToolRequest, WkToolResult


def plan_auth_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    source = dict(os.environ if environment is None else environment)
    forbidden = {name for name, value in source.items() if name.startswith("ANTHROPIC_") and value}
    if forbidden:
        raise RuntimeError("wk tool process received API credential environment")
    return {name: value for name, value in source.items() if name in {"HOME", "PATH", "USER", "TMPDIR", "LANG", "LC_ALL", "NO_COLOR"}}


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class _WkPathTool:
    def __init__(self, *, root: Path, name: str, mutation: WkMutationClass):
        self.root = root.resolve()
        self.name = name
        self.mutation = mutation

    def _path(self, value: object) -> Path:
        if not isinstance(value, str) or not value:
            raise ValueError("path must be a non-empty string")
        path = (self.root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError("path is outside the wk worktree")
        return path


class WkReadTool(_WkPathTool):
    def __init__(self, *, root: Path, max_bytes: int = 1_000_000):
        super().__init__(root=root, name="wk.read", mutation=WkMutationClass.NONE)
        self.max_bytes = max_bytes

    def validate(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        path = self._path(arguments.get("path"))
        return {"path": str(path), "max_bytes": int(arguments.get("max_bytes", self.max_bytes))}

    async def execute(self, request: WkToolRequest) -> WkToolResult:
        path = self._path(request.arguments["path"])
        max_bytes = min(max(1, int(request.arguments.get("max_bytes", self.max_bytes))), self.max_bytes)
        try:
            if path.is_dir():
                output = "\n".join(sorted(item.name for item in path.iterdir()))
                data = output.encode()
            else:
                data = path.read_bytes()
                output = data[:max_bytes].decode("utf-8", errors="replace")
            return WkToolResult(
                success=True,
                exit_code=0,
                stdout=output,
                mutation=self.mutation,
                mutation_receipt={"path": str(path), "byte_count": len(data)},
            )
        except OSError as exc:
            return WkToolResult(
                success=False,
                exit_code=1,
                error_class=type(exc).__name__,
                error_detail=str(exc),
                mutation=self.mutation,
            )


class WkWriteTool(_WkPathTool):
    def __init__(self, *, root: Path):
        super().__init__(root=root, name="wk.write", mutation=WkMutationClass.FILE)

    def validate(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        path = self._path(arguments.get("path"))
        content = arguments.get("content", arguments.get("contents"))
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        return {"path": str(path), "content": content}

    async def execute(self, request: WkToolRequest) -> WkToolResult:
        path = self._path(request.arguments["path"])
        content = str(request.arguments["content"]).encode()
        before = _hash_bytes(path.read_bytes()) if path.exists() else None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            return WkToolResult(
                success=True,
                exit_code=0,
                mutation=WkMutationClass.FILE,
                mutation_receipt={
                    "path": str(path),
                    "before": before,
                    "after": _hash_bytes(content),
                    "byte_count": len(content),
                },
            )
        except OSError as exc:
            return WkToolResult(
                success=False,
                exit_code=1,
                error_class=type(exc).__name__,
                error_detail=str(exc),
                mutation=WkMutationClass.FILE,
            )


class WkEditTool(_WkPathTool):
    def __init__(self, *, root: Path):
        super().__init__(root=root, name="wk.edit", mutation=WkMutationClass.FILE)

    def validate(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        path = self._path(arguments.get("path"))
        old = arguments.get("old")
        new = arguments.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValueError("old and new must be strings")
        return {"path": str(path), "old": old, "new": new, "replace_all": bool(arguments.get("replace_all", False))}

    async def execute(self, request: WkToolRequest) -> WkToolResult:
        path = self._path(request.arguments["path"])
        try:
            before_bytes = path.read_bytes()
            before = before_bytes.decode("utf-8")
            old = str(request.arguments["old"])
            new = str(request.arguments["new"])
            replace_all = bool(request.arguments.get("replace_all", False))
            matches = before.count(old)
            if matches == 0 or (matches > 1 and not replace_all):
                raise ValueError(f"edit matched {matches} ranges")
            after = before.replace(old, new, -1 if replace_all else 1)
            path.write_text(after, encoding="utf-8")
            return WkToolResult(
                success=True,
                exit_code=0,
                mutation=WkMutationClass.FILE,
                mutation_receipt={
                    "path": str(path),
                    "before": _hash_bytes(before_bytes),
                    "after": _hash_bytes(after.encode()),
                    "matched_ranges": matches,
                },
            )
        except (OSError, UnicodeError, ValueError) as exc:
            return WkToolResult(
                success=False,
                exit_code=1,
                error_class=type(exc).__name__,
                error_detail=str(exc),
                mutation=WkMutationClass.FILE,
            )


async def _run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_ms: int,
    env: Mapping[str, str] | None = None,
) -> WkToolResult:
    started = asyncio.get_running_loop().time()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=dict(env or os.environ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=max(timeout_ms, 1) / 1000
            )
        except TimeoutError:
            timed_out = True
            process.kill()
            stdout, stderr = await process.communicate()
        duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        exit_code = process.returncode
        return WkToolResult(
            success=not timed_out and exit_code == 0,
            exit_code=exit_code,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            error_class="timeout" if timed_out else ("process_failed" if exit_code else None),
            timed_out=timed_out,
            duration_ms=duration_ms,
            mutation=WkMutationClass.PROCESS,
            mutation_receipt={
                "pid": process.pid,
                "stdout_sha256": _hash_bytes(stdout),
                "stderr_sha256": _hash_bytes(stderr),
            },
        )
    except OSError as exc:
        return WkToolResult(
            success=False,
            exit_code=127,
            error_class=type(exc).__name__,
            error_detail=str(exc),
            mutation=WkMutationClass.PROCESS,
        )


class WkBashTool:
    name = "wk.bash"

    def __init__(
        self,
        *,
        root: Path,
        timeout_ms: int = 120_000,
        environment: Mapping[str, str] | None = None,
    ):
        self.root = root.resolve()
        self.timeout_ms = timeout_ms
        self.environment = dict(environment) if environment is not None else None

    def validate(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        timeout_ms = min(max(1, int(arguments.get("timeout_ms", self.timeout_ms))), 600_000)
        return {"command": command, "timeout_ms": timeout_ms}

    async def execute(self, request: WkToolRequest) -> WkToolResult:
        return await _run_process(
            ("/bin/sh", "-lc", str(request.arguments["command"])),
            cwd=self.root,
            timeout_ms=int(request.arguments.get("timeout_ms", self.timeout_ms)),
            env=(
                self.environment
                if self.environment is not None
                else plan_auth_environment()
            ),
        )


class WkGateTool(WkBashTool):
    name = "wk.gate"

    def __init__(
        self,
        *,
        root: Path,
        wiki_command: Sequence[str] = ("wiki",),
        environment: Mapping[str, str] | None = None,
    ):
        super().__init__(root=root, environment=environment)
        self.wiki_command = tuple(wiki_command)

    def validate(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        pr = arguments.get("pr")
        if not isinstance(pr, str) or not pr:
            raise ValueError("pr must be a non-empty string")
        value: dict[str, object] = {"pr": pr, "timeout_ms": int(arguments.get("timeout_ms", self.timeout_ms))}
        expected_sha = arguments.get("expected_sha")
        if expected_sha is not None:
            if not isinstance(expected_sha, str) or not expected_sha:
                raise ValueError("expected_sha must be a non-empty string")
            value["expected_sha"] = expected_sha
        return value

    async def execute(self, request: WkToolRequest) -> WkToolResult:
        argv = [*self.wiki_command, "gate", str(request.arguments["pr"]), "--json"]
        expected_sha = request.arguments.get("expected_sha")
        if expected_sha:
            argv.extend(("--expect-sha", str(expected_sha)))
        result = await _run_process(
            argv,
            cwd=self.root,
            timeout_ms=int(request.arguments.get("timeout_ms", self.timeout_ms)),
            env=(
                self.environment
                if self.environment is not None
                else plan_auth_environment()
            ),
        )
        if not result.stdout:
            return result
        try:
            verdict = json.loads(result.stdout)
        except json.JSONDecodeError:
            return result
        if not isinstance(verdict, Mapping):
            return result
        receipt = dict(result.mutation_receipt or {})
        receipt["verdict"] = dict(verdict)
        return replace(result, mutation_receipt=receipt)


class WkStatusTool:
    name = "wk.status"

    def __init__(self, *, loop: WkLoop):
        self.loop = loop

    def validate(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        state = arguments.get("state")
        step = arguments.get("step")
        if not isinstance(state, str) or not isinstance(step, str):
            raise ValueError("state and step are required strings")
        return {
            "state": state,
            "step": step,
            "pr": arguments.get("pr"),
            "blocker": arguments.get("blocker"),
        }

    async def execute(self, request: WkToolRequest) -> WkToolResult:
        sequence = self.loop.write_status(
            state=str(request.arguments["state"]),
            pr=str(request.arguments["pr"]) if request.arguments.get("pr") else None,
            step=str(request.arguments["step"]),
            blocker=(
                str(request.arguments["blocker"])
                if request.arguments.get("blocker")
                else None
            ),
        )
        return WkToolResult(
            success=True,
            exit_code=0,
            stdout=json.dumps({"status_write_seq": sequence}, sort_keys=True),
        )


def register_default_wk_tools(
    registry: WkToolRegistry,
    *,
    root: Path,
    loop: WkLoop,
    wiki_command: Sequence[str] = ("wiki",),
    environment: Mapping[str, str] | None = None,
) -> WkToolRegistry:
    tools: tuple[object, ...] = (
        WkReadTool(root=root),
        WkWriteTool(root=root),
        WkEditTool(root=root),
        WkBashTool(root=root, environment=environment),
        WkGateTool(root=root, wiki_command=wiki_command, environment=environment),
        WkStatusTool(loop=loop),
    )
    for tool in tools:
        if tool.name not in registry.names:  # type: ignore[attr-defined]
            registry.register(cast(Any, tool))
    return registry
