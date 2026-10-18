"""JIZO's public API.

A third party should only ever need this package::

    from jizo import ApiPolicy, CircuitBreaker, resilient_get

Everything below is resolved on **first use** (PEP 562), not at import time.
That matters: `from jizo import CircuitBreaker` loads only the breaker, so
nobody pays for the database, the web server or the dashboard unless they
actually touch those names. Importing this package costs two dependencies.

The names are split across several modules because each part of JIZO was
built independently. This file is the seam: it is the one place that decides
what "JIZO" means to somebody outside the team.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"

# --------------------------------------------------------------------------
# The public surface.
#
# Left column is what a caller writes. Right column is the module inside the
# package that defines it. Alphabetical by public name, because this table is
# also the documentation of what we promise to keep stable.
#
# A test asserts this stays in sync with `__all__`.
# --------------------------------------------------------------------------
_EXPORTS: dict[str, str] = {
    # --- contracts -------------------------------------------------------
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
    # --- protection ------------------------------------------------------
    "BulkheadPool": "breaker",
    "BreakerRegistry": "breaker",
    "CircuitBreaker": "breaker",
    "GateResult": "breaker",
    "CourtesyLimiter": "proxy",
    "FallbackLadder": "proxy",
    "FallbackValue": "proxy",
    "ResilientResponse": "proxy",
    "get_courtesy_limiter": "proxy",
    "get_breaker_registry": "proxy",
    "reset_breaker_registry": "proxy",
    "resilient_get": "proxy",
    # --- fault injection -------------------------------------------------
    "EvidenceBus": "faults",
    "GuardDecision": "faults",
    "GuardEvaluator": "faults",
    "build_spec": "faults",
    "find_fault_events": "faults",
    "k_of_n_drill": "faults",
    "order_sensitive_drill": "faults",
    "post_effect_drill": "faults",
    # --- scoring ---------------------------------------------------------
    "score_run": "scoring",
    "evaluate": "scoring",
    # --- comparison ------------------------------------------------------
    "CompareResult": "compare",
    "SideDelta": "compare",
    "SideMetrics": "compare",
    "compare_sides": "compare",
    "resolve_mode": "compare",
    "side_delta": "compare",
    "side_metrics": "compare",
    # --- four-axis reporting ---------------------------------------------
    "AXIS_LABELS": "axes",
    "AXIS_ORDER": "axes",
    "Axis": "axes",
    "AxisRun": "axes",
    "AxisScore": "axes",
    "axis_scores": "axes",
    "axis_scores_from_events": "axes",
    # --- policy registry -------------------------------------------------
    "load_policy": "config",
    "register_policy": "config",
    "registered_policies": "config",
    # --- logging ---------------------------------------------------------
    "CORE_FIELDS": "logging_conf",
    "CallLogger": "logging_conf",
    "configure_logging": "logging_conf",
    "get_logger": "logging_conf",
    "log_breaker_transition": "logging_conf",
    # --- observability (needs the [db] extra) ----------------------------
    "Dashboard": "dashboard",
    "snapshot": "dashboard",
}

__all__ = [
    "__version__",
    # contracts
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
    # protection
    "BulkheadPool",
    "BreakerRegistry",
    "CircuitBreaker",
    "CourtesyLimiter",
    "FallbackLadder",
    "FallbackValue",
    "GateResult",
    "ResilientResponse",
    "get_breaker_registry",
    "get_courtesy_limiter",
    "reset_breaker_registry",
    "resilient_get",
    # fault injection
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
    # comparison
    "AXIS_LABELS",
    "AXIS_ORDER",
    "Axis",
    "AxisRun",
    "AxisScore",
    "CompareResult",
    "SideDelta",
    "SideMetrics",
    "axis_scores",
    "axis_scores_from_events",
    "compare_sides",
    "resolve_mode",
    "side_delta",
    "side_metrics",
    # policy registry
    "load_policy",
    "register_policy",
    "registered_policies",
    # logging
    "CORE_FIELDS",
    "CallLogger",
    "configure_logging",
    "get_logger",
    "log_breaker_transition",
    # observability
    "Dashboard",
    "snapshot",
]


if TYPE_CHECKING:
    # Type checkers read this branch and get real signatures. At runtime it is
    # skipped entirely, which is what keeps the import cheap.
    from ._core.axes import (
        AXIS_LABELS,
        AXIS_ORDER,
        Axis,
        AxisRun,
        AxisScore,
        axis_scores,
        axis_scores_from_events,
    )
    from ._core.breaker import (
        BreakerRegistry,
        BulkheadPool,
        CircuitBreaker,
        GateResult,
    )
    from ._core.compare import (
        CompareResult,
        SideDelta,
        SideMetrics,
        compare_sides,
        resolve_mode,
        side_delta,
        side_metrics,
    )
    from ._core.config import load_policy, register_policy, registered_policies
    from ._core.dashboard import Dashboard, snapshot
    from ._core.faults import (
        EvidenceBus,
        GuardDecision,
        GuardEvaluator,
        build_spec,
        find_fault_events,
        k_of_n_drill,
        order_sensitive_drill,
        post_effect_drill,
    )
    from ._core.logging_conf import (
        CORE_FIELDS,
        CallLogger,
        configure_logging,
        get_logger,
        log_breaker_transition,
    )
    from ._core.proxy import (
        CourtesyLimiter,
        FallbackLadder,
        FallbackValue,
        ResilientResponse,
        get_breaker_registry,
        get_courtesy_limiter,
        reset_breaker_registry,
        resilient_get,
    )
    from ._core.schemas import (
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
    from ._core.scoring import evaluate, score_run


def _load(module: str) -> Any:
    """Import one internal module by its short name.

    In an installed JIZO the code lives at `jizo._core.<module>`. In a source
    checkout - the team's repository, and `pytest` run against it - the same
    code still sits in `backend/`, because renaming it would have touched every
    test and every teammate's import. Try the installed path first, fall back
    to the checkout path. This shim disappears the day `backend/` is renamed.
    """
    from importlib import import_module

    try:
        return import_module(f"._core.{module}", __name__)
    except ModuleNotFoundError:
        return import_module(f"backend.{module}")


def __getattr__(name: str) -> Any:
    """Resolve a public name on first use, then remember it.

    PEP 562. Without this, `import jizo` would build the entire library -
    including the database layer - before handing back a circuit breaker.
    """
    try:
        module = _EXPORTS[name]
    except KeyError:
        raise AttributeError(
            f"module 'jizo' has no attribute {name!r}. "
            f"Public names are listed in jizo.__all__."
        ) from None

    value = getattr(_load(module), name)
    globals()[name] = value  # cache, so the next lookup skips all of this
    return value


def __dir__() -> list[str]:
    """Make `dir(jizo)` and tab-completion show the real public surface."""
    return sorted(__all__)