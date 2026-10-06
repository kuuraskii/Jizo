"""
JIZO - Part 1 package root.

Import the public names from here so teammates never need to know the
internal file layout:

    from backend import EvidenceBus, GuardEvaluator, k_of_n_drill
"""

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
from .breaker import (
    BreakerRegistry,
    BulkheadPool,
    CircuitBreaker,
    GateResult,
)
from .logging_conf import (
    CORE_FIELDS,
    CallLogger,
    configure_logging,
    get_logger,
    log_breaker_transition,
)
from .scoring import (
    evaluate,
    score_run,
)
from .axes import (
    AXIS_LABELS,
    AXIS_ORDER,
    Axis,
    AxisRun,
    AxisScore,
    axis_scores,
    axis_scores_from_events,
)
# NOTE: the *function* `dashboard` is deliberately not re-exported here - the
# name would shadow the `backend.dashboard` module. Call
# `backend.dashboard.dashboard(...)`, or import it from `backend.dashboard`.
from .dashboard import Dashboard, snapshot

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