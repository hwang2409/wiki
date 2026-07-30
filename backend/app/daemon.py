"""launchd integration for the persistent Wiki HTTP backend."""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


DEFAULT_LABEL = "com.hwang2409.wiki.backend"
DEFAULT_PORT = 8213
FINDER_SAFE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


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
    executable = Path(
        executable_raw or repo_dir / "dist" / "wiki-backend-sidecar" / "wiki-backend"
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


def plist_payload(config: DaemonConfig) -> dict[str, object]:
    """Return a deterministic user LaunchAgent definition."""

    return {
        "Label": config.label,
        "ProgramArguments": config.program_arguments,
        "WorkingDirectory": str(config.repo_dir),
        "EnvironmentVariables": {
            "PATH": FINDER_SAFE_PATH,
            "WIKI_AGENT_RUNTIME_DIR": str(config.runtime_dir),
            "WIKI_BACKEND_DAEMON": "launchd",
            "WIKI_BACKEND_URL": config.backend_url,
            "WIKI_SUPERVISOR_AUTOSTART": "on",
        },
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


def _write_plist(config: DaemonConfig) -> None:
    config.launch_agents_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.launch_agents_dir.chmod(0o700)
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{config.plist_path.name}.",
        dir=config.launch_agents_dir,
        text=True,
    )
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(render_plist(config))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(0o600)
        os.replace(tmp, config.plist_path)
    finally:
        tmp.unlink(missing_ok=True)


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


def install(config: DaemonConfig) -> dict[str, object]:
    """Install and load the LaunchAgent without touching run state."""

    config.log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.log_path.parent.chmod(0o700)
    _write_plist(config)
    if _service_loaded(config):
        previous = _launchctl(config, "bootout", config.target)
        if previous.returncode != 0 and not _service_absent(config, previous):
            raise DaemonError(
                f"cannot unload {config.target}: {_describe_failure(previous)}"
            )
        if previous.returncode == 0 and _service_loaded(config):
            raise DaemonError(f"{config.target} is still loaded after bootout")
    loaded = _launchctl(config, "bootstrap", config.domain, str(config.plist_path))
    if loaded.returncode != 0:
        raise DaemonError(f"cannot load {config.plist_path}: {_describe_failure(loaded)}")
    return {
        "label": config.label,
        "target": config.target,
        "plist": str(config.plist_path),
        "url": config.backend_url,
        "action": "installed",
    }


def uninstall(config: DaemonConfig) -> dict[str, object]:
    """Unload the LaunchAgent and remove only its generated plist."""

    unloaded = _launchctl(config, "bootout", config.target)
    if unloaded.returncode != 0 and not _service_absent(config, unloaded):
        raise DaemonError(
            f"cannot unload {config.target}: {_describe_failure(unloaded)}"
        )
    if unloaded.returncode == 0 and _service_loaded(config):
        raise DaemonError(f"{config.target} is still loaded after bootout")
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


def status(config: DaemonConfig) -> dict[str, object]:
    loaded = _service_loaded(config)
    health = _health(config)
    return {
        "label": config.label,
        "target": config.target,
        "plist": str(config.plist_path),
        "installed": config.plist_path.is_file(),
        "loaded": loaded,
        "healthy": health["healthy"],
        "url": config.backend_url,
        "health": health.get("payload"),
    }
