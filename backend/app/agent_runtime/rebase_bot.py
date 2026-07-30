"""PR conflict detection and safe, mechanical rebase assistance.

The public operation only starts a short-lived helper when GitHub says that a
PR is conflicting.  The helper prompt is deliberately narrow; the
``run_rebase_helper`` function is also kept deterministic so it can be used by
the helper worker and by fixture tests without involving a model.

Focused submodules keep this file to the orchestration layer:

* ``rebase_parsing`` — conflict-marker parsing and safe line-ending resolution
* ``rebase_lockfiles`` — per-lockfile regeneration pipeline
* ``rebase_durable`` — durable job/outbox state and delivery-dedupe
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .rebase_durable import (
    NotificationSender,
    _DURABLE_JOBS,
    _RebaseJob,
    _durable_key,
    _flush_outbox,
    _load_durable_state,
    _persist_completion,
    _persist_job,
    _prune_durable_jobs as _prune_durable_jobs_impl,
    _result_message,
)
from .rebase_lockfiles import (
    LOCKFILES,
    _regenerate_lockfile,
)
from .rebase_parsing import (
    RebaseError,
    resolve_conflict_file,
)


_REPO_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_GITHUB_URL_PATTERNS = (
    re.compile(
        r"^https://github\.com/"
        r"([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9][A-Za-z0-9._-]*)$"
    ),
    re.compile(
        r"^git@github\.com:"
        r"([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9][A-Za-z0-9._-]*)$"
    ),
    re.compile(
        r"^ssh://git@github\.com/"
        r"([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9][A-Za-z0-9._-]*)$"
    ),
)


_JOB_LOCK = threading.RLock()
_JOBS: dict[tuple[Any, ...], _RebaseJob] = {}
_WORKTREE_LOCKS: dict[str, threading.RLock] = {}
_NOTIFIED_RESULTS: set[str] = set()
_ACTIVE_SHA: dict[int, str] = {}


def _prune_durable_jobs() -> None:
    def clear(pr_number: int, expected_sha: str) -> None:
        _JOBS.pop((pr_number, expected_sha, "production"), None)

    _prune_durable_jobs_impl(clear)


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
    """Return ``org/repo`` for a supported GitHub URL or bare slug, else ``None``.

    Each URL form matches an anchored regex so ``http://`` cannot pretend to
    be ``https://``, extra path segments cannot smuggle through, and only
    the exact host ``github.com`` (not ``github.com.evil``) is accepted.
    Bare ``owner/name`` slugs from the GitHub gate JSON stay allowed.
    """

    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    stripped = raw.removesuffix(".git")
    for pattern in _GITHUB_URL_PATTERNS:
        match = pattern.fullmatch(stripped)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    if _REPO_SLUG.fullmatch(stripped):
        return stripped
    return None


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


def _default_smoke_commands(worktree: Path) -> list[tuple[list[str], Path]]:
    import json as _json

    commands: list[tuple[list[str], Path]] = []
    if (worktree / "backend" / "tests").is_dir():
        commands.append(
            ([sys.executable, "-m", "pytest", "-q", "backend/tests"], worktree)
        )
    package = worktree / "frontend" / "package.json"
    if package.is_file():
        try:
            data = _json.loads(package.read_text(encoding="utf-8"))
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


def _unmerged_stages(worktree: Path, filename: str) -> set[int]:
    """Return the set of index stages Git recorded for a conflicting file."""

    result = _git(worktree, ["ls-files", "--unmerged", "--", filename], timeout=15)
    if result.returncode != 0:
        return set()
    stages: set[int] = set()
    for line in result.stdout.splitlines():
        # Format: <mode> <sha> <stage>\t<file>
        parts = line.split("\t", 1)
        if not parts:
            continue
        head = parts[0].split()
        if len(head) >= 3:
            try:
                stages.add(int(head[2]))
            except ValueError:
                continue
    return stages


def _file_is_binary(worktree: Path, filename: str) -> bool:
    """Detect a binary conflict via the .gitattributes attr and NUL bytes."""

    attr = _git(worktree, ["check-attr", "binary", "--", filename], timeout=15)
    if attr.returncode == 0 and attr.stdout.strip().endswith(": binary: set"):
        return True
    try:
        chunk = (worktree / filename).read_bytes()[:8192]
    except OSError:
        return False
    return b"\x00" in chunk


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


def _mark_active(pr_number: int, expected_sha: str) -> None:
    with _JOB_LOCK:
        _ACTIVE_SHA[pr_number] = expected_sha


def _is_superseded(pr_number: int, expected_sha: str) -> bool:
    """True if a newer generation for this PR has been claimed elsewhere."""

    with _JOB_LOCK:
        current = _ACTIVE_SHA.get(pr_number)
    return current is not None and current != expected_sha


def _run_rebase_helper_unlocked(
    worktree: str | Path,
    *,
    smoke_commands: Sequence[tuple[Sequence[str], Path]] | None = None,
    push: bool = True,
    supersede_check: Callable[[], bool] | None = None,
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
            stages = _unmerged_stages(root, filename)
            if stages and stages != {1, 2, 3} and stages != {2, 3} and stages != {1, 2} and stages != {1, 3}:
                escalated.append(f"{filename}: unexpected index stages {sorted(stages)}")
                continue
            if _file_is_binary(root, filename):
                escalated.append(f"{filename}: binary conflict")
                continue
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
        if supersede_check is not None and supersede_check():
            return _escalated_result(
                initial_sha,
                resolved_files,
                ["superseded by newer HEAD generation for this PR"],
            )
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
            restored = _git(root, ["reset", "--hard", initial_sha], timeout=60)
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
    supersede_check: Callable[[], bool] | None = None,
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
            supersede_check=supersede_check,
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
    supersede_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run one serialized rebase operation in the target worktree."""

    root = Path(worktree).resolve()
    with _worktree_lock(root):
        _preflight_worktree(root, expected_sha)
        return _run_rebase_helper_checked(
            root,
            smoke_commands=smoke_commands,
            push=push,
            supersede_check=supersede_check,
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
                result = dict(
                    _run_rebase_helper_checked(
                        job.worktree,
                        supersede_check=lambda: _is_superseded(
                            job.pr_number, job.expected_sha
                        ),
                    )
                )
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
            # Restore to the SHA the gate claimed we started from, not the
            # sha we currently sit on: if the crash moved HEAD, using the
            # current sha would leave the worktree at the moved position.
            try:
                cleanup_errors = _cleanup_failed_rebase(
                    job.worktree, job.expected_sha or None
                )
                result["escalated_hunks"].extend(cleanup_errors)
            except Exception as cleanup_exc:
                result["escalated_hunks"].append(
                    f"rebase cleanup failed: {cleanup_exc}"
                )
    result["job_id"] = job.job_id
    job.result = result
    job.completed_at = time.time()
    try:
        _persist_completion(job, result)
        if job.durable:
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
                _mark_active(pr_number, expected_sha)
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
        if job.durable:
            _mark_active(pr_number, expected_sha)
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
            _mark_active(pr_number, expected_sha)
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
