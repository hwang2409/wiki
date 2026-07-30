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
import shutil
import subprocess
import sys
import tempfile
import threading
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
_CONFLICT_END = re.compile(r"^>>>>>>>.*$")
_IMPORT = re.compile(
    r"^\s*(?:from\s+[^;]+\s+import\s+|import\s+|const\s+.+\s*=\s*require\(|require\(|#\s*import\b)"
)
_SEMANTIC = re.compile(
    r"^\s*(?:return\b|yield\b|raise\b|def\b|class\b|function\b|if\b|elif\b|else\b|for\b|while\b|try\b|except\b|switch\b|case\b|throw\b|await\b|async\b)"
)


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


_JOB_LOCK = threading.RLock()
_JOBS: dict[tuple[str, str], _RebaseJob] = {}
_WORKTREE_LOCKS: dict[str, threading.Lock] = {}


def _job_key(
    worktree: Path, helper: Callable[[Path], Mapping[str, Any]] | None
) -> tuple[str, str]:
    # Injected helpers are test and embedding hooks.  Separate their jobs so
    # one fixture cannot consume another fixture's completed result.
    return (str(worktree), str(id(helper)) if helper is not None else "production")


def _thread_lock(worktree: Path) -> threading.Lock:
    key = str(worktree)
    with _JOB_LOCK:
        return _WORKTREE_LOCKS.setdefault(key, threading.Lock())


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


def _normal_lines(lines: Sequence[str]) -> list[str]:
    return [line.strip() for line in lines if line.strip()]


def _is_import_block(ours: Sequence[str], theirs: Sequence[str]) -> bool:
    significant = _normal_lines([*ours, *theirs])
    return bool(significant) and all(_IMPORT.match(line) for line in significant)


def _is_whitespace_only(ours: Sequence[str], theirs: Sequence[str]) -> bool:
    def normalize(line: str) -> str:
        # Keep spaces inside literals and between tokens.  Only line endings
        # and whitespace at the end of each line are formatting noise.
        line = line.replace("\r\n", "\n").replace("\r", "\n")
        return line.rstrip(" \t\n")

    return [normalize(line) for line in ours] == [normalize(line) for line in theirs]


def _declaration_key(line: str) -> str:
    assignment = re.match(
        r"^(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=|^([A-Za-z_][A-Za-z0-9_]*)\s*=",
        line,
    )
    if assignment:
        return next(group for group in assignment.groups() if group is not None)
    return line


def _added_lines(lines: Sequence[str], base: Sequence[str]) -> list[str]:
    remaining = list(_normal_lines(base))
    added: list[str] = []
    for line in _normal_lines(lines):
        if line in remaining:
            remaining.remove(line)
        else:
            added.append(line)
    return added


def _is_unrelated_additions(
    ours: Sequence[str], theirs: Sequence[str], base: Sequence[str] | None = None
) -> bool:
    # A two-way conflict cannot prove that two declarations are independent.
    # The diff3 base stage is required for this resolution.
    if base is None:
        return False
    significant = _normal_lines([*ours, *theirs])
    if not significant or any(_SEMANTIC.match(line) for line in significant):
        return False
    # Indented assignments/declarations are normally edits inside a function
    # body, where retaining both branches is a semantic change.
    if any(
        line[:1].isspace() and not line.lstrip().startswith(("#", "//", "*"))
        for line in [*ours, *theirs]
        if line.strip()
    ):
        return False
    # Comments, imports, and simple declarations are safe to retain together
    # when both sides added distinct lines to the same conflict region.  The
    # semantic keyword guard above intentionally rejects function-body edits.
    if not all(
        line.startswith(("#", "//", "/*", "*", "const ", "let ", "var "))
        or bool(re.match(r"^[A-Za-z_][A-Za-z0-9_ .-]*\s*=", line))
        for line in significant
    ):
        return False
    ours_added = _added_lines(ours, base)
    theirs_added = _added_lines(theirs, base)
    if not ours_added or not theirs_added:
        return False
    return set(map(_declaration_key, ours_added)).isdisjoint(
        map(_declaration_key, theirs_added)
    )


