"""
JIZO - Part 2 configuration (Protector Core).

One central place for the sourced thresholds and the per-upstream
``ApiPolicy`` registry. Nothing here talks to the network or a database;
it only answers two questions:

    "which upstreams does the protector know about?"
    "what numbers does it use?"

Why a separate module instead of literals scattered through ``proxy.py``?
The JIZO PRD pins every number to a citation, so a reviewer asking
"where does 1.8s come from?" should find it in exactly one place. Also,
Part 3 loads ``api_registry`` rows from Postgres at startup and needs
somewhere to put them.

The SHAPE of a policy (which fields exist, what they mean) is Part 1's
frozen ``schemas.ApiPolicy``. This file only decides which policies
exist; it never adds a field to that shape.

Owner: Aditi (Part 2).
"""

from __future__ import annotations

from typing import Any

from .schemas import ApiPolicy


# ---------------------------------------------------------------------------
# Sourced defaults
# ---------------------------------------------------------------------------
# Every number below mirrors a value the PRD specifies (Sec. 2, FR-1 and
# Sec. 4) and matches the corresponding field default on ApiPolicy (which
# is frozen, so we did not move them). They live in one dict so the two
# demo policies can be built from a single place, and so a reader can find
# the sourced numbers without searching.
#
# If you ever change one of these, change it in ApiPolicy too - or, better,
# change it in ApiPolicy and update this dict to match.
# ---------------------------------------------------------------------------

#: Total wall-clock budget for one *logical* call, as a multiple of the
#: per-attempt timeout. PRD wording: "budget 2.2x baseline".
#:
#: Distinct from ``timeout_s`` on purpose: timeout is "how long may ONE
#: HTTP attempt hang"; the budget is "how long may the whole retry
#: sequence take before we give up and serve the fallback".
RETRY_BUDGET_MULTIPLIER: float = 2.2

DEFAULTS: dict[str, Any] = {
    # --- Timeout ---------------------------------------------------
    # Seconds allowed for a single HTTP attempt.
    "timeout_s": 3.0,
    # --- Retry -----------------------------------------------------
    # Total tries in one logical call: 1 original + up to 2 retries.
    "max_attempts": 3,
    # Wait before the first retry, doubling each attempt:
    #     min(0.075 * 2**attempt + uniform(0, 0.05), 1.8)
    "backoff_initial_s": 0.075,
    # Cap on any single backoff wait.
    "backoff_max_s": 1.8,
    # Random extra wait, so many callers do not retry in lockstep.
    "jitter_s": 0.05,
    # Total budget (see RETRY_BUDGET_MULTIPLIER above).
    "retry_budget_multiplier": RETRY_BUDGET_MULTIPLIER,
    # --- Circuit breaker -------------------------------------------
    # Part 2 owns breaker.py, which reads these from the ApiPolicy it is
    # handed. Listed here only so the sourced values live in one place.
    "breaker_window": 100,
    "breaker_error_threshold": 0.25,
    "breaker_min_volume": 20,
    "breaker_sleep_s": 10.0,
    "half_open_probes": 10,
    "half_open_window_s": 5.0,
}

#: The subset of DEFAULTS that `ApiPolicy` accepts as fields. The one
#: extra key (`retry_budget_multiplier`) is a proxy concern, not part of
#: the frozen policy shape, so we filter it out when building policies.
#:
#: idempotent / criticality / bulkhead_max_concurrency / courtesy_rps
#: are passed per-upstream below, because they differ between APIs.
_POLICY_FIELDS = (
    "timeout_s",
    "max_attempts",
    "backoff_initial_s",
    "backoff_max_s",
    "jitter_s",
    "breaker_window",
    "breaker_error_threshold",
    "breaker_min_volume",
    "breaker_sleep_s",
    "half_open_probes",
    "half_open_window_s",
)


def _policy_kwargs() -> dict[str, Any]:
    """The sourced defaults, filtered to what `ApiPolicy` accepts."""
    return {name: DEFAULTS[name] for name in _POLICY_FIELDS}


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
# In production Part 3 (Aayush) loads these rows from the api_registry
# table and calls register_policy() once per row. For now we ship one
# entry per upstream the demo test harness talks to. The base URLs are
# the public demo endpoints; swap them via register_policy().
# ---------------------------------------------------------------------------

_WEATHER = ApiPolicy(
    api_key="weather",
    base_url="https://api.open-meteo.com",
    # A weather read has no side effects, so retrying is always safe.
    idempotent=True,
    criticality="medium",
    **_policy_kwargs(),
)

_GEOCODE = ApiPolicy(
    api_key="geocode",
    base_url="https://nominatim.openstreetmap.org",
    # Nominatim is a shared community service; be a polite guest.
    idempotent=True,
    criticality="medium",
    # PRD: "Nominatim 1 rps + User-Agent + cache". The limiter in
    # proxy.py honours this ceiling; the User-Agent has to come from the
    # caller's `headers=` because ApiPolicy has no field for it.
    courtesy_rps=1.0,
    **_policy_kwargs(),
)

_REGISTRY: dict[str, ApiPolicy] = {
    _WEATHER.api_key: _WEATHER,
    _GEOCODE.api_key: _GEOCODE,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_policy(api_key: str) -> ApiPolicy:
    """Return the ApiPolicy registered for ``api_key``.

    Raises ``KeyError`` with a readable message when the key is unknown.
    That is deliberate: a typo on stage should fail loudly rather than
    silently fall through to a default that hides the mistake behind a
    plausible-looking answer.
    """
    try:
        return _REGISTRY[api_key]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none registered)"
        raise KeyError(
            f"no ApiPolicy registered for api_key={api_key!r}; known keys: {known}"
        ) from None


def register_policy(policy: ApiPolicy) -> None:
    """Add or override an ApiPolicy.

    ``ApiPolicy`` is frozen, so this swaps the whole object rather than
    mutating one. Part 3 calls this once per row it loads from
    ``api_registry`` at startup. Calling it twice with the same api_key
    replaces the earlier entry (last writer wins), which is what a seed
    reload after ``docker compose up`` wants.
    """
    _REGISTRY[policy.api_key] = policy


def registered_policies() -> dict[str, ApiPolicy]:
    """A shallow copy of the registry, safe for callers to iterate."""
    return dict(_REGISTRY)


__all__ = [
    "DEFAULTS",
    "RETRY_BUDGET_MULTIPLIER",
    "load_policy",
    "register_policy",
    "registered_policies",
]