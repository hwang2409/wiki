"""PR conflict detection and safe, mechanical rebase assistance.

The public operation only starts a short-lived helper when GitHub says that a
PR is conflicting.  The helper prompt is deliberately narrow; the
``run_rebase_helper`` function is also kept deterministic so it can be used by
the helper worker and by fixture tests without involving a model.
"""

from __future__ import annotations

import json
import hashlib
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LOCKFILES = {
    "pnpm-lock.yaml",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "uv.lock",
    "Cargo.lock",
}
_CONFLICT_START = re.compile(r"^<<<<<<<(?:\s.*)?$")
_CONFLICT_MID = re.compile(r"^=======$")
_CONFLICT_BASE = re.compile(r"^\|{7}(?:\s.*)?$")
_CONFLICT_END = re.compile(r"^>>>>>>>(?:\s.*)?$")
_CONFLICT_LIKE = re.compile(r"^(?:<{7,}|={7,}|>{7,}|\|{7,})(?:\s.*)?$")


class RebaseError(RuntimeError):
    """A rebase operation could not be completed safely."""


@dataclass
class _RebaseJob:
    job_id: str
    worktree: Path
    prompt: str
    done: threading.Event
    helper: Callable[[Path], Mapping[str, Any]] | None = None
    result: dict[str, Any] | None = None
    pr_number: int = 0
    expected_sha: str = ""
    ticket: str = ""
    worker_id: str = ""
    orchestrator: str | None = None
    verdict: dict[str, Any] | None = None
    durable: bool = False
    completed_at: float | None = None


_JOB_LOCK = threading.RLock()
_JOBS: dict[tuple[Any, ...], _RebaseJob] = {}
_WORKTREE_LOCKS: dict[str, threading.RLock] = {}
_DURABLE_STATE_LOADED = False
_DURABLE_STATE_ROOT: Path | None = None
_DURABLE_JOBS: dict[str, dict[str, Any]] = {}
_OUTBOX: dict[str, dict[str, Any]] = {}
_NOTIFIED_RESULTS: set[str] = set()
_JOB_RETENTION_SECONDS = 900
NotificationSender = Callable[[str, str], None]


def _durable_paths() -> tuple[Path, Path]:
    try:
        runtime_dir = getattr(_main(), "AGENT_RUNTIME_DIR", None)
    except Exception:
        runtime_dir = None
    root = (
        Path(runtime_dir)
        if isinstance(runtime_dir, (str, Path))
        else Path(tempfile.gettempdir()) / "wiki-agent-runtime"
    )
    state_dir = root / "rebase-bot"
    return state_dir / "jobs.json", state_dir / "outbox.json"


def _load_durable_state() -> None:
    global _DURABLE_STATE_LOADED, _DURABLE_STATE_ROOT
    jobs_path, outbox_path = _durable_paths()
    state_root = jobs_path.parent
    if _DURABLE_STATE_LOADED and _DURABLE_STATE_ROOT == state_root:
        return
    if _DURABLE_STATE_ROOT != state_root:
        _DURABLE_JOBS.clear()
        _OUTBOX.clear()
    _DURABLE_STATE_ROOT = state_root
    _DURABLE_STATE_LOADED = True
    for path, target in ((jobs_path, _DURABLE_JOBS), (outbox_path, _OUTBOX)):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, Mapping):
            target.update(
                {
                    str(key): dict(item)
                    for key, item in value.items()
                    if isinstance(key, str) and isinstance(item, Mapping)
                }
            )


def _persist_durable_state() -> None:
    jobs_path, outbox_path = _durable_paths()
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    for path, value in ((jobs_path, _DURABLE_JOBS), (outbox_path, _OUTBOX)):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)


def _durable_key(pr_number: int, expected_sha: str) -> str:
    return f"{pr_number}:{expected_sha}"


def _prune_durable_jobs() -> None:
    cutoff = time.time() - _JOB_RETENTION_SECONDS
    for key, record in list(_DURABLE_JOBS.items()):
        completed_at = record.get("completed_at")
        if (
            record.get("status") in {"completed", "failed"}
            and isinstance(completed_at, (int, float))
            and completed_at < cutoff
        ):
            _DURABLE_JOBS.pop(key, None)
            try:
                pr_number, expected_sha = key.split(":", 1)
                _JOBS.pop((int(pr_number), expected_sha, "production"), None)
            except (ValueError, TypeError):
                pass


def _job_key(
    pr_number: int,
    expected_sha: str,
    helper: Callable[[Path], Mapping[str, Any]] | None,
) -> tuple[Any, ...]:
    # Injected helpers are test and embedding hooks.  Separate their jobs so
    # one fixture cannot consume another fixture's completed result.
    return (
        pr_number,
        expected_sha,
        str(id(helper)) if helper is not None else "production",
    )


