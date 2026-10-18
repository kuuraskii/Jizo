"""Tests for the public `jizo` package.

The point of these is that "usable by a third party" is a claim that rots
quietly. A new eager import in some module would quietly make `import jizo`
pull in a database driver again; a renamed function would quietly break
`from jizo import ...`. These tests fail loudly instead.

Owner: third-party packaging work.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import backend
import jizo

#: Repository root, so we can find pyproject.toml.
ROOT = Path(__file__).resolve().parent.parent

#: Third-party packages that must NOT be needed to use the library core.
#: Each one belongs to a feature behind an extra: the evidence store, the
#: FastAPI service and the dashboard's native window.
HEAVY_DEPS = ("sqlalchemy", "fastapi", "asyncpg", "webview", "starlette")


def run_python(code: str) -> str:
    """Run a snippet in a clean interpreter and return its stdout.

    A subprocess, not this process: by the time these tests run, `backend`
    and its database layer are already in `sys.modules`, so measuring imports
    in-process would measure nothing.
    """
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert done.returncode == 0, f"snippet failed:\n{done.stderr}"
    return done.stdout.strip()


# ---------------------------------------------------------------------------
# 1. Importing the library must stay cheap.
# ---------------------------------------------------------------------------


def test_importing_jizo_does_not_drag_in_the_heavy_dependencies():
    """`import jizo` alone must not load the database or the web server.

    This is the whole reason the public package exists. If somebody later adds
    an eager `from .dashboard import ...` at the top of a module that `jizo`
    reaches, this test catches it on the next CI run.
    """
    loaded = run_python(
        "import sys, jizo; "
        f"print(','.join(m for m in {HEAVY_DEPS!r} if m in sys.modules))"
    )
    assert loaded == "", (
        "importing jizo pulled in " + loaded + ". Those belong behind the "
        "db/server/gui extras, not the core import."
    )


def test_using_the_breaker_does_not_drag_in_the_database_layer():
    """Asking for a circuit breaker must not load SQLAlchemy.

    `from jizo import CircuitBreaker` is the single most likely thing a third
    party will write, so it gets its own check.
    """
    loaded = run_python(
        "import sys; from jizo import CircuitBreaker; "
        "print(','.join(m for m in ('sqlalchemy', 'fastapi', 'asyncpg') "
        "if m in sys.modules))"
    )
    assert loaded == "", f"importing the breaker pulled in {loaded}"


def test_the_headline_api_is_reachable_from_the_package_root():
    """`resilient_get` must be importable straight from `jizo`.

    It used to live only at `backend.proxy.resilient_get`, which forced every
    caller to know the internal file layout. That is not a public API.
    """
    assert callable(jizo.resilient_get)
    assert jizo.resilient_get is not None


# ---------------------------------------------------------------------------
# 2. The public surface must stay complete and self-consistent.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", jizo.__all__)
def test_every_advertised_name_actually_resolves(name):
    """Nothing in `__all__` may be a promise the package cannot keep."""
    assert getattr(jizo, name) is not None


def test_the_export_map_and_all_list_agree():
    """`_EXPORTS` drives resolution, `__all__` drives the docs and IDE hints.

    They are written out separately on purpose - `__all__` has to be a literal
    for type checkers - so a test has to hold them together.
    """
    assert set(jizo._EXPORTS) | {"__version__"} == set(jizo.__all__)


def test_the_export_map_and_all_list_agree_for_the_internal_package():
    """Same guarantee for `backend`, which the team still imports from."""
    assert set(backend._EXPORTS) == set(backend.__all__)


def test_an_unknown_name_fails_with_a_helpful_message():
    """A typo should say what went wrong, not raise a bare ImportError."""
    with pytest.raises(AttributeError) as caught:
        jizo.definitely_not_a_real_name  # noqa: B018 - the access is the test
    assert "definitely_not_a_real_name" in str(caught.value)
    assert "__all__" in str(caught.value)


def test_dir_lists_the_public_surface():
    """Tab completion in an editor should offer the real names."""
    listed = dir(jizo)
    for name in ("resilient_get", "CircuitBreaker", "ApiPolicy"):
        assert name in listed


# ---------------------------------------------------------------------------
# 3. The internal package must not lose anything.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", backend.__all__)
def test_the_teams_existing_imports_still_work(name):
    """`from backend import ...` is what the rest of the team writes.

    Making the package lazy was an optimisation; it must not be a breaking
    change for the four parts of JIZO built before this one.
    """
    assert getattr(backend, name) is not None


@pytest.mark.parametrize(
    "name",
    ["store", "db", "compare", "schemas", "models", "secrets", "axes"],
)
def test_submodules_are_still_reachable_as_attributes(name):
    """`backend.store` worked by accident before; it works deliberately now."""
    module = getattr(backend, name)
    assert module.__name__.endswith(name)


def test_dashboard_factory_is_not_shadowed_by_the_module():
    """A regression guard, kept from the original `backend/__init__.py`.

    Re-exporting the `dashboard()` *function* here would shadow the
    `backend.dashboard` *module* of the same name and break `backend.dashboard.x`.
    """
    assert backend.dashboard.__name__.endswith("dashboard")
    assert callable(backend.dashboard.dashboard)


# ---------------------------------------------------------------------------
# 4. Packaging metadata must agree with the code.
# ---------------------------------------------------------------------------


def test_the_version_is_defined_once_and_matches_the_packaging_metadata():
    """`pyproject.toml` reads the version from here, so they cannot drift."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = pyproject["tool"]["setuptools"]["dynamic"]["version"]["attr"]

    assert declared == "jizo.__version__", (
        "pyproject should read the version from jizo.__version__ so there is "
        "only one place to update it"
    )
    assert jizo.__version__.count(".") == 2, (
        f"{jizo.__version__!r} should look like a released version, e.g. 0.1.0"
    )


