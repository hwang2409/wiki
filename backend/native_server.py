from __future__ import annotations

import argparse
from contextlib import nullcontext
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn


DAEMON_AUTH_SOCKET_NAME = "wiki-app-secret.sock"
DAEMON_LOG_MAX_BYTES = 10 * 1024 * 1024
DAEMON_LOG_BACKUPS = 5
_LOG_REDIRECT_LOCK = threading.Lock()


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


def rotate_log_file(
    path: Path, *, max_bytes: int = DAEMON_LOG_MAX_BYTES, backups: int = DAEMON_LOG_BACKUPS
) -> None:
    """Rotate the daemon log before launchd reconnects its standard streams."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    for index in range(backups, 0, -1):
        source = path.with_name(f"{path.name}.{index - 1}" if index > 1 else path.name)
        destination = path.with_name(f"{path.name}.{index}")
        if source.exists():
            source.replace(destination)


def _redirect_daemon_log(path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.dup2(descriptor, sys.stdout.fileno())
        os.dup2(descriptor, sys.stderr.fileno())
    finally:
        if descriptor > 2:
            os.close(descriptor)


def configure_daemon_log(path: Path) -> None:
    rotate_log_file(path)
    with _LOG_REDIRECT_LOCK:
        _redirect_daemon_log(path)


def rotate_daemon_log(
    path: Path,
    *,
    max_bytes: int = DAEMON_LOG_MAX_BYTES,
    backups: int = DAEMON_LOG_BACKUPS,
    reopen: bool = True,
) -> bool:
    with _LOG_REDIRECT_LOCK:
        if not path.exists() or path.stat().st_size < max_bytes:
            return False
        rotate_log_file(path, max_bytes=max_bytes, backups=backups)
        if reopen:
            _redirect_daemon_log(path)
        return True


def start_log_rotator(path: Path) -> tuple[threading.Event, threading.Thread]:
    stop = threading.Event()

    def watch() -> None:
        while not stop.wait(1.0):
            rotate_daemon_log(path)

    thread = threading.Thread(target=watch, name="wiki-daemon-log-rotator", daemon=True)
    thread.start()
    return stop, thread


class DaemonAuthSocket:
    def __init__(self, runtime_dir: Path, secret: str) -> None:
        self.path = runtime_dir / DAEMON_AUTH_SOCKET_NAME
        self.secret = secret.encode("utf-8") + b"\n"
        self.stop = threading.Event()
        self.listener: socket.socket | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        if self.path.exists():
            if not self.path.is_socket():
                raise RuntimeError(f"daemon auth path is not a socket: {self.path}")
            self.path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            listener.listen(4)
            listener.settimeout(0.25)
            self.listener = listener
            self.thread = threading.Thread(
                target=self._serve, name="wiki-daemon-auth", daemon=True
            )
            self.thread.start()
        except BaseException:
            listener.close()
            self.path.unlink(missing_ok=True)
            raise

    def _serve(self) -> None:
        listener = self.listener
        if listener is None:
            return
        while not self.stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                if not self.stop.is_set():
                    connection.sendall(self.secret)

    def close(self) -> None:
        self.stop.set()
        listener = self.listener
        if listener is not None:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
                    wake.connect(str(self.path))
            except OSError:
                pass
            listener.close()
        if self.thread is not None:
            self.thread.join(timeout=1.0)
        self.path.unlink(missing_ok=True)


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
    log_path = Path(args.log_path).expanduser().absolute() if args.log_path else None
    if args.daemon and log_path:
        configure_daemon_log(log_path)

    from backend.app.main import app, wiki_app_secret

    runtime_dir = Path(
        os.environ.get("WIKI_AGENT_RUNTIME_DIR")
        or Path.home() / ".wiki" / "agent-runtime"
    )
    auth_socket = (
        DaemonAuthSocket(runtime_dir, wiki_app_secret())
        if args.daemon
        else None
    )
    log_rotator: tuple[threading.Event, threading.Thread] | None = None
    if auth_socket is not None:
        auth_socket.start()
    if args.daemon and log_path:
        log_rotator = start_log_rotator(log_path)

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

    def request_shutdown(_signum: int, _frame: object) -> None:
        server.should_exit = True

    if args.daemon:
        signal.signal(signal.SIGTERM, request_shutdown)
        signal.signal(signal.SIGINT, request_shutdown)
        # uvicorn otherwise replaces these handlers and re-raises SIGTERM
        # after shutdown, which can bypass the socket cleanup below.

        def no_signal_capture():
            return nullcontext()

        server.capture_signals = no_signal_capture
    try:
        server.run()
    finally:
        if log_rotator is not None:
            log_rotator[0].set()
            log_rotator[1].join(timeout=2.0)
        if auth_socket is not None:
            auth_socket.close()


if __name__ == "__main__":
    main()