def _parse_conflicts(
    text: str,
) -> tuple[list[tuple[list[str], list[str] | None, list[str]]], bool]:
    lines = text.splitlines(keepends=True)
    hunks: list[tuple[list[str], list[str] | None, list[str]]] = []
    output: list[str] = []
    index = 0
    found = False
    while index < len(lines):
        if not _CONFLICT_START.match(lines[index].rstrip("\r\n")):
            output.append(lines[index])
            index += 1
            continue
        found = True
        index += 1
        ours: list[str] = []
        while (
            index < len(lines)
            and not _CONFLICT_MID.match(lines[index].rstrip("\r\n"))
            and not lines[index].startswith("|||||||")
        ):
            if _CONFLICT_START.match(lines[index].rstrip("\r\n")):
                raise RebaseError("nested conflict marker")
            ours.append(lines[index])
            index += 1
        if index >= len(lines):
            raise RebaseError("incomplete conflict hunk")
        base: list[str] | None = None
        if lines[index].startswith("|||||||"):
            index += 1
            base = []
            while index < len(lines) and not _CONFLICT_MID.match(
                lines[index].rstrip("\r\n")
            ):
                base.append(lines[index])
                index += 1
            if index >= len(lines):
                raise RebaseError("incomplete diff3 conflict hunk")
        index += 1
        theirs: list[str] = []
        while index < len(lines) and not _CONFLICT_END.match(
            lines[index].rstrip("\r\n")
        ):
            theirs.append(lines[index])
            index += 1
        if index >= len(lines):
            raise RebaseError("incomplete conflict hunk")
        index += 1
        hunks.append((ours, base, theirs))
        output.append("\n")
    return hunks, found


def _mechanical_resolution(
    ours: Sequence[str], theirs: Sequence[str], base: Sequence[str] | None = None
) -> list[str] | None:
    def clean(line: str) -> str:
        return line.rstrip(" \t\r\n") + "\n"

    if _is_whitespace_only(ours, theirs):
        return [clean(line) for line in ours]
    if _is_import_block(ours, theirs):
        unique = {line.strip(): line for line in [*ours, *theirs]}
        return [clean(unique[key]) for key in sorted(unique)]
    if _is_unrelated_additions(ours, theirs, base):
        unique: dict[str, str] = {}
        for line in [*ours, *theirs]:
            unique.setdefault(line.strip(), line)
        return [clean(line) for line in unique.values()]
    return None


def resolve_conflict_file(path: Path) -> tuple[bool, str | None]:
    """Resolve one file if every conflict hunk is mechanical.

    Returns ``(resolved, summary)``.  A false result never writes the file.
    """

    raw = path.read_text(encoding="utf-8", errors="surrogateescape")
    hunks, found = _parse_conflicts(raw)
    if not found:
        return True, None

    resolved_hunks: list[list[str]] = []
    for ours, base, theirs in hunks:
        resolution = _mechanical_resolution(ours, theirs, base)
        if resolution is None:
            excerpt = "".join(
                ["<<<<<<< ours\n", *ours, "=======\n", *theirs, ">>>>>>> theirs\n"]
            )
            return False, excerpt.strip()[:1200]
        resolved_hunks.append(resolution)

    lines = raw.splitlines(keepends=True)
    output: list[str] = []
    hunk_index = 0
    index = 0
    while index < len(lines):
        if not _CONFLICT_START.match(lines[index].rstrip("\r\n")):
            output.append(lines[index])
            index += 1
            continue
        index += 1
        while index < len(lines) and not _CONFLICT_MID.match(
            lines[index].rstrip("\r\n")
        ):
            index += 1
        index += 1
        while index < len(lines) and not _CONFLICT_END.match(
            lines[index].rstrip("\r\n")
        ):
            index += 1
        index += 1
        output.extend(resolved_hunks[hunk_index])
        hunk_index += 1
    normalized = "".join(output).replace("\r\n", "\n").replace("\r", "\n")
    path.write_text(normalized, encoding="utf-8", errors="surrogateescape", newline="")
    return True, None


def _lockfile_command(worktree: Path, filename: str) -> list[str] | None:
    if filename == "package-lock.json" or filename == "npm-shrinkwrap.json":
        return ["npm", "install", "--package-lock-only", "--ignore-scripts"]
    if filename == "pnpm-lock.yaml":
        return ["pnpm", "install", "--lockfile-only", "--ignore-scripts"]
    if filename == "yarn.lock":
        return ["yarn", "install", "--mode=skip-builds"]
    if filename == "uv.lock":
        return ["uv", "lock"]
    if filename == "Cargo.lock":
        return ["cargo", "generate-lockfile"]
    return None


