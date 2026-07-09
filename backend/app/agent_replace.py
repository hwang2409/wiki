from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from fastapi import HTTPException

from . import transcripts


ROOT_DIR = Path(os.environ.get("WIKI_REPO_DIR", Path(__file__).resolve().parents[2])).resolve()
AGENT_REGISTRY_PATH = Path(os.environ.get("WIKI_AGENT_REGISTRY_PATH") or "/tmp/agent-registry.json")
AGENT_STATUS_DIR = Path(os.environ.get("WIKI_AGENT_STATUS_DIR") or "/tmp/agent-status")
AGENT_ARCHIVE_DIR = Path(
    os.environ.get("WIKI_AGENT_ARCHIVE_DIR") or Path.home() / "me" / "fun" / "agent-archive"
)
AGENT_TMP_DIR = Path(os.environ.get("WIKI_AGENT_TMP_DIR") or "/tmp")
TICKET_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")
ORCH_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}
SPINNER_PATTERN = re.compile(r"esc to interrupt|\(\d+m\s\d+s\b|\(\d+s\b")


def tmux_live_windows() -> set[str]:
    try:
        result = subprocess.run(
            ["tmux", "list-windows", "-a", "-F", "#{window_id}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return set()
        return {line.strip() for line in result.stdout.split("\n") if line.strip()}
    except (OSError, subprocess.TimeoutExpired):
        return set()


def read_agent_status(ticket: str) -> dict | None:
    path = AGENT_STATUS_DIR / f"{ticket}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_mtime"] = path.stat().st_mtime
        return data
    except (OSError, ValueError):
        return None


def _read_agent_registry() -> dict:
    try:
        data = json.loads(AGENT_REGISTRY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _capture_pane_tail(window: str, lines: int) -> str | None:
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", window],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    cleaned: list[str] = []
    blank_run = 0
    for line in result.stdout.split("\n"):
        line = line.rstrip()
        if not line:
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        cleaned.append(line)
    return "\n".join(cleaned).strip("\n")


def _pane_is_working(pane: str) -> bool:
    return bool(SPINNER_PATTERN.search(pane))


def _deliver_message(window: str, text: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", window, "-l", text], timeout=5, check=False)
    time.sleep(0.5)
    subprocess.run(["tmux", "send-keys", "-t", window, "Enter"], timeout=5, check=False)
    time.sleep(2)
    pane = _capture_pane_tail(window, 30) or ""
    if text[:60] in pane.replace("\n", " "):
        subprocess.run(["tmux", "send-keys", "-t", window, "Enter"], timeout=5, check=False)


def _accept_claude_trust_prompt(window: str, *, timeout_seconds: float = 8.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        pane = _capture_pane_tail(window, 40) or ""
        if "Quick safety check" not in pane and (
            "? for shortcuts" in pane or "-- INSERT --" in pane or "bypass permissions on" in pane
        ):
            return
        if "Quick safety check" in pane and "Yes, I trust this folder" in pane:
            subprocess.run(["tmux", "send-keys", "-t", window, "Enter"], timeout=5, check=False)
            time.sleep(0.5)
            return
        time.sleep(0.25)


def settle_claude_kickoff(window: str, prompt: str) -> None:
    _accept_claude_trust_prompt(window)
    time.sleep(2)
    pane = _capture_pane_tail(window, 80) or ""
    flat_pane = " ".join(pane.split())
    prompt_head = " ".join(prompt.strip().split())[:80]
    if prompt_head and prompt_head in flat_pane:
        subprocess.run(["tmux", "send-keys", "-t", window, "Enter"], timeout=5, check=False)
        return
    if pane and not _pane_is_working(pane):
        _deliver_message(window, prompt)


def _resolve_existing_dir(raw_path: str, *, field_name: str) -> Path:
    try:
        resolved = Path(raw_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} does not exist") from exc
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail=f"{field_name} must be a directory")
    return resolved


def _run_checked(
    args: list[str],
    *,
    timeout: int,
    label: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except OSError as exc:
        raise RuntimeError(f"{label}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{label}: timed out") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise RuntimeError(f"{label}: {detail[:240]}")
    return result


def _wiki_cli_env() -> dict[str, str]:
    env = os.environ.copy()
    env["WIKI_AGENT_REGISTRY_PATH"] = str(AGENT_REGISTRY_PATH)
    env["WIKI_AGENT_STATUS_DIR"] = str(AGENT_STATUS_DIR)
    env["WIKI_AGENT_ARCHIVE_DIR"] = str(AGENT_ARCHIVE_DIR)
    env["WIKI_AGENT_TMP_DIR"] = str(AGENT_TMP_DIR)
    return env


def _require_live_window(window: object) -> str:
    if not isinstance(window, str) or not re.fullmatch(r"@\d+", window):
        raise HTTPException(status_code=409, detail="Registered tmux window id is invalid")
    if window not in tmux_live_windows():
        raise HTTPException(status_code=409, detail=f"Registered tmux window {window} is not live")
    return window


def _tmux_window_session(window: str) -> str:
    result = _run_checked(
        ["tmux", "display-message", "-p", "-t", window, "-F", "#{session_name}"],
        timeout=5,
        label="tmux session lookup failed",
    )
    session = result.stdout.strip()
    if not session:
        raise RuntimeError("tmux session lookup failed: empty session name")
    return session


def _tmux_kill_window(window: str) -> None:
    _run_checked(["tmux", "kill-window", "-t", window], timeout=5, label="tmux kill-window failed")


def _tmux_keepalive_if_last(target_session: str) -> str | None:
    listed = _run_checked(
        ["tmux", "list-windows", "-t", f"{target_session}:", "-F", "#{window_id}"],
        timeout=5,
        label="tmux list-windows failed",
    )
    windows = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if len(windows) != 1:
        return None
    created = _run_checked(
        [
            "tmux",
            "new-window",
            "-dP",
            "-F",
            "#{window_id}",
            "-n",
            "replace-keepalive",
            "-t",
            f"{target_session}:",
            "sleep 60",
        ],
        timeout=10,
        label="tmux keepalive window failed",
    )
    window = created.stdout.strip()
    if not re.fullmatch(r"@\d+", window):
        raise RuntimeError(f"tmux keepalive window failed: unexpected window id {window!r}")
    return window


def _tmux_kill_best_effort(window: str | None) -> None:
    if window:
        subprocess.run(["tmux", "kill-window", "-t", window], timeout=5, check=False)


def _tmux_new_window(name: str, cwd: Path, command: str, target_session: str) -> str:
    created = _run_checked(
        [
            "tmux",
            "new-window",
            "-dP",
            "-F",
            "#{window_id}",
            "-n",
            name,
            "-c",
            str(cwd),
            "-t",
            f"{target_session}:",
            command,
        ],
        timeout=10,
        label="tmux new-window failed",
    )
    window = created.stdout.strip()
    if not re.fullmatch(r"@\d+", window):
        raise RuntimeError(f"tmux new-window failed: unexpected window id {window!r}")
    return window


def _tmux_pipe_pane(window: str, log_path: Path) -> None:
    _run_checked(
        ["tmux", "pipe-pane", "-t", window, "-o", f"cat >> {shlex.quote(str(log_path))}"],
        timeout=5,
        label="tmux pipe-pane failed",
    )


def _tmux_lock_window_name(window: str) -> None:
    for option in ("allow-rename", "automatic-rename"):
        _run_checked(
            ["tmux", "set-window-option", "-t", window, option, "off"],
            timeout=5,
            label=f"tmux {option} failed",
        )


def _next_log_path(existing: object, fallback_name: str) -> Path:
    path = Path(existing) if isinstance(existing, str) and existing.strip() else AGENT_TMP_DIR / fallback_name
    stem = path.stem
    match = re.match(r"^(.*)-r(\d+)$", stem)
    if match:
        base = match.group(1)
        next_index = int(match.group(2)) + 1
    else:
        base = stem
        next_index = 1
    return path.with_name(f"{base}-r{next_index}{path.suffix or '.log'}")


def _write_prompt_file(path: Path, prompt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prompt if prompt.endswith("\n") else f"{prompt}\n", encoding="utf-8")


def _worker_transcript_path(ticket: str, current: dict) -> str | None:
    try:
        found = transcripts.find_session(
            current.get("kind"),
            ticket,
            current.get("spawned_at"),
            current.get("session_id") if isinstance(current.get("session_id"), str) else None,
            current.get("worktree"),
        )
    except Exception:
        return None
    return str(found[1]) if found else None


def _worker_pr_hint(ticket: str, current: dict) -> str | None:
    status = read_agent_status(ticket) or {}
    for value in (status.get("pr"), current.get("pr")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _worker_replacement_prompt(ticket: str, current: dict, prior_transcript: str | None) -> str:
    old_session = current.get("session_id") or current.get("session") or "unknown"
    status_path = AGENT_STATUS_DIR / f"{ticket}.json"
    prior_log = current.get("log") or "not recorded"
    pr_hint = _worker_pr_hint(ticket, current)
    pr_line = (
        f"A PR appears to exist at {pr_hint}; read the latest PR handoff comment before continuing."
        if pr_hint
        else "If a PR exists for this ticket, read the latest PR handoff comment before continuing."
    )
    return f"""You are a replacement WORKER, replacing agent run/session `{old_session}`.
You are working on ticket {ticket}.

Identity to preserve:
- ticket: {ticket}
- kind: {current.get("kind")}
- role: {current.get("role")}
- model: {current.get("model")}

Context recovery paths:
- prior pane log: {prior_log}
- prior transcript: {prior_transcript or "not resolved"}
- status file: {status_path}

{pr_line}

Continue this ticket under the same status-file contract. Rewrite `{status_path}` before every state transition or long operation, keep `state`, `pr`, `step`, and `blocker` current, and finish with the required sentinel when the ticket is merge-ready or blocked.
"""


def _worker_command(kind: str, model: str, prompt_path: Path, current: dict) -> str:
    prompt_shell = shlex.quote(str(prompt_path))
    model_shell = shlex.quote(model)
    if kind == "cdx":
        effort = current.get("effort") if current.get("effort") in REASONING_EFFORTS else "high"
        return (
            f"codex --yolo -m {model_shell} "
            f"-c model_reasoning_effort={shlex.quote(effort)} "
            f'"$(cat {prompt_shell})"'
        )
    if kind == "cc":
        return f'claude --model {model_shell} --dangerously-skip-permissions "$(cat {prompt_shell})"'
    raise HTTPException(status_code=409, detail="Worker kind must be cdx or cc")


def _register_worker_replacement(ticket: str, current: dict, window: str, log_path: Path) -> None:
    args = [
        str(ROOT_DIR / "wiki"),
        "agent",
        "register",
        ticket,
        "--window",
        window,
        "--kind",
        current["kind"],
        "--role",
        current["role"],
        "--model",
        current["model"],
        "--worktree",
        current["worktree"],
        "--log",
        str(log_path),
    ]
    if current.get("orch"):
        args.extend(["--orch", current["orch"]])
    _run_checked(
        args,
        timeout=10,
        label="wiki agent register failed",
        env=_wiki_cli_env(),
    )


def _replace_worker(ticket: str, entry: dict) -> dict[str, object]:
    current = entry.get("current") if isinstance(entry.get("current"), dict) else None
    if not current:
        raise HTTPException(status_code=404, detail=f"{ticket} is not a registered worker")
    for field in ("kind", "role", "model", "worktree"):
        if not isinstance(current.get(field), str) or not current[field].strip():
            raise HTTPException(status_code=409, detail=f"Worker registry entry is missing {field}")
    worktree = _resolve_existing_dir(current["worktree"], field_name="Worker worktree")
    old_window = _require_live_window(current.get("window"))
    keepalive: str | None = None
    prior_transcript = _worker_transcript_path(ticket, current)
    prompt = _worker_replacement_prompt(ticket, current, prior_transcript)
    log_path = _next_log_path(current.get("log"), f"{current['kind']}-{ticket}.log")
    prompt_path = AGENT_TMP_DIR / f"{log_path.stem}-prompt.md"
    command = _worker_command(current["kind"], current["model"], prompt_path, current)

    try:
        target_session = _tmux_window_session(old_window)
        keepalive = _tmux_keepalive_if_last(target_session)

        _tmux_kill_window(old_window)
        _write_prompt_file(prompt_path, prompt)
        new_window = _tmux_new_window(f"{current['kind']}:{ticket}", worktree, command, target_session)
        try:
            _tmux_lock_window_name(new_window)
            _tmux_pipe_pane(new_window, log_path)
            _register_worker_replacement(ticket, current, new_window, log_path)
            if current["kind"] == "cc":
                settle_claude_kickoff(new_window, prompt)
        except Exception:
            _tmux_kill_best_effort(new_window)
            raise
    except HTTPException:
        _tmux_kill_best_effort(keepalive)
        raise
    except RuntimeError as exc:
        _tmux_kill_best_effort(keepalive)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except OSError as exc:
        _tmux_kill_best_effort(keepalive)
        raise HTTPException(status_code=500, detail=f"Could not prepare replacement files: {exc}") from exc
    except Exception as exc:
        _tmux_kill_best_effort(keepalive)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    _tmux_kill_best_effort(keepalive)

    registry = _read_agent_registry()
    return {
        "id": ticket,
        "type": "worker",
        "window": new_window,
        "log": str(log_path),
        "prompt_path": str(prompt_path),
        "registration": (registry.get(ticket) or {}).get("current"),
    }


def _orch_replacement_prompt(orch_id: str, orch: dict, model: str) -> str:
    old_session = orch.get("session_id") or "unknown"
    prior_transcript = orch.get("transcript") or "not recorded"
    return f"""You are a replacement ORCHESTRATOR with id `{orch_id}`, replacing session `{old_session}`; prior transcript: `{prior_transcript}`.

FIRST, self-register this Claude Code session before anything else:
`~/me/fun/wiki/wiki agent orch {orch_id} --model {model} --window "$(tmux display-message -p -t "$TMUX_PANE" '#{{window_id}}')"`

Then read the full protocol before taking any action:
`~/me/fun/wiki/vault/tools/orchestrator-worker-protocol.md`

Recover context from the prior transcript if it exists, inspect the current worker registry and status files, then continue orchestrating under the same worker/orchestrator protocol.
"""


def _replace_orchestrator(orch_id: str, orch: dict) -> dict[str, object]:
    cwd = orch.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        raise HTTPException(status_code=409, detail="Orchestrator registry entry is missing cwd")
    workdir = _resolve_existing_dir(cwd, field_name="Orchestrator cwd")
    old_window = _require_live_window(orch.get("window"))
    model = orch.get("model") if isinstance(orch.get("model"), str) and orch.get("model").strip() else "opus"
    keepalive: str | None = None
    prompt = _orch_replacement_prompt(orch_id, orch, model)
    log_path = _next_log_path(orch.get("log"), f"cc-orch-{orch_id}.log")
    prompt_path = AGENT_TMP_DIR / f"{log_path.stem}-prompt.md"
    command = (
        f"claude --model {shlex.quote(model)} --dangerously-skip-permissions "
        f'"$(cat {shlex.quote(str(prompt_path))})"'
    )

    try:
        target_session = _tmux_window_session(old_window)
        keepalive = _tmux_keepalive_if_last(target_session)

        _tmux_kill_window(old_window)
        _write_prompt_file(prompt_path, prompt)
        new_window = _tmux_new_window("thinker", workdir, command, target_session)
        try:
            _tmux_lock_window_name(new_window)
            _tmux_pipe_pane(new_window, log_path)
            settle_claude_kickoff(new_window, prompt)
        except Exception:
            _tmux_kill_best_effort(new_window)
            raise
    except HTTPException:
        _tmux_kill_best_effort(keepalive)
        raise
    except RuntimeError as exc:
        _tmux_kill_best_effort(keepalive)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except OSError as exc:
        _tmux_kill_best_effort(keepalive)
        raise HTTPException(status_code=500, detail=f"Could not prepare replacement files: {exc}") from exc
    except Exception as exc:
        _tmux_kill_best_effort(keepalive)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    _tmux_kill_best_effort(keepalive)

    return {
        "id": orch_id,
        "type": "orchestrator",
        "window": new_window,
        "log": str(log_path),
        "prompt_path": str(prompt_path),
        "model": model,
        "registration": {
            "pending_self_register": True,
            "old_session_id": orch.get("session_id"),
        },
    }


def replace_agent(agent_id: str) -> dict[str, object]:
    raw_id = agent_id.strip()
    if not raw_id or not (TICKET_PATTERN.fullmatch(raw_id) or ORCH_ID_PATTERN.fullmatch(raw_id)):
        raise HTTPException(status_code=400, detail="Bad agent id")
    registry = _read_agent_registry()
    worker_id = raw_id if raw_id in registry else raw_id.upper()
    if worker_id in registry and not worker_id.startswith("_"):
        return _replace_worker(worker_id, registry[worker_id])
    orch = (registry.get("_orchestrators") or {}).get(raw_id)
    if orch:
        return _replace_orchestrator(raw_id, orch)
    raise HTTPException(status_code=404, detail=f"No registered worker or orchestrator named {raw_id}")
