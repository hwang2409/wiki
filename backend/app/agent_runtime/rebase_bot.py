"""PR conflict detection and safe, mechanical rebase assistance.

The public operation only starts a short-lived helper when GitHub says that a
PR is conflicting.  The helper prompt is deliberately narrow; the
``run_rebase_helper`` function is also kept deterministic so it can be used by
the helper worker and by fixture tests without involving a model.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
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
    tracking_ref = tracking.rsplit("/", 1)[-1] if tracking else None
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
        return "".join(line.split())

    return [normalize(line) for line in ours] == [normalize(line) for line in theirs]


def _is_unrelated_additions(ours: Sequence[str], theirs: Sequence[str]) -> bool:
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
    return all(
        line.startswith(("#", "//", "/*", "*", "const ", "let ", "var "))
        or bool(re.match(r"^[A-Za-z_][A-Za-z0-9_ .-]*\s*=", line))
        for line in significant
    )


def _parse_conflicts(text: str) -> tuple[list[tuple[list[str], list[str]]], bool]:
    lines = text.splitlines(keepends=True)
    hunks: list[tuple[list[str], list[str]]] = []
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
        while index < len(lines) and not _CONFLICT_MID.match(
            lines[index].rstrip("\r\n")
        ):
            if _CONFLICT_START.match(lines[index].rstrip("\r\n")):
                raise RebaseError("nested conflict marker")
            ours.append(lines[index])
            index += 1
        if index >= len(lines):
            raise RebaseError("incomplete conflict hunk")
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
        hunks.append((ours, theirs))
        output.append("\n")
    return hunks, found


def _mechanical_resolution(
    ours: Sequence[str], theirs: Sequence[str]
) -> list[str] | None:
    def clean(line: str) -> str:
        return line.rstrip(" \t\r\n") + "\n"

    if _is_whitespace_only(ours, theirs):
        return [clean(line) for line in ours]
    if _is_import_block(ours, theirs):
        unique = {line.strip(): line for line in [*ours, *theirs]}
        return [clean(unique[key]) for key in sorted(unique)]
    if _is_unrelated_additions(ours, theirs):
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
    for ours, theirs in hunks:
        resolution = _mechanical_resolution(ours, theirs)
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


def run_rebase_helper(
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
    result = dict((helper or run_rebase_helper)(worktree))
    if result.get("status") in {"resolved", "escalated"}:
        if steer is not None:
            steer(orchestrator or worker_id, result)
        else:
            _escalate_to_orchestrator(worker_id, orchestrator, result)
    return result


__all__ = [
    "RebaseError",
    "helper_prompt",
    "rebase_dirty_pr",
    "resolve_conflict_file",
    "run_rebase_helper",
]
