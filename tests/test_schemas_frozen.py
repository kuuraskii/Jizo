"""
The P1 contract is frozen. This file is the cheapest insurance for Parts 3-5.

`backend/schemas.py` is the surface every later part builds on: P3 persists
it, P4 scores from it, the dashboard renders it. Adding, removing, or
renaming a field - even "harmlessly" - silently breaks a part whose author is
not in the room. So this test pins the exact field set of every contract
model. If you need a new field, that is a team decision, and you update this
file in the same commit so the change is visible in review.

Deliberately field-names only, not types or defaults: a widened default is a
compatible change, a renamed field is not, and this test cares about the
second kind.
"""

from __future__ import annotations

from backend import schemas as S

# Model name -> exact field set, in declaration order. Regenerate with:
#   python -c "import sys; sys.path.insert(0,'.'); from backend import schemas as S;
#   [print(n+': '+','.join(getattr(S,n).model_fields.keys())) for n in FROZEN]"
FROZEN: dict[str, tuple[str, ...]] = {
    "ApiPolicy": (
        "api_key", "base_url", "timeout_s", "max_attempts",
        "backoff_initial_s", "backoff_max_s", "jitter_s",
        "breaker_window", "breaker_error_threshold", "breaker_min_volume",
        "breaker_sleep_s", "half_open_probes", "half_open_window_s",
        "idempotent", "criticality", "bulkhead_max_concurrency",
        "courtesy_rps",
    ),
    "EvidenceEvent": (
        "trace_id", "api_key", "phase", "served_from", "status_code",
        "effect_applied", "fault", "occurrence", "call_index",
        "latency_ms", "breaker_state", "leaked_raw_error", "note",
    ),
    "DrillOutcome": (
        "correct_withstand", "policy_success", "premature", "missed",
        "duplicate", "notes",
    ),
    "ScoreResult": (
        "run_id", "pattern", "ts", "cw", "ps", "prem", "miss", "mult",
        "timeline", "spec", "notes",
    ),
    "DrillSpec": (
        "run_id", "pattern", "fault", "target", "guard",
        "total_occurrences", "idempotent",
    ),
    "FaultTarget": ("api_key", "phase", "occurrence"),
    "GuardAfter": ("api_key", "phase", "min_count"),
    "FiRun": (
        "run_id", "pattern", "fault", "target", "guard",
        "total_occurrences", "idempotent",
    ),
    "FiRunResult": (
        "run_id", "pattern", "ts", "cw", "ps", "prem", "miss", "mult",
        "timeline", "spec", "notes",
    ),
}


def test_schemas_are_frozen():
    """Every contract model has exactly the pinned fields, no more, no less."""
    for name, expected in FROZEN.items():
        cls = getattr(S, name, None)
        assert cls is not None, f"{name} was removed from schemas.py"
        actual = tuple(cls.model_fields.keys())
        assert actual == expected, (
            f"{name} changed contract.\n"
            f"  expected: {expected}\n"
            f"  actual:   {actual}\n"
            "If this change is intentional, update FROZEN in this same commit "
            "and tell the team - P3/P4 build on these fields."
        )


def test_no_new_contract_model_slips_in_unnoticed():
    """A new model in schemas.py must be added to FROZEN deliberately."""
    from pydantic import BaseModel

    models = {
        name for name in dir(S)
        if isinstance(getattr(S, name), type)
        and issubclass(getattr(S, name), BaseModel)
        and getattr(S, name).__module__ == S.__name__
    }
    unpinned = models - set(FROZEN)
    assert not unpinned, (
        f"new contract models are not pinned: {sorted(unpinned)}. "
        "Add them to FROZEN so their fields are locked too."
    )
