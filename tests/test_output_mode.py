from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner
from warcraft_api.cache import FileCacheStore
from wowhead_cli.main import app

from tests.cli_testkit import all_cli_apps

runner = CliRunner()
CLI_APPS = all_cli_apps()


def test_search_defaults_to_compact_json(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 3, "id": 19019, "name": "Thunderfury", "typeName": "Item"},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["search", "thunderfury"])
    assert result.exit_code == 0
    assert result.stdout.startswith('{"ok":true,')
    assert '"query":"thunderfury"' in result.stdout
    assert result.stdout.count("\n") == 1


def test_pretty_flag_emits_indented_json(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 3, "id": 19019, "name": "Thunderfury", "typeName": "Item"},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["--pretty", "search", "thunderfury"])
    assert result.exit_code == 0
    assert result.stdout.startswith("{\n")
    assert '  "query": "thunderfury",' in result.stdout


def test_search_results_include_ranking_metadata(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 3, "id": 19019, "name": "Thunderfury", "typeName": "Item", "popularity": 5},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["search", "thunderfury", "--limit", "1"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["results"][0]["ranking"]["score"] > 0
    assert "match_reasons" in payload["data"]["results"][0]["ranking"]


def test_search_results_include_follow_up_metadata(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 3, "id": 19019, "name": "Thunderfury", "typeName": "Item", "popularity": 5},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["search", "thunderfury", "--limit", "1"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["results"][0]["follow_up"]["recommended_surface"] == "entity"
    assert payload["data"]["results"][0]["follow_up"]["command"] == "wowhead entity item 19019"


def test_compact_flag_truncates_long_string_fields(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        return {
            "name": "Thunderfury",
            "tooltip": "long tooltip prose " * 50,
        }

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["--compact", "entity", "item", "19019"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    tooltip = payload["data"]["tooltip"]["html"]
    assert isinstance(tooltip, str)
    assert len(tooltip) == 280
    assert tooltip.endswith("...")


def test_fields_flag_projects_requested_fields(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 3, "id": 19019, "name": "Thunderfury", "typeName": "Item"},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["--fields", "query,data.count,data.results", "search", "thunderfury", "--limit", "1"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert set(payload.keys()) == {"query", "data"}
    assert set(payload["data"]) == {"count", "results"}
    assert payload["query"] == "thunderfury"
    assert payload["data"]["count"] == 1
    assert payload["data"]["results"][0]["id"] == 19019


def test_fields_strict_fails_when_requested_path_is_missing(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        return {"name": "Thunderfury", "quality": 5}

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(
        app,
        ["--fields-strict", "--fields", "data.entity.name,data.tooltip.summary", "entity", "item", "19019"],
    )
    # missing_fields is a caller mistake, so it exits 2 (usage) per docs/foundation/ERROR_CONTRACT.md.
    assert result.exit_code == 2

    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "missing_fields"
    assert payload["error"]["details"] == {"missing_fields": ["data.tooltip.summary"]}


def test_profile_human_emits_pretty_json(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 3, "id": 19019, "name": "Thunderfury", "typeName": "Item"},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(app, ["--profile", "human", "search", "thunderfury"])
    assert result.exit_code == 0
    assert result.stdout.startswith("{\n")


def test_compact_max_chars_flag_controls_truncation(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        return {
            "name": "Thunderfury",
            "tooltip": "long tooltip prose " * 50,
        }

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(app, ["--compact", "--compact-max-chars", "120", "entity", "item", "19019"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    tooltip = payload["data"]["tooltip"]["html"]
    assert len(tooltip) == 120
    assert tooltip.endswith("...")


def test_fields_flag_supports_nested_paths(monkeypatch) -> None:
    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        return {"name": "Thunderfury", "quality": 5}

    def fake_html(self, entity_type: str, entity_id: int):  # noqa: ANN001
        return "<html><body><script>var lv_comments0 = [];</script></body></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.entity_page_html", fake_html)
    result = runner.invoke(
        app, ["--fields", "data.entity.name,data.tooltip.quality,data.tooltip.summary", "entity", "item", "19019"]
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert set(payload.keys()) == {"data", "fields_missing"}
    assert payload["data"] == {"entity": {"name": "Thunderfury"}, "tooltip": {"quality": 5}}
    # Without --fields-strict the absent path is reported rather than silently dropped.
    assert payload["fields_missing"] == ["data.tooltip.summary"]


def test_cache_inspect_reports_file_cache_stats(tmp_path: Path, monkeypatch) -> None:
    cache_dir = tmp_path / "cache"
    store = FileCacheStore(cache_dir)
    now = 1000.0
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "file")
    monkeypatch.setenv("WOWHEAD_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr("warcraft_api.cache.time.time", lambda: now)

    store.set("search_suggestions:active", {"query": "thunderfury"}, ttl_seconds=60)
    store.set("entity_response:expired", {"entity": {"id": 19019}}, ttl_seconds=10)

    monkeypatch.setattr("warcraft_api.cache.time.time", lambda: now + 20)
    result = runner.invoke(app, ["cache-inspect"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["settings"]["backend"] == "file"
    assert payload["data"]["stats"]["totals"] == {"active": 1, "expired": 1, "invalid": 0, "total": 2}
    assert payload["data"]["stats"]["age_summary"]["oldest_entry_age_hours"] >= 0
    assert payload["data"]["stats"]["namespaces"]["entity_response"]["expired"] == 1
    assert payload["data"]["stats"]["namespaces"]["search_suggestions"]["active"] == 1


def test_cache_inspect_summary_hides_zero_value_fields(tmp_path: Path, monkeypatch) -> None:
    cache_dir = tmp_path / "cache"
    store = FileCacheStore(cache_dir)
    now = 1000.0
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "file")
    monkeypatch.setenv("WOWHEAD_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr("warcraft_api.cache.time.time", lambda: now)

    store.set("search_suggestions:active", {"query": "thunderfury"}, ttl_seconds=60)
    store.set("entity_response:expired", {"entity": {"id": 19019}}, ttl_seconds=10)

    monkeypatch.setattr("warcraft_api.cache.time.time", lambda: now + 20)
    result = runner.invoke(app, ["cache-inspect", "--summary", "--namespace-limit", "1", "--hide-zero"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["stats"]["totals"] == {"active": 1, "expired": 1, "total": 2}
    assert payload["data"]["stats"]["namespace_count"] == 2
    assert payload["data"]["stats"]["top_namespaces"] == [
        {"namespace": "entity_response", "expired": 1, "total": 1}
    ]
    assert payload["data"]["stats"]["truncated_namespaces"] is True
    assert payload["data"]["stats"]["age_summary"]["oldest_entry_age_hours"] >= 0
    assert "namespaces" not in payload["data"]["stats"]


def test_cache_clear_prunes_legacy_unscoped_entries_by_namespace(tmp_path: Path, monkeypatch) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    now = 1000.0
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "file")
    monkeypatch.setenv("WOWHEAD_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr("warcraft_api.cache.time.time", lambda: now + 20)

    expired_path = cache_dir / ("a" * 64 + ".json")
    expired_path.write_text(json.dumps({"expires_at": now + 10, "payload": {}}), encoding="utf-8")
    active_path = cache_dir / ("b" * 64 + ".json")
    active_path.write_text(json.dumps({"expires_at": now + 120, "payload": {}}), encoding="utf-8")

    result = runner.invoke(app, ["cache-clear", "--namespace", "legacy_unscoped", "--expired-only"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["removed"] == {"total": 1, "namespaces": {"legacy_unscoped": 1}}
    assert (expired_path.exists(), active_path.exists()) == (False, True)


def test_cache_clear_rejects_a_namespace_the_cache_never_writes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "file")
    monkeypatch.setenv("WOWHEAD_CACHE_DIR", str(tmp_path / "cache"))

    result = runner.invoke(app, ["cache-clear", "--namespace", "entity_response,typo"])

    assert result.exit_code == 2
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "invalid_argument"
    assert error["message"].startswith("Unknown cache namespace typo. Known: ")


def test_cache_clear_fails_as_a_network_error_when_redis_is_unreachable(monkeypatch) -> None:
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "redis")
    monkeypatch.setenv("WOWHEAD_REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(
        "wowhead_cli.main.inspect_redis_cache",
        lambda redis_url, *, prefix, **kwargs: {"kind": "redis", "available": False, "error": "Error 61 connecting to 127.0.0.1:1."},
    )

    result = runner.invoke(app, ["cache-clear"])

    assert result.exit_code == 5
    assert json.loads(result.stderr)["error"] == {
        "code": "network_error",
        "message": "Redis cache is unavailable: Error 61 connecting to 127.0.0.1:1.",
    }


def test_cache_inspect_can_request_redis_prefix_visibility(monkeypatch) -> None:
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "redis")
    monkeypatch.setenv("WOWHEAD_REDIS_URL", "redis://cache.example:6379/3")
    monkeypatch.setenv("WOWHEAD_REDIS_PREFIX", "wowhead_cli")

    def fake_inspect(redis_url: str | None, *, prefix: str, include_prefix_visibility: bool = False, prefix_limit: int = 10, import_module_func=None):  # noqa: ANN001
        assert redis_url == "redis://cache.example:6379/3"
        assert prefix == "wowhead_cli"
        assert include_prefix_visibility is True
        assert prefix_limit == 4
        return {
            "kind": "redis",
            "available": True,
            "count": 2,
            "namespaces": {"entity_response": 2},
            "error": None,
            "prefix_visibility": {
                "current_prefix": "wowhead_cli",
                "current_prefix_count": 2,
                "other_prefix_count": 1,
                "other_prefixes_present": True,
                "isolated": False,
                "total_prefixes": 2,
                "prefixes": [
                    {"prefix": "wowhead_cli", "count": 2, "current": True},
                    {"prefix": "other_app", "count": 1, "current": False},
                ],
                "truncated": False,
            },
        }

    monkeypatch.setattr("wowhead_cli.main.inspect_redis_cache", fake_inspect)
    result = runner.invoke(app, ["cache-inspect", "--show-redis-prefixes", "--redis-prefix-limit", "4"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["settings"]["backend"] == "redis"
    assert payload["data"]["stats"]["prefix_visibility"]["other_prefix_count"] == 1
    assert payload["data"]["stats"]["prefix_visibility"]["prefixes"][1]["prefix"] == "other_app"


def test_cache_clear_can_remove_expired_entries_by_namespace(tmp_path: Path, monkeypatch) -> None:
    cache_dir = tmp_path / "cache"
    store = FileCacheStore(cache_dir)
    now = 1000.0
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "file")
    monkeypatch.setenv("WOWHEAD_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr("warcraft_api.cache.time.time", lambda: now)

    store.set("search_suggestions:active", {"query": "thunderfury"}, ttl_seconds=60)
    store.set("entity_response:expired", {"entity": {"id": 19019}}, ttl_seconds=10)
    store.set("entity_response:active", {"entity": {"id": 19020}}, ttl_seconds=60)

    monkeypatch.setattr("warcraft_api.cache.time.time", lambda: now + 20)
    result = runner.invoke(app, ["cache-clear", "--namespace", "entity_response", "--expired-only"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["removed"] == {"total": 1, "namespaces": {"entity_response": 1}}
    assert payload["data"]["remaining"]["totals"] == {"total": 2, "active": 2, "expired": 0, "invalid": 0}


def test_invalid_cache_config_returns_structured_error(monkeypatch) -> None:
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "broken")
    result = runner.invoke(app, ["search", "thunderfury"])
    assert result.exit_code == 1

    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["provider"] == "wowhead"
    assert payload["error"] == {
        "code": "invalid_cache_config",
        "message": "WOWHEAD_CACHE_BACKEND must be one of: file, redis, none.",
    }


# --- Cross-binary coverage of the shared output flags -------------------------------------------
# Every binary installs warcraft_core.cli's common options, so the same five flags must behave
# identically everywhere. `doctor` is the one command each binary can answer offline.

DOCTOR_ARGS = {"wowhead": ["doctor", "--no-live"], "warcraftlogs": ["doctor", "--no-live"]}


def _doctor_args(binary: str) -> list[str]:
    return DOCTOR_ARGS.get(binary, ["doctor"])


@pytest.mark.parametrize("binary", sorted(CLI_APPS), ids=sorted(CLI_APPS))
def test_doctor_defaults_to_single_line_json(binary: str) -> None:
    result = runner.invoke(CLI_APPS[binary], _doctor_args(binary))
    assert result.exit_code == 0, result.stderr
    assert result.stdout.count("\n") == 1
    assert isinstance(json.loads(result.stdout), dict)


@pytest.mark.parametrize("binary", sorted(CLI_APPS), ids=sorted(CLI_APPS))
@pytest.mark.parametrize("flags", [["--pretty"], ["--profile", "human"]], ids=["pretty", "profile-human"])
def test_doctor_pretty_prints(binary: str, flags: list[str]) -> None:
    result = runner.invoke(CLI_APPS[binary], [*flags, *_doctor_args(binary)])
    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("{\n")


@pytest.mark.parametrize("binary", sorted(CLI_APPS), ids=sorted(CLI_APPS))
def test_doctor_fields_projects_to_the_requested_key(binary: str) -> None:
    args = _doctor_args(binary)
    key = next(iter(json.loads(runner.invoke(CLI_APPS[binary], args).stdout)))
    result = runner.invoke(CLI_APPS[binary], ["--fields", key, *args])
    assert result.exit_code == 0, result.stderr
    assert set(json.loads(result.stdout)) == {key}


@pytest.mark.parametrize("binary", sorted(CLI_APPS), ids=sorted(CLI_APPS))
def test_doctor_fields_strict_rejects_a_missing_path(binary: str) -> None:
    result = runner.invoke(CLI_APPS[binary], ["--fields-strict", "--fields", "nope.missing", *_doctor_args(binary)])
    assert result.exit_code == 2, result.stdout
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "missing_fields"
