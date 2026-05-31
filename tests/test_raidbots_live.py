"""Opt-in live smoke tests for the Raidbots provider.

Live coverage follows the repo's per-provider convention: gated by the ``RAIDBOTS_LIVE_TESTS``
env flag (wired in ``tests/conftest.py``) and run via ``make test-live``. There is intentionally
no dedicated CI workflow — only wowhead has one (its scraper canaries). Every other provider
(raiderio/method/warcraftlogs/wowprogress/…) relies on generic ``ci.yml`` (``make test-fast``)
plus this opt-in live path, and raidbots matches that.
"""

from __future__ import annotations

import os

import pytest
from raidbots_cli.client import RaidbotsClient, resolve_report_id
from raidbots_cli.report import parse_report

pytestmark = pytest.mark.live


def _live_report_id() -> str:
    raw = os.getenv("RAIDBOTS_LIVE_REPORT_ID")
    if not raw:
        pytest.skip("Set RAIDBOTS_LIVE_REPORT_ID to a real Raidbots report ID/URL to run this smoke test.")
    return resolve_report_id(raw)


def test_live_inspect_report_smoke() -> None:
    report_id = _live_report_id()
    with RaidbotsClient() as client:
        data = client.report_data(report_id)
    parsed = parse_report(data, report_id=report_id)
    assert parsed["kind"] in {"quick_sim", "multi_profile", "unknown"}
    assert parsed["report_id"] == report_id


def test_live_report_input_smoke() -> None:
    report_id = _live_report_id()
    with RaidbotsClient() as client:
        text = client.report_input(report_id)
    assert isinstance(text, str)
    assert text.strip()
