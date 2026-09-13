"""Repo tooling that only breaks silently.

`make test-live` is the documented way to run the live suites, so a provider flag that exists in
tests/conftest.py but not in the Makefile means that provider's live tests are skipped without
saying so.
"""

from __future__ import annotations

import re
from pathlib import Path

from conftest import LIVE_TEST_ENV_BY_FILE

REPO_ROOT = Path(__file__).resolve().parents[1]


def _makefile_live_env_flags() -> set[str]:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    block = re.search(r"^LIVE_TEST_ENV :=(.*?)(?=\n\n)", makefile, re.DOTALL | re.MULTILINE)
    assert block is not None, "Makefile no longer defines LIVE_TEST_ENV"
    return set(re.findall(r"([A-Z_]+_LIVE_TESTS)=1", block.group(1)))


def test_makefile_live_env_matches_conftest_registry() -> None:
    registered = {
        flag
        for value in LIVE_TEST_ENV_BY_FILE.values()
        for flag in ((value,) if isinstance(value, str) else value)
    }
    assert _makefile_live_env_flags() == registered
