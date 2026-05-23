from __future__ import annotations

import pytest
from article_provider_testkit import error_payload, payload_for_live, require_live
from method_cli.main import app
from typer.testing import CliRunner

pytestmark = pytest.mark.live

runner = CliRunner()
GUIDE_REF = "mistweaver-monk"
SECONDARY_GUIDE_REF = "midnight-alchemy-profession-guide"
TERTIARY_GUIDE_REF = "harati-renown-reputation-guide"
QUATERNARY_GUIDE_REF = "world-of-warcraft-midnight-season-1-dungeon-locations"


def test_live_method_search_contract() -> None:
    require_live("Method")
    payload = payload_for_live(runner, app, ["search", "mistweaver monk guide", "--limit", "5"], provider_name="Method")

    assert payload["search_query"] == "mistweaver monk"
    assert payload["count"] >= 1
    first = payload["results"][0]
    assert first["entity_type"] == "guide"
    assert first["id"] == GUIDE_REF
    assert first["follow_up"]["recommended_command"] == f"method guide {GUIDE_REF}"


def test_live_method_unsupported_query_scope_hint() -> None:
    require_live("Method")
    payload = payload_for_live(runner, app, ["search", "tier list", "--limit", "5"], provider_name="Method")

    assert payload["count"] == 0
    assert payload["results"] == []
    assert payload["scope_hint"]["code"] == "tier_list"


def test_live_method_resolve_contract() -> None:
    require_live("Method")
    payload = payload_for_live(runner, app, ["resolve", "mistweaver monk"], provider_name="Method")

    assert payload["resolved"] is True
    assert payload["match"]["id"] == GUIDE_REF
    assert payload["next_command"] == f"method guide {GUIDE_REF}"


def test_live_method_guide_contract() -> None:
    require_live("Method")
    payload = payload_for_live(runner, app, ["guide", GUIDE_REF], provider_name="Method")

    assert payload["guide"]["slug"] == GUIDE_REF
    assert payload["guide"]["page_url"].startswith("https://www.method.gg/guides/mistweaver-monk")
    assert payload["navigation"]["count"] >= 2
    assert payload["article"]["section_count"] >= 1
    assert payload["linked_entities"]["count"] > 0


def test_live_method_guide_full_contract() -> None:
    require_live("Method")
    payload = payload_for_live(runner, app, ["guide-full", GUIDE_REF], provider_name="Method")

    assert payload["guide"]["slug"] == GUIDE_REF
    assert payload["guide"]["page_count"] >= 2
    assert len(payload["pages"]) == payload["guide"]["page_count"]
    assert len(payload["citations"]["pages"]) == payload["guide"]["page_count"]
    assert payload["linked_entities"]["count"] >= len(payload["linked_entities"]["items"])


def test_live_method_unsupported_surface_contract() -> None:
    require_live("Method")
    result = runner.invoke(app, ["guide", "tier-list"])

    assert result.exit_code == 1
    payload = error_payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "unsupported_guide_surface"


def test_live_method_secondary_supported_family_contract() -> None:
    require_live("Method")
    payload = payload_for_live(runner, app, ["guide", SECONDARY_GUIDE_REF], provider_name="Method")

    assert payload["guide"]["slug"] == SECONDARY_GUIDE_REF
    assert payload["guide"]["content_family"] == "profession_guide"
    assert payload["guide"]["supported_surface"] is True
    assert payload["guide"]["author"] == "Roguery"
    assert payload["guide"]["last_updated"] == "5th March 2026"
    assert payload["article"]["section_count"] >= 1


def test_live_method_reputation_family_contract() -> None:
    require_live("Method")
    payload = payload_for_live(runner, app, ["guide", TERTIARY_GUIDE_REF], provider_name="Method")

    assert payload["guide"]["slug"] == TERTIARY_GUIDE_REF
    assert payload["guide"]["content_family"] == "reputation_guide"
    assert payload["guide"]["supported_surface"] is True
    assert payload["guide"]["author"] == "Roguery"
    assert payload["guide"]["last_updated"] == "26th February 2026"
    assert payload["linked_entities"]["count"] >= 1


def test_live_method_article_family_contract() -> None:
    require_live("Method")
    payload = payload_for_live(runner, app, ["guide", QUATERNARY_GUIDE_REF], provider_name="Method")

    assert payload["guide"]["slug"] == QUATERNARY_GUIDE_REF
    assert payload["guide"]["content_family"] == "article_guide"
    assert payload["guide"]["supported_surface"] is True
    assert payload["guide"]["author"] == "Tayder"
    assert payload["guide"]["last_updated"] == "26th February 2026"
    assert payload["article"]["section_count"] >= 1
