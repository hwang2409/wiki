"""launchd integration for the persistent Wiki HTTP backend."""

from __future__ import annotations

import ctypes
import fcntl
import json
import os
import platform
import plistlib
import re
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


DEFAULT_LABEL = "com.hwang2409.wiki.backend"
DEFAULT_PORT = 8213
FINDER_SAFE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
INSTALLED_WIKI_APP_PATH = Path("/Applications/Wiki.app")
SOURCE_WIKI_APP_PATH = (
    Path(__file__).resolve().parents[2]
    / "src-tauri"
    / "target"
    / "release"
    / "bundle"
    / "macos"
    / "Wiki.app"
)
DEFAULT_WIKI_APP_PATH = Path(
    os.environ.get("WIKI_APP_PATH")
    or (
        SOURCE_WIKI_APP_PATH
        if SOURCE_WIKI_APP_PATH.is_dir()
        else INSTALLED_WIKI_APP_PATH
    )
)
HEALTH_TIMEOUT_SECONDS = 15.0
HEALTH_POLL_SECONDS = 0.2
DAEMON_TRANSACTION_LOCK_NAME = "daemon.transaction.lock"
DAEMON_SETTINGS_NAME = "daemon-settings.json"


class DaemonError(RuntimeError):
    """Raised when a launchd operation cannot complete safely."""


@dataclass(frozen=True)
class DaemonConfig:
    label: str
    port: int
    repo_dir: Path
    vault_dir: Path
    runtime_dir: Path
    executable: Path
    python_module: str
    log_path: Path
    launch_agents_dir: Path

    @property
    def plist_path(self) -> Path:
        return self.launch_agents_dir / f"{self.label}.plist"

    @property
    def domain(self) -> str:
        return f"gui/{os.getuid()}"

    @property
    def target(self) -> str:
        return f"{self.domain}/{self.label}"

    @property
    def backend_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def program_arguments(self) -> list[str]:
        command = [str(self.executable)]
        if self.python_module:
            command.extend(["-m", self.python_module])
        command.extend(
            [
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--repo-dir",
                str(self.repo_dir),
                "--vault-dir",
                str(self.vault_dir),
                "--daemon",
                "--log-path",
                str(self.log_path),
            ]
        )
        return command


@dataclass(frozen=True)
class PriorDaemon:
    config: DaemonConfig
    fingerprint: str


@contextmanager
def hold_daemon_transaction_lock(runtime_dir: Path | str):
    """Serialize plist and LaunchAgent mutations for one runtime."""

    runtime = Path(runtime_dir).expanduser()
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime.chmod(0o700)
    path = runtime / DAEMON_TRANSACTION_LOCK_NAME
    handle = path.open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        handle.close()
        raise DaemonError(f"daemon transaction is already active: {path}") from exc
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def daemon_settings_path(runtime_dir: Path | str) -> Path:
    return Path(runtime_dir).expanduser() / DAEMON_SETTINGS_NAME


