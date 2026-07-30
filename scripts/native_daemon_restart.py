"""Restart the installed Wiki LaunchAgent after a native bundle swap."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable


DEFAULT_LABEL = "com.hwang2409.wiki.backend"


def _launch_agents_dir() -> Path:
    return Path(
        os.environ.get("WIKI_LAUNCH_AGENTS_DIR")
        or Path.home() / "Library" / "LaunchAgents"
    ).expanduser()


def _daemon_is_installed(
    runtime_dir: Path,
    *,
    launchctl: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    target = f"gui/{os.getuid()}/{DEFAULT_LABEL}"
    try:
        result = launchctl(
            ["launchctl", "print", target],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        result = None
    plist = _launch_agents_dir() / f"{DEFAULT_LABEL}.plist"
    return (result is not None and result.returncode == 0) or plist.is_file()


def restart_daemon_if_installed(
    live_bundle: Path,
    runtime_dir: Path,
    repo_root: Path,
    *,
    launchctl: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    """Reload the installed daemon so it runs the new bundle executable."""

    if not _daemon_is_installed(runtime_dir, launchctl=launchctl):
        return False
    environment = os.environ.copy()
    environment["WIKI_APP_PATH"] = str(live_bundle)
    environment["WIKI_AGENT_RUNTIME_DIR"] = str(runtime_dir)
    result = command(
        [
            sys.executable,
            str(repo_root / "wiki"),
            "daemon",
            "install",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "daemon restart failed").strip()
        raise RuntimeError(detail)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("live_bundle", type=Path)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    args = parser.parse_args()
    restart_daemon_if_installed(args.live_bundle, args.runtime_dir, args.repo_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
