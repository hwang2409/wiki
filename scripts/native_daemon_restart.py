"""Restart the installed Wiki LaunchAgent after a native bundle swap."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app import daemon as backend_daemon

def _daemon_is_installed(
    runtime_dir: Path,
    *,
    launchctl: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    config = backend_daemon.config_from_env(
        overrides={"WIKI_AGENT_RUNTIME_DIR": str(runtime_dir)}
    )
    target = config.target
    try:
        result = launchctl(
            ["launchctl", "print", target],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        result = None
    plist = config.plist_path
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


def restart_daemon_in_process(
    live_bundle: Path,
    runtime_dir: Path,
    repo_root: Path,
) -> bool:
    """Restart the daemon while the caller owns the daemon transaction lock."""

    if not _daemon_is_installed(runtime_dir):
        return False
    config = backend_daemon.config_from_env(
        overrides={
            "WIKI_APP_PATH": str(live_bundle),
            "WIKI_AGENT_RUNTIME_DIR": str(runtime_dir),
            "WIKI_REPO_DIR": str(repo_root),
        }
    )
    backend_daemon.install(config, transaction_lock_held=True)
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
