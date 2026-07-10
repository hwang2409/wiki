from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .types import ProviderKind


@dataclass(frozen=True)
class ProviderProcessIdentity:
    pid: int
    transcript_path: str


def select_transcript_identity(
    candidates: Sequence[tuple[int, str]],
    process_family: Sequence[int],
    session_id: str | None,
    *,
    reported_path: str | None = None,
) -> ProviderProcessIdentity | None:
    """Select only an exact or unambiguous transcript handle."""

    family_rank = {pid: index for index, pid in enumerate(process_family)}

    def choose(matches: list[tuple[int, str]]) -> ProviderProcessIdentity | None:
        paths = {path for _, path in matches}
        if len(paths) != 1:
            return None
        pid, path = max(matches, key=lambda item: family_rank.get(item[0], -1))
        return ProviderProcessIdentity(pid=pid, transcript_path=path)

    values = list(candidates)
    identity_supplied = bool(reported_path or session_id)
    if reported_path:
        exact_path = choose([item for item in values if item[1] == reported_path])
        if exact_path is not None:
            return exact_path
    if session_id:
        suffix = re.compile(rf"(?:^|-){re.escape(session_id)}\.jsonl$")
        exact_session = choose(
            [item for item in values if suffix.search(Path(item[1]).name)]
        )
        if exact_session is not None:
            return exact_session
    if identity_supplied:
        return None
    return choose(values)


async def _process_family(root_pid: int, *, timeout: float) -> list[int]:
    """Return root plus descendants, parent-before-child, from one ps snapshot."""

    try:
        process = await asyncio.create_subprocess_exec(
            "ps",
            "-axo",
            "pid=,ppid=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return [root_pid]
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return [root_pid]

    children: dict[int, list[int]] = {}
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, parent = (int(part) for part in parts)
        except ValueError:
            continue
        children.setdefault(parent, []).append(pid)
    family = [root_pid]
    for parent in family:
        family.extend(sorted(children.get(parent, [])))
    return family


async def resolve_provider_identity(
    wrapper_pid: int | None,
    provider: ProviderKind,
    session_id: str | None,
    *,
    reported_path: str | None = None,
    timeout: float = 2.0,
) -> ProviderProcessIdentity | None:
    """Resolve the provider PID and transcript from actual open handles.

    Codex's executable is a Node wrapper whose native child owns the rollout,
    and one native process may hold root plus subagent rollouts. Selection is
    therefore deliberately closed: exact protocol path, exact session id, or
    one unambiguous transcript. Lexical guessing is forbidden.
    """

    if wrapper_pid is None or wrapper_pid <= 1:
        return None
    family = await _process_family(wrapper_pid, timeout=timeout)
    try:
        process = await asyncio.create_subprocess_exec(
            "lsof",
            "-a",
            "-p",
            ",".join(str(pid) for pid in family),
            "-Fn",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return None
    if process.returncode != 0:
        return None

    current_pid: int | None = None
    candidates: list[tuple[int, str]] = []
    for raw_line in stdout.decode("utf-8", errors="replace").splitlines():
        if raw_line.startswith("p"):
            try:
                current_pid = int(raw_line[1:])
            except ValueError:
                current_pid = None
            continue
        if current_pid is None or not raw_line.startswith("n/"):
            continue
        path = Path(raw_line[1:])
        if path.suffix != ".jsonl":
            continue
        path_text = str(path)
        if provider is ProviderKind.CODEX and "rollout-" not in path.name:
            continue
        if provider is ProviderKind.CLAUDE and not (
            "/.claude/" in path_text or (session_id and session_id in path_text)
        ):
            continue
        candidates.append((current_pid, path_text))
    if not candidates:
        return None

    return select_transcript_identity(
        candidates,
        family,
        session_id,
        reported_path=reported_path,
    )


async def terminate_process_group(
    process: asyncio.subprocess.Process | None,
    *,
    timeout: float = 5.0,
) -> None:
    """Terminate a provider wrapper and every descendant in its own group."""

    if process is None:
        return

    def group_exists() -> bool:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    async def wait_group(timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while group_exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        return not group_exists()

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if not await wait_group(timeout):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await wait_group(1.0)
    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except TimeoutError:
            process.kill()
            await process.wait()


def command_tuple(command: Sequence[str]) -> tuple[str, ...]:
    value = tuple(str(part) for part in command)
    if not value:
        raise ValueError("provider command cannot be empty")
    return value
