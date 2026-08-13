from __future__ import annotations

import argparse
import ctypes
import fcntl
import os
import platform
import plistlib
import re
import resource
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path

import uvicorn

from backend.app import native_trust

DAEMON_AUTH_SOCKET_NAME = "wiki-app-secret.sock"
DAEMON_AUTH_LOCK_NAME = "wiki-app-secret.lock"
TAURI_BUNDLE_IDENTIFIER = native_trust.TAURI_BUNDLE_IDENTIFIER
DEFAULT_TAURI_BUNDLE_PATH = Path("/Applications/Wiki.app")
SOURCE_TAURI_BUNDLE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src-tauri"
    / "target"
    / "release"
    / "bundle"
    / "macos"
    / "Wiki.app"
)
# Tests and packaged deployments can override this after import. The source
# bundle is the documented native-build output when no installed app exists.
TAURI_BUNDLE_PATH = Path(
    os.environ.get("WIKI_APP_PATH")
    or (
        SOURCE_TAURI_BUNDLE_PATH
        if SOURCE_TAURI_BUNDLE_PATH.is_dir()
        else DEFAULT_TAURI_BUNDLE_PATH
    )
)
DAEMON_LOG_MAX_BYTES = 10 * 1024 * 1024
DAEMON_LOG_BACKUPS = 5
_LOG_REDIRECT_LOCK = threading.Lock()


