from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

import uvicorn


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wiki native backend sidecar")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--repo-dir")
    parser.add_argument("--vault-dir")
    parser.add_argument("--frontend-dist")
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--log-path")
    return parser.parse_args()


def bundled_frontend_dist() -> Path | None:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    candidate = base / "frontend_dist"
    if (candidate / "index.html").is_file():
        return candidate
    return None


def configure_environment(args: argparse.Namespace) -> None:
    client_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    os.environ["WIKI_BACKEND_PORT"] = str(args.port)
    os.environ["WIKI_BACKEND_URL"] = f"http://{client_host}:{args.port}"
    if args.repo_dir:
        os.environ["WIKI_REPO_DIR"] = args.repo_dir
    if args.vault_dir:
        os.environ["WIKI_VAULT_DIR"] = args.vault_dir
    if args.daemon:
        os.environ["WIKI_BACKEND_DAEMON"] = "launchd"

    frontend_dist = args.frontend_dist or os.environ.get("WIKI_FRONTEND_DIST")
    if frontend_dist:
        os.environ["WIKI_FRONTEND_DIST"] = frontend_dist
        return

    bundled = bundled_frontend_dist()
    if bundled is not None:
        os.environ["WIKI_FRONTEND_DIST"] = str(bundled)


def rotate_log_file(path: Path, *, max_bytes: int = 10 * 1024 * 1024, backups: int = 5) -> None:
    """Rotate the daemon log before launchd reconnects its standard streams."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    for index in range(backups, 0, -1):
        source = path.with_name(f"{path.name}.{index - 1}" if index > 1 else path.name)
        destination = path.with_name(f"{path.name}.{index}")
        if source.exists():
            source.replace(destination)


def configure_daemon_log(path: Path) -> None:
    rotate_log_file(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.dup2(descriptor, sys.stdout.fileno())
        os.dup2(descriptor, sys.stderr.fileno())
    finally:
        if descriptor > 2:
            os.close(descriptor)


def parent_is_alive(parent_pid: int) -> bool:
    if parent_pid <= 1:
        return False
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def start_parent_watchdog(server: uvicorn.Server, parent_pid: int | None) -> None:
    if not parent_pid:
        return

    def watch() -> None:
        while not server.should_exit:
            time.sleep(0.5)
            if parent_is_alive(parent_pid):
                continue
            sys.stderr.write(f"parent {parent_pid} exited; stopping wiki-backend\n")
            sys.stderr.flush()
            server.should_exit = True
            time.sleep(3)
            server.force_exit = True
            sys.stderr.write("parent watchdog forcing wiki-backend exit\n")
            sys.stderr.flush()
            time.sleep(1)
            os._exit(1)

    threading.Thread(target=watch, name="wiki-parent-watchdog", daemon=True).start()


def main() -> None:
    # Frozen terminal sessions re-exec this binary with --terminal-child;
    # dispatch before the backend argument parser sees the child arguments.
    if "--terminal-child" in sys.argv[1:]:
        flag_index = sys.argv.index("--terminal-child")
        sys.argv = [sys.argv[0], *sys.argv[flag_index + 1 :]]
        from backend.app.terminal_child import main as terminal_child_main

        terminal_child_main()
        return
    if "--wiki-artifacts-mcp" in sys.argv[1:]:
        from backend.app.wiki_artifacts import main as artifacts_main

        artifacts_main()
        return
    # The frozen supervisor autostart (agent_runtime/client.py) re-execs this
    # binary with --supervisor; route to the daemon before backend argparse.
    if "--supervisor" in sys.argv[1:]:
        sys.argv.remove("--supervisor")
        from backend.app.agent_runtime.daemon import main as supervisor_main

        supervisor_main()
        return

    args = parse_args()
    configure_environment(args)
    if args.daemon and args.log_path:
        configure_daemon_log(Path(args.log_path).expanduser().absolute())

    from backend.app.main import app, write_wiki_app_secret_file

    if args.daemon:
        # The native app reads this owner-only file after the private daemon
        # health probe. The secret never enters launchd stdout or stderr.
        write_wiki_app_secret_file()

    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        reload=False,
        workers=1,
        log_level="info",
    )
    server = uvicorn.Server(config)
    start_parent_watchdog(server, args.parent_pid)
    server.run()


if __name__ == "__main__":
    main()
