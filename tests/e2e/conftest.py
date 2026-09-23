"""Session setup for the local end-to-end journeys (``make test-e2e``).

Policy: a provider that cannot be exercised is a failure, not a skip, unless it is named in
``WARCRAFT_E2E_SKIP`` (comma-separated provider names, plus ``redis``). Caches are real but
isolated to a per-session directory so journeys can assert cache hits without touching
``~/.cache``; config, state, and data roots stay real so credentials, saved tokens, and the local
SimulationCraft checkout resolve exactly as they do for you.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.e2e import harness

E2E_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Every test under tests/e2e is an end-to-end journey; no module can forget the marker."""
    del config
    for item in items:
        if E2E_DIR in Path(str(item.path)).resolve().parents:
            item.add_marker(pytest.mark.e2e)


def _skip_list() -> frozenset[str]:
    raw = os.environ.get("WARCRAFT_E2E_SKIP", "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


@pytest.fixture(scope="session", autouse=True)
def e2e_session_env() -> Iterator[dict[str, str]]:
    """Point every binary at an isolated cache root for the whole session."""
    with tempfile.TemporaryDirectory(prefix="warcraft-e2e-cache-") as cache_root:
        env = {
            "XDG_CACHE_HOME": cache_root,
            # Politeness stays on (per-host interval); nothing here should need to be faster.
        }
        env.pop("WARCRAFT_HTTP_MIN_INTERVAL_SECONDS", None)
        harness.SESSION_ENV.clear()
        harness.SESSION_ENV.update(env)
        yield env
        harness.SESSION_ENV.clear()


@pytest.fixture(scope="session")
def cache_root(e2e_session_env: dict[str, str]) -> Path:
    return Path(e2e_session_env["XDG_CACHE_HOME"])


@pytest.fixture(scope="session")
def doctor_rows(e2e_session_env: dict[str, str]) -> dict[str, dict[str, Any]]:
    """``warcraft doctor`` once per session: provider readiness and auth posture by name."""
    result = harness.run("warcraft", "doctor")
    rows = result.data["providers"]
    return {row["provider"]: row for row in rows}


@pytest.fixture(scope="session")
def skip_list() -> frozenset[str]:
    return _skip_list()


@pytest.fixture
def require(doctor_rows: dict[str, dict[str, Any]], skip_list: frozenset[str]):
    """``require("warcraftlogs")``: skip only when excluded explicitly; otherwise a missing credential fails."""

    def _require(*providers: str) -> None:
        for provider in providers:
            if provider in skip_list:
                pytest.skip(f"{provider} excluded via WARCRAFT_E2E_SKIP")
            row = doctor_rows.get(provider)
            if row is None:
                raise AssertionError(f"{provider} is not registered in warcraft doctor: {sorted(doctor_rows)}")
            auth = row.get("auth") or {}
            # Anything other than an explicit True is "not configured": a doctor that stops
            # reporting the field must fail the run, not quietly let the journeys through.
            if auth.get("required") and auth.get("configured") is not True:
                raise AssertionError(
                    f"{provider} needs credentials that are not configured; see docs/architecture/E2E_TESTING.md "
                    f"(auth={json.dumps(auth)[:300]})"
                )

    return _require


@pytest.fixture
def optional(skip_list: frozenset[str]):
    """``optional("redis")``: components that are only exercised when configured."""

    def _optional(component: str, env_var: str) -> str:
        if component in skip_list:
            pytest.skip(f"{component} excluded via WARCRAFT_E2E_SKIP")
        value = os.environ.get(env_var, "").strip()
        if not value:
            pytest.skip(f"{component} not configured: set {env_var} to exercise it")
        return value

    return _optional


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    """Per-test scratch directory for exports and packets."""
    path = tmp_path / "out"
    path.mkdir()
    return path
