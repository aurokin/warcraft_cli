from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner
from wowhead_cli.compare_presets import resolve_compare_options, resolve_compare_preset
from wowhead_cli.main import app

runner = CliRunner()


def test_resolve_compare_preset_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="gear, quest, spell"):
        resolve_compare_preset("npc")


def test_resolve_compare_options_uses_preset_defaults() -> None:
    options = resolve_compare_options(preset="gear")
    assert options.comparable_fields == ("name", "quality", "icon")
    assert options.comment_sample == 2
    assert options.include_gatherer is True


def test_resolve_compare_options_explicit_flags_override_preset() -> None:
    options = resolve_compare_options(preset="gear", comment_sample=5, include_gatherer=False)
    assert options.comment_sample == 5
    assert options.include_gatherer is False


def test_compare_gear_preset_omits_title_field_diff(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env: int = 11):  # noqa: ANN001
        if entity_id == 19019:
            return {"name": "Thunderfury", "quality": 5, "icon": "inv_sword_39"}
        return {"name": "Maladath", "quality": 4, "icon": "inv_sword_49"}

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return """
        <html><head>
          <meta property="og:title" content="Title">
          <meta name="description" content="Desc">
        </head><body></body></html>
        """

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)

    result = runner.invoke(app, ["compare", "--preset", "gear", "item:19019", "item:19351", "--comment-sample", "0"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["preset"]["key"] == "gear"
    assert set(payload["comparison"]["fields"]) == {"name", "quality", "icon"}
    assert "title" not in payload["comparison"]["fields"]
