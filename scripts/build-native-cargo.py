#!/usr/bin/env python3
"""Serialize the shared Cargo target's native build and bundle copy."""

from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import subprocess
from pathlib import Path


def build_and_copy(stage_src: Path, cargo_target: Path, staged_bundle: Path) -> None:
    """Build under a shared-target lock, then copy its bundle before unlock."""

    cargo_target.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = cargo_target / ".build.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        env = os.environ.copy()
        env["CARGO_TARGET_DIR"] = str(cargo_target)
        subprocess.run(
            ["cargo", "tauri", "build", "--bundles", "app"],
            cwd=stage_src,
            env=env,
            check=True,
        )

        shared_bundle = cargo_target / "release/bundle/macos/Wiki.app"
        if not shared_bundle.is_dir():
            raise FileNotFoundError(f"Tauri build did not produce {shared_bundle}")
        staged_bundle.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copytree(shared_bundle, staged_bundle, symlinks=True)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage_src", type=Path)
    parser.add_argument("cargo_target", type=Path)
    parser.add_argument("staged_bundle", type=Path)
    args = parser.parse_args()
    build_and_copy(args.stage_src, args.cargo_target, args.staged_bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
