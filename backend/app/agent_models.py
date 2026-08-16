from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from .agent_runtime.wk_feature import wk_enabled


AgentKind = Literal["cc", "cdx", "wk-claude", "wk-codex"]
AgentProvider = Literal["claude", "codex"]


@dataclass(frozen=True)
class AgentModelOption:
    id: str
    label: str
    kind: AgentKind
    provider: AgentProvider
    supports_reasoning_effort: bool
    default_worker: bool = False
    default_orchestrator: bool = False


MODEL_OPTIONS: tuple[AgentModelOption, ...] = (
    AgentModelOption(
        id="gpt-5.6-sol",
        label="GPT 5.6 Sol",
        kind="cdx",
        provider="codex",
        supports_reasoning_effort=True,
        default_orchestrator=True,
    ),
    AgentModelOption(
        id="gpt-5.6-terra",
        label="GPT 5.6 Terra",
        kind="cdx",
        provider="codex",
        supports_reasoning_effort=True,
    ),
    AgentModelOption(
        id="gpt-5.6-luna",
        label="GPT 5.6 Luna",
        kind="cdx",
        provider="codex",
        supports_reasoning_effort=True,
    ),
    AgentModelOption(
        id="gpt-5.5",
        label="GPT 5.5",
        kind="cdx",
        provider="codex",
        supports_reasoning_effort=True,
    ),
    AgentModelOption(
        id="gpt-5.4",
        label="GPT 5.4",
        kind="cdx",
        provider="codex",
        supports_reasoning_effort=True,
        default_worker=True,
    ),
    AgentModelOption(
        id="gpt-5.4-mini",
        label="GPT 5.4 Mini",
        kind="cdx",
        provider="codex",
        supports_reasoning_effort=True,
    ),
    AgentModelOption(
        id="gpt-5.3-codex-spark",
        label="GPT 5.3 Codex Spark",
        kind="cdx",
        provider="codex",
        supports_reasoning_effort=True,
    ),
    AgentModelOption(
        id="claude-fable-5",
        label="Claude Fable 5",
        kind="cc",
        provider="claude",
        supports_reasoning_effort=False,
    ),
    AgentModelOption(
        id="opus-4.7",
        label="Opus 4.7",
        kind="cc",
        provider="claude",
        supports_reasoning_effort=False,
    ),
    AgentModelOption(
        id="opus",
        label="Opus",
        kind="cc",
        provider="claude",
        supports_reasoning_effort=False,
        default_orchestrator=True,
    ),
    AgentModelOption(
        id="sonnet",
        label="Sonnet",
        kind="cc",
        provider="claude",
        supports_reasoning_effort=False,
        default_worker=True,
    ),
    AgentModelOption(
        id="sonnet-4.6",
        label="Sonnet 4.6",
        kind="cc",
        provider="claude",
        supports_reasoning_effort=False,
    ),
    AgentModelOption(
        id="haiku",
        label="Haiku",
        kind="cc",
        provider="claude",
        supports_reasoning_effort=False,
    ),
    AgentModelOption(
        id="haiku-4.5",
        label="Haiku 4.5",
        kind="cc",
        provider="claude",
        supports_reasoning_effort=False,
    ),
)

WK_MODEL_OPTIONS: tuple[AgentModelOption, ...] = (
    AgentModelOption(
        id="sonnet",
        label="Sonnet (wk-claude)",
        kind="wk-claude",
        provider="claude",
        supports_reasoning_effort=False,
        default_worker=True,
    ),
    AgentModelOption(
        id="gpt-5.6-sol",
        label="GPT 5.6 Sol (wk-codex)",
        kind="wk-codex",
        provider="codex",
        supports_reasoning_effort=True,
        default_worker=True,
    ),
)


def list_model_options() -> list[dict[str, object]]:
    options = MODEL_OPTIONS + WK_MODEL_OPTIONS if wk_enabled() else MODEL_OPTIONS
    return [asdict(option) for option in options]


def model_ids_for_kind(kind: str) -> tuple[str, ...]:
    options = MODEL_OPTIONS + WK_MODEL_OPTIONS if wk_enabled() else MODEL_OPTIONS
    return tuple(option.id for option in options if option.kind == kind)


def is_model_allowed(kind: str, model: str) -> bool:
    return model in model_ids_for_kind(kind)


def default_model_for_kind(kind: str, *, target: str) -> str:
    field = "default_orchestrator" if target == "Orchestrator" else "default_worker"
    options = MODEL_OPTIONS + WK_MODEL_OPTIONS if wk_enabled() else MODEL_OPTIONS
    for option in options:
        if option.kind == kind and getattr(option, field):
            return option.id
    raise ValueError(f"No default {target.lower()} model for {kind}")
