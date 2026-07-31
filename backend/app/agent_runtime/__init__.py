"""Headless agent runtime primitives.

The package is intentionally additive while the legacy tmux runtime is still
serving the application.  Backend routes switch to these primitives in the
next migration phase.
"""

from .client import SupervisorClient
from .command_log import (
    AgentCommand,
    CommandConflict,
    CommandError,
    CommandLog,
    CommandQueue,
    CommandReceipt,
    decide,
)
from .factory import RealAdapterFactory
from .provider import AdapterStatus, ProviderAdapter, ProviderEvent, StartRequest
from .store import RunStore, RuntimePaths
from .types import (
    EventDisposition,
    LifecycleState,
    ProviderKind,
    RecoveryAction,
    RunRecord,
    restart_recovery_decision,
)

__all__ = [
    "AdapterStatus",
    "AgentCommand",
    "CommandConflict",
    "CommandError",
    "CommandLog",
    "CommandQueue",
    "CommandReceipt",
    "EventDisposition",
    "LifecycleState",
    "ProviderAdapter",
    "ProviderEvent",
    "ProviderKind",
    "RecoveryAction",
    "RealAdapterFactory",
    "RunRecord",
    "RunStore",
    "RuntimePaths",
    "StartRequest",
    "SupervisorClient",
    "restart_recovery_decision",
    "decide",
]
