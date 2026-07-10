from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


AgentKind = Literal["cc", "cdx"]
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
        id="gpt-5.6",
        label="GPT 5.6",
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
        id="opus-4.8",
        label="Opus 4.8",
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
)


def list_model_options() -> list[dict[str, object]]:
    return [asdict(option) for option in MODEL_OPTIONS]


def model_ids_for_kind(kind: str) -> set[str]:
    return {option.id for option in MODEL_OPTIONS if option.kind == kind}


def is_model_allowed(kind: str, model: str) -> bool:
    return model in model_ids_for_kind(kind)