def raise_nofile_limit() -> tuple[int, int]:
    """Raise the soft file-descriptor limit and report the resulting limits."""

    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"unable to read RLIMIT_NOFILE: {exc}\n")
        sys.stderr.flush()
        return (-1, -1)

    if soft < hard:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"unable to raise RLIMIT_NOFILE: {exc}\n")
            sys.stderr.flush()
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"unable to read resulting RLIMIT_NOFILE: {exc}\n")
            sys.stderr.flush()
            return (-1, -1)

    sys.stderr.write(f"RLIMIT_NOFILE soft={soft} hard={hard}\n")
    sys.stderr.flush()
    return soft, hard


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

    if args.daemon and (args.frontend_dist or os.environ.get("WIKI_FRONTEND_DIST")):
        raise RuntimeError(
            "daemon mode does not accept external frontend overrides"
        )
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
    def __init__(
        self,
        runtime_dir: Path,
        secret: str,
        *,
        peer_checker: Callable[[socket.socket], bool] | None = None,
    ) -> None:
        self.path = runtime_dir / DAEMON_AUTH_SOCKET_NAME
        self.lock_path = runtime_dir / DAEMON_AUTH_LOCK_NAME
        self.secret = secret.encode("utf-8") + b"\n"
        self.peer_checker = peer_checker or is_trusted_tauri_peer
        self.stop = threading.Event()
        self.listener: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.lock_file = None
        self.bound_inode: int | None = None

    def start(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self.lock_file = self.lock_path.open("a+")
        self.lock_path.chmod(0o600)
        try:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock_file.close()
            self.lock_file = None
            raise RuntimeError("another Wiki daemon auth server is active") from exc
        if self.path.exists():
            if not self.path.is_socket():
                self._release_lock()
                raise RuntimeError(f"daemon auth path is not a socket: {self.path}")
            self.path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            self.bound_inode = self.path.stat().st_ino
            listener.listen(4)
            listener.settimeout(0.25)
            self.listener = listener
            self.thread = threading.Thread(
                target=self._serve, name="wiki-daemon-auth", daemon=True
            )
            self.thread.start()
        except BaseException:
            listener.close()
            self._unlink_bound_socket()
            self._release_lock()
            raise

    def _release_lock(self) -> None:
        if self.lock_file is None:
            return
        fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
        self.lock_file.close()
        self.lock_file = None

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
                try:
                    if not self.stop.is_set() and self.peer_checker(connection):
                        connection.sendall(self.secret)
                except Exception:
                    # A client can disappear while peer verification or sendall
                    # runs. Keep the listener alive for the next app handshake.
                    continue

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
        self._unlink_bound_socket()
        self._release_lock()

    def _unlink_bound_socket(self) -> None:
        if self.bound_inode is None:
            return
        try:
            if self.path.stat().st_ino == self.bound_inode:
                self.path.unlink()
        except FileNotFoundError:
            pass


def _peer_pid(connection: socket.socket) -> int | None:
    if platform.system() != "Darwin":
        return None
    try:
        raw_pid = connection.getsockopt(0, 0x002, 4)
    except OSError:
        return None
    return int.from_bytes(raw_pid, byteorder=sys.byteorder)


def _peer_executable(pid: int) -> Path | None:
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        libproc.proc_pidpath.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        libproc.proc_pidpath.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(4096)
        size = libproc.proc_pidpath(pid, buffer, ctypes.sizeof(buffer))
    except (OSError, AttributeError):
        return None
    if size <= 0:
        return None
    return Path(buffer.value.decode("utf-8"))


def _security_code_identity(
    pid: int,
) -> tuple[str, str | None, frozenset[str]] | None:
    """Read and validate the live process identity from Security.framework."""

    if platform.system() != "Darwin":
        return None
    try:
        security = ctypes.CDLL(
            "/System/Library/Frameworks/Security.framework/Security"
        )
        core_foundation = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        copy_guest_with_attributes = security.SecCodeCopyGuestWithAttributes
        copy_guest_with_attributes.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        copy_guest_with_attributes.restype = ctypes.c_int
        check_validity = security.SecCodeCheckValidity
        check_validity.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        check_validity.restype = ctypes.c_int
        copy_signing_information = security.SecCodeCopySigningInformation
        copy_signing_information.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        copy_signing_information.restype = ctypes.c_int
        dictionary_get_value = core_foundation.CFDictionaryGetValue
        dictionary_get_value.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        dictionary_get_value.restype = ctypes.c_void_p
        string_get_cstring = core_foundation.CFStringGetCString
        string_get_cstring.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        string_get_cstring.restype = ctypes.c_bool
        data_get_length = core_foundation.CFDataGetLength
        data_get_length.argtypes = [ctypes.c_void_p]
        data_get_length.restype = ctypes.c_long
        data_get_byte_ptr = core_foundation.CFDataGetBytePtr
        data_get_byte_ptr.argtypes = [ctypes.c_void_p]
        data_get_byte_ptr.restype = ctypes.POINTER(ctypes.c_ubyte)
        number_create = core_foundation.CFNumberCreate
        number_create.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        number_create.restype = ctypes.c_void_p
        dictionary_create = core_foundation.CFDictionaryCreate
        dictionary_create.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        dictionary_create.restype = ctypes.c_void_p
        release = core_foundation.CFRelease
        release.argtypes = [ctypes.c_void_p]
    except (AttributeError, OSError):
        return None

    pid_number = ctypes.c_int(pid)
    pid_value = number_create(None, 3, ctypes.byref(pid_number))
    if not pid_value:
        return None
    key = ctypes.c_void_p.in_dll(security, "kSecGuestAttributePid")
    keys = (ctypes.c_void_p * 1)(key)
    values = (ctypes.c_void_p * 1)(pid_value)
    attributes = dictionary_create(None, keys, values, 1, None, None)
    if not attributes:
        release(pid_value)
        return None
    code = ctypes.c_void_p()
    try:
        if (
            copy_guest_with_attributes(None, attributes, 0, ctypes.byref(code)) != 0
            or not code
            or check_validity(code, 0, None) != 0
        ):
            return None
        signing_information = ctypes.c_void_p()
        # kSecCSSigningInformation | kSecCSDynamicInformation.
        if (
            copy_signing_information(code, 0x02 | 0x08, ctypes.byref(signing_information))
            != 0
            or not signing_information
        ):
            return None
        try:
            def value_for(name: str) -> ctypes.c_void_p | None:
                try:
                    key = ctypes.c_void_p.in_dll(security, name)
                except ValueError:
                    return None
                value = dictionary_get_value(signing_information, key)
                return value or None

            def string_value(value: ctypes.c_void_p | None) -> str | None:
                if not value:
                    return None
                buffer = ctypes.create_string_buffer(512)
                if not string_get_cstring(value, buffer, 512, 0x08000100):
                    return None
                return buffer.value.decode("utf-8")

            def data_value(value: ctypes.c_void_p | None) -> str | None:
                if not value:
                    return None
                length = data_get_length(value)
                pointer = data_get_byte_ptr(value)
                if length <= 0 or not pointer:
                    return None
                return bytes(pointer[:length]).hex()

            identifier = string_value(value_for("kSecCodeInfoIdentifier"))
            if not identifier:
                return None
            team = string_value(value_for("kSecCodeInfoTeamIdentifier"))
            unique = data_value(value_for("kSecCodeInfoUnique"))
            if not unique:
                return None
            return identifier, team, frozenset({unique})
        finally:
            release(signing_information)
    finally:
        if code:
            release(code)
        release(attributes)
        release(pid_value)


def _codesign_details(executable: Path) -> list[str] | None:
    try:
        result = subprocess.run(
            ["/usr/bin/codesign", "-dvvv", str(executable)],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout + result.stderr).splitlines()


def _bundle_team_identifier(app_path: Path) -> str | None:
    details = _codesign_details(app_path)
    if not details or any(line == "Signature=adhoc" for line in details):
        return None
    team = next(
        (line.removeprefix("TeamIdentifier=") for line in details if line.startswith("TeamIdentifier=")),
        None,
    )
    if not team or team == "not set" or not any(line.startswith("Authority=") for line in details):
        return None
    return team


def _bundle_executable(app_path: Path) -> Path | None:
    try:
        with (app_path / "Contents" / "Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    name = info.get("CFBundleExecutable") if isinstance(info, dict) else None
    if not isinstance(name, str) or not name or Path(name).name != name:
        return None
    return app_path / "Contents" / "MacOS" / name


def _code_directory_identity(details: list[str]) -> str | None:
    for prefix in ("CDHashFull=", "CDHash="):
        value = next(
            (line.removeprefix(prefix) for line in details if line.startswith(prefix)),
            None,
        )
        if value:
            return value
    return None


def _code_directory_identities(details: list[str]) -> frozenset[str]:
    identities = {
        line.removeprefix(prefix)
        for prefix in ("CDHashFull=", "CDHash=")
        for line in details
        if line.startswith(prefix)
    }
    return frozenset(identities)


def _same_selected_executable(executable: Path, selected: Path) -> bool:
    try:
        return executable.resolve(strict=True) == selected.resolve(strict=True) and os.path.samefile(
            executable, selected
        )
    except OSError:
        return False


def _verify_signature(
    executable: Path,
    requirement: str | None = None,
    *,
    ignore_resources: bool = False,
) -> bool:
    arguments = ["/usr/bin/codesign", "--verify", "--strict"]
    if ignore_resources:
        arguments.append("--ignore-resources")
    if requirement is not None:
        arguments.extend(["--test-requirement", f"={requirement}"])
    arguments.append(str(executable))
    try:
        result = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0


def _verify_adhoc_identity(
    executable: Path,
    details: list[str],
    selected_executable: Path,
    selected_bundle: Path,
    live_identity: tuple[str, str | None, frozenset[str]],
) -> bool:
    """Verify the selected ad-hoc bundle, including unsigned launchers."""

    if not _same_selected_executable(executable, selected_executable):
        return False
    selected_details = _codesign_details(selected_bundle)
    if not selected_details or "Signature=adhoc" not in selected_details:
        return False
    if f"Identifier={TAURI_BUNDLE_IDENTIFIER}" not in selected_details:
        return False
    selected_identities = _code_directory_identities(selected_details)
    if not selected_identities:
        return False
    if "Signature=adhoc" not in details:
        return False
    if f"Identifier={TAURI_BUNDLE_IDENTIFIER}" not in details:
        return False
    if not _code_directory_identities(details) & selected_identities:
        return False
    live_identifier, live_team, live_identities = live_identity
    if live_identifier != TAURI_BUNDLE_IDENTIFIER or live_team is not None:
        return False
    if not live_identities & selected_identities:
        return False
    # Tauri's ad-hoc bundle can contain an unsigned nested launcher. Verify
    # only the exact signed main executable and ignore unrelated resources.
    return _verify_signature(selected_executable, ignore_resources=True)


def _verify_code_identity(
    executable: Path,
    team_identifier: str | None = None,
    *,
    selected_executable: Path | None = None,
    selected_bundle: Path | None = None,
    live_identity: tuple[str, str | None, frozenset[str]] | None = None,
) -> bool:
    """Verify a signed identity or the selected ad-hoc source executable."""

    details = _codesign_details(executable)
    if not details:
        return False
    if any(line == "Signature=adhoc" for line in details):
        return (
            team_identifier is None
            and selected_executable is not None
            and selected_bundle is not None
            and live_identity is not None
            and _verify_adhoc_identity(
                executable,
                details,
                selected_executable,
                selected_bundle,
                live_identity,
            )
        )
    team = (team_identifier or "").strip()
    if not team or not re.fullmatch(r"[A-Z0-9]{10}", team):
        return False
    if f"Identifier={TAURI_BUNDLE_IDENTIFIER}" not in details:
        return False
    if f"TeamIdentifier={team}" not in details:
        return False
    if not any(line.startswith("Authority=") for line in details):
        return False
    if live_identity is None:
        return False
    live_identifier, live_team, live_identities = live_identity
    if (
        live_identifier != TAURI_BUNDLE_IDENTIFIER
        or live_team != team
        or not live_identities & _code_directory_identities(details)
    ):
        return False
    return native_trust.verify_designated_requirement(executable, team)


def is_trusted_tauri_peer(connection: socket.socket) -> bool:
    """Accept only a signed process matching the selected Wiki.app team."""

    pid = _peer_pid(connection)
    executable = _peer_executable(pid) if pid is not None else None
    live_identity = _security_code_identity(pid) if pid is not None else None
    trusted_team = _bundle_team_identifier(TAURI_BUNDLE_PATH)
    if executable is None or live_identity is None:
        return False
    if trusted_team is not None:
        return _verify_code_identity(
            executable,
            trusted_team,
            live_identity=live_identity,
        )
    # Ad-hoc signatures have no developer trust anchor. Development bundles
    # must use sidecar mode, so they never receive the daemon secret.
    return False


def read_sidecar_secret() -> str:
    secret = sys.stdin.readline().strip()
    sys.stdin.close()
    os.environ.pop("WIKI_APP_SECRET", None)
    if not secret:
        raise RuntimeError("sidecar auth pipe was empty")
    return secret


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
    raise_nofile_limit()

    from backend.app.main import app, set_wiki_app_secret, wiki_app_secret

    if args.daemon:
        os.environ.pop("WIKI_APP_SECRET", None)
        set_wiki_app_secret(secrets.token_urlsafe(32))
    else:
        set_wiki_app_secret(read_sidecar_secret())

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
