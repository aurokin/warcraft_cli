"""Live parser canaries for pinned Wowhead entity pages (AUR-359)."""

from __future__ import annotations

import time

import httpx
import pytest
from wowhead_cli.expansion_profiles import build_entity_url, resolve_expansion
from wowhead_cli.page_parser import (
    extract_comments_dataset,
    extract_linked_entities_from_href,
    parse_page_meta_json,
    parse_page_metadata,
)

from tests.fixtures.wowhead_canaries import PARSER_CANARIES, ParserCanary

pytestmark = pytest.mark.live


def _require_live() -> None:
    import os

    if os.getenv("WOWHEAD_LIVE_TESTS", "").strip().lower() not in {"1", "true", "yes", "on"}:
        pytest.skip("Set WOWHEAD_LIVE_TESTS=1 to run Wowhead parser canaries.")


def _fetch_entity_html(canary: ParserCanary) -> tuple[str, str, float]:
    profile = resolve_expansion(canary.expansion)
    url = build_entity_url(profile, canary.entity_type, canary.entity_id)
    started = time.perf_counter()
    with httpx.Client(timeout=25.0, follow_redirects=True) as client:
        response = client.get(url)
        latency_ms = (time.perf_counter() - started) * 1000
        response.raise_for_status()
    return url, response.text, latency_ms


@pytest.mark.parametrize("canary", PARSER_CANARIES, ids=lambda row: row.case_id)
def test_live_wowhead_parser_canary_page(canary: ParserCanary) -> None:
    _require_live()
    profile = resolve_expansion(canary.expansion)
    url, html, latency_ms = _fetch_entity_html(canary)

    assert latency_ms < 15000, f"canary page fetch too slow ({latency_ms:.0f}ms) for {canary.case_id}"

    meta = parse_page_metadata(html, fallback_url=url)
    assert isinstance(meta.get("canonical_url"), str)
    assert meta["canonical_url"].startswith(profile.wowhead_base)

    page_meta = parse_page_meta_json(html)
    data_env = page_meta.get("dataEnv") if isinstance(page_meta, dict) else None
    assert isinstance(data_env, dict)
    assert data_env.get("env") == profile.data_env

    linked = extract_linked_entities_from_href(html, source_url=meta["canonical_url"])
    assert len(linked) > 0, f"no linked entities parsed for {canary.case_id}"

    comments = extract_comments_dataset(html)
    assert isinstance(comments, list)
