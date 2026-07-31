from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import psutil

from .types import ProviderKind


@dataclass(frozen=True)
class ProviderProcessIdentity:
    pid: int
    transcript_path: str


@dataclass(frozen=True)
class ProviderProcessStatus:
    pid: int
    parent_pid: int
    created_at: float
    process_group_id: int
    executable: str | None = None


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


async def provider_process_status(
    pid: int | None,
    *,
    timeout: float = 1.0,
) -> ProviderProcessStatus | None:
    """Return a stable PID snapshot including parent/start time/process group."""

    if pid is None or pid <= 1:
        return None

    def inspect_process() -> ProviderProcessStatus | None:
        try:
            process = psutil.Process(pid)
            parent_pid = process.ppid()
            created_at = process.create_time()
            process_group_id = os.getpgid(pid)
            executable = process.exe()
        except (ProcessLookupError, PermissionError, psutil.Error, OSError):
            return None
        return ProviderProcessStatus(
            pid=pid,
            parent_pid=parent_pid,
            created_at=created_at,
            process_group_id=process_group_id,
            executable=executable,
        )

    try:
        return await asyncio.wait_for(asyncio.to_thread(inspect_process), timeout=timeout)
    except TimeoutError:
        return None


def provider_process_status_sync(pid: int | None) -> ProviderProcessStatus | None:
    """Return a synchronous process identity for durable run metadata."""

    if pid is None or pid <= 1:
        return None
    try:
        process = psutil.Process(pid)
        return ProviderProcessStatus(
            pid=pid,
            parent_pid=process.ppid(),
            created_at=process.create_time(),
            process_group_id=os.getpgid(pid),
            executable=process.exe(),
        )
    except (ProcessLookupError, PermissionError, psutil.Error, OSError):
        return None


def provider_processes_for_run_sync(
    run_id: str,
    agent_id: str,
) -> list[ProviderProcessStatus]:
    """Find provider processes carrying the exact durable run identity."""

    matches: list[ProviderProcessStatus] = []
    for process in psutil.process_iter(["pid", "create_time", "exe"]):
        try:
            pid = int(process.info["pid"])
            if pid <= 1:
                continue
            environment = process.environ()
            if (
                environment.get("WIKI_RUN_ID") != run_id
                or environment.get("WIKI_AGENT_ID") != agent_id
            ):
                continue
            status = provider_process_status_sync(pid)
            if status is not None:
                matches.append(status)
        except (
            ProcessLookupError,
            PermissionError,
            psutil.Error,
            OSError,
            TypeError,
            ValueError,
        ):
            continue
    return matches


def provider_process_group_members_sync(
    process_group_id: int,
) -> list[dict[str, int | float | str | None]]:
    """Capture process identities that share one provider process group."""

    members: list[dict[str, int | float | str | None]] = []
    if process_group_id <= 1:
        return members
    for process in psutil.process_iter(["pid", "create_time", "exe"]):
        try:
            pid = int(process.info["pid"])
            if pid <= 1:
                continue
            if os.getpgid(pid) != process_group_id:
                continue
            members.append(
                {
                    "pid": pid,
                    "created_at": float(process.info["create_time"]),
                    "executable": process.info.get("exe"),
                }
            )
        except (
            ProcessLookupError,
            PermissionError,
            psutil.Error,
            OSError,
            TypeError,
            ValueError,
        ):
            continue
    return members


