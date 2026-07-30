"""Lockfile regeneration commands for the mechanical rebase helper."""

from __future__ import annotations

import subprocess
from pathlib import Path


LOCKFILES = {
    "pnpm-lock.yaml",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "uv.lock",
    "Cargo.lock",
}


def _lockfile_commands(worktree: Path, filename: str) -> list[list[str]] | None:
    basename = Path(filename).name
    if basename == "package-lock.json":
        return [["npm", "install", "--package-lock-only", "--ignore-scripts"]]
    if basename == "npm-shrinkwrap.json":
        # `npm install --package-lock-only` produces package-lock.json; then
        # `npm shrinkwrap` renames it to npm-shrinkwrap.json.  The two-step
        # form is the only way to end up with the requested filename.
        return [
            ["npm", "install", "--package-lock-only", "--ignore-scripts"],
            ["npm", "shrinkwrap"],
        ]
    if basename == "pnpm-lock.yaml":
        return [["pnpm", "install", "--lockfile-only", "--ignore-scripts"]]
    if basename == "yarn.lock":
        return [["yarn", "install", "--mode=skip-builds"]]
    if basename == "uv.lock":
        return [["uv", "lock"]]
    if basename == "Cargo.lock":
        return [["cargo", "generate-lockfile"]]
    return None


def _lockfile_command(worktree: Path, filename: str) -> list[str] | None:
    """Compat shim: the first step of the requested lockfile's regen pipeline."""

    commands = _lockfile_commands(worktree, filename)
    return commands[0] if commands else None


def _regenerate_lockfile(worktree: Path, filename: str) -> str | None:
    commands = _lockfile_commands(worktree, filename)
    if commands is None:
        return f"no lockfile generator configured for {filename}"
    lockfile_dir = (worktree / filename).parent
    for command in commands:
        try:
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
    if not (worktree / filename).exists():
        return (
            f"{filename} was not produced by regeneration; refusing to stage a"
            " different lockfile"
        )
    return None
