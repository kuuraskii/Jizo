"""
JIZO - Part 3 secrets loading (environment + Azure Key Vault).

Owner: Aayush (P3).

Named `secrets.py`, NOT `config.py`.

The build doc Sec. 11.1 tree lists a single `config.py` holding "env + Key
Vault hook + defaults". Part 2 (Aditi) landed first and claimed that name for
the policy registry (`load_policy` / `register_policy` / sourced `DEFAULTS`),
and `proxy.py` imports `RETRY_BUDGET_MULTIPLIER, load_policy` from it - so
overwriting it would break P2.

Rather than clobber a merged module, this file owns ONLY the env + Key Vault
half; P2's `config.py` owns the defaults half. The doc's single-file intent is
therefore split across two modules. That is deliberate, but it is worth
flagging to the team so nobody re-merges them by accident - and so the
duplicated numeric defaults get reconciled to one source.

Variable names follow Appendix A.1 verbatim, so a teammate can paste that
block into `.env` and it works:

    UPSTREAM_TIMEOUT=3.0
    MAX_ATTEMPTS=3
    OPENMETEO_BASE=https://api.open-meteo.com/v1/forecast
    NOMINATIM_BASE=https://nominatim.openstreetmap.org/search
    NOMINATIM_UA=JIZO/1.0 (+https://github.com/kuuraskii/Jizo)
    DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/resilient
    AZURE_KEYVAULT_URL=

**The upstreams are keyless.** Sec. 1 states "Upstream demo APIs are free and
keyless", so there is deliberately no WEATHER_API_KEY or GEOCODE_API_KEY. My
first cut invented those; that was wrong, and an invented key would have sent
requests to Open-Meteo with a parameter it ignores.

Defaults come from Sec. 11.4, which is the single place the build plan says
to read them from ("Do not invent thresholds; use defaults in Sec. 11.4"):

    TIMEOUT=3.0; MAX_ATTEMPTS=3; BACKOFF_INIT=0.075; BACKOFF_MAX=1.8;
    JITTER=0.05; WINDOW=100; ERROR_PCT=25; VOLUME_MIN=20; SLEEP=10;
    PROBE_RATE='10/5s'; SYNC_TIMEOUT_PROD=1.8; ASYNC_TIMEOUT_PROD=6.5;
    RETRY_BUDGET=2.2

**Key custody:** Azure Key Vault in cloud, `.env` locally (Sec. 7.5, Sec.
10). Cloud auth is Managed Identity via `azure-identity`, so there is no
connection string and no secret in any file.

**The fallback is the important design decision.** If Key Vault is
unreachable we log and fall back to `.env` instead of crashing. A demo that
dies because a cloud service hiccuped is worse than one that runs with local
credentials - and that is exactly what happens on stage.

Nothing here ever prints a secret value; `describe()` reports presence only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: Repo root, so `.env` resolves regardless of the working directory.
ROOT = Path(__file__).resolve().parent.parent

#: Secrets we expect to resolve, from Key Vault or `.env`.
SECRET_KEYS = ("DATABASE_URL",)

#: Non-secret settings, always safe to log.
PLAIN_KEYS = (
    "UPSTREAM_TIMEOUT",
    "MAX_ATTEMPTS",
    "OPENMETEO_BASE",
    "NOMINATIM_BASE",
    "NOMINATIM_UA",
    "AZURE_KEYVAULT_URL",
)

#: Sec. 11.4 config defaults. Single source of truth - the same numbers are
#: seeded into `api_registry` by `backend/seed.py`, so keep them in step.
DEFAULTS: dict[str, str] = {
    # Sec. 11.4 / Sec. 4.2. Enterprise profile is 1.8s sync, 6.5s async
    # (Pasunoori 2025 Sec. 7); the demo default is 3.0s.
    "UPSTREAM_TIMEOUT": "3.0",
    "SYNC_TIMEOUT_PROD": "1.8",
    "ASYNC_TIMEOUT_PROD": "6.5",
    "MAX_ATTEMPTS": "3",
    "BACKOFF_INIT": "0.075",
    "BACKOFF_MAX": "1.8",
    "JITTER": "0.05",
    # Sec. 4.4 breaker: 5 parameters from Falahah et al. 2021 Sec. 3.
    "WINDOW": "100",
    "ERROR_PCT": "25",
    "VOLUME_MIN": "20",
    "SLEEP": "10",
    "PROBE_RATE": "10/5s",
    # Sec. 4.3: budget 2.2x baseline volume (Pasunoori 2025 Sec. 6).
    "RETRY_BUDGET": "2.2",
    # Sec. 6.1 / Appendix A.1 - the two keyless demo upstreams.
    "OPENMETEO_BASE": "https://api.open-meteo.com/v1/forecast",
    "NOMINATIM_BASE": "https://nominatim.openstreetmap.org/search",
    # Sec. 10: Nominatim requires a custom User-Agent and <= 1 rps courtesy.
    "NOMINATIM_UA": "JIZO/1.0 (+https://github.com/kuuraskii/Jizo)",
    "NOMINATIM_RPS": "1",
    # Appendix A.1 database name is `resilient`, not `jizo`.
    "DATABASE_URL": (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/resilient"
    ),
    "AZURE_KEYVAULT_URL": "",
}

#: Cache so Key Vault is read once per process, not once per lookup.
_CACHE: dict[str, str] = {}


def _parse_env_file(path: Path) -> dict[str, str]:
    """Read `KEY=VALUE` lines out of a `.env` file.

    A tiny parser rather than a dependency: the format is four lines of
    spec, and P1's "no network, minimal deps" rule applies here too. Blank
    lines and `#` comments are skipped. Values keep everything after the
    first `=`, so a URL containing `=` survives intact.
    """
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    # `utf-8-sig` rather than `utf-8`: a `.env` saved by PowerShell's
    # `Set-Content -Encoding UTF8` starts with a BOM, which would turn the
    # FIRST key into "﻿DATABASE_URL" and it would silently never resolve.
    # Every Windows teammate would hit this.
    #
    # The read is guarded because Windows Notepad's "Unicode" save writes
    # UTF-16, which `utf-8-sig` cannot decode. Without this guard a bad
    # encoding propagated out of `check_health()` as a 500 - and that
    # endpoint's whole contract is "never raise".
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (UnicodeDecodeError, OSError) as exc:
        print(f"[secrets] could not read {path.name} "
              f"({type(exc).__name__}); ignoring it and using defaults")
        return {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _from_key_vault(vault_uri: str) -> dict[str, str]:
    """Fetch secrets from Azure Key Vault using Managed Identity.

    Any failure is non-fatal by design - the caller falls back to `.env`. We
    catch broadly on purpose: a missing vault, a revoked identity and a
    network timeout should all produce the same graceful degradation rather
    than three different stack traces.
    """
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        # DefaultAzureCredential picks up the Managed Identity token from
        # the App Service / Container Apps environment automatically.
        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=vault_uri, credential=credential)

        found: dict[str, str] = {}
        for key in SECRET_KEYS:
            # Key Vault names cannot contain '_', so DATABASE_URL is stored
            # as `database-url`.
            name = key.replace("_", "-").lower()
            secret = client.get_secret(name)
            if secret and secret.value:
                found[key] = secret.value
        return found

    except Exception as exc:  # noqa: BLE001 - see docstring
        # Deliberately broad. Never print the exception payload - it can
        # contain the vault URI, and a traceback on stage is not helpful.
        print(f"[config] Key Vault unavailable ({type(exc).__name__}); "
              "falling back to .env")
        return {}


def _load() -> dict[str, str]:
    """Resolve configuration from the best available source."""
    # A real environment variable always wins, so CI and the container
    # platform can override without editing files.
    resolved: dict[str, str] = {}
    env_file = _parse_env_file(ROOT / ".env")
    values = {**DEFAULTS, **env_file}

    vault_uri = os.environ.get("AZURE_KEYVAULT_URL") or values.get("AZURE_KEYVAULT_URL")
    if vault_uri:
        resolved.update(_from_key_vault(vault_uri))
        if not resolved:
            print("[config] Key Vault returned nothing usable; using .env")

    # Precedence: real env var > Key Vault > .env > built-in default.
    #
    # The `key in resolved` guard matters: `values` is {**DEFAULTS, **env_file}
    # and DEFAULTS always contains DATABASE_URL, so without the guard the
    # `elif` below would fire for every key and silently clobber whatever
    # Key Vault returned on the line above. That inverted the documented
    # precedence and, in cloud, defeated the entire point of the vault.
    for key in set(PLAIN_KEYS) | set(SECRET_KEYS) | set(DEFAULTS):
        from_env = os.environ.get(key)
        if from_env:
            resolved[key] = from_env
        elif key in resolved:
            # Already supplied by Key Vault - a lower-priority source must
            # not overwrite it.
            continue
        elif key in values and values[key]:
            resolved[key] = values[key]

    return resolved


@dataclass(frozen=True)
class Config:
    """Resolved configuration for one running process.

    `frozen=True` for the same reason `ApiPolicy` is: configuration must not
    be mutated mid-flight by accident.
    """

    database_url: str
    upstream_timeout: float
    max_attempts: int
    openmeteo_base: str
    nominatim_base: str
    nominatim_ua: str
    nominatim_rps: float
    key_vault_url: Optional[str]

    def describe(self) -> str:
        """A log-safe summary. Presence only, never values."""
        return (
            f"database={'set' if self.database_url else 'MISSING'} "
            f"timeout={self.upstream_timeout}s "
            f"max_attempts={self.max_attempts} "
            f"key_vault={'configured' if self.key_vault_url else 'none'}"
        )


def _num(values: dict[str, str], key: str, fallback: float) -> float:
    """Read a numeric setting, tolerating junk by falling back."""
    try:
        return float(values[key])
    except (KeyError, TypeError, ValueError):
        return fallback


def get_config(refresh: bool = False) -> Config:
    """Build the config object. Cached after the first call."""
    if refresh:
        _CACHE.clear()

    if not _CACHE:
        _CACHE.update(_load())

    values = _CACHE
    return Config(
        database_url=values.get("DATABASE_URL", ""),
        upstream_timeout=_num(values, "UPSTREAM_TIMEOUT", 3.0),
        max_attempts=int(_num(values, "MAX_ATTEMPTS", 3)),
        openmeteo_base=values.get("OPENMETEO_BASE", DEFAULTS["OPENMETEO_BASE"]),
        nominatim_base=values.get("NOMINATIM_BASE", DEFAULTS["NOMINATIM_BASE"]),
        nominatim_ua=values.get("NOMINATIM_UA", DEFAULTS["NOMINATIM_UA"]),
        nominatim_rps=_num(values, "NOMINATIM_RPS", 1.0),
        key_vault_url=values.get("AZURE_KEYVAULT_URL") or None,
    )