def terminate_verified_provider_group(
    *,
    pid: int,
    created_at: float,
    executable: str,
    process_group_id: int,
    group_members: list[dict[str, int | float | str | None]] | None = None,
    run_id: str | None = None,
    agent_id: str | None = None,
    grace: float = 0.5,
    kill_timeout: float = 1.0,
) -> bool:
    """Terminate a recorded provider group only after identity checks."""

    if pid <= 1 or process_group_id <= 1 or process_group_id == os.getpgrp():
        return False

    def matching() -> bool | None:
        current = provider_process_status_sync(pid)
        if current is None:
            try:
                if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                    return False
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return None
            return None
        if (
            current.created_at != created_at
            or current.executable != executable
            or current.process_group_id != process_group_id
        ):
            return None
        return True

    def group_alive() -> bool:
        inspected = False
        for process in psutil.process_iter(["pid", "status"]):
            try:
                if os.getpgid(int(process.info["pid"])) != process_group_id:
                    continue
                inspected = True
                if process.info.get("status") != psutil.STATUS_ZOMBIE:
                    return True
            except (
                ProcessLookupError,
                PermissionError,
                psutil.Error,
                OSError,
                TypeError,
                ValueError,
            ):
                continue
        if inspected:
            return False
        return False

    def group_verified() -> bool:
        if not group_members:
            return False
        expected = {
            (
                int(item["pid"]),
                float(item["created_at"]),
                item.get("executable"),
            )
            for item in group_members
            if "pid" in item and "created_at" in item
        }
        current = provider_process_group_members_sync(process_group_id)
        if not current:
            return False
        dedicated_session_group = process_group_id == pid

        def is_later_owned_member(item: dict[str, int | float | str | None]) -> bool:
            try:
                member_pid = int(item["pid"])
                member_created_at = float(item["created_at"])
            except (
                KeyError,
                TypeError,
                ValueError,
            ):
                return False
            if member_created_at < created_at:
                return False
            if dedicated_session_group:
                # The recorded PID is also the session leader. Its PGID is a
                # dedicated ownership boundary for children started later.
                return True
            if run_id is None or agent_id is None:
                return False
            try:
                environment = psutil.Process(member_pid).environ()
            except (
                ProcessLookupError,
                PermissionError,
                psutil.Error,
                OSError,
            ):
                return False
            # A child may use a different executable, but it must carry the
            # exact run identity when no dedicated session boundary exists.
            return (
                environment.get("WIKI_RUN_ID") == run_id
                and environment.get("WIKI_AGENT_ID") == agent_id
            )

        return all(
            (
                (
                    int(item["pid"]),
                    float(item["created_at"]),
                    item.get("executable"),
                )
                in expected
                or is_later_owned_member(item)
            )
            for item in current
        )

    def kill_group_and_wait() -> bool:
        if not group_verified():
            return False
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        deadline = time.monotonic() + kill_timeout
        while time.monotonic() < deadline:
            if not group_alive():
                return True
            time.sleep(0.02)
        return not group_alive()

    identity = matching()
    if identity is False:
        if not group_alive():
            return True
        if not group_verified():
            return False
        return kill_group_and_wait()
    if identity is not True:
        return False
    if not group_verified():
        return False

    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        identity = matching()
        if identity is False:
            if not group_alive():
                return True
            if not group_verified():
                return False
            return kill_group_and_wait()
        if identity is None:
            if not group_alive():
                return True
            if group_verified():
                return kill_group_and_wait()
            return False
        time.sleep(0.02)

    # Verify the recorded PID again before escalating. If only an unverified
    # process remains in the group, retain the run for manual inspection.
    identity = matching()
    if identity is None:
        return False
    if identity is True:
        if not group_verified():
            return False
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
    elif group_alive():
        if not group_verified():
            return False
        return kill_group_and_wait()

    deadline = time.monotonic() + kill_timeout
    while time.monotonic() < deadline:
        if not group_alive():
            return True
        time.sleep(0.02)
    return not group_alive()


async def provider_parent_pid(
    pid: int | None,
    *,
    timeout: float = 1.0,
) -> int | None:
    """Return the PID's current parent, or None when it cannot be verified."""

    status = await provider_process_status(pid, timeout=timeout)
    return status.parent_pid if status is not None else None


async def provider_pid_is_orphan(
    pid: int | None,
    *,
    timeout: float = 1.0,
) -> bool:
    """Treat a provider PID as orphaned only when its parent is init."""

    status = await provider_process_status(pid, timeout=timeout)
    return status is not None and status.parent_pid == 1


async def orphaned_provider_process(
    pid: int | None,
    *,
    timeout: float = 1.0,
) -> ProviderProcessStatus | None:
    """Return the verified orphan snapshot, or None when the PID is not orphaned."""

    status = await provider_process_status(pid, timeout=timeout)
    if status is None or status.parent_pid != 1:
        return None
    return status


async def _current_matching_process(
    process: ProviderProcessStatus | None,
    *,
    timeout: float = 1.0,
) -> ProviderProcessStatus | None:
    if process is None or process.pid <= 1:
        return None
    current = await provider_process_status(process.pid, timeout=timeout)
    if current is None or current.created_at != process.created_at:
        return None
    return current


async def _wait_for_process_exit(
    process: ProviderProcessStatus | None,
    *,
    timeout: float,
    poll_interval: float = 0.05,
) -> bool:
    if process is None or process.pid <= 1:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await _current_matching_process(process, timeout=poll_interval) is None:
            return True
        await asyncio.sleep(poll_interval)
    return await _current_matching_process(process, timeout=poll_interval) is None


def _signal_process(process: ProviderProcessStatus, sig: signal.Signals) -> bool | None:
    try:
        if process.process_group_id > 1:
            os.killpg(process.process_group_id, sig)
        else:
            os.kill(process.pid, sig)
    except ProcessLookupError:
        return None
    except PermissionError:
        return False
    return True


async def terminate_detached_provider_pid(
    process: ProviderProcessStatus | None,
    *,
    grace: float = 0.5,
    terminate_timeout: float = 5.0,
    kill_timeout: float = 1.0,
) -> bool:
    """Wait briefly, then terminate a verified orphaned detached provider PID."""

    if process is None or process.pid <= 1:
        return True
    if await _wait_for_process_exit(process, timeout=grace):
        return True

    current = await _current_matching_process(process)
    if current is None:
        return True
    if current.parent_pid != 1:
        return False

    result = _signal_process(current, signal.SIGTERM)
    if result is None:
        return True
    if result is False:
        return False
    if await _wait_for_process_exit(process, timeout=terminate_timeout):
        return True

    current = await _current_matching_process(process)
    if current is None:
        return True
    if current.parent_pid != 1:
        return False

    result = _signal_process(current, signal.SIGKILL)
    if result is None:
        return True
    if result is False:
        return False
    return await _wait_for_process_exit(process, timeout=kill_timeout)


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
