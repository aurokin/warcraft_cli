from __future__ import annotations

import pytest
from article_provider_testkit import error_payload, payload_for_live, require_live
from icy_veins_cli.main import app
from typer.testing import CliRunner

pytestmark = pytest.mark.live

runner = CliRunner()
SPEC_GUIDE_REF = "mistweaver-monk-pve-healing-guide"
CLASS_HUB_REF = "monk-guide"
ROLE_GUIDE_REF = "healing-guide"
EASY_MODE_REF = "fury-warrior-pve-dps-easy-mode"
LEVELING_REF = "mistweaver-monk-leveling-guide"
PVP_REF = "mistweaver-monk-pvp-guide"
SPEC_BUILDS_TALENTS_REF = "mistweaver-monk-pve-healing-spec-builds-talents"
ROTATION_REF = "mistweaver-monk-pve-healing-rotation-cooldowns-abilities"
STAT_PRIORITY_REF = "mistweaver-monk-pve-healing-stat-priority"
GEMS_ENCHANTS_CONSUMABLES_REF = "mistweaver-monk-pve-healing-gems-enchants-consumables"
SPELL_SUMMARY_REF = "mistweaver-monk-pve-healing-spell-summary"
RESOURCES_REF = "mistweaver-monk-resources"
MACROS_ADDONS_REF = "mistweaver-monk-pve-healing-macros-addons"
MYTHIC_PLUS_TIPS_REF = "mistweaver-monk-pve-healing-mythic-plus-tips"
SIMULATIONS_REF = "mistweaver-monk-pve-healing-simulations"
RAID_GUIDE_REF = "mistweaver-monk-pve-healing-nerub-ar-palace-raid-guide"
SPECIAL_EVENT_REF = "mistweaver-monk-mists-of-pandaria-remix-guide"

FAMILY_GUIDE_CASES = [
    (SPEC_GUIDE_REF, "spec_guide", "family_navigation"),
    (CLASS_HUB_REF, "class_hub", "current_page"),
    (ROLE_GUIDE_REF, "role_guide", "current_page"),
    (RAID_GUIDE_REF, "raid_guide", "family_navigation"),
    (SPECIAL_EVENT_REF, "special_event_guide", "family_navigation"),
    (PVP_REF, "pvp", "family_navigation"),
    (LEVELING_REF, "leveling", "family_navigation"),
    (SPEC_BUILDS_TALENTS_REF, "spec_builds_talents", "family_navigation"),
    (ROTATION_REF, "rotation_guide", "family_navigation"),
    (STAT_PRIORITY_REF, "stat_priority", "family_navigation"),
    (GEMS_ENCHANTS_CONSUMABLES_REF, "gems_enchants_consumables", "family_navigation"),
    (SPELL_SUMMARY_REF, "spell_summary", "family_navigation"),
    (RESOURCES_REF, "resources", "family_navigation"),
    (MACROS_ADDONS_REF, "macros_addons", "family_navigation"),
    (MYTHIC_PLUS_TIPS_REF, "mythic_plus_tips", "family_navigation"),
    (SIMULATIONS_REF, "simulations", "family_navigation"),
]


def test_live_icy_search_contract() -> None:
    require_live("Icy Veins")
    payload = payload_for_live(runner, app, ["search", "mistweaver monk guide", "--limit", "5"], provider_name="Icy Veins")

    assert payload["count"] >= 1
    first = payload["results"][0]
    assert first["entity_type"] == "guide"
    assert first["id"] == SPEC_GUIDE_REF
    assert first["metadata"]["content_family"] == "spec_guide"
    assert first["follow_up"]["recommended_command"] == f"icy-veins guide {SPEC_GUIDE_REF}"


def test_live_icy_resolve_easy_mode_contract() -> None:
    require_live("Icy Veins")
    payload = payload_for_live(runner, app, ["resolve", "fury warrior easy mode", "--limit", "5"], provider_name="Icy Veins")

    assert payload["resolved"] is True
    assert payload["match"]["id"] == EASY_MODE_REF
    assert payload["match"]["metadata"]["content_family"] == "easy_mode"
    assert payload["next_command"] == f"icy-veins guide {EASY_MODE_REF}"


@pytest.mark.parametrize(("guide_ref", "content_family", "traversal_scope"), FAMILY_GUIDE_CASES)
def test_live_icy_family_guide_contracts(guide_ref: str, content_family: str, traversal_scope: str) -> None:
    require_live("Icy Veins")
    payload = payload_for_live(runner, app, ["guide", guide_ref], provider_name="Icy Veins")

    assert payload["guide"]["slug"] == guide_ref
    assert payload["guide"]["content_family"] == content_family
    assert payload["guide"]["traversal_scope"] == traversal_scope
    assert payload["article"]["section_count"] >= 1


def test_live_icy_spec_guide_has_navigation() -> None:
    require_live("Icy Veins")
    payload = payload_for_live(runner, app, ["guide", SPEC_GUIDE_REF], provider_name="Icy Veins")

    assert payload["navigation"]["count"] >= 2


def test_live_icy_class_hub_guide_full_stays_local() -> None:
    require_live("Icy Veins")
    payload = payload_for_live(runner, app, ["guide-full", CLASS_HUB_REF], provider_name="Icy Veins")

    assert payload["guide"]["slug"] == CLASS_HUB_REF
    assert payload["guide"]["content_family"] == "class_hub"
    assert payload["guide"]["page_count"] == 1
    assert len(payload["pages"]) == 1
    assert payload["pages"][0]["guide"]["slug"] == CLASS_HUB_REF


def test_live_icy_invalid_ref_contract() -> None:
    require_live("Icy Veins")
    result = runner.invoke(app, ["guide", "news-roundup"])

    assert result.exit_code == 1
    payload = error_payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_guide_ref"


def test_live_icy_unsupported_query_scope_hint() -> None:
    require_live("Icy Veins")
    payload = payload_for_live(runner, app, ["search", "patch notes", "--limit", "5"], provider_name="Icy Veins")

    assert payload["count"] == 0
    assert payload["results"] == []
    assert payload["scope_hint"]["code"] == "patch_notes"