def _regenerate_lockfile(worktree: Path, filename: str) -> str | None:
    command = _lockfile_command(worktree, filename)
    if command is None:
        return f"no lockfile generator configured for {filename}"
    try:
        result = subprocess.run(
            command,
            cwd=str(worktree),
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


def _run_formatters(worktree: Path, files: Sequence[str]) -> str | None:
    """Run repository formatters before the merge commit is created."""

    python_files = [
        filename for filename in files if filename.endswith((".py", ".pyi"))
    ]
    if python_files:
        ruff = shutil.which("ruff")
        if ruff is None:
            return "ruff formatter unavailable; escalating instead of merging unformatted files"
        result = subprocess.run(
            [ruff, "format", *python_files],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if result.returncode != 0:
            return (
                f"ruff format failed: {(result.stderr or result.stdout).strip()[:600]}"
            )
    return None


def _conflict_files(worktree: Path) -> list[str]:
    result = _git(worktree, ["diff", "--name-only", "--diff-filter=U"], timeout=15)
    if result.returncode != 0:
        raise RebaseError(
            (result.stderr or result.stdout or "could not list conflicts").strip()
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _abort_merge(worktree: Path) -> None:
    _git(worktree, ["merge", "--abort"], timeout=30)


def _restore_head(worktree: Path, initial_sha: str | None) -> None:
    if initial_sha and _head_sha(worktree) != initial_sha:
        # The helper owns this dedicated worker worktree.  Rolling back a
        # failed post-merge smoke run keeps the branch clean and ensures a
        # later manual retry starts from the original PR head.
        _git(worktree, ["reset", "--merge", initial_sha], timeout=60)


def _run_rebase_helper_unlocked(
    worktree: str | Path,
    *,
    smoke_commands: Sequence[tuple[Sequence[str], Path]] | None = None,
    push: bool = True,
    formatter: Callable[[Path, Sequence[str]], str | None] | None = None,
) -> dict[str, Any]:
    """Fetch, merge, mechanically resolve, smoke-test, and push.

    Semantic conflicts and failed smoke tests abort the merge and never push.
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
            diff3 = _git(
                root, ["checkout", "--conflict=diff3", "--", filename], timeout=30
            )
            if diff3.returncode != 0:
                escalated.append(
                    f"{filename}: could not read merge-base stage for conflict proof"
                )
                continue
            ok, detail = resolve_conflict_file(path)
            if ok:
                resolved_files.append(filename)
            else:
                escalated.append(f"{filename}: {detail or 'semantic conflict'}")
        if not escalated:
            for filename in lockfiles:
                # Package managers need a parseable source file.  The lockfile
                # itself is disposable because it is regenerated from the
                # manifest/source-of-truth immediately below.
                checked_out = _git(
                    root, ["checkout", "--ours", "--", filename], timeout=30
                )
                if checked_out.returncode != 0:
                    escalated.append(f"could not stage {filename} for regeneration")
                    continue
                error = _regenerate_lockfile(root, filename)
                if error:
                    escalated.append(error)
                else:
                    resolved_files.append(filename)
        if not escalated:
            format_error = (formatter or _run_formatters)(root, files)
            if format_error:
                _abort_merge(root)
                return _escalated_result(initial_sha, resolved_files, [format_error])
        if escalated:
            _abort_merge(root)
            return _escalated_result(initial_sha, resolved_files, escalated)
        added = _git(root, ["add", "--", *files], timeout=30)
        if added.returncode != 0:
            _abort_merge(root)
            return _escalated_result(
                initial_sha,
                resolved_files,
                [f"git add failed: {(added.stderr or added.stdout).strip()[:600]}"],
            )
        remaining = _conflict_files(root)
        if remaining:
            _abort_merge(root)
            return _escalated_result(
                initial_sha,
                resolved_files,
                [f"unresolved conflict: {name}" for name in remaining],
            )
        finish = _git(root, ["commit", "--no-edit"], timeout=60)
        if finish.returncode != 0:
            _abort_merge(root)
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
            _restore_head(root, initial_sha)
            return _escalated_result(
                initial_sha,
                resolved_files,
                [f"smoke test {' '.join(command)} failed: {exc}"],
            )
        if smoke.returncode != 0:
            detail = (smoke.stderr or smoke.stdout or "smoke test failed").strip()
            _restore_head(root, initial_sha)
            return _escalated_result(
                initial_sha,
                resolved_files,
                [f"smoke test {' '.join(command)} failed: {detail[:800]}"],
            )

    if push:
        pushed = _git(root, ["push", "origin", "HEAD"], timeout=180)
        if pushed.returncode != 0:
            _restore_head(root, initial_sha)
            return _escalated_result(
                _head_sha(root),
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
    formatter: Callable[[Path, Sequence[str]], str | None] | None = None,
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
            formatter=formatter,
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
    formatter: Callable[[Path, Sequence[str]], str | None] | None = None,
) -> dict[str, Any]:
    """Run one serialized rebase operation in the target worktree."""

    root = Path(worktree).resolve()
    with _worktree_lock(root):
        return _run_rebase_helper_checked(
            root,
            smoke_commands=smoke_commands,
            push=push,
            formatter=formatter,
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
resolve only mechanical conflicts: import ordering, whitespace/line endings,
lockfiles regenerated from package.json/pyproject.toml, and unrelated adjacent
line additions where both sides can be retained. both branches changing the
same function or incompatible logic is semantic: abort the merge, do not push,
and report each conflicting file and hunk to the orchestrator.

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
    worker_id: str, orchestrator: str | None, result: Mapping[str, Any]
) -> None:
    if not orchestrator:
        return
    main = _main()
    status = result.get("status")
    if status == "resolved":
        resolved_files = ", ".join(
            str(item) for item in result.get("resolved_files", [])
        )
        message = f"rebase-bot resolved {worker_id}: " + (
            resolved_files or "rebase completed"
        )
    elif status == "escalated":
        message = f"rebase-bot escalated {worker_id}: " + "; ".join(
            str(item) for item in result.get("escalated_hunks", [])
        )
    else:
        return
    try:
        main.agent_message(
            orchestrator,
            main.MessageIn(text=message[:4000], mode="now", source="rebase-bot"),
            main.BackgroundTasks(),
        )
    except Exception:
        # The result remains in the API response; a transient steering failure
        # must not turn a safe, already-aborted rebase into a false success.
        return


def _finish_rebase_job(
    job: _RebaseJob,
    *,
    worker_id: str,
    orchestrator: str | None,
    steer: Callable[[str, Mapping[str, Any]], None] | None,
) -> dict[str, Any]:
    try:
        if job.helper is not None:
            with _worktree_lock(job.worktree):
                result = dict(job.helper(job.worktree))
        else:
            result = dict(run_rebase_helper(job.worktree))
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
    result["job_id"] = job.job_id
    with _JOB_LOCK:
        job.result = result
        job.done.set()
    if result.get("status") in {"resolved", "escalated"}:
        if steer is not None:
            steer(orchestrator or worker_id, result)
        else:
            _escalate_to_orchestrator(worker_id, orchestrator, result)
    return result


def _existing_or_new_job(
    worktree: Path,
    prompt: str,
    helper: Callable[[Path], Mapping[str, Any]] | None,
) -> tuple[_RebaseJob, bool]:
    key = _job_key(worktree, helper)
    with _JOB_LOCK:
        existing = _JOBS.get(key)
        if existing is not None:
            return existing, False
        job = _RebaseJob(
            job_id=hashlib.sha256(f"{key}:{id(prompt)}".encode()).hexdigest()[:16],
            worktree=worktree,
            prompt=prompt,
            done=threading.Event(),
            helper=helper,
        )
        _JOBS[key] = job
        return job, True


def rebase_dirty_pr(
    pr_number: int,
    ticket: str,
    worker_id: str,
    *,
    gate: Callable[[int], Mapping[str, Any]] | None = None,
    helper: Callable[[Path], Mapping[str, Any]] | None = None,
    steer: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Start or execute the safe rebase helper for one conflicting PR."""

    main = _main()
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
    job, owner = _existing_or_new_job(worktree, prompt, helper)
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
        )
    thread = threading.Thread(
        target=_finish_rebase_job,
        kwargs={
            "job": job,
            "worker_id": worker_id,
            "orchestrator": orchestrator,
            "steer": steer,
        },
        name=f"rebase-bot-{job.job_id}",
        daemon=True,
    )
    thread.start()
    return {
        "status": "started",
        "source": "rebase-bot",
        "job_id": job.job_id,
        "prompt": prompt,
    }


__all__ = [
    "RebaseError",
    "helper_prompt",
    "rebase_dirty_pr",
    "resolve_conflict_file",
    "run_rebase_helper",
]
