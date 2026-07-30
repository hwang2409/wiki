"""launchd integration for the persistent Wiki HTTP backend."""

from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import tempfile
import time
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
        label=values.get("WIKI_DAEMON_LABEL") or DEFAULT_LABEL,
        port=port,
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
    if unloaded.returncode != 0 and not _service_absent(config, unloaded):
        raise DaemonError(
            f"cannot unload {config.target}: {_describe_failure(unloaded)}"
        )
    if unloaded.returncode == 0 and _service_loaded(config):
        raise DaemonError(f"{config.target} is still loaded after bootout")


def _restore_prior_service(config: DaemonConfig, was_loaded: bool) -> None:
    if not was_loaded or _service_loaded(config):
        return
    restored = _launchctl(config, "bootstrap", config.domain, str(config.plist_path))
    if restored.returncode != 0:
        raise DaemonError(
            f"cannot restore {config.target}: {_describe_failure(restored)}"
        )
    if not _service_loaded(config):
        raise DaemonError(f"{config.target} was not loaded after rollback")


def _rollback_install(
    config: DaemonConfig,
    backup: tuple[bytes, int] | None,
    was_loaded: bool,
    bootstrap_attempted: bool,
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
    if plist_restored and backup is not None and was_loaded:
        try:
            _restore_prior_service(config, was_loaded)
        except DaemonError as error:
            errors.append(f"service rollback failed: {error}")
    return errors


def install(config: DaemonConfig) -> dict[str, object]:
    """Install and load the LaunchAgent without touching run state."""

    if not config.executable.is_file() or not os.access(config.executable, os.X_OK):
        raise DaemonError(
            f"backend executable is missing or not executable: {config.executable}"
        )
    backup = _capture_plist(config.plist_path)
    was_loaded = _service_loaded(config)
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
    except Exception as error:
        rollback_errors = _rollback_install(
            config, backup, was_loaded, bootstrap_attempted
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


def uninstall(config: DaemonConfig) -> dict[str, object]:
    """Unload the LaunchAgent and remove only its generated plist."""

    _unload_and_verify_absent(config)
    config.plist_path.unlink(missing_ok=True)
    return {
        "label": config.label,
        "target": config.target,
        "plist": str(config.plist_path),
        "action": "uninstalled",
    }


def _health(config: DaemonConfig) -> dict[str, object]:
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
    return {
        "healthy": payload.get("status") == "ok"
        and payload.get("daemon_managed") is True,
        "payload": payload,
    }


def _expected_backend_fingerprint(config: DaemonConfig) -> str:
    from .agent_runtime.version import frozen_runtime_fingerprint

    return frozen_runtime_fingerprint(config.executable)


def _wait_for_healthy(config: DaemonConfig) -> dict[str, object]:
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    expected = _expected_backend_fingerprint(config)
    last_health: dict[str, object] = {"healthy": False}
    while time.monotonic() < deadline:
        last_health = _health(config)
        payload = last_health.get("payload")
        if last_health.get("healthy") and isinstance(payload, dict):
            if payload.get("backend_fingerprint") != expected:
                raise DaemonError(
                    "daemon health fingerprint does not match the installed backend"
                )
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