def _thread_lock(worktree: Path) -> threading.RLock:
    key = str(worktree)
    with _JOB_LOCK:
        return _WORKTREE_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _worktree_lock(worktree: Path):
    """Serialize rebase jobs in this process and across backend processes."""

    process_lock = _thread_lock(worktree)
    lock_name = hashlib.sha256(str(worktree).encode("utf-8")).hexdigest()
    lock_path = Path(tempfile.gettempdir()) / f"wiki-rebase-{lock_name}.lock"
    with process_lock, lock_path.open("a+") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError) as exc:
            raise RebaseError(f"could not lock rebase worktree: {exc}") from exc
        try:
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass


def _main() -> Any:
    # Avoid the main -> runtime -> main import cycle at module import time.
    from .. import main

    return main


def _git(
    worktree: Path, args: Sequence[str], *, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RebaseError(f"git {' '.join(args)} failed: {exc}") from exc


def _head_sha(worktree: Path) -> str | None:
    result = _git(worktree, ["rev-parse", "HEAD"], timeout=15)
    return result.stdout.strip() if result.returncode == 0 else None


def _git_value(worktree: Path, args: Sequence[str]) -> str | None:
    result = _git(worktree, args, timeout=15)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _repo_name(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip().removesuffix(".git")
    for prefix in (
        "https://github.com/",
        "http://github.com/",
        "git@github.com:",
        "ssh://git@github.com/",
    ):
        if raw.startswith(prefix):
            raw = raw.removeprefix(prefix)
            break
    parts = [part for part in raw.strip("/").split("/") if part]
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


def _validate_pr_binding(worktree: Path, verdict: Mapping[str, Any]) -> None:
    raw = verdict.get("raw")
    source = raw if isinstance(raw, Mapping) else verdict
    gate_repo = _repo_name(
        source.get("repo") if isinstance(source.get("repo"), str) else None
    )
    gate_ref = source.get("head_ref_name") or source.get("headRefName")
    gate_sha = source.get("head_sha")
    remote = _repo_name(_git_value(worktree, ["remote", "get-url", "origin"]))
    branch = _git_value(worktree, ["symbolic-ref", "--short", "HEAD"])
    tracking = _git_value(
        worktree, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]
    )
    current_sha = _head_sha(worktree)
    mismatches: list[str] = []
    if not gate_repo or not remote or gate_repo.lower() != remote.lower():
        mismatches.append(
            f"repo gate={gate_repo or 'unknown'} worktree={remote or 'unknown'}"
        )
    tracking_ref = tracking.removeprefix("origin/") if tracking else None
    if (
        not isinstance(gate_ref, str)
        or not branch
        or gate_ref != branch
        or (tracking_ref is not None and tracking_ref != gate_ref)
    ):
        mismatches.append(
            f"head ref gate={gate_ref or 'unknown'} worktree={branch or 'detached'}"
            f" tracking={tracking or 'none'}"
        )
    if (
        not isinstance(gate_sha, str)
        or not current_sha
        or not current_sha.startswith(gate_sha)
    ):
        mismatches.append(
            f"head sha gate={gate_sha or 'unknown'} worktree={current_sha or 'unknown'}"
        )
    if mismatches:
        raise RebaseError("PR/worktree binding mismatch: " + "; ".join(mismatches))


def _line_ending_only(line: str) -> str:
    return line.replace("\r\n", "\n").replace("\r", "\n")


def _parse_conflicts(
    text: str,
) -> tuple[list[tuple[list[str], list[str] | None, list[str]]], bool]:
    """Parse only Git's exact seven-character conflict markers.

    Marker-like lines with a different marker size are rejected.  They must
    not turn malformed conflict data into a false clean result.
    """

    def marker(line: str) -> str | None:
        if _CONFLICT_START.fullmatch(line):
            return "start"
        if _CONFLICT_BASE.fullmatch(line):
            return "base"
        if _CONFLICT_MID.fullmatch(line):
            return "middle"
        if _CONFLICT_END.fullmatch(line):
            return "end"
        if _CONFLICT_LIKE.fullmatch(line):
            raise RebaseError("malformed conflict marker")
        return None

    lines = text.splitlines(keepends=True)
    hunks: list[tuple[list[str], list[str] | None, list[str]]] = []
    index = 0
    found = False
    while index < len(lines):
        kind = marker(lines[index].rstrip("\r\n"))
        if kind is None:
            index += 1
            continue
        if kind != "start":
            raise RebaseError("unexpected conflict marker")
        found = True
        index += 1
        ours: list[str] = []
        while index < len(lines):
            kind = marker(lines[index].rstrip("\r\n"))
            if kind in {"middle", "base"}:
                break
            if kind is not None:
                raise RebaseError("nested or misplaced conflict marker")
            ours.append(lines[index])
            index += 1
        if index >= len(lines):
            raise RebaseError("incomplete conflict hunk")
        base: list[str] | None = None
        if marker(lines[index].rstrip("\r\n")) == "base":
            index += 1
            base = []
            while index < len(lines):
                kind = marker(lines[index].rstrip("\r\n"))
                if kind == "middle":
                    break
                if kind is not None:
                    raise RebaseError("nested or misplaced conflict marker")
                base.append(lines[index])
                index += 1
            if index >= len(lines):
                raise RebaseError("incomplete diff3 conflict hunk")
        index += 1
        theirs: list[str] = []
        while index < len(lines):
            kind = marker(lines[index].rstrip("\r\n"))
            if kind == "end":
                break
            if kind is not None:
                raise RebaseError("nested or misplaced conflict marker")
            theirs.append(lines[index])
            index += 1
        if index >= len(lines):
            raise RebaseError("incomplete conflict hunk")
        index += 1
        hunks.append((ours, base, theirs))
    return hunks, found


def resolve_conflict_file(path: Path) -> tuple[bool, str | None]:
    """Resolve only conflicts whose sides differ in line endings.

    Returns ``(resolved, summary)``.  A false result never writes the file.
    """

    raw = path.read_text(encoding="utf-8", errors="surrogateescape")
    try:
        hunks, found = _parse_conflicts(raw)
    except RebaseError as exc:
        return False, str(exc)
    if not found:
        return True, None

    for ours, _base, theirs in hunks:
        if [_line_ending_only(line) for line in ours] != [
            _line_ending_only(line) for line in theirs
        ]:
            excerpt = "".join(
                ["<<<<<<< ours\n", *ours, "=======\n", *theirs, ">>>>>>> theirs\n"]
            )
            return False, excerpt.strip()[:1200]

    lines = raw.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    while index < len(lines):
        if not _CONFLICT_START.fullmatch(lines[index].rstrip("\r\n")):
            output.append(lines[index])
            index += 1
            continue
        index += 1
        ours_start = index
        while index < len(lines) and not _CONFLICT_MID.fullmatch(
            lines[index].rstrip("\r\n")
        ):
            index += 1
        ours = lines[ours_start:index]
        if index < len(lines) and _CONFLICT_BASE.fullmatch(
            lines[index].rstrip("\r\n")
        ):
            index += 1
            while index < len(lines) and not _CONFLICT_MID.fullmatch(
                lines[index].rstrip("\r\n")
            ):
                index += 1
        index += 1
        while index < len(lines) and not _CONFLICT_END.fullmatch(
            lines[index].rstrip("\r\n")
        ):
            index += 1
        index += 1
        output.extend(_line_ending_only(line) for line in ours)
    normalized = "".join(output).replace("\r\n", "\n").replace("\r", "\n")
    path.write_text(normalized, encoding="utf-8", errors="surrogateescape", newline="")
    return True, None


def _lockfile_command(worktree: Path, filename: str) -> list[str] | None:
    basename = Path(filename).name
    if basename == "package-lock.json" or basename == "npm-shrinkwrap.json":
        return ["npm", "install", "--package-lock-only", "--ignore-scripts"]
    if basename == "pnpm-lock.yaml":
        return ["pnpm", "install", "--lockfile-only", "--ignore-scripts"]
    if basename == "yarn.lock":
        return ["yarn", "install", "--mode=skip-builds"]
    if basename == "uv.lock":
        return ["uv", "lock"]
    if basename == "Cargo.lock":
        return ["cargo", "generate-lockfile"]
    return None


def _regenerate_lockfile(worktree: Path, filename: str) -> str | None:
    command = _lockfile_command(worktree, filename)
    if command is None:
        return f"no lockfile generator configured for {filename}"
    try:
        lockfile_dir = (worktree / filename).parent
        result = subprocess.run(
            command,
            cwd=str(lockfile_dir),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{filename} regeneration failed: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "generator failed").strip()
        return f"{filename} regeneration failed: {detail[:600]}"
    return None


def _default_smoke_commands(worktree: Path) -> list[tuple[list[str], Path]]:
    commands: list[tuple[list[str], Path]] = []
    if (worktree / "backend" / "tests").is_dir():
        commands.append(
            ([sys.executable, "-m", "pytest", "-q", "backend/tests"], worktree)
        )
    package = worktree / "frontend" / "package.json"
    if package.is_file():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if isinstance(scripts, dict) and "build" in scripts:
            commands.append((["npm", "run", "build"], worktree / "frontend"))
    return commands


def _conflict_files(worktree: Path) -> list[str]:
    result = _git(worktree, ["diff", "--name-only", "--diff-filter=U"], timeout=15)
    if result.returncode != 0:
        raise RebaseError(
            (result.stderr or result.stdout or "could not list conflicts").strip()
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _preflight_worktree(worktree: Path, expected_sha: str | None = None) -> None:
    head_sha = _head_sha(worktree)
    if expected_sha and (not head_sha or not head_sha.startswith(expected_sha)):
        raise RebaseError(
            f"worktree head changed before rebase: expected {expected_sha}, "
            f"found {head_sha or 'unknown'}"
        )
    status = _git(
        worktree, ["status", "--porcelain", "--untracked-files=all"], timeout=15
    )
    if status.returncode != 0:
        raise RebaseError(
            (status.stderr or status.stdout or "could not inspect worktree").strip()
        )
    if status.stdout.strip():
        raise RebaseError(
            f"worktree is not clean before rebase: {status.stdout.strip()[:600]}"
        )


def _push_destination_error(worktree: Path) -> str | None:
    """Reject a configured push URL that differs from origin's fetch URL."""

    fetch_url = _git_value(worktree, ["remote", "get-url", "origin"])
    push_urls_result = _git(
        worktree, ["remote", "get-url", "--push", "--all", "origin"], timeout=15
    )
    if push_urls_result.returncode != 0:
        return "could not verify origin push destination"
    push_urls = [line.strip() for line in push_urls_result.stdout.splitlines() if line.strip()]
    if not fetch_url or not push_urls:
        return "origin has no verifiable push destination"
    if any(url != fetch_url for url in push_urls):
        return "origin pushurl differs from origin fetch URL"
    return None


def _run_rebase_helper_unlocked(
    worktree: str | Path,
    *,
    smoke_commands: Sequence[tuple[Sequence[str], Path]] | None = None,
    push: bool = True,
) -> dict[str, Any]:
    """Fetch, merge narrow conflict classes, smoke-test, and push.

    All conflicts except lockfiles and line-ending-only files escalate.
    ``push=False`` is useful for isolated fixture tests; production callers use
    the default and still never force-push.
    """

    root = Path(worktree).resolve()
    initial_sha = _head_sha(root)
    fetch = _git(root, ["fetch", "origin", "main"], timeout=120)
    if fetch.returncode != 0:
        return _escalated_result(
            initial_sha,
            [],
            [f"git fetch failed: {(fetch.stderr or fetch.stdout).strip()[:600]}"],
        )
    merge = _git(root, ["merge", "origin/main"], timeout=120)
    if merge.returncode != 0:
        files = _conflict_files(root)
        resolved_files: list[str] = []
        escalated: list[str] = []
        lockfiles: list[str] = []
        for filename in files:
            path = root / filename
            if path.name in LOCKFILES:
                lockfiles.append(filename)
                continue
            ok, detail = resolve_conflict_file(path)
            if ok:
                resolved_files.append(filename)
            else:
                escalated.append(f"{filename}: {detail or 'semantic conflict'}")
        if not escalated:
            for filename in lockfiles:
                removed = _git(root, ["rm", "-f", "--", filename], timeout=30)
                if removed.returncode != 0:
                    escalated.append(
                        f"could not remove {filename} for regeneration: "
                        f"{(removed.stderr or removed.stdout).strip()[:600]}"
                    )
                    continue
                error = _regenerate_lockfile(root, filename)
                if error:
                    escalated.append(error)
                else:
                    resolved_files.append(filename)
        if escalated:
            return _escalated_result(initial_sha, resolved_files, escalated)
        added = _git(root, ["add", "--", *files], timeout=30)
        if added.returncode != 0:
            return _escalated_result(
                initial_sha,
                resolved_files,
                [f"git add failed: {(added.stderr or added.stdout).strip()[:600]}"],
            )
        remaining = _conflict_files(root)
        if remaining:
            return _escalated_result(
                initial_sha,
                resolved_files,
                [f"unresolved conflict: {name}" for name in remaining],
            )
        finish = _git(root, ["commit", "--no-edit"], timeout=60)
        if finish.returncode != 0:
            return _escalated_result(
                initial_sha,
                resolved_files,
                [
                    f"merge commit failed: {(finish.stderr or finish.stdout).strip()[:600]}"
                ],
            )
    elif merge.returncode == 0:
        # A DIRTY gate can become clean between the gate read and fetch.  The
        # helper is still safe: there is simply no merge to resolve.
        files = []
        resolved_files = []

    commands = (
        list(smoke_commands)
        if smoke_commands is not None
        else _default_smoke_commands(root)
    )
    for command, cwd in commands:
        try:
            smoke = subprocess.run(
                command,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _escalated_result(
                initial_sha,
                resolved_files,
                [f"smoke test {' '.join(command)} failed: {exc}"],
            )
        if smoke.returncode != 0:
            detail = (smoke.stderr or smoke.stdout or "smoke test failed").strip()
            return _escalated_result(
                initial_sha,
                resolved_files,
                [f"smoke test {' '.join(command)} failed: {detail[:800]}"],
            )

    if push:
        push_error = _push_destination_error(root)
        if push_error:
            return _escalated_result(initial_sha, resolved_files, [push_error])
        pushed = _git(root, ["push", "origin", "HEAD"], timeout=180)
        if pushed.returncode != 0:
            return _escalated_result(
                initial_sha,
                resolved_files,
                [f"push failed: {(pushed.stderr or pushed.stdout).strip()[:600]}"],
            )
    return {
        "status": "resolved",
        "source": "rebase-bot",
        "head_sha": _head_sha(root),
        "resolved_files": resolved_files,
        "escalated_hunks": [],
    }


def _cleanup_failed_rebase(root: Path, initial_sha: str | None) -> list[str]:
    """Abort, restore, and verify a failed merge before reporting it."""

    errors: list[str] = []
    try:
        active = _git(root, ["rev-parse", "--verify", "MERGE_HEAD"], timeout=15)
    except RebaseError as exc:
        errors.append(f"could not inspect merge state: {exc}")
        active = None
    if active is not None and active.returncode == 0:
        try:
            aborted = _git(root, ["merge", "--abort"], timeout=30)
            if aborted.returncode != 0:
                errors.append(
                    f"merge abort failed: {(aborted.stderr or aborted.stdout).strip()[:600]}"
                )
        except RebaseError as exc:
            errors.append(f"merge abort failed: {exc}")
    try:
        current_sha = _head_sha(root)
    except RebaseError as exc:
        current_sha = None
        errors.append(f"could not inspect HEAD during cleanup: {exc}")
    if initial_sha and current_sha != initial_sha:
        try:
            restored = _git(root, ["reset", "--merge", initial_sha], timeout=60)
            if restored.returncode != 0:
                errors.append(
                    f"reset failed: {(restored.stderr or restored.stdout).strip()[:600]}"
                )
        except RebaseError as exc:
            errors.append(f"reset failed: {exc}")
    try:
        final_sha = _head_sha(root)
        if initial_sha and final_sha != initial_sha:
            errors.append(
                f"cleanup left HEAD at {final_sha or 'unknown'} instead of {initial_sha}"
            )
        status = _git(
            root, ["status", "--porcelain", "--untracked-files=all"], timeout=15
        )
        if status.returncode != 0:
            errors.append(
                f"could not verify clean state: {status.stderr.strip()[:600]}"
            )
        elif status.stdout.strip():
            errors.append(f"cleanup left worktree dirty: {status.stdout.strip()[:600]}")
    except RebaseError as exc:
        errors.append(f"could not verify clean state: {exc}")
    return errors


def _run_rebase_helper_checked(
    worktree: str | Path,
    *,
    smoke_commands: Sequence[tuple[Sequence[str], Path]] | None = None,
    push: bool = True,
) -> dict[str, Any]:
    """Run the merge and clean up every failed or raised operation."""

    root = Path(worktree).resolve()
    initial_sha: str | None = None
    result: dict[str, Any] | None = None
    try:
        initial_sha = _head_sha(root)
        result = _run_rebase_helper_unlocked(
            root,
            smoke_commands=smoke_commands,
            push=push,
        )
    except Exception as exc:
        result = _escalated_result(
            initial_sha,
            [],
            [f"rebase helper failed: {exc}"],
        )
    finally:
        if result is None:
            result = _escalated_result(
                initial_sha,
                [],
                ["rebase helper failed without a result"],
            )
        if result.get("status") != "resolved":
            cleanup_errors = _cleanup_failed_rebase(root, initial_sha)
            if cleanup_errors:
                result.setdefault("escalated_hunks", []).extend(cleanup_errors)
    return result


def run_rebase_helper(
    worktree: str | Path,
    *,
    smoke_commands: Sequence[tuple[Sequence[str], Path]] | None = None,
    push: bool = True,
    expected_sha: str | None = None,
) -> dict[str, Any]:
    """Run one serialized rebase operation in the target worktree."""

    root = Path(worktree).resolve()
    with _worktree_lock(root):
        _preflight_worktree(root, expected_sha)
        return _run_rebase_helper_checked(
            root,
            smoke_commands=smoke_commands,
            push=push,
        )


def _escalated_result(
    head_sha: str | None, resolved_files: Sequence[str], hunks: Sequence[str]
) -> dict[str, Any]:
    return {
        "status": "escalated",
        "source": "rebase-bot",
        "head_sha": head_sha,
        "resolved_files": list(resolved_files),
        "escalated_hunks": list(hunks),
    }


def helper_prompt(*, ticket: str, pr_number: int, worker_id: str) -> str:
    return f"""you are the scoped rebase helper for ticket {ticket} (PR #{pr_number}).

worktree owner: {worker_id}
run `git fetch origin main && git merge origin/main` in this existing worktree.
resolve only lockfile conflicts by deleting and regenerating the lockfile from
its package directory, or conflicts where both sides differ only in CR/LF.
every other conflict is semantic: abort the merge, do not push, and report each
conflicting file and hunk to the orchestrator.

after a clean resolution run the minimum repository smoke set (backend tests
and frontend build/typecheck when configured). if any smoke command fails,
abort/escalate and do not push. push with `git push origin HEAD` only; never
force-push or bypass repository verification hooks. report status, head sha,
resolved files, and escalated hunks. do not touch main directly.
"""


def _gate_is_dirty(verdict: Mapping[str, Any]) -> bool:
    raw = verdict.get("raw")
    source = raw if isinstance(raw, Mapping) else verdict
    return str(source.get("mergeable") or "").upper() == "CONFLICTING"


def _gate_is_clean(verdict: Mapping[str, Any]) -> bool:
    raw = verdict.get("raw")
    source = raw if isinstance(raw, Mapping) else verdict
    mergeable = str(source.get("mergeable") or "").upper()
    return mergeable == "MERGEABLE" or (
        "mergeable" not in source
        and (verdict.get("verdict") == "pass" or verdict.get("ready") is True)
    )


def _clean_result(verdict: Mapping[str, Any]) -> dict[str, Any]:
    raw = verdict.get("raw")
    source = raw if isinstance(raw, Mapping) else verdict
    return {
        "status": "resolved",
        "head_sha": source.get("head_sha")
        if isinstance(source.get("head_sha"), str)
        else None,
        "resolved_files": [],
        "escalated_hunks": [],
        "no_op": True,
    }


def _result_message(worker_id: str, result: Mapping[str, Any]) -> str | None:
    status = result.get("status")
    if status == "resolved":
        resolved_files = ", ".join(
            str(item) for item in result.get("resolved_files", [])
        )
        return f"rebase-bot resolved {worker_id}: " + (
            resolved_files or "rebase completed"
        )
    if status == "escalated":
        return f"rebase-bot escalated {worker_id}: " + "; ".join(
            str(item) for item in result.get("escalated_hunks", [])
        )
    return None


def _persist_job(job: _RebaseJob, result: Mapping[str, Any] | None = None) -> None:
    if not job.durable:
        return
    _load_durable_state()
    key = _durable_key(job.pr_number, job.expected_sha)
    record = _DURABLE_JOBS.setdefault(key, {})
    record.update(
        {
            "job_id": job.job_id,
            "pr_number": job.pr_number,
            "expected_sha": job.expected_sha,
            "ticket": job.ticket,
            "worker_id": job.worker_id,
            "worktree": str(job.worktree),
            "orchestrator": job.orchestrator,
            "prompt": job.prompt,
            "verdict": job.verdict or {},
            "status": "completed" if result is not None else "running",
            "result": dict(result) if result is not None else None,
            "updated_at": time.time(),
            "completed_at": time.time() if result is not None else None,
        }
    )
    _persist_durable_state()


def _enqueue_result(job: _RebaseJob, result: Mapping[str, Any]) -> None:
    if not job.durable or not job.orchestrator:
        return
    message = _result_message(job.worker_id, result)
    if message is None:
        return
    _load_durable_state()
    entry_id = f"{job.job_id}:result"
    _OUTBOX.setdefault(
        entry_id,
        {
            "id": entry_id,
            "target": job.orchestrator,
            "worker_id": job.worker_id,
            "result": dict(result),
            "attempts": 0,
            "last_error": None,
        },
    )
    _persist_durable_state()


def _flush_outbox(notify: NotificationSender | None = None) -> None:
    _load_durable_state()
    if notify is None:
        return
    for entry_id, entry in list(_OUTBOX.items()):
        target = entry.get("target")
        result = entry.get("result")
        worker_id = entry.get("worker_id")
        if not isinstance(target, str) or not isinstance(result, Mapping):
            _OUTBOX.pop(entry_id, None)
            continue
        message = _result_message(str(worker_id or "worker"), result)
        if message is None:
            _OUTBOX.pop(entry_id, None)
            continue
        try:
            notify(target, message[:4000])
        except Exception as exc:
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
            entry["last_error"] = str(exc)[:600]
            continue
        _OUTBOX.pop(entry_id, None)
    _persist_durable_state()


def _escalate_to_orchestrator(
    worker_id: str,
    orchestrator: str | None,
    result: Mapping[str, Any],
    notify: NotificationSender | None,
    event_id: str,
) -> None:
    if not orchestrator or notify is None:
        return
    message = _result_message(worker_id, result)
    if message is None:
        return
    with _JOB_LOCK:
        if event_id in _NOTIFIED_RESULTS:
            return
    try:
        notify(orchestrator, message[:4000])
    except Exception:
        # The result remains in the API response; a transient steering failure
        # must not turn a safe, already-aborted rebase into a false success.
        return
    with _JOB_LOCK:
        _NOTIFIED_RESULTS.add(event_id)


def _finish_rebase_job(
    job: _RebaseJob,
    *,
    worker_id: str,
    orchestrator: str | None,
    steer: Callable[[str, Mapping[str, Any]], None] | None,
    notify: NotificationSender | None,
) -> dict[str, Any]:
    try:
        if job.helper is not None:
            with _worktree_lock(job.worktree):
                result = dict(job.helper(job.worktree))
        else:
            with _worktree_lock(job.worktree):
                if job.verdict is None:
                    raise RebaseError("durable rebase job has no gate verdict")
                _validate_pr_binding(job.worktree, job.verdict)
                _preflight_worktree(job.worktree, job.expected_sha)
                result = dict(_run_rebase_helper_checked(job.worktree))
    except Exception as exc:
        try:
            head_sha = _head_sha(job.worktree)
        except Exception:
            head_sha = None
        result = _escalated_result(
            head_sha,
            [],
            [f"rebase worker failed: {exc}"],
        )
        if job.helper is None:
            try:
                cleanup_sha = _head_sha(job.worktree)
                if cleanup_sha:
                    result["escalated_hunks"].extend(
                        _cleanup_failed_rebase(job.worktree, cleanup_sha)
                    )
            except Exception as cleanup_exc:
                result["escalated_hunks"].append(
                    f"rebase cleanup failed: {cleanup_exc}"
                )
    result["job_id"] = job.job_id
    job.result = result
    job.completed_at = time.time()
    try:
        _persist_job(job, result)
        if job.durable:
            _enqueue_result(job, result)
            _flush_outbox(notify)
    except Exception as exc:
        result.setdefault("escalated_hunks", []).append(
            f"durable rebase status update failed: {exc}"
        )
    with _JOB_LOCK:
        job.done.set()
    if result.get("status") in {"resolved", "escalated"}:
        if steer is not None:
            steer(orchestrator or worker_id, result)
        elif not job.durable:
            _escalate_to_orchestrator(
                worker_id,
                orchestrator,
                result,
                notify,
                f"{job.job_id}:{result.get('status')}:{result.get('head_sha')}",
            )
    return result


def _existing_or_new_job(
    pr_number: int,
    expected_sha: str,
    ticket: str,
    worker_id: str,
    worktree: Path,
    prompt: str,
    verdict: Mapping[str, Any],
    orchestrator: str | None,
    helper: Callable[[Path], Mapping[str, Any]] | None,
) -> tuple[_RebaseJob, bool, bool]:
    key = _job_key(pr_number, expected_sha, helper)
    with _JOB_LOCK:
        existing = _JOBS.get(key)
        if existing is not None:
            return existing, False, False
        if helper is None:
            _load_durable_state()
            _prune_durable_jobs()
            record = _DURABLE_JOBS.get(_durable_key(pr_number, expected_sha))
            if isinstance(record, Mapping):
                job = _RebaseJob(
                    job_id=str(record.get("job_id") or "rebase-unknown"),
                    worktree=Path(str(record.get("worktree") or worktree)).resolve(),
                    prompt=str(record.get("prompt") or prompt),
                    done=threading.Event(),
                    pr_number=pr_number,
                    expected_sha=expected_sha,
                    ticket=str(record.get("ticket") or ticket),
                    worker_id=str(record.get("worker_id") or worker_id),
                    orchestrator=(
                        str(record["orchestrator"])
                        if isinstance(record.get("orchestrator"), str)
                        else orchestrator
                    ),
                    verdict=(
                        dict(record["verdict"])
                        if isinstance(record.get("verdict"), Mapping)
                        else dict(verdict)
                    ),
                    durable=True,
                )
                if isinstance(record.get("result"), Mapping):
                    job.result = dict(record["result"])
                    job.completed_at = float(record.get("completed_at") or time.time())
                    job.done.set()
                    _JOBS[key] = job
                    return job, False, False
                _JOBS[key] = job
                return job, True, True
        job = _RebaseJob(
            job_id=hashlib.sha256(f"{key}:{id(prompt)}".encode()).hexdigest()[:16],
            worktree=worktree,
            prompt=prompt,
            done=threading.Event(),
            helper=helper,
            pr_number=pr_number,
            expected_sha=expected_sha,
            ticket=ticket,
            worker_id=worker_id,
            orchestrator=orchestrator,
            verdict=dict(verdict),
            durable=helper is None,
        )
        _JOBS[key] = job
        _persist_job(job)
        return job, True, False


def _start_rebase_thread(
    job: _RebaseJob,
    steer: Callable[[str, Mapping[str, Any]], None] | None = None,
    notify: NotificationSender | None = None,
) -> None:
    threading.Thread(
        target=_finish_rebase_job,
        kwargs={
            "job": job,
            "worker_id": job.worker_id,
            "orchestrator": job.orchestrator,
            "steer": steer,
            "notify": notify,
        },
        name=f"rebase-bot-{job.job_id}",
        daemon=True,
    ).start()


def resume_pending_jobs(notify: NotificationSender | None = None) -> None:
    """Resume durable rebase workers and retry their result outbox."""

    with _JOB_LOCK:
        _load_durable_state()
        _prune_durable_jobs()
        pending: list[_RebaseJob] = []
        for record in _DURABLE_JOBS.values():
            if record.get("status") not in {"running", "pending"}:
                continue
            try:
                pr_number = int(record["pr_number"])
                expected_sha = str(record["expected_sha"])
                worktree = Path(str(record["worktree"])).resolve()
            except (KeyError, TypeError, ValueError):
                continue
            key = _job_key(pr_number, expected_sha, None)
            if key in _JOBS:
                continue
            job = _RebaseJob(
                job_id=str(record.get("job_id") or "rebase-unknown"),
                worktree=worktree,
                prompt=str(record.get("prompt") or ""),
                done=threading.Event(),
                pr_number=pr_number,
                expected_sha=expected_sha,
                ticket=str(record.get("ticket") or ""),
                worker_id=str(record.get("worker_id") or ""),
                orchestrator=(
                    str(record["orchestrator"])
                    if isinstance(record.get("orchestrator"), str)
                    else None
                ),
                verdict=(
                    dict(record["verdict"])
                    if isinstance(record.get("verdict"), Mapping)
                    else None
                ),
                durable=True,
            )
            _JOBS[key] = job
            pending.append(job)
    _flush_outbox(notify)
    for job in pending:
        _start_rebase_thread(job, notify=notify)


def rebase_dirty_pr(
    pr_number: int,
    ticket: str,
    worker_id: str,
    *,
    gate: Callable[[int], Mapping[str, Any]] | None = None,
    helper: Callable[[Path], Mapping[str, Any]] | None = None,
    steer: Callable[[str, Mapping[str, Any]], None] | None = None,
    notify: NotificationSender | None = None,
) -> dict[str, Any]:
    """Start or execute the safe rebase helper for one conflicting PR."""

    main = _main()
    if helper is None:
        _load_durable_state()
        _flush_outbox(notify)
    verdict = (
        gate(pr_number)
        if gate is not None
        else main.composer_gate(main.ComposerGateIn(pr=str(pr_number)))
    )
    if not isinstance(verdict, Mapping):
        raise RebaseError("gate returned an invalid verdict")
    if _gate_is_clean(verdict):
        return _clean_result(verdict)
    if not _gate_is_dirty(verdict):
        raise RebaseError("gate did not report a stable MERGEABLE or CONFLICTING state")

    raw = verdict.get("raw")
    source = raw if isinstance(raw, Mapping) else verdict
    expected_sha = source.get("head_sha")
    if not isinstance(expected_sha, str) or not expected_sha:
        raise RebaseError("gate did not provide the expected head sha")

    resolved = main._registry_agent(main._read_agent_registry(), worker_id)  # noqa: SLF001
    if resolved is None:
        raise RebaseError(f"worker {worker_id!r} is not registered")
    _key, _entry, current = resolved
    raw_worktree = current.get("worktree") or current.get("cwd")
    if not isinstance(raw_worktree, str) or not raw_worktree.strip():
        raise RebaseError(f"worker {worker_id!r} has no worktree")
    worktree = Path(raw_worktree).resolve()
    _validate_pr_binding(worktree, verdict)
    orchestrator = current.get("orch") if isinstance(current.get("orch"), str) else None
    prompt = helper_prompt(ticket=ticket, pr_number=pr_number, worker_id=worker_id)
    job, owner, resumed = _existing_or_new_job(
        pr_number,
        expected_sha,
        ticket,
        worker_id,
        worktree,
        prompt,
        verdict,
        orchestrator,
        helper,
    )
    if not owner:
        if not job.done.wait(timeout=0 if job.result is not None else 0):
            return {
                "status": "running",
                "source": "rebase-bot",
                "job_id": job.job_id,
                "no_op": True,
                "joined": True,
            }
        return {**(job.result or {}), "no_op": True, "joined": True}
    if helper is not None:
        # Synchronous hooks keep deterministic callers useful.  Production
        # requests use the worker thread below and return before the 15-second
        # client timeout.
        return _finish_rebase_job(
            job,
            worker_id=worker_id,
            orchestrator=orchestrator,
            steer=steer,
            notify=notify,
        )
    _start_rebase_thread(job, steer, notify)
    return {
        "status": "running" if resumed else "started",
        "source": "rebase-bot",
        "job_id": job.job_id,
        "prompt": prompt,
    }


__all__ = [
    "RebaseError",
    "helper_prompt",
    "rebase_dirty_pr",
    "resolve_conflict_file",
    "resume_pending_jobs",
    "run_rebase_helper",
]
