"""Local guide-bundle list, search, query, inspect, rebuild, and refresh commands."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wowhead_cli.main import app
from wowhead_cli.wowhead_client import WowheadClient

from tests.wowhead_testkit import SAMPLE_GUIDE_HTML, runner, write_bundle_fixture


def test_guide_bundle_refresh_skips_fresh_bundle_with_default_max_age(tmp_path: Path) -> None:
    root = tmp_path / "wowhead_exports"
    bundle_dir = root / "guide-3143-frost"
    bundle_dir.mkdir(parents=True)
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    (bundle_dir / "manifest.json").write_text(
        json.dumps(
            {
                "export_version": 2,
                "exported_at": now,
                "guide_fetched_at": now,
                "expansion": "retail",
                "output_dir": str(bundle_dir),
                "guide": {
                    "input": "3143",
                    "id": 3143,
                    "page_url": "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
                },
                "page": {
                    "title": "Frost Death Knight DPS Guide - Midnight",
                    "canonical_url": "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
                },
                "counts": {
                    "sections": 11,
                    "navigation_links": 15,
                    "linked_entities": 52,
                    "gatherer_entities": 52,
                    "hydrated_entities": 0,
                    "comments": 9,
                },
                "hydration": {
                    "enabled": False,
                    "types": [],
                    "limit": 0,
                    "hydrated_at": None,
                },
                "export_options": {
                    "guide_ref": "3143",
                    "max_links": 250,
                    "include_replies": False,
                },
                "files": {"manifest_json": "manifest.json"},
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["guide-bundle-refresh", "3143", "--root", str(root)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["refresh"] == {
        "updated": False,
        "reason": "fresh",
        "max_age_hours": 24,
    }



def test_guide_bundle_refresh_updates_stale_bundle_and_reuses_manifest_settings(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fake_guide_page_html(self, guide_id: int):  # noqa: ANN001
        assert guide_id == 3143
        return SAMPLE_GUIDE_HTML

    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        if (entity_type, entity_id) == ("spell", 49020):
            return {
                "name": "Obliterate",
                "tooltip": "<table><tr><td><b>Obliterate</b><br>Talent<br>Instant<br>A brutal attack.</td></tr></table>",
            }
        if (entity_type, entity_id) == ("item", 249277):
            return {
                "name": "Bellamy's Final Judgement",
                "tooltip": "<table><tr><td><b>Bellamy's Final Judgement</b><br>Item Level 639</td></tr></table>",
            }
        raise AssertionError(f"Unexpected tooltip lookup: {(entity_type, entity_id)}")

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_page_html", fake_guide_page_html)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)

    export_dir = tmp_path / "guide-export"
    export_result = runner.invoke(
        app,
        [
            "guide-export",
            "3143",
            "--out",
            str(export_dir),
            "--hydrate-linked-entities",
            "--hydrate-type",
            "spell,item",
            "--hydrate-limit",
            "2",
        ],
    )
    assert export_result.exit_code == 0

    manifest_path = export_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stale = (datetime.now(UTC) - timedelta(hours=48)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    manifest["exported_at"] = stale
    manifest["guide_fetched_at"] = stale
    manifest["hydration"]["hydrated_at"] = stale
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = runner.invoke(app, ["guide-bundle-refresh", str(export_dir)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["refresh"] == {
        "updated": True,
        "reason": "stale",
        "max_age_hours": 24,
    }
    assert payload["data"]["hydration"]["enabled"] is True
    assert payload["data"]["hydration"]["types"] == ["spell", "item"]
    assert payload["data"]["counts"]["hydrated_entities"] == 2
    assert (export_dir / "entities" / "manifest.json").exists()



def test_guide_bundle_refresh_fetches_from_the_bundle_expansion(monkeypatch, tmp_path: Path) -> None:
    fetched_from: list[str] = []

    def fake_guide_page_html(self: WowheadClient, guide_id: int) -> str:
        fetched_from.append(self.expansion.key)
        return SAMPLE_GUIDE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_page_html", fake_guide_page_html)
    export_dir = tmp_path / "guide-export"
    export_result = runner.invoke(app, ["--expansion", "wotlk", "guide-export", "3143", "--out", str(export_dir)])
    assert export_result.exit_code == 0, export_result.output

    result = runner.invoke(app, ["guide-bundle-refresh", str(export_dir), "--force"])

    assert result.exit_code == 0, result.output
    assert fetched_from == ["wotlk", "wotlk"]
    assert json.loads(result.stdout)["data"]["expansion"] == "wotlk"
    assert json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))["expansion"] == "wotlk"


def test_guide_bundle_refresh_rehydrates_only_stale_hydrated_entities(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tooltip_calls: dict[tuple[str, int], int] = {}

    def fake_guide_page_html(self, guide_id: int):  # noqa: ANN001
        assert guide_id == 3143
        return SAMPLE_GUIDE_HTML

    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        key = (entity_type, entity_id)
        tooltip_calls[key] = tooltip_calls.get(key, 0) + 1
        if key == ("spell", 49020):
            return {
                "name": "Obliterate",
                "tooltip": "<table><tr><td><b>Obliterate</b><br>Talent<br>Instant<br>A brutal attack.</td></tr></table>",
            }
        if key == ("item", 249277):
            return {
                "name": "Bellamy's Final Judgement",
                "tooltip": "<table><tr><td><b>Bellamy's Final Judgement</b><br>Item Level 639</td></tr></table>",
            }
        raise AssertionError(f"Unexpected tooltip lookup: {key}")

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.guide_page_html", fake_guide_page_html)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)

    export_dir = tmp_path / "guide-export"
    export_result = runner.invoke(
        app,
        [
            "guide-export",
            "3143",
            "--out",
            str(export_dir),
            "--hydrate-linked-entities",
            "--hydrate-type",
            "spell,item",
            "--hydrate-limit",
            "2",
        ],
    )
    assert export_result.exit_code == 0
    assert tooltip_calls == {
        ("spell", 49020): 1,
        ("item", 249277): 1,
    }

    tooltip_calls.clear()

    stale = (datetime.now(UTC) - timedelta(hours=48)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    fresh = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    manifest_path = export_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["exported_at"] = stale
    manifest["guide_fetched_at"] = stale
    manifest["hydration"]["hydrated_at"] = stale
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    entities_manifest_path = export_dir / "entities" / "manifest.json"
    entities_manifest = json.loads(entities_manifest_path.read_text(encoding="utf-8"))
    items = entities_manifest["items"]
    for row in items:
        if row["entity_type"] == "spell":
            row["stored_at"] = fresh
        elif row["entity_type"] == "item":
            row["stored_at"] = stale
    entities_manifest["hydrated_at"] = stale
    entities_manifest_path.write_text(json.dumps(entities_manifest), encoding="utf-8")

    result = runner.invoke(app, ["guide-bundle-refresh", str(export_dir)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["refresh"] == {
        "updated": True,
        "reason": "stale",
        "max_age_hours": 24,
    }
    assert tooltip_calls == {
        ("item", 249277): 1,
    }

    refreshed_entities_manifest = json.loads(entities_manifest_path.read_text(encoding="utf-8"))
    refreshed_items = {
        (row["entity_type"], row["id"]): row
        for row in refreshed_entities_manifest["items"]
    }
    assert refreshed_items[("spell", 49020)]["stored_at"] == fresh
    assert refreshed_items[("item", 249277)]["stored_at"] != stale
    assert refreshed_items[("spell", 49020)]["storage_source"] == "bundle_store"
    assert refreshed_items[("item", 249277)]["storage_source"] == "live_fetch"
    assert refreshed_entities_manifest["counts_by_storage_source"] == {
        "bundle_store": 1,
        "live_fetch": 1,
    }
    assert payload["data"]["hydration"]["source_counts"] == {
        "bundle_store": 1,
        "live_fetch": 1,
    }



def test_guide_bundle_list_discovers_exported_bundles(tmp_path) -> None:
    root = tmp_path / "wowhead_exports"
    corpus_a = root / "guide-3143-frost"
    corpus_b = root / "guide-42-other"
    junk = root / "not-a-corpus"
    corpus_a.mkdir(parents=True)
    corpus_b.mkdir(parents=True)
    junk.mkdir(parents=True)
    fresh = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    stale = (datetime.now(UTC) - timedelta(hours=48)).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    (corpus_a / "manifest.json").write_text(
        json.dumps(
            {
                "export_version": 1,
                "expansion": "retail",
                "output_dir": str(corpus_a),
                "exported_at": stale,
                "guide_fetched_at": stale,
                "guide": {"id": 3143, "page_url": "https://www.wowhead.com/guide=3143"},
                "page": {
                    "title": "Frost Death Knight DPS Guide - Midnight",
                    "canonical_url": "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
                },
                "counts": {
                    "sections": 11,
                    "navigation_links": 15,
                    "linked_entities": 27,
                    "gatherer_entities": 52,
                    "hydrated_entities": 2,
                    "comments": 9,
                },
                "hydration": {
                    "enabled": True,
                    "types": ["spell", "item"],
                    "limit": 2,
                    "hydrated_at": stale,
                    "source_counts": {"entity_cache": 1, "live_fetch": 1},
                },
                "files": {"manifest_json": "manifest.json"},
            }
        ),
        encoding="utf-8",
    )
    (corpus_b / "manifest.json").write_text(
        json.dumps(
            {
                "export_version": 1,
                "expansion": "classic",
                "output_dir": str(corpus_b),
                "exported_at": fresh,
                "guide_fetched_at": fresh,
                "guide": {"id": 42, "page_url": "https://www.wowhead.com/guide=42"},
                "page": {
                    "title": "Arcane Mage Guide",
                    "canonical_url": "https://www.wowhead.com/guide/classes/mage/arcane/overview-pve-dps",
                },
                "counts": {
                    "sections": 4,
                    "navigation_links": 6,
                    "linked_entities": 5,
                    "gatherer_entities": 3,
                    "hydrated_entities": 0,
                    "comments": 2,
                },
                "hydration": {
                    "enabled": False,
                    "types": [],
                    "limit": 0,
                    "hydrated_at": None,
                    "source_counts": {},
                },
                "files": {"manifest_json": "manifest.json"},
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["guide-bundle-list", "--root", str(root)])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["root"] == str(root)
    assert payload["data"]["count"] == 2
    assert payload["data"]["max_age_hours"] == 24
    assert [row["guide_id"] for row in payload["data"]["bundles"]] == [42, 3143]
    assert payload["data"]["bundles"][0]["dir_name"] == "guide-42-other"
    assert payload["data"]["bundles"][0]["title"] == "Arcane Mage Guide"
    assert payload["data"]["bundles"][0]["freshness"]["max_age_hours"] == 24
    assert payload["data"]["bundles"][0]["freshness"]["bundle"] == "fresh"
    assert payload["data"]["bundles"][0]["freshness"]["bundle_reasons"] == []
    assert payload["data"]["bundles"][0]["freshness"]["hydration"] == "disabled"
    assert payload["data"]["bundles"][0]["freshness"]["hydration_reasons"] == ["disabled"]
    assert payload["data"]["bundles"][0]["hydration"] == {
        "enabled": False,
        "types": [],
        "limit": 0,
        "hydrated_at": None,
        "hydrated_entities": 0,
        "source_counts": {},
    }
    assert payload["data"]["bundles"][1]["counts"]["linked_entities"] == 27
    assert payload["data"]["bundles"][1]["freshness"]["max_age_hours"] == 24
    assert payload["data"]["bundles"][1]["freshness"]["bundle"] == "stale"
    assert payload["data"]["bundles"][1]["freshness"]["bundle_reasons"] == ["max_age_exceeded"]
    assert payload["data"]["bundles"][1]["freshness"]["hydration"] == "stale"
    assert "bundle_stale" in payload["data"]["bundles"][1]["freshness"]["hydration_reasons"]
    assert "max_age_exceeded" in payload["data"]["bundles"][1]["freshness"]["hydration_reasons"]
    assert payload["data"]["bundles"][1]["hydration"] == {
        "enabled": True,
        "types": ["spell", "item"],
        "limit": 2,
        "hydrated_at": stale,
        "hydrated_entities": 2,
        "source_counts": {"entity_cache": 1, "live_fetch": 1},
    }

    result = runner.invoke(app, ["guide-bundle-list", "--root", str(root), "--max-age-hours", "72"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["max_age_hours"] == 72
    assert payload["data"]["stale_reason_counts"] == {"bundle": {}, "hydration": {}}
    assert payload["data"]["bundles"][1]["freshness"]["max_age_hours"] == 72
    assert payload["data"]["bundles"][1]["freshness"]["bundle"] == "fresh"
    assert payload["data"]["bundles"][1]["freshness"]["bundle_reasons"] == []
    assert payload["data"]["bundles"][1]["freshness"]["hydration"] == "fresh"
    assert payload["data"]["bundles"][1]["freshness"]["hydration_reasons"] == []



def test_guide_bundle_list_uses_root_index_when_available(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "wowhead_exports"
    bundle_dir = root / "guide-3143-frost"
    bundle_dir.mkdir(parents=True)
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    (bundle_dir / "manifest.json").write_text(
        json.dumps({"guide": {"id": 3143}}),
        encoding="utf-8",
    )
    (root / "index.json").write_text(
        json.dumps(
            {
                "index_version": 1,
                "updated_at": now,
                "root": str(root),
                "count": 1,
                "bundles": [
                    {
                        "path": str(bundle_dir),
                        "dir_name": bundle_dir.name,
                        "guide_id": 3143,
                        "title": "Frost Death Knight DPS Guide - Midnight",
                        "canonical_url": "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
                        "expansion": "retail",
                        "export_version": 2,
                        "counts": {
                            "sections": 11,
                            "navigation_links": 15,
                            "linked_entities": 52,
                            "gatherer_entities": 52,
                            "hydrated_entities": 1,
                            "comments": 9,
                        },
                        "exported_at": now,
                        "guide_fetched_at": now,
                        "hydration": {
                            "enabled": True,
                            "types": ["spell"],
                            "limit": 1,
                            "hydrated_at": now,
                            "hydrated_entities": 1,
                            "source_counts": {"entity_cache": 1},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def fail_scan(root_path: Path) -> list[dict[str, object]]:  # noqa: ANN202
        raise AssertionError(f"scan should not be used when a valid index exists: {root_path}")

    monkeypatch.setattr("wowhead_cli.main._scan_guide_bundle_rows", fail_scan)

    result = runner.invoke(app, ["guide-bundle-list", "--root", str(root)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["bundles"][0]["guide_id"] == 3143
    assert payload["data"]["stale_reason_counts"] == {"bundle": {}, "hydration": {}}
    assert payload["data"]["bundles"][0]["hydration"]["source_counts"] == {"entity_cache": 1}
    assert payload["data"]["bundles"][0]["freshness"]["max_age_hours"] == 24
    assert payload["data"]["bundles"][0]["freshness"]["bundle"] == "fresh"
    assert payload["data"]["bundles"][0]["freshness"]["bundle_reasons"] == []
    assert payload["data"]["bundles"][0]["freshness"]["hydration"] == "fresh"
    assert payload["data"]["bundles"][0]["freshness"]["hydration_reasons"] == []



def test_guide_bundle_search_returns_ranked_matches_and_follow_up_commands(tmp_path: Path) -> None:
    root = tmp_path / "wowhead_exports"
    frost = root / "guide-3143-frost"
    arcane = root / "guide-42-arcane"
    frost.mkdir(parents=True)
    arcane.mkdir(parents=True)
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    for bundle_dir, guide_id, title, expansion in [
        (frost, 3143, "Frost Death Knight DPS Guide - Midnight", "retail"),
        (arcane, 42, "Arcane Mage Guide", "classic"),
    ]:
        (bundle_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "export_version": 2,
                    "output_dir": str(bundle_dir),
                    "exported_at": now,
                    "guide_fetched_at": now,
                    "expansion": expansion,
                    "guide": {"id": guide_id, "page_url": f"https://www.wowhead.com/guide={guide_id}"},
                    "page": {
                        "title": title,
                        "canonical_url": f"https://www.wowhead.com/guide/{guide_id}",
                    },
                    "counts": {
                        "sections": 1,
                        "navigation_links": 1,
                        "linked_entities": 1,
                        "gatherer_entities": 1,
                        "hydrated_entities": 0,
                        "comments": 1,
                    },
                    "hydration": {
                        "enabled": False,
                        "types": [],
                        "limit": 0,
                        "hydrated_at": None,
                        "source_counts": {},
                    },
                }
            ),
            encoding="utf-8",
        )

    result = runner.invoke(app, ["guide-bundle-search", "frost death knight", "--root", str(root)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"] == "frost death knight"
    assert payload["data"]["count"] == 1
    assert payload["data"]["stale_reason_counts"] == {"bundle": {}, "hydration": {}}
    assert payload["data"]["matches"][0]["guide_id"] == 3143
    assert "title" in payload["data"]["matches"][0]["match_reasons"]
    assert payload["data"]["matches"][0]["suggested_query_command"] == (
        f"wowhead guide-query 3143 'frost death knight' --root {root}"
    )

    result = runner.invoke(app, ["guide-bundle-search", "42", "--root", str(root)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["matches"][0]["guide_id"] == 42
    assert "guide_id" in payload["data"]["matches"][0]["match_reasons"]

    result = runner.invoke(app, ["guide-bundle-search", "classic", "--root", str(root), "--limit", "1"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["count"] == 1
    assert payload["data"]["matches"][0]["guide_id"] == 42
    assert "expansion" in payload["data"]["matches"][0]["match_reasons"]



def test_guide_bundle_search_uses_root_index_when_available(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "wowhead_exports"
    bundle_dir = root / "guide-3143-frost"
    bundle_dir.mkdir(parents=True)
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    (bundle_dir / "manifest.json").write_text(json.dumps({"guide": {"id": 3143}}), encoding="utf-8")
    (root / "index.json").write_text(
        json.dumps(
            {
                "index_version": 1,
                "updated_at": now,
                "root": str(root),
                "count": 1,
                "bundles": [
                    {
                        "path": str(bundle_dir),
                        "dir_name": bundle_dir.name,
                        "guide_id": 3143,
                        "title": "Frost Death Knight DPS Guide - Midnight",
                        "canonical_url": "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
                        "expansion": "retail",
                        "export_version": 2,
                        "counts": {
                            "sections": 11,
                            "navigation_links": 15,
                            "linked_entities": 52,
                            "gatherer_entities": 52,
                            "hydrated_entities": 1,
                            "comments": 9,
                        },
                        "exported_at": now,
                        "guide_fetched_at": now,
                        "hydration": {
                            "enabled": True,
                            "types": ["spell"],
                            "limit": 1,
                            "hydrated_at": now,
                            "hydrated_entities": 1,
                            "source_counts": {"entity_cache": 1},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def fail_scan(root_path: Path) -> list[dict[str, object]]:  # noqa: ANN202
        raise AssertionError(f"scan should not be used when a valid index exists: {root_path}")

    monkeypatch.setattr("wowhead_cli.main._scan_guide_bundle_rows", fail_scan)

    result = runner.invoke(app, ["guide-bundle-search", "frost", "--root", str(root)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["matches"][0]["guide_id"] == 3143
    assert "title" in payload["data"]["matches"][0]["match_reasons"]



def test_guide_bundle_query_returns_cross_bundle_matches(tmp_path: Path) -> None:
    root = tmp_path / "wowhead_exports"
    write_bundle_fixture(
        root,
        dir_name="guide-3143-frost",
        guide_id=3143,
        title="Frost Death Knight DPS Guide - Midnight",
        sections=[
            {
                "ordinal": 1,
                "level": 2,
                "title": "Rotation",
                "content_text": "Use Obliterate and Frost Strike in your rotation.",
            }
        ],
        linked_entities=[
            {
                "entity_type": "spell",
                "id": 49020,
                "name": "Obliterate",
                "url": "https://www.wowhead.com/spell=49020/obliterate",
                "citation_url": "https://www.wowhead.com/spell=49020/obliterate",
                "sources": ["href", "gatherer"],
                "source_kind": "href",
            }
        ],
    )
    write_bundle_fixture(
        root,
        dir_name="guide-42-arcane",
        guide_id=42,
        title="Arcane Mage Guide",
        expansion="classic",
        sections=[
            {
                "ordinal": 1,
                "level": 2,
                "title": "Rotation",
                "content_text": "Use Arcane Blast and Arcane Missiles.",
            }
        ],
        linked_entities=[
            {
                "entity_type": "spell",
                "id": 30451,
                "name": "Arcane Blast",
                "url": "https://www.wowhead.com/spell=30451/arcane-blast",
                "citation_url": "https://www.wowhead.com/spell=30451/arcane-blast",
                "sources": ["href"],
                "source_kind": "href",
            }
        ],
    )

    result = runner.invoke(app, ["guide-bundle-query", "obliterate", "--root", str(root)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["searched_bundle_count"] == 2
    assert payload["data"]["count"] == 1
    assert payload["data"]["counts"] == {
        "sections": 1,
        "analysis_surfaces": 0,
        "navigation": 0,
        "linked_entities": 1,
        "gatherer_entities": 0,
        "comments": 0,
    }
    assert payload["data"]["bundles"][0]["guide_id"] == 3143
    assert payload["data"]["bundles"][0]["match_count"] == 2
    assert payload["data"]["bundles"][0]["match_counts"]["linked_entities"] == 1
    assert payload["data"]["bundles"][0]["suggested_query_command"] == (
        f"wowhead guide-query 3143 obliterate --root {root}"
    )
    assert payload["data"]["top"][0]["kind"] == "linked_entity"
    assert payload["data"]["top"][0]["bundle"]["guide_id"] == 3143



def test_guide_bundle_query_uses_filters_and_root_index(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "wowhead_exports"
    frost = write_bundle_fixture(
        root,
        dir_name="guide-3143-frost",
        guide_id=3143,
        title="Frost Death Knight DPS Guide - Midnight",
        linked_entities=[
            {
                "entity_type": "spell",
                "id": 49020,
                "name": "Obliterate",
                "url": "https://www.wowhead.com/spell=49020/obliterate",
                "citation_url": "https://www.wowhead.com/spell=49020/obliterate",
                "sources": ["href", "gatherer"],
                "source_kind": "href",
            }
        ],
    )
    write_bundle_fixture(
        root,
        dir_name="guide-42-arcane",
        guide_id=42,
        title="Arcane Mage Guide",
        linked_entities=[
            {
                "entity_type": "spell",
                "id": 30451,
                "name": "Obliterate Echo",
                "url": "https://www.wowhead.com/spell=30451/obliterate-echo",
                "citation_url": "https://www.wowhead.com/spell=30451/obliterate-echo",
                "sources": ["href"],
                "source_kind": "href",
            }
        ],
    )
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    (root / "index.json").write_text(
        json.dumps(
            {
                "index_version": 1,
                "updated_at": now,
                "root": str(root),
                "count": 2,
                "bundles": [
                    {
                        "path": str(frost),
                        "dir_name": frost.name,
                        "guide_id": 3143,
                        "title": "Frost Death Knight DPS Guide - Midnight",
                        "canonical_url": "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
                        "expansion": "retail",
                        "export_version": 2,
                        "counts": {
                            "sections": 0,
                            "navigation_links": 0,
                            "linked_entities": 1,
                            "gatherer_entities": 0,
                            "hydrated_entities": 0,
                            "comments": 0,
                        },
                        "exported_at": now,
                        "guide_fetched_at": now,
                        "hydration": {
                            "enabled": False,
                            "types": [],
                            "limit": 0,
                            "hydrated_at": None,
                            "hydrated_entities": 0,
                            "source_counts": {},
                        },
                    },
                    {
                        "path": str(root / "guide-42-arcane"),
                        "dir_name": "guide-42-arcane",
                        "guide_id": 42,
                        "title": "Arcane Mage Guide",
                        "canonical_url": "https://www.wowhead.com/guide/42",
                        "expansion": "classic",
                        "export_version": 2,
                        "counts": {
                            "sections": 0,
                            "navigation_links": 0,
                            "linked_entities": 1,
                            "gatherer_entities": 0,
                            "hydrated_entities": 0,
                            "comments": 0,
                        },
                        "exported_at": now,
                        "guide_fetched_at": now,
                        "hydration": {
                            "enabled": False,
                            "types": [],
                            "limit": 0,
                            "hydrated_at": None,
                            "hydrated_entities": 0,
                            "source_counts": {},
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    def fail_scan(root_path: Path) -> list[dict[str, object]]:  # noqa: ANN202
        raise AssertionError(f"scan should not be used when a valid index exists: {root_path}")

    monkeypatch.setattr("wowhead_cli.main._scan_guide_bundle_rows", fail_scan)

    result = runner.invoke(
        app,
        [
            "guide-bundle-query",
            "obliterate",
            "--root",
            str(root),
            "--kind",
            "linked_entities",
            "--linked-source",
            "multi",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["count"] == 1
    assert payload["data"]["filters"]["kinds"] == ["linked_entities"]
    assert payload["data"]["filters"]["linked_sources"] == ["multi"]
    assert payload["data"]["counts"] == {
        "sections": 0,
        "analysis_surfaces": 0,
        "navigation": 0,
        "linked_entities": 1,
        "gatherer_entities": 0,
        "comments": 0,
    }
    assert payload["data"]["bundles"][0]["guide_id"] == 3143
    assert set(payload["data"]["top"][0]["sources"]) == {"href", "gatherer"}



def test_guide_bundle_inspect_reports_counts_and_index_status(tmp_path: Path) -> None:
    root = tmp_path / "wowhead_exports"
    bundle_dir = write_bundle_fixture(
        root,
        dir_name="guide-3143-frost",
        guide_id=3143,
        title="Frost Death Knight DPS Guide - Midnight",
        sections=[
            {
                "ordinal": 1,
                "level": 2,
                "title": "Rotation",
                "content_text": "Use Obliterate.",
            }
        ],
        linked_entities=[
            {
                "entity_type": "spell",
                "id": 49020,
                "name": "Obliterate",
                "url": "https://www.wowhead.com/spell=49020/obliterate",
                "citation_url": "https://www.wowhead.com/spell=49020/obliterate",
                "sources": ["href", "gatherer"],
                "source_kind": "href",
            }
        ],
        comments=[
            {
                "id": 7,
                "user": "Tester",
                "body": "Obliterate section is clear.",
                "citation_url": "https://www.wowhead.com/guide=3143#comments:id=7",
            }
        ],
    )
    manifest_path = bundle_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["counts"]["hydrated_entities"] = 1
    manifest["hydration"] = {
        "enabled": True,
        "types": ["spell"],
        "limit": 1,
        "hydrated_at": manifest["exported_at"],
        "source_counts": {"entity_cache": 1},
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    entities_dir = bundle_dir / "entities"
    entities_dir.mkdir()
    (entities_dir / "manifest.json").write_text(
        json.dumps(
            {
                "hydrated_at": manifest["exported_at"],
                "count": 1,
                "counts_by_type": {"spell": 1},
                "counts_by_storage_source": {"entity_cache": 1},
                "items": [
                    {
                        "entity_type": "spell",
                        "id": 49020,
                        "path": "entities/spell/49020.json",
                        "stored_at": manifest["exported_at"],
                        "storage_source": "entity_cache",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    rebuild = runner.invoke(app, ["guide-bundle-index-rebuild", "--root", str(root)])
    assert rebuild.exit_code == 0

    result = runner.invoke(app, ["guide-bundle-inspect", "3143", "--root", str(root)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["guide"]["id"] == 3143
    assert payload["data"]["freshness"]["bundle"] == "fresh"
    assert payload["data"]["freshness"]["bundle_reasons"] == []
    assert payload["data"]["freshness"]["hydration"] == "fresh"
    assert payload["data"]["freshness"]["hydration_reasons"] == []
    assert payload["data"]["counts"]["manifest"] == payload["data"]["counts"]["observed"]
    assert payload["data"]["hydration"]["enabled"] is True
    assert payload["data"]["entities_manifest"]["count"] == 1
    assert payload["data"]["index"]["valid"] is True
    assert payload["data"]["index"]["contains_bundle"] is True
    assert payload["data"]["issues"] == []

    summary_result = runner.invoke(app, ["guide-bundle-inspect", "3143", "--root", str(root), "--summary"])
    assert summary_result.exit_code == 0
    summary_payload = json.loads(summary_result.stdout)
    assert summary_payload["data"]["issue_count"] == 0
    assert summary_payload["data"]["issue_codes"] == []
    assert summary_payload["data"]["missing_files"] == []
    assert summary_payload["data"]["count_mismatches"] == []



def test_guide_bundle_inspect_reports_missing_files_and_invalid_index(tmp_path: Path) -> None:
    root = tmp_path / "wowhead_exports"
    bundle_dir = write_bundle_fixture(
        root,
        dir_name="guide-3143-frost",
        guide_id=3143,
        title="Frost Death Knight DPS Guide - Midnight",
        sections=[
            {
                "ordinal": 1,
                "level": 2,
                "title": "Rotation",
                "content_text": "Use Obliterate.",
            }
        ],
    )
    (bundle_dir / "sections.jsonl").unlink()
    (root / "index.json").write_text(json.dumps({"broken": True}), encoding="utf-8")

    result = runner.invoke(app, ["guide-bundle-inspect", str(bundle_dir)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    issue_codes = {row["code"] for row in payload["data"]["issues"]}
    assert {"missing_file", "count_mismatch", "invalid_index"}.issubset(issue_codes)
    assert payload["data"]["files"]["sections_jsonl"]["exists"] is False
    assert payload["data"]["counts"]["manifest"]["sections"] == 1
    assert payload["data"]["counts"]["observed"]["sections"] == 0
    assert payload["data"]["index"]["exists"] is True
    assert payload["data"]["index"]["valid"] is False



def test_guide_bundle_index_rebuild_rewrites_invalid_index(tmp_path: Path) -> None:
    root = tmp_path / "wowhead_exports"
    write_bundle_fixture(
        root,
        dir_name="guide-3143-frost",
        guide_id=3143,
        title="Frost Death Knight DPS Guide - Midnight",
    )
    write_bundle_fixture(
        root,
        dir_name="guide-42-arcane",
        guide_id=42,
        title="Arcane Mage Guide",
        expansion="classic",
    )
    (root / "index.json").write_text(json.dumps({"broken": True}), encoding="utf-8")

    result = runner.invoke(app, ["guide-bundle-index-rebuild", "--root", str(root)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["count"] == 2
    assert payload["data"]["index"]["previous"] == {"exists": True, "valid": False, "count": 0}
    assert payload["data"]["index"]["current"] == {"exists": True, "valid": True, "count": 2}

    rebuilt_index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    assert rebuilt_index["count"] == 2
    assert {row["guide_id"] for row in rebuilt_index["bundles"]} == {42, 3143}




def test_guide_bundle_search_reports_the_matches_its_limit_cut_off(tmp_path: Path) -> None:
    root = tmp_path / "wowhead_exports"
    for guide_id, spec in [(3143, "Frost"), (3144, "Unholy"), (3145, "Blood")]:
        write_bundle_fixture(
            root,
            dir_name=f"guide-{guide_id}-{spec.lower()}",
            guide_id=guide_id,
            title=f"{spec} Death Knight Guide",
        )

    result = runner.invoke(app, ["guide-bundle-search", "death knight", "--root", str(root), "--limit", "2"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert data["count"] == len(data["matches"]) == 2
    assert data["total_matches"] == 3
    assert data["truncated"] is True
