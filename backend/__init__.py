"""
JIZO - Part 1 package root.

Import the public names from here so teammates never need to know the
internal file layout:

    from backend import EvidenceBus, GuardEvaluator, k_of_n_drill

Names are resolved on first use (PEP 562) rather than at import time. That
keeps `import backend` cheap and, more importantly, keeps it *honest*: a
caller who only wants the circuit breaker should not have a SQLAlchemy import
failure because they happened to touch the dashboard module. The set of
names promised here is unchanged - only when they get loaded has moved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

# --------------------------------------------------------------------------
# The public surface: name -> module inside this package that defines it.
# Alphabetical within each group. This table is the contract; `__all__` below
# must stay in step with it, and a test enforces that.
# --------------------------------------------------------------------------
_EXPORTS: dict[str, str] = {
    # schemas
    "ApiPolicy": "schemas",
    "BreakerState": "schemas",
    "DrillOutcome": "schemas",
    "DrillSpec": "schemas",
    "EvidenceEvent": "schemas",
    "FaultTarget": "schemas",
    "FaultType": "schemas",
    "FiRun": "schemas",
    "FiRunResult": "schemas",
    "GuardAfter": "schemas",
    "Phase": "schemas",
    "Pattern": "schemas",
    "ScoreResult": "schemas",
    "ServedFrom": "schemas",
    # faults
    "EvidenceBus": "faults",
    "GuardDecision": "faults",
    "GuardEvaluator": "faults",
    "build_spec": "faults",
    "find_fault_events": "faults",
    "k_of_n_drill": "faults",
    "order_sensitive_drill": "faults",
    "post_effect_drill": "faults",
    # scoring
    "evaluate": "scoring",
    "score_run": "scoring",
    # breaker (P2)
    "BreakerRegistry": "breaker",
    "BulkheadPool": "breaker",
    "CircuitBreaker": "breaker",
    "GateResult": "breaker",
    # logging (P2)
    "CORE_FIELDS": "logging_conf",
    "CallLogger": "logging_conf",
    "configure_logging": "logging_conf",
    "get_logger": "logging_conf",
    "log_breaker_transition": "logging_conf",
    # axes (P4)
    "AXIS_LABELS": "axes",
    "AXIS_ORDER": "axes",
    "Axis": "axes",
    "AxisRun": "axes",
    "AxisScore": "axes",
    "axis_scores": "axes",
    "axis_scores_from_events": "axes",
    # dashboard (P5) - the module's own `dashboard()` factory is reached via
    # `backend.dashboard.dashboard(...)`, never re-exported here, because the
    # name would shadow the `backend.dashboard` module.
    "Dashboard": "dashboard",
    "snapshot": "dashboard",
}

#: Every module in this package. `backend.db` and friends have always worked
#: as attributes (the eager imports used to pull them in as a side effect), so
#: they are kept working deliberately rather than by accident.
_SUBMODULES = frozenset({
    "axes", "breaker", "compare", "config", "dashboard", "db", "faults",
    "health", "logging_conf", "main", "models", "proxy", "schemas",
    "scoring", "secrets", "seed", "store",
})

__all__ = [
    # schemas
    "ApiPolicy",
    "BreakerState",
    "DrillOutcome",
    "DrillSpec",
    "EvidenceEvent",
    "FaultTarget",
    "FaultType",
    "FiRun",
    "FiRunResult",
    "GuardAfter",
    "Phase",
    "Pattern",
    "ScoreResult",
    "ServedFrom",
    # faults
    "EvidenceBus",
    "GuardDecision",
    "GuardEvaluator",
    "build_spec",
    "find_fault_events",
    "k_of_n_drill",
    "order_sensitive_drill",
    "post_effect_drill",
    # scoring
    "evaluate",
    "score_run",
    # breaker (P2)
    "BreakerRegistry",
    "BulkheadPool",
    "CircuitBreaker",
    "GateResult",
    # logging (P2)
    "CORE_FIELDS",
    "CallLogger",
    "configure_logging",
    "get_logger",
    "log_breaker_transition",
    # axes (P4)
    "AXIS_LABELS",
    "AXIS_ORDER",
    "Axis",
    "AxisRun",
    "AxisScore",
    "axis_scores",
    "axis_scores_from_events",
    # dashboard (P5) - the module's own `dashboard()` factory is reached via
    # `backend.dashboard.dashboard(...)`
    "Dashboard",
    "snapshot",
]


if TYPE_CHECKING:
    # Type checkers get real signatures from here. At runtime this whole block
    # is skipped, which is what keeps the import cheap.
    from .axes import (
        AXIS_LABELS,
        AXIS_ORDER,
        Axis,
        AxisRun,
        AxisScore,
        axis_scores,
        axis_scores_from_events,
    )
    from .breaker import (
        BreakerRegistry,
        BulkheadPool,
        CircuitBreaker,
        GateResult,
    )
    from .dashboard import Dashboard, snapshot
    from .faults import (
        EvidenceBus,
        GuardDecision,
        GuardEvaluator,
        build_spec,
        find_fault_events,
        k_of_n_drill,
        order_sensitive_drill,
        post_effect_drill,
    )
    from .logging_conf import (
        CORE_FIELDS,
        CallLogger,
        configure_logging,
        get_logger,
        log_breaker_transition,
    )
    from .schemas import (
        ApiPolicy,
        BreakerState,
        DrillOutcome,
        DrillSpec,
        EvidenceEvent,
        FaultTarget,
        FaultType,
        FiRun,
        FiRunResult,
        GuardAfter,
        Phase,
        Pattern,
        ScoreResult,
        ServedFrom,
    )
    from .scoring import evaluate, score_run


def __getattr__(name: str) -> Any:
    """Resolve a public name, or a submodule, on first use.

    PEP 562. Two things can arrive here:

    1. a name in `_EXPORTS` - the documented public surface, and
    2. a submodule name, so `backend.store`, `backend.compare` and friends
       keep behaving the way they did when this file imported eagerly.
    """
    from importlib import import_module

    if name in _EXPORTS:
        value = getattr(import_module(f".{_EXPORTS[name]}", __name__), name)
        globals()[name] = value  # cache; next lookup skips all of this
        return value

    if name in _SUBMODULES:
        return import_module(f".{name}", __name__)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Make `dir(backend)` and tab-completion show the real surface."""
    return sorted(set(__all__) | _SUBMODULES)