def test_the_core_dependencies_are_only_the_ones_the_core_needs():
    """A third party's first `pip install jizo` must stay small.

    Two dependencies. Anything heavier belongs in an extra - that is what
    makes the library pleasant to adopt for a single HTTP call.
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    core = pyproject["project"]["dependencies"]

    names = sorted(dep.split(">")[0].split("=")[0].split("<")[0].strip()
                   for dep in core)
    assert names == ["httpx", "pydantic"], (
        f"core dependencies are {names}; expected only httpx and pydantic"
    )


def test_the_heavy_libraries_are_declared_as_extras():
    """Anything we moved out of the core has to be installable on request."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = pyproject["project"]["optional-dependencies"]

    for extra, package in [
        ("db", "sqlalchemy"),
        ("server", "fastapi"),
        ("gui", "pywebview"),
    ]:
        declared = " ".join(extras[extra])
        assert package in declared, f"{package} must be in the [{extra}] extra"


def test_the_install_instructions_do_not_point_at_the_wrong_package():
    """`pip install jizo` installs someone else's package, not ours.

    The distribution name `jizo` is taken on PyPI by an unrelated 1 KB
    placeholder published in February 2025. On Python 3.11 it does not even
    install - that package declares it needs >= 3.12. So a README saying
    `pip install jizo` would hand a third party the wrong code and let them
    conclude JIZO is broken.

    The supported route is a direct reference to this repository.
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "git+https://github.com/kuuraskii/Jizo.git" in readme, (
        "the README should document installing straight from GitHub"
    )

    # Flag only lines that install the `jizo` package itself and name no
    # source. Other install commands in the README (`pip install -r
    # requirements-gui.txt`) are fine and are not our business here.
    bare = [
        line.strip()
        for line in readme.splitlines()
        if line.strip().startswith("pip install")
        and "jizo" in line
        and "git+https://github.com/kuuraskii/Jizo" not in line
    ]
    assert not bare, (
        "these lines would install a stranger's package from PyPI:\n  "
        + "\n  ".join(bare)
    )


def test_the_package_declares_the_types_it_ships():
    """PEP 561: without the marker, consumers get no types at all."""
    assert (ROOT / "jizo" / "py.typed").exists()
    assert (ROOT / "backend" / "py.typed").exists()


def test_the_install_publishes_the_code_under_a_private_name():
    """`backend` must not be installed top-level.

    A package called `backend` in site-packages collides with unrelated
    projects on the same machine. It is published as `jizo._core` instead.
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packages = pyproject["tool"]["setuptools"]["packages"]

    assert "jizo" in packages
    assert "jizo._core" in packages
    assert "backend" not in packages, (
        "shipping a top-level `backend` package would collide with other "
        "projects; the code must be published as jizo._core"
    )


# ---------------------------------------------------------------------------
# 5. The public API has to actually work, not just exist.
# ---------------------------------------------------------------------------


@pytest.fixture()
def healthy_upstream():
    """A stand-in upstream that answers 200 and counts how often it was hit.

    Lets the test below assert on the *number of outbound requests*, which is
    the thing that actually matters: a protected call should not quietly retry
    its way to an answer.
    """
    import httpx

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"temperature_c": 21.5})

    transport = httpx.MockTransport(handler)
    return transport, calls


def test_a_third_party_can_guard_a_call_using_only_the_public_api(healthy_upstream):
    """The end-to-end promise, written the way an outsider would write it.

    Everything here comes from `jizo`. If this needs `backend.*` to pass, the
    public package has a hole in it.

    Synchronous test wrapping `asyncio.run`, to match the rest of the suite.
    """
    import asyncio

    import httpx

    transport, calls = healthy_upstream

    async def scenario() -> tuple[jizo.ResilientResponse, jizo.CircuitBreaker]:
        policy = jizo.ApiPolicy(
            api_key="demo",
            base_url="https://example.invalid",
            timeout_s=2.0,
            breaker_min_volume=2,
        )
        breaker = jizo.CircuitBreaker(policy)

        async with httpx.AsyncClient(transport=transport) as client:
            last = None
            for _ in range(2):
                last = await jizo.resilient_get(
                    "demo",
                    "https://example.invalid/weather",
                    # Pass the policy *and* the breaker. Without a `policy=`,
                    # `resilient_get` looks the key up in the global registry
                    # and raises a KeyError for anything unregistered.
                    policy=policy,
                    breaker=breaker,
                    client=client,
                )
        assert last is not None
        return last, breaker

    result, breaker = asyncio.run(scenario())

    assert len(calls) == 2, "a healthy call must reach the upstream exactly once"
    assert result.status_code == 200
    assert result.data["temperature_c"] == 21.5
    assert result.served_from == jizo.ServedFrom.LIVE
    assert result.attempts == 1, "a healthy call must not retry"
    assert breaker.state == jizo.BreakerState.CLOSED


def test_the_policy_registry_is_reachable_from_the_public_package():
    """`resilient_get` falls back to the registry when no policy is passed.

    A third party registering policies at startup must be able to do it
    through `jizo`, not by reaching into `backend.config`.
    """
    assert callable(jizo.register_policy)
    assert callable(jizo.load_policy)

    known = jizo.registered_policies()
    assert known, "the seed registers weather and geocode at import time"
    assert "weather" in known