def _load_daemon_settings(runtime_dir: Path) -> dict[str, object]:
    path = daemon_settings_path(runtime_dir)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_daemon_settings(config: DaemonConfig) -> None:
    content = json.dumps(
        {"label": config.label, "port": config.port},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    _write_atomic(daemon_settings_path(config.runtime_dir), content, 0o600)


def _repo_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def config_from_env(*, overrides: dict[str, str | None] | None = None) -> DaemonConfig:
    values = dict(os.environ)
    for key, value in (overrides or {}).items():
        if value is not None:
            values[key] = value

    repo_dir = Path(values.get("WIKI_REPO_DIR") or _repo_dir()).expanduser().absolute()
    vault_dir = Path(values.get("WIKI_VAULT_DIR") or repo_dir / "vault").expanduser().absolute()
    runtime_dir = Path(
        values.get("WIKI_AGENT_RUNTIME_DIR") or Path.home() / ".wiki" / "agent-runtime"
    ).expanduser().absolute()
    settings = _load_daemon_settings(runtime_dir)
    stored_label = settings.get("label")
    stored_port = settings.get("port")
    executable_raw = values.get("WIKI_BACKEND_EXECUTABLE")
    app_path = Path(values.get("WIKI_APP_PATH") or DEFAULT_WIKI_APP_PATH)
    executable = Path(
        executable_raw
        or app_path / "Contents" / "Resources" / "wiki-backend-sidecar" / "wiki-backend"
    ).expanduser().absolute()
    python_module = ""
    log_path = Path(
        values.get("WIKI_DAEMON_LOG_PATH")
        or Path.home() / "Library" / "Logs" / "Wiki" / "wiki-backend-daemon.log"
    ).expanduser().absolute()
    launch_agents_dir = Path(
        values.get("WIKI_LAUNCH_AGENTS_DIR")
        or Path.home() / "Library" / "LaunchAgents"
    ).expanduser().absolute()
    try:
        port = int(values.get("WIKI_BACKEND_PORT") or DEFAULT_PORT)
    except ValueError as exc:
        raise DaemonError("WIKI_BACKEND_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise DaemonError("backend port must be between 1 and 65535")
    return DaemonConfig(
        label=values.get("WIKI_DAEMON_LABEL")
        or (stored_label if isinstance(stored_label, str) else DEFAULT_LABEL),
        port=(
            port
            if values.get("WIKI_BACKEND_PORT")
            else (
                stored_port
                if isinstance(stored_port, int) and 1 <= stored_port <= 65535
                else DEFAULT_PORT
            )
        ),
        repo_dir=repo_dir,
        vault_dir=vault_dir,
        runtime_dir=runtime_dir,
        executable=executable,
        python_module=python_module,
        log_path=log_path,
        launch_agents_dir=launch_agents_dir,
    )


def _bundle_path_for_executable(executable: Path) -> Path | None:
    for parent in executable.parents:
        if parent.name == "Wiki.app":
            return parent
    return None


def plist_payload(config: DaemonConfig) -> dict[str, object]:
    """Return a deterministic user LaunchAgent definition."""

    environment = {
        "PATH": FINDER_SAFE_PATH,
        "WIKI_AGENT_RUNTIME_DIR": str(config.runtime_dir),
        "WIKI_BACKEND_DAEMON": "launchd",
        "WIKI_BACKEND_URL": config.backend_url,
        "WIKI_SUPERVISOR_AUTOSTART": "on",
    }
    bundle_path = _bundle_path_for_executable(config.executable)
    if bundle_path is not None:
        environment["WIKI_APP_PATH"] = str(bundle_path)
    return {
        "Label": config.label,
        "ProgramArguments": config.program_arguments,
        "WorkingDirectory": str(config.repo_dir),
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "ProcessType": "Background",
        "StandardOutPath": str(config.log_path),
        "StandardErrorPath": str(config.log_path),
    }


def render_plist(config: DaemonConfig) -> str:
    return plistlib.dumps(
        plist_payload(config), fmt=plistlib.FMT_XML, sort_keys=False
    ).decode("utf-8")


def _write_atomic(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _write_plist(config: DaemonConfig) -> None:
    _write_atomic(config.plist_path, render_plist(config).encode("utf-8"), 0o600)


def _capture_plist(path: Path) -> tuple[bytes, int] | None:
    if not path.is_file():
        return None
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _restore_plist(config: DaemonConfig, backup: tuple[bytes, int] | None) -> None:
    if backup is None:
        config.plist_path.unlink(missing_ok=True)
        if config.plist_path.exists():
            raise DaemonError(f"{config.plist_path} still exists after rollback")
        return
    content, mode = backup
    _write_atomic(config.plist_path, content, mode)
    restored = _capture_plist(config.plist_path)
    if restored != backup:
        raise DaemonError(f"{config.plist_path} does not match its rollback copy")


def _launchctl(config: DaemonConfig, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["launchctl", *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise DaemonError(f"launchctl is unavailable: {exc}") from exc


def _describe_failure(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "launchctl failed").strip()
    return detail


def _service_pid(config: DaemonConfig) -> int | None:
    result = _launchctl(config, "print", config.target)
    if result.returncode != 0:
        if _service_absent(config, result):
            return None
        raise DaemonError(
            f"cannot inspect {config.target}: {_describe_failure(result)}"
        )
    match = re.search(r"(?m)^\s*pid\s*=\s*(\d+)\s*$", _describe_failure(result))
    return int(match.group(1)) if match else None


def _process_executable(pid: int) -> Path | None:
    try:
        if platform.system() == "Darwin":
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
            libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            libproc.proc_pidpath.restype = ctypes.c_int
            buffer = ctypes.create_string_buffer(4096)
            size = libproc.proc_pidpath(pid, buffer, ctypes.sizeof(buffer))
            if size <= 0:
                return None
            return Path(buffer.value.decode("utf-8"))
        return Path(f"/proc/{pid}/exe").resolve(strict=True)
    except (OSError, UnicodeDecodeError, AttributeError):
        return None


def _same_executable(first: Path, second: Path) -> bool:
    try:
        return first.resolve(strict=True) == second.resolve(strict=True) and os.path.samefile(
            first, second
        )
    except OSError:
        return False


def _health_process_matches(
    config: DaemonConfig,
    payload: dict[str, object],
    expected_fingerprint: str | None = None,
) -> bool:
    raw_pid = payload.get("process_id")
    if isinstance(raw_pid, bool) or not isinstance(raw_pid, int) or raw_pid <= 0:
        return False
    service_pid = _service_pid(config)
    if service_pid != raw_pid:
        return False
    executable = _process_executable(raw_pid)
    if executable is None or not _same_executable(executable, config.executable):
        return False
    from .agent_runtime.version import frozen_runtime_fingerprint

    expected = expected_fingerprint or _expected_backend_fingerprint(config)
    return frozen_runtime_fingerprint(executable) == expected


def _service_absent_message(config: DaemonConfig) -> str:
    return (
        "Bad request.\n"
        f'Could not find service "{config.label}" in domain for user gui: {os.getuid()}'
    )


def _service_absent(
    config: DaemonConfig, result: subprocess.CompletedProcess[str]
) -> bool:
    return (
        result.returncode == 113
        and _describe_failure(result) == _service_absent_message(config)
    )


def _bootout_absent(
    _config: DaemonConfig, result: subprocess.CompletedProcess[str]
) -> bool:
    """Match launchctl bootout's distinct missing-service response."""

    detail = _describe_failure(result).lower()
    return result.returncode == 3 and "no such process" in detail


def _service_loaded(config: DaemonConfig) -> bool:
    """Return service state, rejecting launchctl errors we cannot classify."""

    result = _launchctl(config, "print", config.target)
    if result.returncode == 0:
        return True
    if _service_absent(config, result):
        return False
    raise DaemonError(
        f"cannot inspect {config.target}: {_describe_failure(result)}"
    )


def _unload_and_verify_absent(config: DaemonConfig) -> None:
    unloaded = _launchctl(config, "bootout", config.target)
    if unloaded.returncode != 0 and not _bootout_absent(config, unloaded):
        raise DaemonError(
            f"cannot unload {config.target}: {_describe_failure(unloaded)}"
        )
    if unloaded.returncode == 0 and _service_loaded(config):
        raise DaemonError(f"{config.target} is still loaded after bootout")


def _prior_config(
    config: DaemonConfig,
    backup: tuple[bytes, int] | None,
) -> PriorDaemon | None:
    if backup is None:
        return None
    try:
        document = plistlib.loads(backup[0])
        arguments = document.get("ProgramArguments")
        executable = arguments[0] if isinstance(arguments, list) and arguments else None
        if not isinstance(executable, str) or not executable:
            return None
        prior_config = DaemonConfig(**{**config.__dict__, "executable": Path(executable)})
        if not prior_config.executable.is_file():
            return None
        return PriorDaemon(
            config=prior_config,
            fingerprint=_expected_backend_fingerprint(prior_config),
        )
    except (OSError, TypeError, ValueError, plistlib.InvalidFileException):
        return None


def _restore_prior_service(
    config: DaemonConfig,
    was_loaded: bool,
    prior: PriorDaemon | None,
) -> None:
    if not was_loaded:
        return
    if prior is None:
        raise DaemonError("cannot verify the prior daemon executable during rollback")
    if not _service_loaded(config):
        restored = _launchctl(config, "bootstrap", config.domain, str(config.plist_path))
        if restored.returncode != 0:
            raise DaemonError(
                f"cannot restore {config.target}: {_describe_failure(restored)}"
            )
        if not _service_loaded(config):
            raise DaemonError(f"{config.target} was not loaded after rollback")
    _wait_for_healthy(prior.config, expected_fingerprint=prior.fingerprint)


def _rollback_install(
    config: DaemonConfig,
    backup: tuple[bytes, int] | None,
    was_loaded: bool,
    bootstrap_attempted: bool,
    prior: PriorDaemon | None,
) -> list[str]:
    errors: list[str] = []
    if bootstrap_attempted:
        try:
            _unload_and_verify_absent(config)
        except DaemonError as error:
            errors.append(f"cleanup failed: {error}")
    plist_restored = False
    try:
        _restore_plist(config, backup)
        plist_restored = True
    except Exception as error:
        errors.append(f"plist rollback failed: {error}")
    if plist_restored and was_loaded:
        try:
            _restore_prior_service(config, was_loaded, prior)
        except DaemonError as error:
            errors.append(f"service rollback failed: {error}")
    return errors


def _install_unlocked(config: DaemonConfig) -> dict[str, object]:
    """Install and load the LaunchAgent without touching run state."""

    if not config.executable.is_file() or not os.access(config.executable, os.X_OK):
        raise DaemonError(
            f"backend executable is missing or not executable: {config.executable}"
        )
    backup = _capture_plist(config.plist_path)
    was_loaded = _service_loaded(config)
    prior = _prior_config(config, backup)
    config.log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.log_path.parent.chmod(0o700)
    bootstrap_attempted = False
    try:
        _write_plist(config)
        if was_loaded:
            _unload_and_verify_absent(config)
        bootstrap_attempted = True
        loaded = _launchctl(config, "bootstrap", config.domain, str(config.plist_path))
        if loaded.returncode != 0:
            raise DaemonError(
                f"cannot load {config.plist_path}: {_describe_failure(loaded)}"
            )
        _wait_for_healthy(config)
        _write_daemon_settings(config)
    except Exception as error:
        rollback_errors = _rollback_install(
            config, backup, was_loaded, bootstrap_attempted, prior
        )
        if rollback_errors:
            detail = "; ".join([f"install failed: {error}", *rollback_errors])
            raise DaemonError(detail) from error
        raise
    return {
        "label": config.label,
        "target": config.target,
        "plist": str(config.plist_path),
        "url": config.backend_url,
        "backend_fingerprint": _expected_backend_fingerprint(config),
        "action": "installed",
    }


def install(
    config: DaemonConfig,
    *,
    transaction_lock_held: bool = False,
) -> dict[str, object]:
    if transaction_lock_held:
        return _install_unlocked(config)
    with hold_daemon_transaction_lock(config.runtime_dir):
        return _install_unlocked(config)


def _uninstall_unlocked(config: DaemonConfig) -> dict[str, object]:
    """Unload the LaunchAgent and remove only its generated plist."""

    backup_path = config.plist_path.with_name(
        f".{config.plist_path.name}.uninstall.{os.getpid()}.bak"
    )
    if backup_path.exists():
        raise DaemonError(f"stale uninstall backup exists: {backup_path}")
    if config.plist_path.exists():
        os.replace(config.plist_path, backup_path)
    try:
        _unload_and_verify_absent(config)
    except Exception:
        if backup_path.exists():
            os.replace(backup_path, config.plist_path)
        raise
    try:
        backup_path.unlink(missing_ok=True)
    except OSError as exc:
        raise DaemonError(
            f"service unloaded but uninstall backup cleanup failed: {exc}"
        ) from exc
    try:
        daemon_settings_path(config.runtime_dir).unlink(missing_ok=True)
    except OSError as exc:
        raise DaemonError(
            f"service unloaded but daemon settings cleanup failed: {exc}"
        ) from exc
    return {
        "label": config.label,
        "target": config.target,
        "plist": str(config.plist_path),
        "action": "uninstalled",
    }


def uninstall(
    config: DaemonConfig,
    *,
    transaction_lock_held: bool = False,
) -> dict[str, object]:
    if transaction_lock_held:
        return _uninstall_unlocked(config)
    with hold_daemon_transaction_lock(config.runtime_dir):
        return _uninstall_unlocked(config)


def _health(
    config: DaemonConfig,
    *,
    expected_fingerprint: str | None = None,
) -> dict[str, object]:
    request = Request(f"{config.backend_url}/health", method="GET")
    try:
        with urlopen(request, timeout=0.75) as response:  # noqa: S310 - loopback URL
            if response.status != 200:
                return {"healthy": False}
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError):
        return {"healthy": False}
    if not isinstance(payload, dict):
        return {"healthy": False}
    identity_matches = _health_process_matches(config, payload, expected_fingerprint)
    return {
        "healthy": payload.get("status") == "ok"
        and payload.get("daemon_managed") is True
        and identity_matches,
        "identity_matches": identity_matches,
        "payload": payload,
    }


def _expected_backend_fingerprint(config: DaemonConfig) -> str:
    from .agent_runtime.version import frozen_runtime_fingerprint

    return frozen_runtime_fingerprint(config.executable)


def _wait_for_healthy(
    config: DaemonConfig,
    *,
    expected_fingerprint: str | None = None,
) -> dict[str, object]:
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    expected = expected_fingerprint or _expected_backend_fingerprint(config)
    last_health: dict[str, object] = {"healthy": False}
    while time.monotonic() < deadline:
        if expected_fingerprint is None:
            last_health = _health(config)
        else:
            last_health = _health(config, expected_fingerprint=expected_fingerprint)
        payload = last_health.get("payload")
        if (
            last_health.get("healthy")
            and last_health.get("identity_matches", True) is True
            and isinstance(payload, dict)
        ):
            if payload.get("backend_fingerprint") != expected:
                time.sleep(HEALTH_POLL_SECONDS)
                continue
            return last_health
        time.sleep(HEALTH_POLL_SECONDS)
    raise DaemonError(
        f"daemon did not become healthy at {config.backend_url}: {last_health}"
    )


def status(config: DaemonConfig) -> dict[str, object]:
    loaded = _service_loaded(config)
    health = _health(config)
    payload = health.get("payload")
    expected_fingerprint = (
        _expected_backend_fingerprint(config) if config.executable.is_file() else None
    )
    running_fingerprint = (
        payload.get("backend_fingerprint")
        if isinstance(payload, dict)
        else None
    )
    fingerprint_match = (
        expected_fingerprint is not None
        and running_fingerprint == expected_fingerprint
    )
    return {
        "label": config.label,
        "target": config.target,
        "plist": str(config.plist_path),
        "installed": config.plist_path.is_file(),
        "loaded": loaded,
        "healthy": health["healthy"] and fingerprint_match,
        "url": config.backend_url,
        "health": payload,
        "backend_fingerprint": running_fingerprint,
        "expected_backend_fingerprint": expected_fingerprint,
        "fingerprint_match": fingerprint_match,
    }
