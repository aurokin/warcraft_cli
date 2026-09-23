"""Run the installed binaries as real processes and hold every call to the shared contract.

Every journey goes through :func:`run`: a subprocess of ``.venv/bin/<binary>`` with the session's
isolated cache root, whose output must be exactly one JSON envelope on the right stream with the
expected exit code. Anything else is a failure, never a skip; explicit exclusions live in
``WARCRAFT_E2E_SKIP`` (see tests/e2e/conftest.py).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from warcraft_core.envelope import envelope_violations

REPO_ROOT = Path(__file__).resolve().parents[2]
BIN_DIR = REPO_ROOT / ".venv" / "bin"
DEFAULT_TIMEOUT_SECONDS = 180.0

# Exit codes from docs/foundation/ERROR_CONTRACT.md.
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_NOT_FOUND = 4
EXIT_NETWORK = 5

# Process environment shared by every binary run in the session; tests/e2e/conftest.py fills it.
SESSION_ENV: dict[str, str] = {}

# The binaries' per-host rate limiter lives inside each process, and every journey call is a fresh
# process, so the session paces itself here: a minimum gap between consecutive starts of the same
# binary. Wowhead answers bursts with an IP-level 403 that looks exactly like an auth failure.
PACE_SECONDS = float(os.environ.get("WARCRAFT_E2E_PACE_SECONDS", "0.75"))
# Wowhead throttles at the IP level and two full journey files back to back tripped it; give it more room.
PACE_OVERRIDES = {"wowhead": max(PACE_SECONDS, 1.5)}
_LAST_START: dict[str, float] = {}


def _pace(binary: str) -> None:
    previous = _LAST_START.get(binary)
    if previous is not None:
        wait = PACE_OVERRIDES.get(binary, PACE_SECONDS) - (time.monotonic() - previous)
        if wait > 0:
            time.sleep(wait)
    _LAST_START[binary] = time.monotonic()


class JourneyFailure(AssertionError):
    """A binary broke the contract; the message carries the command and both streams."""


@dataclass
class Result:
    binary: str
    args: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    seconds: float
    _payload: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def command(self) -> str:
        return " ".join([self.binary, *self.args])

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK

    @property
    def payload(self) -> dict[str, Any]:
        """The envelope: stdout on success, stderr on failure."""
        if self._payload is None:
            source = self.stdout if self.ok else self.stderr
            self._payload = _single_json_object(source, self)
        return self._payload

    @property
    def data(self) -> dict[str, Any]:
        data = self.payload.get("data")
        return data if isinstance(data, dict) else {}

    @property
    def error_code(self) -> str | None:
        error = self.payload.get("error")
        return error.get("code") if isinstance(error, dict) else None

    def describe(self) -> str:
        out = self.stdout if len(self.stdout) < 2000 else self.stdout[:2000] + "...<truncated>"
        err = self.stderr if len(self.stderr) < 2000 else self.stderr[:2000] + "...<truncated>"
        return f"$ {self.command}\nexit={self.exit_code} ({self.seconds:.1f}s)\n--- stdout ---\n{out}\n--- stderr ---\n{err}"


def _single_json_object(text: str, result: Result) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise JourneyFailure(f"expected one JSON envelope, got nothing\n{result.describe()}")
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise JourneyFailure(f"expected one JSON envelope, got non-JSON ({exc})\n{result.describe()}") from exc
    if not isinstance(value, dict):
        raise JourneyFailure(f"expected a JSON object, got {type(value).__name__}\n{result.describe()}")
    return value


def binary_path(binary: str) -> Path:
    path = BIN_DIR / binary
    if not path.exists():
        raise JourneyFailure(f"{path} is missing; run make dev-deploy-no-link first")
    return path


def run_raw(binary: str, *args: str, timeout: float = DEFAULT_TIMEOUT_SECONDS, env: dict[str, str] | None = None, stdin: str | None = None) -> Result:
    """Execute a binary and capture both streams without asserting anything."""
    merged_env = {**os.environ, **SESSION_ENV, **(env or {})}
    _pace(binary)
    started = time.monotonic()
    completed = subprocess.run(  # noqa: S603 — argv list, never a shell string
        [str(binary_path(binary)), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=merged_env,
        input=stdin,
        cwd=REPO_ROOT,
        check=False,
    )
    return Result(binary, tuple(args), completed.returncode, completed.stdout, completed.stderr, time.monotonic() - started)


def run(
    binary: str,
    *args: str,
    expect: int | None = EXIT_OK,
    error_code: str | None = None,
    stream: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> Result:
    """Run a binary and enforce the output contract.

    - ``expect``: required exit code (``None`` to accept any).
    - ``error_code``: required ``error.code`` for failures.
    - ``stream``: the command emits JSONL (``--stream``); the header line is the envelope.
    On success stdout carries one envelope and stderr carries no JSON error. On failure stdout is
    empty and stderr carries one error envelope. Every envelope must pass ``envelope_violations``.
    A ``--fields`` call prunes the envelope keys this checks, so those journeys use ``run_raw``.
    """
    result = run_raw(binary, *args, timeout=timeout, env=env, stdin=stdin)
    if expect is not None and result.exit_code != expect:
        raise JourneyFailure(f"expected exit {expect}\n{result.describe()}")
    if "Traceback (most recent call last)" in result.stderr:
        raise JourneyFailure(f"traceback leaked\n{result.describe()}")
    if result.ok:
        if stream:
            lines = [line for line in result.stdout.splitlines() if line.strip()]
            if not lines:
                raise JourneyFailure(f"stream produced no lines\n{result.describe()}")
            result._payload = _single_json_object(lines[0], result)
        else:
            result.payload  # noqa: B018 — parses and validates stdout
        if result.payload.get("ok") is not True:
            raise JourneyFailure(f"success exit but ok is not true\n{result.describe()}")
    else:
        if result.stdout.strip():
            raise JourneyFailure(f"failure wrote to stdout\n{result.describe()}")
        if result.payload.get("ok") is not False:
            raise JourneyFailure(f"failure exit but ok is not false\n{result.describe()}")
        if error_code is not None and result.error_code != error_code:
            raise JourneyFailure(f"expected error.code={error_code!r}, got {result.error_code!r}\n{result.describe()}")
    problems = envelope_violations(result.payload)
    if problems:
        raise JourneyFailure(f"envelope violations: {problems}\n{result.describe()}")
    return result


TRANSIENT_ERROR_CODES = frozenset({"timeout", "rate_limited", "upstream_error"})


def run_retrying(binary: str, *args: str, attempts: int = 3, **kwargs: Any) -> Result:
    """``run`` that retries only transient upstream failures (timeouts, rate limits, 5xx) with backoff.

    Everything else fails immediately; a provider that is down three times in a row fails too.
    """
    last: JourneyFailure | None = None
    for attempt in range(1, attempts + 1):
        try:
            return run(binary, *args, **kwargs)
        except JourneyFailure as exc:
            transient = any(f'"code":"{code}"' in str(exc) for code in TRANSIENT_ERROR_CODES)
            if not transient or attempt == attempts:
                raise
            last = exc
            time.sleep(2.0 * attempt)
    raise last or JourneyFailure("unreachable")


def run_text(binary: str, *args: str, expect: int = EXIT_OK, timeout: float = DEFAULT_TIMEOUT_SECONDS, env: dict[str, str] | None = None) -> Result:
    """Run a command whose output is plain text (``--help``)."""
    result = run_raw(binary, *args, timeout=timeout, env=env)
    if result.exit_code != expect:
        raise JourneyFailure(f"expected exit {expect}\n{result.describe()}")
    if "Traceback (most recent call last)" in result.stderr:
        raise JourneyFailure(f"traceback leaked\n{result.describe()}")
    return result


def stream_records(result: Result) -> list[dict[str, Any]]:
    """The ``{"record": ...}`` lines after a JSONL header."""
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    records: list[dict[str, Any]] = []
    for line in lines[1:]:
        value = json.loads(line)
        if not isinstance(value, dict) or "record" not in value:
            raise JourneyFailure(f"malformed stream record: {line[:200]}\n{result.describe()}")
        records.append(value["record"])
    return records


def dead_proxy_env() -> dict[str, str]:
    """Environment that makes every outbound HTTP connection fail fast (exit 5 journeys).

    Combine with :func:`no_cache_env` unless the journey deliberately proves a cache hit.
    """
    return {"HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""}


CACHE_ENV_PREFIXES = (
    "ICY_VEINS", "METHOD", "RAIDBOTS", "RAIDERIO", "WARCRAFT_WIKI", "WARCRAFTLOGS", "WOWHEAD", "LORRGS", "BLIZZARD", "CURSEFORGE",
)


def no_cache_env() -> dict[str, str]:
    """Disable every provider cache so a call must reach the network (or fail trying)."""
    return {f"{prefix}_CACHE_BACKEND": "none" for prefix in CACHE_ENV_PREFIXES}
