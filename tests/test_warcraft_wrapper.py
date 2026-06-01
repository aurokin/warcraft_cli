from __future__ import annotations

import json
from pathlib import Path

import httpx
import typer
from method_cli.main import app as method_app
from typer.testing import CliRunner
from warcraft_cli.main import app as warcraft_app
from warcraft_cli.providers import PROVIDERS, get_provider
from warcraft_content.article_bundle import write_article_bundle
from warcraftlogs_cli.main import app as warcraftlogs_app
from wowhead_cli.main import app as wowhead_app

runner = CliRunner()


def _disable_wowhead_page_fetch(monkeypatch) -> None:  # noqa: ANN001
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        raise httpx.ConnectError("network disabled", request=httpx.Request("GET", page_url))

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)


def _disable_wowhead_client_init(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr("wowhead_cli.main._client", lambda ctx: (_ for _ in ()).throw(typer.Exit(1)))


def _simc_build_input_summary(args: list[str]) -> dict[str, object]:
    summary: dict[str, object] = {"command": args[0], "args": args}
    if "--build-packet" in args:
        packet = json.loads(Path(args[args.index("--build-packet") + 1]).read_text())
        transport_forms = packet.get("transport_forms") if isinstance(packet.get("transport_forms"), dict) else {}
        summary["build_input"] = "packet"
        summary["packet_transport_status"] = packet["transport_status"]
        summary["packet_transport_url"] = transport_forms.get("wowhead_talent_calc_url")
        summary["packet_transport_form_keys"] = sorted(transport_forms)
    elif "--build-text" in args:
        summary["build_input"] = "text"
        summary["build_text"] = args[args.index("--build-text") + 1]
    return summary


class _EndToEndWarcraftLogsClient:
    def close(self) -> None:
        return None

    def report(self, *, code: str, allow_unlisted: bool = False) -> dict[str, object]:
        assert code == "abcd1234"
        return {
            "code": "abcd1234",
            "title": "Manaforge Omega - Liquid",
            "startTime": 123,
            "endTime": 456,
            "visibility": "public",
            "archiveStatus": {
                "isArchived": True,
                "isAccessible": True,
                "archiveDate": 789,
            },
            "segments": 1,
            "exportedSegments": 0,
            "zone": {"id": 38, "name": "Manaforge Omega"},
        }

    def report_fights(
        self,
        *,
        code: str,
        difficulty: int | None = None,
        allow_unlisted: bool = False,
        ttl_override: int | None = None,
    ) -> dict[str, object]:
        assert code == "abcd1234"
        return {
            "code": "abcd1234",
            "title": "Manaforge Omega - Liquid",
            "zone": {"id": 38, "name": "Manaforge Omega"},
            "fights": [
                {
                    "id": 1,
                    "name": "Dimensius, the All-Devouring",
                    "encounterID": 3012,
                    "difficulty": 5,
                    "kill": True,
                    "completeRaid": False,
                    "startTime": 100000,
                    "endTime": 200000,
                    "fightPercentage": 100,
                    "bossPercentage": 0,
                    "averageItemLevel": 685.2,
                    "size": 20,
                }
            ],
        }

    def encounter(self, *, encounter_id: int) -> dict[str, object]:
        assert encounter_id == 3012
        return {
            "id": 3012,
            "name": "Dimensius, the All-Devouring",
            "journalID": 9001,
            "zone": {"id": 38, "name": "Manaforge Omega", "expansion": {"id": 12, "name": "Midnight"}},
        }

    def report_player_details(self, *, code: str, allow_unlisted: bool = False, options, ttl_override: int | None = None) -> dict[str, object]:  # noqa: ANN001
        assert code == "abcd1234"
        assert options.fight_ids == [1]
        return {
            "code": "abcd1234",
            "title": "Manaforge Omega - Liquid",
            "zone": {"id": 38, "name": "Manaforge Omega"},
            "playerDetails": {
                "data": {
                    "tanks": [],
                    "healers": [],
                    "dps": [
                        {
                            "name": "Auropower",
                            "id": 9,
                            "type": "Paladin",
                            "specs": [{"spec": "Retribution", "count": 1}],
                            "combatantInfo": {
                                "talentTree": [
                                    {"id": 103324, "nodeID": 82244, "rank": 1},
                                    {"id": 109839, "nodeID": 88206, "rank": 1},
                                    {"id": 117176, "nodeID": 94585, "rank": 1},
                                ]
                            },
                        }
                    ],
                }
            },
        }


def _patch_simc_describe_pipeline(
    monkeypatch,
    *,
    transport_form: str,
    transport_status: str,
) -> None:
    def fake_loader(_paths, **kwargs):  # noqa: ANN001
        build_packet = kwargs["build_packet"]
        assert isinstance(build_packet, str) and build_packet
        split_class = "103324:1" if transport_form == "simc_split_talents" else None
        split_spec = "109839:1" if transport_form == "simc_split_talents" else None
        split_hero = "117176:1" if transport_form == "simc_split_talents" else None
        return (
            type(
                "BuildSpec",
                (),
                {
                    "actor_class": "druid",
                    "spec": "balance",
                    "talents": None,
                    "class_talents": split_class,
                    "spec_talents": split_spec,
                    "hero_talents": split_hero,
                    "source_kind": transport_form,
                    "source_notes": ["talent transport packet"],
                    "transport_form": transport_form,
                    "transport_status": transport_status,
                    "transport_source": build_packet,
                },
            )(),
            type(
                "BuildIdentity",
                (),
                {
                    "actor_class": "druid",
                    "spec": "balance",
                    "confidence": "high",
                    "source": transport_form,
                    "candidate_count": 1,
                    "candidates": [("druid", "balance")],
                    "source_notes": ["talent transport packet"],
                },
            )(),
        )

    monkeypatch.setattr("simc_cli.main._load_identified_build_spec", fake_loader)

    resolution = type(
        "Resolution",
        (),
        {
            "actor_class": "druid",
            "spec": "balance",
            "source_kind": transport_form,
            "enabled_talents": {"moonkin_form"},
            "talents_by_tree": {"class": [], "spec": [], "hero": [], "selection": []},
            "source_notes": ["talent transport packet"],
        },
    )()

    def fake_resolve_prune_context(_paths, _apl, option_values, targets):  # noqa: ANN001
        assert isinstance(option_values["build_packet"], str)
        context = type(
            "Context",
            (),
            {
                "targets": targets,
                "enabled_talents": {"moonkin_form"},
                "disabled_talents": set(),
                "talent_sources": {},
            },
        )()
        return context, resolution

    monkeypatch.setattr("simc_cli.main._resolve_prune_context", fake_resolve_prune_context)
    monkeypatch.setattr(
        "simc_cli.main._describe_target_payload",
        lambda _resolved, context, *, start_list, priority_limit, inactive_limit: {
            "targets": context.targets,
            "focus_list": "default",
            "focus_path": ["default"],
            "focus_resolution": "direct",
            "active_priority": [],
            "inactive_priority": [],
            "active_action_names": ["moonfire"],
            "inactive_action_names": [],
            "talent_tree": {
                "class": {"selected": [], "skipped": []},
                "spec": {"selected": [], "skipped": []},
                "hero": {"selected": [], "skipped": []},
            },
            "inactive_talents": [],
            "active_talents": [],
            "explained_intent": {"setup": [], "helpers": [], "burst": [], "priorities": []},
            "runtime_sensitive": [],
        },
    )


def _comparison_payload(
    *,
    provider: str,
    slug: str,
    page_url: str,
    page_title: str,
    analysis_tags: list[str],
    build_code: str | None = None,
) -> dict[str, object]:
    build_items: list[dict[str, object]] = []
    if build_code is not None:
        build_items.append(
            {
                "kind": "build_reference",
                "reference_type": "wowhead_talent_calc_url",
                "url": f"https://www.wowhead.com/talent-calc/monk/mistweaver/{build_code}",
                "label": "Raid Build",
                "build_code": build_code,
                "build_identity": {
                    "kind": "build_identity",
                    "status": "inferred",
                    "class_spec_identity": {"identity": {"actor_class": "monk", "spec": "mistweaver"}},
                },
                "source_urls": [page_url],
            }
        )
    return {
        "guide": {
            "slug": slug,
            "page_url": page_url,
            "section_slug": "overview",
            "section_title": "Overview",
            "page_count": 1,
        },
        "page": {
            "title": page_title,
            "description": page_title,
            "canonical_url": page_url,
        },
        "navigation": {"count": 1, "items": [{"title": "Overview", "url": page_url, "section_slug": "overview", "active": True, "ordinal": 1}]},
        "pages": [
            {
                "guide": {"slug": slug, "page_url": page_url, "section_slug": "overview", "section_title": "Overview"},
                "page": {"title": page_title, "description": page_title, "canonical_url": page_url},
                "article": {
                    "html": "<h2>Overview</h2><p>Guide copy</p>",
                    "text": "Overview Guide copy",
                    "headings": [{"title": "Overview", "level": 2, "ordinal": 1}],
                    "sections": [{"title": "Overview", "level": 2, "ordinal": 1, "text": "Guide copy", "html": "<p>Guide copy</p>"}],
                },
            }
        ],
        "linked_entities": {"count": 0, "items": []},
        "build_references": {"count": len(build_items), "items": build_items},
        "analysis_surfaces": {
            "count": 1,
            "items": [
                {
                    "kind": "guide_analysis_surface",
                    "surface_tags": analysis_tags,
                    "confidence": "high",
                    "source_kind": "section_heading",
                    "provider": provider,
                    "content_family": None,
                    "page_url": page_url,
                    "section_slug": "overview",
                    "section_title": "Overview",
                    "page_title": page_title,
                    "text_preview": "Guide copy",
                    "match_reasons": ["keyword:overview"],
                    "citation": {"page_url": page_url, "section_title": "Overview", "page_title": page_title},
                }
            ],
        },
    }


def test_method_stub_commands_expose_coming_soon_contract() -> None:
    result = runner.invoke(method_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["provider"] == "method"
    assert payload["status"] == "ready"
    assert payload["capabilities"]["search"] == "ready"
    assert payload["capabilities"]["resolve"] == "ready"


def test_warcraft_doctor_reports_ready_and_stubbed_providers() -> None:
    result = runner.invoke(warcraft_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["wrapper"]["provider_count"] == 10
    providers = {row["provider"]: row for row in payload["providers"]}
    assert providers["wowhead"]["status"] == "ready"
    assert providers["method"]["status"] == "ready"
    assert providers["icy-veins"]["status"] == "ready"
    assert providers["raiderio"]["status"] == "partial"
    assert providers["warcraftlogs"]["status"] == "partial"
    assert providers["warcraft-wiki"]["status"] == "ready"
    assert providers["wowprogress"]["status"] == "partial"
    assert providers["simc"]["status"] == "partial"
    assert providers["wowhead"]["expansion_support"]["mode"] == "profiled"
    assert providers["wowhead"]["expansion_support"]["review_status"] == "reviewed"
    assert providers["method"]["expansion_support"]["mode"] == "fixed"
    assert providers["method"]["expansion_support"]["review_status"] == "reviewed"
    assert providers["warcraftlogs"]["expansion_support"]["mode"] == "fixed"
    assert providers["warcraftlogs"]["expansion_support"]["review_status"] == "reviewed"
    assert providers["warcraft-wiki"]["expansion_support"]["mode"] == "fixed"
    assert providers["warcraft-wiki"]["expansion_support"]["review_status"] == "reviewed"
    assert providers["warcraft-wiki"]["expansion_support"]["supported_expansions"] == ["retail"]
    assert providers["method"]["details"]["capabilities"]["guide"] == "ready"
    assert providers["icy-veins"]["details"]["capabilities"]["guide"] == "ready"
    assert providers["raiderio"]["details"]["capabilities"]["search"] == "ready"
    assert providers["warcraftlogs"]["details"]["capabilities"]["search"] == "ready_explicit_report_only"
    assert providers["warcraft-wiki"]["details"]["capabilities"]["article"] == "ready"
    assert providers["wowprogress"]["details"]["capabilities"]["leaderboard"] == "ready"
    assert providers["simc"]["details"]["capabilities"]["decode_build"] == "ready"
    assert providers["simc"]["details"]["capabilities"]["validate_talent_transport"] == "ready"
    assert providers["warcraftlogs"]["auth"]["required"] is True
    assert providers["simc"]["wrapper_surfaces"]["search"]["ready"] is False
    assert providers["simc"]["wrapper_surfaces"]["search"]["status"] == "coming_soon"
    assert providers["blizzard-api"]["status"] == "partial"
    assert providers["blizzard-api"]["auth"]["required"] is True
    assert providers["blizzard-api"]["auth"]["flow"] == "oauth_client_credentials"
    assert providers["blizzard-api"]["expansion_support"]["mode"] == "none"
    assert providers["blizzard-api"]["wrapper_surfaces"]["search"]["status"] == "coming_soon"
    assert providers["blizzard-api"]["details"]["capabilities"]["doctor"] == "ready"


def _provider_doctor_capabilities(registration) -> dict[str, str]:  # noqa: ANN001
    """Invoke a provider CLI's own doctor and return its reported capabilities map."""
    result = runner.invoke(registration.app, list(registration.doctor_args))
    assert result.exit_code == 0, f"{registration.name} doctor exited {result.exit_code}"
    payload = json.loads(result.stdout)
    capabilities = payload.get("capabilities")
    assert isinstance(capabilities, dict), f"{registration.name} doctor emitted no capabilities map"
    return capabilities


def test_wrapper_capabilities_match_each_cli_doctor_for_search_and_resolve() -> None:
    # Registry-vs-CLI parity: the wrapper must never advertise a search/resolve
    # surface more capable than the provider CLI itself reports. (The `doctor`
    # surface is intentionally excluded: only warcraftlogs/simc/blizzard-api emit
    # a `doctor` capability key, so it is not a parity surface.)
    overstated = {"coming_soon", "not_supported", "ready_explicit_report_only"}
    assert len(PROVIDERS) == 10
    for registration in PROVIDERS:
        capabilities = _provider_doctor_capabilities(registration)
        for surface in ("search", "resolve"):
            wrapper_status = registration.wrapper_capabilities[surface]
            cli_status = capabilities.get(surface)
            assert cli_status is not None, f"{registration.name} doctor missing `{surface}` capability"
            assert wrapper_status == cli_status, (
                f"{registration.name} wrapper advertises {surface}={wrapper_status!r} "
                f"but the CLI reports {cli_status!r}"
            )
            if wrapper_status == "ready":
                assert cli_status not in overstated, (
                    f"{registration.name} overstates {surface} as ready while the CLI reports {cli_status!r}"
                )


def _stub_non_warcraftlogs_fanout(monkeypatch) -> None:  # noqa: ANN001
    """Run the real warcraftlogs search/resolve; return empty payloads for everyone else."""
    import warcraft_cli.main as wrapper_main

    real_search = wrapper_main.provider_search
    real_resolve = wrapper_main.provider_resolve

    def only_wcl_search(provider: str, query: str, *, limit: int = 5, expansion=None):  # noqa: ANN001, ANN202
        if provider == "warcraftlogs":
            return real_search(provider, query, limit=limit, expansion=expansion)
        return {"provider": provider, "exit_code": 0, "payload": {"provider": provider, "count": 0, "results": []}}

    def only_wcl_resolve(provider: str, query: str, *, limit: int = 5, expansion=None):  # noqa: ANN001, ANN202
        if provider == "warcraftlogs":
            return real_resolve(provider, query, limit=limit, expansion=expansion)
        return {"provider": provider, "exit_code": 0, "payload": {"provider": provider, "resolved": False, "match": None}}

    monkeypatch.setattr("warcraft_cli.main.provider_search", only_wcl_search)
    monkeypatch.setattr("warcraft_cli.main.provider_resolve", only_wcl_resolve)


def test_warcraft_search_keeps_warcraftlogs_as_explicit_report_only_discovery_hint(monkeypatch) -> None:
    _stub_non_warcraftlogs_fanout(monkeypatch)
    # "Liquid" is a guild name, not a report code (REPORT_CODE_PATTERN needs a digit),
    # so warcraftlogs must return its structured discovery hint, not a fabricated match.
    result = runner.invoke(warcraft_app, ["search", "Liquid"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)

    assert get_provider("warcraftlogs").wrapper_capabilities["search"] == "ready_explicit_report_only"
    assert "warcraftlogs" in payload["included_providers"]
    assert "warcraftlogs" not in {row["provider"] for row in payload["excluded_providers"]}

    wcl = {row["provider"]: row for row in payload["providers"]}["warcraftlogs"]["payload"]
    assert wcl["count"] == 0
    assert "explicit report URL or a bare report code" in wcl["message"]
    assert wcl["supported_inputs"]
    assert wcl["suggested_commands"]
    # Never surface a fabricated resolved match for a non-report query.
    assert all(row["provider"] != "warcraftlogs" for row in payload["results"])


def test_warcraft_resolve_never_fabricates_a_warcraftlogs_match_for_non_report_query(monkeypatch) -> None:
    _stub_non_warcraftlogs_fanout(monkeypatch)
    result = runner.invoke(warcraft_app, ["resolve", "Liquid"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)

    assert "warcraftlogs" in payload["included_providers"]
    wcl = {row["provider"]: row for row in payload["providers"]}["warcraftlogs"]["payload"]
    assert wcl["resolved"] is False
    assert wcl["match"] is None
    assert wcl["supported_inputs"]
    assert wcl["suggested_commands"]
    # The wrapper must not resolve to warcraftlogs off a non-report query.
    assert payload["provider"] != "warcraftlogs"
    assert payload["resolved"] is False


def test_warcraft_doctor_reports_expansion_filtering_state() -> None:
    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["wrapper"]["requested_expansion"] == "wotlk"
    assert payload["wrapper"]["expansion_filter_active"] is True
    assert payload["included_providers"] == ["wowhead"]
    assert {row["provider"] for row in payload["excluded_providers"]} == {
        "method",
        "icy-veins",
        "raiderio",
        "warcraftlogs",
        "warcraft-wiki",
        "wowprogress",
        "simc",
        "raidbots",
        "blizzard-api",
    }


def test_warcraft_passthrough_relaxes_none_expansion_provider_with_advisory() -> None:
    # blizzard-api is expansion_mode=none: a wrapper --expansion has no semantics to honor,
    # so the command is passed through unchanged with an advisory note (relax-to-passthrough),
    # not rejected with exit 1.
    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "blizzard", "doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    # provider payload preserved verbatim
    assert payload["ok"] is True
    assert payload["provider"] == "blizzard-api"
    assert payload["capabilities"]["doctor"] == "ready"
    # additive advisory note
    assert payload["expansion_filter"] == "passthrough_no_expansion_semantics"
    assert payload["expansion_advisory"]["requested_expansion"] == "wotlk"
    assert payload["expansion_advisory"]["provider_expansion_mode"] == "none"


def test_warcraft_passthrough_relaxes_none_expansion_simc() -> None:
    # Use `simc doctor` (fully offline, deterministic) rather than `simc version`, which
    # depends on a built SimC binary that CI does not provide.
    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "simc", "doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "simc"
    assert payload["capabilities"]["doctor"] == "ready"
    assert payload["expansion_filter"] == "passthrough_no_expansion_semantics"
    assert payload["expansion_advisory"]["provider_expansion_mode"] == "none"


def test_warcraft_passthrough_rejects_fixed_provider_expansion_mismatch() -> None:
    # Fixed/profiled providers asked for an unsupported expansion are a genuine mismatch
    # and still hard-error (only none-expansion providers relax to passthrough).
    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "method", "search", "foo"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unsupported_provider_expansion"
    assert payload["provider"] == "method"


def test_warcraft_passthrough_without_expansion_has_no_advisory() -> None:
    result = runner.invoke(warcraft_app, ["blizzard", "doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "expansion_filter" not in payload
    assert "expansion_advisory" not in payload


def test_warcraft_doctor_reports_retail_filter_state() -> None:
    result = runner.invoke(warcraft_app, ["--expansion", "retail", "doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["wrapper"]["requested_expansion"] == "retail"
    assert payload["wrapper"]["expansion_filter_active"] is True
    assert set(payload["included_providers"]) == {
        "wowhead",
        "method",
        "icy-veins",
        "raiderio",
        "warcraftlogs",
        "warcraft-wiki",
        "wowprogress",
        "raidbots",
    }
    assert {row["provider"] for row in payload["excluded_providers"]} == {"simc", "blizzard-api"}


def test_warcraft_doctor_reports_worktree_runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WARCRAFT_WORKTREE_ROOT", str(tmp_path / "repo"))

    result = runner.invoke(warcraft_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["paths"]["data_root"] == str((tmp_path / "repo" / ".warcraft" / "runtime" / "data").resolve())
    assert payload["paths"]["cache_root"] == str((tmp_path / "repo" / ".warcraft" / "runtime" / "cache").resolve())
    assert payload["paths"]["worktree_runtime"] == {
        "active": True,
        "worktree_root": str((tmp_path / "repo").resolve()),
        "runtime_root": str((tmp_path / "repo" / ".warcraft" / "runtime").resolve()),
        "isolated_roots": ["data", "cache"],
        "shared_roots": ["config", "state"],
    }


def test_warcraft_doctor_reports_xdg_overrides_in_worktree_runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WARCRAFT_WORKTREE_ROOT", str(tmp_path / "repo"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(warcraft_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["paths"]["data_root"] == str((tmp_path / "data" / "warcraft").resolve())
    assert payload["paths"]["cache_root"] == str((tmp_path / "cache" / "warcraft").resolve())
    assert payload["paths"]["worktree_runtime"] == {
        "active": True,
        "worktree_root": str((tmp_path / "repo").resolve()),
        "runtime_root": str((tmp_path / "repo" / ".warcraft" / "runtime").resolve()),
        "isolated_roots": [],
        "shared_roots": ["data", "cache", "config", "state"],
    }


def test_warcraft_doctor_reports_explicit_runtime_dir_without_worktree_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WARCRAFT_WORKTREE_RUNTIME_DIR", str(tmp_path / "runtime"))

    result = runner.invoke(warcraft_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    expected_worktree_root = str(Path(__file__).resolve().parent.parent)
    assert payload["paths"]["data_root"] == str((tmp_path / "runtime" / "data").resolve())
    assert payload["paths"]["cache_root"] == str((tmp_path / "runtime" / "cache").resolve())
    assert payload["paths"]["worktree_runtime"] == {
        "active": True,
        "worktree_root": expected_worktree_root,
        "runtime_root": str((tmp_path / "runtime").resolve()),
        "isolated_roots": ["data", "cache"],
        "shared_roots": ["config", "state"],
    }


def test_warcraft_search_fans_out_across_providers(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 3, "id": 19019, "name": "Thunderfury", "typeName": "Item", "popularity": 10},
            ],
        }

    monkeypatch.setattr(
        "method_cli.main.MethodClient.sitemap_guides",
        lambda self: [{"slug": "mistweaver-monk", "name": "Mistweaver Monk", "url": "https://www.method.gg/guides/mistweaver-monk"}],
    )
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, limit: (0, []))
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(warcraft_app, ["search", "thunderfury", "--limit", "3"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["provider_count"] == 10
    assert payload["count"] == 1
    assert payload["results"][0]["provider"] == "wowhead"
    providers = {row["provider"]: row for row in payload["providers"]}
    assert providers["method"]["payload"]["count"] == 0
    assert providers["icy-veins"]["payload"]["count"] == 0
    assert providers["raiderio"]["payload"]["count"] == 0
    assert providers["warcraftlogs"]["payload"]["count"] == 0
    assert "explicit report URL or a bare report code" in providers["warcraftlogs"]["payload"]["message"]
    assert providers["warcraft-wiki"]["payload"]["count"] == 0
    assert providers["wowprogress"]["payload"]["count"] == 0
    assert "structured queries" in providers["wowprogress"]["payload"]["message"]
    assert "simc" not in providers
    assert "raidbots" not in providers
    excluded = {row["provider"]: row for row in payload["excluded_providers"]}
    assert excluded["simc"]["reason"] == "provider_surface_not_ready"
    assert excluded["simc"]["surface_support"]["status"] == "coming_soon"
    assert excluded["raidbots"]["surface_support"]["status"] == "not_supported"
    assert "blizzard-api" not in providers
    assert excluded["blizzard-api"]["surface_support"]["status"] == "coming_soon"
    assert providers["wowhead"]["payload"]["results"][0]["name"] == "Thunderfury"


def test_warcraft_guide_compare_returns_cross_provider_bundle_packet(tmp_path: Path) -> None:
    method_dir = tmp_path / "method-guide"
    icy_dir = tmp_path / "icy-guide"
    write_article_bundle(
        _comparison_payload(
            provider="method",
            slug="mistweaver-monk",
            page_url="https://www.method.gg/guides/mistweaver-monk/talents",
            page_title="Method Talents",
            analysis_tags=["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        ),
        provider="method",
        export_dir=method_dir,
    )
    write_article_bundle(
        _comparison_payload(
            provider="icy-veins",
            slug="mistweaver-monk-pve-healing-guide",
            page_url="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
            page_title="Icy Overview",
            analysis_tags=["overview"],
            build_code="ABC123",
        ),
        provider="icy-veins",
        export_dir=icy_dir,
    )

    result = runner.invoke(warcraft_app, ["guide-compare", str(method_dir), str(icy_dir)])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["provider"] == "warcraft"
    assert payload["kind"] == "guide_bundle_comparison"
    assert payload["compared_bundle_count"] == 2
    assert payload["comparison_scope"] == ["section_evidence", "analysis_surfaces", "build_references"]
    assert payload["section_evidence"]["matching_rule"] == "exact_normalized_section_title"
    assert payload["section_evidence"]["shared"] == ["overview"]
    assert payload["analysis_surface_tags"]["count"] == 3
    assert payload["build_references"]["count"] == 1
    assert payload["build_references"]["shared"] == [
        "monk::mistweaver::ABC123::https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123"
    ]


def test_warcraft_guide_compare_query_orchestrates_resolve_export_and_compare(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fake_provider_resolve(provider: str, query: str, *, limit: int = 5, expansion: str | None = None) -> dict[str, object]:
        assert query == "mistweaver monk guide"
        assert limit == 5
        assert expansion is None
        refs = {
            "method": ("mistweaver-monk", "Method Mistweaver Monk Guide"),
            "icy-veins": ("mistweaver-monk-pve-healing-guide", "Icy Veins Mistweaver Monk Guide"),
        }
        ref, name = refs[provider]
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "resolved": True,
                "confidence": "high",
                "match": {
                    "id": ref,
                    "name": name,
                    "entity_type": "guide",
                    "url": f"https://example.test/{ref}",
                },
                "next_command": f"{provider} guide {ref}",
            },
        }

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert args[0] == "guide-export"
        export_dir = Path(args[3])
        payload = _comparison_payload(
            provider=provider,
            slug=args[1],
            page_url=f"https://example.test/{provider}/{args[1]}",
            page_title=f"{provider} guide",
            analysis_tags=["overview"] if provider == "icy-veins" else ["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        )
        write_article_bundle(payload, provider=provider, export_dir=export_dir)
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {"output_dir": str(export_dir), "guide": payload["guide"]},
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", fake_provider_resolve)
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        [
            "guide-compare-query",
            "mistweaver monk guide",
            "--provider",
            "method",
            "--provider",
            "icy-veins",
            "--out-root",
            str(tmp_path / "orchestrated"),
        ],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["kind"] == "guide_bundle_comparison_orchestration"
    assert payload["exported_bundle_count"] == 2
    assert payload["comparison"]["kind"] == "guide_bundle_comparison"
    assert payload["comparison"]["compared_bundle_count"] == 2
    assert all(row["status"] == "exported" for row in payload["provider_results"])
    assert {row["candidate"]["selection_source"] for row in payload["provider_results"]} == {"resolve"}


def test_warcraft_guide_compare_query_uses_conservative_search_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fake_provider_resolve(provider: str, query: str, *, limit: int = 5, expansion: str | None = None) -> dict[str, object]:
        if provider == "method":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "resolved": True,
                    "confidence": "high",
                    "match": {
                        "id": "mistweaver-monk",
                        "name": "Method Mistweaver Monk Guide",
                        "entity_type": "guide",
                        "url": "https://example.test/method/mistweaver-monk",
                    },
                    "next_command": "method guide mistweaver-monk",
                },
            }
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "resolved": False,
                "confidence": "none",
                "match": None,
                "next_command": None,
            },
        }

    def fake_provider_search(provider: str, query: str, *, limit: int = 5, expansion: str | None = None) -> dict[str, object]:
        if provider == "icy-veins":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "results": [
                        {
                            "id": "mistweaver-monk-pve-healing-guide",
                            "name": "Icy Veins Mistweaver Monk Guide",
                            "entity_type": "guide",
                            "url": "https://example.test/icy-veins/mistweaver-monk-pve-healing-guide",
                            "ranking": {"score": 72},
                            "follow_up": {
                                "recommended_command": "icy-veins guide mistweaver-monk-pve-healing-guide",
                            },
                        },
                        {
                            "id": "mistweaver-monk-pvp-guide",
                            "name": "Icy Veins Mistweaver Monk PvP Guide",
                            "entity_type": "guide",
                            "url": "https://example.test/icy-veins/mistweaver-monk-pvp-guide",
                            "ranking": {"score": 41},
                        },
                    ]
                },
            }
        return {"provider": provider, "exit_code": 0, "payload": {"results": []}}

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        export_dir = Path(args[3])
        payload = _comparison_payload(
            provider=provider,
            slug=args[1],
            page_url=f"https://example.test/{provider}/{args[1]}",
            page_title=f"{provider} guide",
            analysis_tags=["overview"] if provider == "icy-veins" else ["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        )
        write_article_bundle(payload, provider=provider, export_dir=export_dir)
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {"output_dir": str(export_dir), "guide": payload["guide"]},
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", fake_provider_resolve)
    monkeypatch.setattr("warcraft_cli.main.provider_search", fake_provider_search)
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        [
            "guide-compare-query",
            "mistweaver monk guide",
            "--provider",
            "method",
            "--provider",
            "icy-veins",
            "--out-root",
            str(tmp_path / "orchestrated"),
        ],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["comparison"]["compared_bundle_count"] == 2
    icy_row = next(row for row in payload["provider_results"] if row["provider"] == "icy-veins")
    assert icy_row["candidate"]["selection_source"] == "search_fallback"
    assert icy_row["candidate"]["selection_contract"]["minimum_top_score"] == 50
    assert icy_row["candidate"]["selection_contract"]["minimum_margin_over_runner_up"] == 25


def test_warcraft_guide_compare_query_skips_weak_search_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fake_provider_resolve(provider: str, query: str, *, limit: int = 5, expansion: str | None = None) -> dict[str, object]:
        if provider == "method":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "resolved": True,
                    "confidence": "high",
                    "match": {
                        "id": "mistweaver-monk",
                        "name": "Method Mistweaver Monk Guide",
                        "entity_type": "guide",
                        "url": "https://example.test/method/mistweaver-monk",
                    },
                    "next_command": "method guide mistweaver-monk",
                },
            }
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "resolved": False,
                "confidence": "none",
                "match": None,
                "next_command": None,
            },
        }

    def fake_provider_search(provider: str, query: str, *, limit: int = 5, expansion: str | None = None) -> dict[str, object]:
        if provider == "icy-veins":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "results": [
                        {
                            "id": "mistweaver-monk-pve-healing-guide",
                            "name": "Icy Veins Mistweaver Monk Guide",
                            "entity_type": "guide",
                            "url": "https://example.test/icy-veins/mistweaver-monk-pve-healing-guide",
                            "ranking": {"score": 58},
                        },
                        {
                            "id": "mistweaver-monk-pvp-guide",
                            "name": "Icy Veins Mistweaver Monk PvP Guide",
                            "entity_type": "guide",
                            "url": "https://example.test/icy-veins/mistweaver-monk-pvp-guide",
                            "ranking": {"score": 42},
                        },
                    ]
                },
            }
        return {"provider": provider, "exit_code": 0, "payload": {"results": []}}

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        export_dir = Path(args[3])
        payload = _comparison_payload(
            provider=provider,
            slug=args[1],
            page_url=f"https://example.test/{provider}/{args[1]}",
            page_title=f"{provider} guide",
            analysis_tags=["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        )
        write_article_bundle(payload, provider=provider, export_dir=export_dir)
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {"output_dir": str(export_dir), "guide": payload["guide"]},
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", fake_provider_resolve)
    monkeypatch.setattr("warcraft_cli.main.provider_search", fake_provider_search)
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        [
            "guide-compare-query",
            "mistweaver monk guide",
            "--provider",
            "method",
            "--provider",
            "icy-veins",
            "--out-root",
            str(tmp_path / "orchestrated"),
        ],
    )
    assert result.exit_code == 1

    payload = json.loads(result.stderr or result.output)
    icy_row = next(row for row in payload["provider_results"] if row["provider"] == "icy-veins")
    assert icy_row["status"] == "skipped"
    assert icy_row["reason"] == "search_results_not_decisive"
    assert payload["error"]["code"] == "insufficient_guides"


def test_warcraft_guide_compare_query_reuses_fresh_orchestrated_bundles(
    monkeypatch,
    tmp_path: Path,
) -> None:
    invoke_calls: list[tuple[str, str]] = []

    def fake_provider_resolve(provider: str, query: str, *, limit: int = 5, expansion: str | None = None) -> dict[str, object]:
        refs = {
            "method": ("mistweaver-monk", "Method Mistweaver Monk Guide"),
            "icy-veins": ("mistweaver-monk-pve-healing-guide", "Icy Veins Mistweaver Monk Guide"),
        }
        ref, name = refs[provider]
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "resolved": True,
                "confidence": "high",
                "match": {
                    "id": ref,
                    "name": name,
                    "entity_type": "guide",
                    "url": f"https://example.test/{ref}",
                },
                "next_command": f"{provider} guide {ref}",
            },
        }

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        invoke_calls.append((provider, args[1]))
        export_dir = Path(args[3])
        payload = _comparison_payload(
            provider=provider,
            slug=args[1],
            page_url=f"https://example.test/{provider}/{args[1]}",
            page_title=f"{provider} guide",
            analysis_tags=["overview"] if provider == "icy-veins" else ["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        )
        write_article_bundle(payload, provider=provider, export_dir=export_dir)
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {"output_dir": str(export_dir), "guide": payload["guide"]},
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", fake_provider_resolve)
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    out_root = tmp_path / "orchestrated"
    first = runner.invoke(
        warcraft_app,
        ["guide-compare-query", "mistweaver monk guide", "--provider", "method", "--provider", "icy-veins", "--out-root", str(out_root)],
    )
    assert first.exit_code == 0
    second = runner.invoke(
        warcraft_app,
        ["guide-compare-query", "mistweaver monk guide", "--provider", "method", "--provider", "icy-veins", "--out-root", str(out_root)],
    )
    assert second.exit_code == 0

    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert [row["status"] for row in first_payload["provider_results"]] == ["exported", "exported"]
    assert [row["status"] for row in second_payload["provider_results"]] == ["reused", "reused"]
    assert len(invoke_calls) == 2


def test_warcraft_guide_compare_query_refreshes_stale_orchestrated_bundles(
    monkeypatch,
    tmp_path: Path,
) -> None:
    invoke_calls: list[tuple[str, str]] = []

    def fake_provider_resolve(provider: str, query: str, *, limit: int = 5, expansion: str | None = None) -> dict[str, object]:
        refs = {
            "method": ("mistweaver-monk", "Method Mistweaver Monk Guide"),
            "icy-veins": ("mistweaver-monk-pve-healing-guide", "Icy Veins Mistweaver Monk Guide"),
        }
        ref, name = refs[provider]
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "resolved": True,
                "confidence": "high",
                "match": {
                    "id": ref,
                    "name": name,
                    "entity_type": "guide",
                    "url": f"https://example.test/{ref}",
                },
                "next_command": f"{provider} guide {ref}",
            },
        }

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        invoke_calls.append((provider, args[1]))
        export_dir = Path(args[3])
        payload = _comparison_payload(
            provider=provider,
            slug=args[1],
            page_url=f"https://example.test/{provider}/{args[1]}",
            page_title=f"{provider} guide",
            analysis_tags=["overview"] if provider == "icy-veins" else ["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        )
        write_article_bundle(payload, provider=provider, export_dir=export_dir)
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {"output_dir": str(export_dir), "guide": payload["guide"]},
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", fake_provider_resolve)
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    out_root = tmp_path / "orchestrated"
    first = runner.invoke(
        warcraft_app,
        ["guide-compare-query", "mistweaver monk guide", "--provider", "method", "--provider", "icy-veins", "--out-root", str(out_root)],
    )
    assert first.exit_code == 0

    manifest_path = out_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for row in manifest["providers"]:
        row["exported_at"] = "2000-01-01T00:00:00Z"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    second = runner.invoke(
        warcraft_app,
        [
            "guide-compare-query",
            "mistweaver monk guide",
            "--provider",
            "method",
            "--provider",
            "icy-veins",
            "--out-root",
            str(out_root),
            "--max-age-hours",
            "1",
        ],
    )
    assert second.exit_code == 0

    second_payload = json.loads(second.stdout)
    assert [row["status"] for row in second_payload["provider_results"]] == ["exported", "exported"]
    assert len(invoke_calls) == 4


def test_warcraft_guide_compare_query_can_include_simc_build_handoff(
    monkeypatch,
    tmp_path: Path,
) -> None:
    invoke_calls: list[tuple[str, list[str]]] = []
    apl_path = tmp_path / "monk_mistweaver.simc"
    apl_path.write_text("actions=spinning_crane_kick\n", encoding="utf-8")

    def fake_provider_resolve(provider: str, query: str, *, limit: int = 5, expansion: str | None = None) -> dict[str, object]:
        refs = {
            "method": ("mistweaver-monk", "Method Mistweaver Monk Guide"),
            "wowhead": ("mistweaver-monk", "Wowhead Mistweaver Monk Guide"),
        }
        ref, name = refs[provider]
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "resolved": True,
                "confidence": "high",
                "match": {
                    "id": ref,
                    "name": name,
                    "entity_type": "guide",
                    "url": f"https://example.test/{provider}/{ref}",
                },
                "next_command": f"{provider} guide {ref}",
            },
        }

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        invoke_calls.append({"provider": provider, **(_simc_build_input_summary(args) if provider == "simc" else {"args": args})})
        if provider in {"method", "wowhead"}:
            export_dir = Path(args[3])
            payload = _comparison_payload(
                provider=provider,
                slug=args[1],
                page_url=f"https://example.test/{provider}/{args[1]}",
                page_title=f"{provider} guide",
                analysis_tags=["overview"] if provider == "wowhead" else ["builds_talents", "talent_recommendations"],
                build_code="ABC123",
            )
            write_article_bundle(payload, provider=provider, export_dir=export_dir)
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {"output_dir": str(export_dir), "guide": payload["guide"]},
                "stdout": "",
            }
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {"provider": "simc", "kind": args[0]},
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", fake_provider_resolve)
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        [
            "guide-compare-query",
            "mistweaver monk guide",
            "--provider",
            "method",
            "--provider",
            "wowhead",
            "--out-root",
            str(tmp_path / "orchestrated"),
            "--simc-build-handoff",
            "--simc-apl-path",
            str(apl_path),
        ],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    handoff = payload["simc_build_handoff"]
    assert handoff["kind"] == "guide_builds_simc_handoff"
    assert handoff["source"]["kind"] == "orchestration_root"
    assert handoff["bundle_count"] == 2
    assert handoff["build_reference_count"] == 1
    assert handoff["apl_path"] == str(apl_path)
    assert len(handoff["builds"][0]["sources"]) == 2
    assert handoff["builds"][0]["talent_transport_packet"]["transport_status"] == "exact"
    assert handoff["builds"][0]["simc"]["identify"]["payload"]["kind"] == "identify-build"
    assert handoff["builds"][0]["simc"]["decode"]["payload"]["kind"] == "decode-build"
    assert handoff["builds"][0]["simc"]["describe"]["payload"]["kind"] == "describe-build"
    assert invoke_calls[0] == {
        "provider": "method",
        "args": ["guide-export", "mistweaver-monk", "--out", str(tmp_path / "orchestrated" / "method")],
    }
    assert invoke_calls[1] == {
        "provider": "wowhead",
        "args": ["guide-export", "mistweaver-monk", "--out", str(tmp_path / "orchestrated" / "wowhead")],
    }
    assert invoke_calls[2]["provider"] == "simc"
    assert invoke_calls[2]["command"] == "identify-build"
    assert invoke_calls[2]["build_input"] == "packet"
    assert invoke_calls[2]["packet_transport_status"] == "exact"
    assert invoke_calls[2]["packet_transport_url"] == "https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123"
    assert invoke_calls[3]["command"] == "decode-build"
    assert invoke_calls[3]["build_input"] == "packet"
    assert invoke_calls[4]["command"] == "describe-build"
    assert invoke_calls[4]["build_input"] == "packet"
    assert invoke_calls[4]["args"][1:3] == ["--apl-path", str(apl_path)]


def test_warcraft_guide_builds_simc_reads_bundle_build_refs(monkeypatch, tmp_path: Path) -> None:
    bundle_dir = tmp_path / "method-guide"
    write_article_bundle(
        _comparison_payload(
            provider="method",
            slug="mistweaver-monk",
            page_url="https://www.method.gg/guides/mistweaver-monk/talents",
            page_title="Method Talents",
            analysis_tags=["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        ),
        provider="method",
        export_dir=bundle_dir,
    )

    invoke_calls: list[dict[str, object]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "simc"
        assert args[0] in {"identify-build", "decode-build"}
        invoke_calls.append(_simc_build_input_summary(args))
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "provider": "simc",
                "kind": "identify_build" if args[0] == "identify-build" else "decode_build",
                "build_spec": {"talents": "ABC123", "actor_class": "monk", "spec": "mistweaver"},
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["guide-builds-simc", str(bundle_dir)])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["kind"] == "guide_builds_simc_handoff"
    assert payload["source"]["kind"] == "bundle"
    assert payload["provenance"]["explicit_build_reference_only"] is True
    assert payload["freshness"]["status"] == "unknown"
    assert payload["freshness"]["reason"] == "bundle_manifest_has_no_export_timestamp"
    assert payload["citations"]["build_reference_urls"] == ["https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123"]
    assert payload["build_reference_count"] == 1
    assert payload["summary"]["returned_build_count"] == 1
    assert payload["summary"]["identify_success_count"] == 1
    assert payload["summary"]["decode_success_count"] == 1
    assert payload["builds"][0]["reference"]["build_code"] == "ABC123"
    assert payload["builds"][0]["talent_transport_packet"]["transport_status"] == "exact"
    assert (
        payload["builds"][0]["talent_transport_packet"]["transport_forms"]["wowhead_talent_calc_url"]
        == "https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123"
    )
    assert payload["builds"][0]["evidence"]["explicit_build_reference_only"] is True
    assert payload["builds"][0]["evidence"]["provider_count"] == 1
    assert payload["builds"][0]["simc"]["identify"]["payload"]["kind"] == "identify_build"
    assert payload["builds"][0]["simc"]["decode"]["payload"]["kind"] == "decode_build"
    assert payload["builds"][0]["simc"]["describe"] is None
    assert len(invoke_calls) == 2
    assert invoke_calls[0]["command"] == "identify-build"
    assert invoke_calls[0]["build_input"] == "packet"
    assert invoke_calls[0]["packet_transport_status"] == "exact"
    assert invoke_calls[0]["packet_transport_url"] == "https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123"
    assert invoke_calls[1]["command"] == "decode-build"
    assert invoke_calls[1]["build_input"] == "packet"


def test_warcraft_guide_builds_simc_reads_orchestration_root_and_dedupes_builds(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "orchestrated"
    method_dir = root / "method"
    wowhead_dir = root / "wowhead"
    write_article_bundle(
        _comparison_payload(
            provider="method",
            slug="mistweaver-monk",
            page_url="https://www.method.gg/guides/mistweaver-monk/talents",
            page_title="Method Talents",
            analysis_tags=["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        ),
        provider="method",
        export_dir=method_dir,
    )
    write_article_bundle(
        _comparison_payload(
            provider="wowhead",
            slug="mistweaver-monk",
            page_url="https://www.wowhead.com/guide/mistweaver-monk",
            page_title="Wowhead Talents",
            analysis_tags=["overview"],
            build_code="ABC123",
        ),
        provider="wowhead",
        export_dir=wowhead_dir,
    )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "kind": "guide_compare_orchestration_manifest",
                "query": "mistweaver monk guide",
                "updated_at": "2026-03-15T04:00:00Z",
                "providers": [
                    {
                        "provider": "method",
                        "bundle_path": str(method_dir),
                        "candidate_ref": "mistweaver-monk",
                        "exported_at": "2026-03-15T00:00:00Z",
                    },
                    {
                        "provider": "wowhead",
                        "bundle_path": str(wowhead_dir),
                        "candidate_ref": "mistweaver-monk",
                        "exported_at": "2026-03-15T00:00:00Z",
                    },
                ],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    invoke_calls: list[dict[str, object]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        invoke_calls.append(_simc_build_input_summary(args))
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {"provider": "simc", "kind": args[0]},
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["guide-builds-simc", str(root), "--no-decode"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["source"]["kind"] == "orchestration_root"
    assert payload["source"]["query"] == "mistweaver monk guide"
    assert payload["bundle_count"] == 2
    assert payload["build_reference_count"] == 1
    assert payload["freshness"]["status"] == "known"
    assert payload["freshness"]["sampled_at"] == "2026-03-15T04:00:00Z"
    assert payload["citations"]["bundle_paths"] == [str(method_dir), str(wowhead_dir)]
    assert len(payload["builds"][0]["sources"]) == 2
    assert payload["builds"][0]["evidence"]["provider_count"] == 2
    assert payload["builds"][0]["talent_transport_packet"]["transport_status"] == "exact"
    assert payload["builds"][0]["simc"]["decode"] is None
    assert payload["builds"][0]["simc"]["describe"] is None
    assert len(invoke_calls) == 1
    assert invoke_calls[0]["command"] == "identify-build"
    assert invoke_calls[0]["build_input"] == "packet"
    assert invoke_calls[0]["packet_transport_status"] == "exact"
    assert invoke_calls[0]["packet_transport_url"] == "https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123"


def test_warcraft_guide_builds_simc_can_include_describe_build_with_apl(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "method-guide"
    apl_path = tmp_path / "monk_mistweaver.simc"
    apl_path.write_text("actions=spinning_crane_kick\n", encoding="utf-8")
    write_article_bundle(
        _comparison_payload(
            provider="method",
            slug="mistweaver-monk",
            page_url="https://www.method.gg/guides/mistweaver-monk/talents",
            page_title="Method Talents",
            analysis_tags=["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        ),
        provider="method",
        export_dir=bundle_dir,
    )

    invoke_calls: list[dict[str, object]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        invoke_calls.append(_simc_build_input_summary(args))
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {"provider": "simc", "kind": args[0]},
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["guide-builds-simc", str(bundle_dir), "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["apl_path"] == str(apl_path)
    assert payload["summary"]["describe_success_count"] == 1
    assert payload["builds"][0]["talent_transport_packet"]["transport_status"] == "exact"
    assert payload["builds"][0]["simc"]["describe"]["payload"]["kind"] == "describe-build"
    assert len(invoke_calls) == 3
    assert invoke_calls[0]["command"] == "identify-build"
    assert invoke_calls[0]["build_input"] == "packet"
    assert invoke_calls[1]["command"] == "decode-build"
    assert invoke_calls[1]["build_input"] == "packet"
    assert invoke_calls[2]["command"] == "describe-build"
    assert invoke_calls[2]["build_input"] == "packet"
    assert invoke_calls[2]["args"][1:3] == ["--apl-path", str(apl_path)]


def test_warcraft_guide_builds_simc_hides_deleted_temp_packet_paths(monkeypatch, tmp_path: Path) -> None:
    bundle_dir = tmp_path / "method-guide"
    apl_path = tmp_path / "monk_mistweaver.simc"
    apl_path.write_text("actions=spinning_crane_kick\n", encoding="utf-8")
    write_article_bundle(
        _comparison_payload(
            provider="method",
            slug="mistweaver-monk",
            page_url="https://www.method.gg/guides/mistweaver-monk/talents",
            page_title="Method Talents",
            analysis_tags=["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        ),
        provider="method",
        export_dir=bundle_dir,
    )

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "simc"
        packet_path = args[args.index("--build-packet") + 1]
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "provider": "simc",
                "kind": args[0],
                "build_spec": {
                    "source_notes": [f"build packet: {packet_path}", "talent transport packet"],
                    "transport_packet": {
                        "path": packet_path,
                        "transport_form": "wowhead_talent_calc_url",
                        "transport_status": "exact",
                    }
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["guide-builds-simc", str(bundle_dir), "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    simc_payloads = payload["builds"][0]["simc"]
    assert "path" not in simc_payloads["identify"]["payload"]["build_spec"]["transport_packet"]
    assert "path" not in simc_payloads["decode"]["payload"]["build_spec"]["transport_packet"]
    assert "path" not in simc_payloads["describe"]["payload"]["build_spec"]["transport_packet"]
    assert simc_payloads["identify"]["payload"]["build_spec"]["source_notes"] == ["talent transport packet"]
    assert simc_payloads["decode"]["payload"]["build_spec"]["source_notes"] == ["talent transport packet"]
    assert simc_payloads["describe"]["payload"]["build_spec"]["source_notes"] == ["talent transport packet"]


def test_warcraft_guide_builds_simc_skips_buildless_talent_calc_refs(monkeypatch, tmp_path: Path) -> None:
    bundle_dir = tmp_path / "method-guide"
    payload = _comparison_payload(
        provider="method",
        slug="mistweaver-monk",
        page_url="https://www.method.gg/guides/mistweaver-monk/talents",
        page_title="Method Talents",
        analysis_tags=["builds_talents", "talent_recommendations"],
    )
    payload["build_references"] = {
        "count": 1,
        "items": [
            {
                "kind": "build_reference",
                "reference_type": "wowhead_talent_calc_url",
                "url": "https://www.wowhead.com/talent-calc/monk/mistweaver",
                "label": "Landing Page",
                "build_code": None,
                "source_urls": ["https://www.method.gg/guides/mistweaver-monk/talents"],
            }
        ],
    }
    write_article_bundle(payload, provider="method", export_dir=bundle_dir)

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        raise AssertionError(f"unexpected provider call: {provider} {args}")

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["guide-builds-simc", str(bundle_dir)])
    assert result.exit_code == 0
    handoff = json.loads(result.stdout)
    assert handoff["build_reference_count"] == 1
    assert handoff["summary"]["returned_build_count"] == 0
    assert handoff["summary"]["excluded_build_count"] == 1
    assert handoff["builds"] == []


def test_warcraft_guide_builds_simc_backfills_build_code_from_explicit_url(monkeypatch, tmp_path: Path) -> None:
    bundle_dir = tmp_path / "method-guide"
    payload = _comparison_payload(
        provider="method",
        slug="mistweaver-monk",
        page_url="https://www.method.gg/guides/mistweaver-monk/talents",
        page_title="Method Talents",
        analysis_tags=["builds_talents", "talent_recommendations"],
        build_code="ABC123",
    )
    payload["build_references"]["items"][0]["build_code"] = None
    write_article_bundle(payload, provider="method", export_dir=bundle_dir)

    invoke_calls: list[dict[str, object]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "simc"
        assert args[0] in {"identify-build", "decode-build"}
        invoke_calls.append(_simc_build_input_summary(args))
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "provider": "simc",
                "kind": "identify_build" if args[0] == "identify-build" else "decode_build",
                "build_spec": {"talents": "ABC123", "actor_class": "monk", "spec": "mistweaver"},
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["guide-builds-simc", str(bundle_dir)])
    assert result.exit_code == 0

    handoff = json.loads(result.stdout)
    assert handoff["build_reference_count"] == 1
    assert handoff["summary"]["returned_build_count"] == 1
    assert handoff["summary"]["excluded_build_count"] == 0
    assert handoff["builds"][0]["reference"]["build_code"] == "ABC123"
    assert handoff["builds"][0]["talent_transport_packet"]["transport_status"] == "exact"
    assert invoke_calls[0]["build_input"] == "packet"
    assert invoke_calls[0]["packet_transport_url"] == "https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123"


def test_warcraft_guide_compare_query_fails_when_too_few_guides_export(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fake_provider_resolve(provider: str, query: str, *, limit: int = 5, expansion: str | None = None) -> dict[str, object]:
        if provider == "method":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "resolved": True,
                    "confidence": "high",
                    "match": {
                        "id": "mistweaver-monk",
                        "name": "Method Mistweaver Monk Guide",
                        "entity_type": "guide",
                        "url": "https://example.test/method/mistweaver-monk",
                    },
                    "next_command": "method guide mistweaver-monk",
                },
            }
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "resolved": False,
                "confidence": "none",
                "match": None,
                "next_command": None,
            },
        }

    def fake_provider_search(provider: str, query: str, *, limit: int = 5, expansion: str | None = None) -> dict[str, object]:
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "results": [
                    {
                        "id": "mistweaver-monk-pve-healing-guide",
                        "name": "Icy Veins Mistweaver Monk Guide",
                        "entity_type": "guide",
                        "url": "https://example.test/icy-veins/mistweaver-monk-pve-healing-guide",
                        "ranking": {"score": 25},
                    },
                    {
                        "id": "mistweaver-monk-pvp-guide",
                        "name": "Icy Veins Mistweaver Monk PvP Guide",
                        "entity_type": "guide",
                        "url": "https://example.test/icy-veins/mistweaver-monk-pvp-guide",
                        "ranking": {"score": 22},
                    },
                ]
            },
        }

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        export_dir = Path(args[3])
        payload = _comparison_payload(
            provider=provider,
            slug="mistweaver-monk",
            page_url="https://example.test/method/mistweaver-monk",
            page_title="method guide",
            analysis_tags=["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        )
        write_article_bundle(payload, provider=provider, export_dir=export_dir)
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {"output_dir": str(export_dir), "guide": payload["guide"]},
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", fake_provider_resolve)
    monkeypatch.setattr("warcraft_cli.main.provider_search", fake_provider_search)
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        [
            "guide-compare-query",
            "mistweaver monk guide",
            "--provider",
            "method",
            "--provider",
            "icy-veins",
            "--out-root",
            str(tmp_path / "orchestrated"),
        ],
    )
    assert result.exit_code == 1

    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "insufficient_guides"
    assert payload["exported_bundle_count"] == 1
    assert payload["comparison"] is None
    icy_row = next(row for row in payload["provider_results"] if row["provider"] == "icy-veins")
    assert icy_row["status"] == "skipped"
    assert icy_row["reason"] == "search_top_guide_score_too_low:25"


def test_warcraft_search_sorts_results_globally_by_ranking(monkeypatch) -> None:
    def fake_wowhead_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {
                    "id": 19019,
                    "name": "Thunderfury",
                    "entity_type": "item",
                    "ranking": {"score": 15, "match_reasons": ["name_contains_query"]},
                },
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_wowhead_search)
    monkeypatch.setattr(
        "method_cli.main.MethodClient.sitemap_guides",
        lambda self: [{"slug": "mistweaver-monk", "name": "Mistweaver Monk", "url": "https://www.method.gg/guides/mistweaver-monk"}],
    )
    monkeypatch.setattr(
        "icy_veins_cli.main.IcyVeinsClient.sitemap_guides",
        lambda self: [{"slug": "frost-death-knight-pve-dps-guide", "name": "Frost Death Knight PvE DPS Guide",
                       "url": "https://www.icy-veins.com/wow/frost-death-knight-pve-dps-guide"}],
    )
    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, limit: (0, []))

    result = runner.invoke(warcraft_app, ["search", "mistweaver monk guide", "--limit", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["results"][0]["provider"] == "method"
    assert payload["results"][0]["wrapper_ranking"]["score"] >= payload["results"][0]["ranking"]["score"]


def test_warcraft_search_expansion_filter_excludes_nonmatching_providers(monkeypatch) -> None:
    def fake_wowhead_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {
                    "id": 19019,
                    "name": "Thunderfury",
                    "entity_type": "item",
                    "ranking": {"score": 20, "match_reasons": ["name_contains_query"]},
                },
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_wowhead_search)
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: (
        _ for _ in ()).throw(AssertionError("method should be excluded")))
    monkeypatch.setattr(
        "icy_veins_cli.main.IcyVeinsClient.sitemap_guides",
        lambda self: (_ for _ in ()).throw(AssertionError("icy-veins should be excluded")),
    )
    monkeypatch.setattr(
        "raiderio_cli.main.RaiderIOClient.search",
        lambda self, *, term, kind=None: (_ for _ in ()).throw(AssertionError("raiderio should be excluded")),
    )
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (_ for _ in ()).throw(AssertionError("warcraft-wiki should be excluded")),
    )
    monkeypatch.setattr(
        "wowprogress_cli.main.WowProgressClient.probe_search_route",
        lambda self, *, region, realm, name, obj_type: (_ for _ in ()).throw(AssertionError("wowprogress should be excluded")),
    )

    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "search", "thunderfury", "--limit", "3"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["requested_expansion"] == "wotlk"
    assert payload["expansion_filter_active"] is True
    assert payload["included_providers"] == ["wowhead"]
    assert payload["results"][0]["provider"] == "wowhead"
    assert payload["results"][0]["provider_expansion"]["mode"] == "profiled"
    assert {row["provider"] for row in payload["excluded_providers"]} == {
        "method",
        "icy-veins",
        "raiderio",
        "warcraftlogs",
        "warcraft-wiki",
        "wowprogress",
        "simc",
        "raidbots",
        "blizzard-api",
    }
    excluded = {row["provider"]: row["expansion_support"]["exclusion_reason"] for row in payload["excluded_providers"]}
    assert excluded["method"] == "provider_fixed_to_other_expansion"
    assert excluded["warcraft-wiki"] == "provider_fixed_to_other_expansion"


def test_warcraft_search_compact_expansion_debug(monkeypatch) -> None:
    def fake_wowhead_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {
                    "id": 19019,
                    "name": "Thunderfury",
                    "entity_type": "item",
                    "ranking": {"score": 20, "match_reasons": ["name_contains_query"]},
                },
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_wowhead_search)
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: (
        _ for _ in ()).throw(AssertionError("method should be excluded")))
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: (
        _ for _ in ()).throw(AssertionError("icy-veins should be excluded")))
    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.search", lambda self, *, term,
                        kind=None: (_ for _ in ()).throw(AssertionError("raiderio should be excluded")))
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query,
                        limit: (_ for _ in ()).throw(AssertionError("warcraft-wiki should be excluded")))
    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.probe_search_route", lambda self, *, region, realm,
                        name, obj_type: (_ for _ in ()).throw(AssertionError("wowprogress should be excluded")))

    result = runner.invoke(
        warcraft_app,
        ["--expansion", "wotlk", "search", "thunderfury", "--limit", "3", "--compact", "--expansion-debug"],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["providers"] == []
    assert payload["results"][0]["provider_expansion"]["mode"] == "profiled"
    snapshot = {row["provider"]: row["expansion_support"] for row in payload["expansion_debug"]}
    assert snapshot["wowhead"]["allowed"] is True
    assert snapshot["wowhead"]["review_status"] == "reviewed"
    assert snapshot["method"]["allowed"] is False
    assert snapshot["method"]["exclusion_reason"] == "provider_fixed_to_other_expansion"
    assert "retail-focused" in snapshot["method"]["policy_note"]


def test_warcraft_search_retail_filter_keeps_fixed_retail_providers_and_excludes_none(monkeypatch) -> None:
    def fake_wowhead_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {
                    "id": 19019,
                    "name": "Thunderfury",
                    "entity_type": "item",
                    "ranking": {"score": 20, "match_reasons": ["name_contains_query"]},
                },
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_wowhead_search)
    monkeypatch.setattr(
        "method_cli.main.MethodClient.sitemap_guides",
        lambda self: [{"slug": "mistweaver-monk", "name": "Mistweaver Monk", "url": "https://www.method.gg/guides/mistweaver-monk"}],
    )
    monkeypatch.setattr(
        "icy_veins_cli.main.IcyVeinsClient.sitemap_guides",
        lambda self: [{"slug": "mistweaver-monk-pve-healing-guide", "name": "Mistweaver Monk PvE Healing Guide",
                       "url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide"}],
    )
    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: [{"title": "Mistweaver Monk", "pageid": 1}],
    )
    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.probe_search_route", lambda self, *, region, realm, name, obj_type: None)

    result = runner.invoke(warcraft_app, ["--expansion", "retail", "search", "mistweaver monk guide", "--limit", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["requested_expansion"] == "retail"
    assert payload["expansion_filter_active"] is True
    assert set(payload["included_providers"]) == {
        "wowhead",
        "method",
        "icy-veins",
        "raiderio",
        "warcraftlogs",
        "warcraft-wiki",
        "wowprogress",
    }
    assert {row["provider"] for row in payload["excluded_providers"]} == {"simc", "raidbots", "blizzard-api"}
    results = {row["provider"] for row in payload["results"]}
    assert {"method", "icy-veins"} & results


def test_warcraft_resolve_retail_filter_keeps_fixed_retail_profile_provider(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr(
        "raiderio_cli.main.RaiderIOClient.search",
        lambda self, *, term, kind=None: {"matches": []},
    )
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: [],
    )
    monkeypatch.setattr(
        "wowprogress_cli.main.WowProgressClient.probe_search_route",
        lambda self, *, region, realm, name, obj_type: {
            "_search_kind": "guild",
            "guild": {
                "name": "Liquid",
                "region": "us",
                "realm": "illidan",
                "faction": "Horde",
                "page_url": "https://www.wowprogress.com/guild/us/illidan/Liquid",
            },
        }
        if obj_type == "guild"
        else None,
    )

    result = runner.invoke(warcraft_app, ["--expansion", "retail", "resolve", "guild us illidan Liquid"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["requested_expansion"] == "retail"
    assert payload["expansion_filter_active"] is True
    assert payload["provider"] == "wowprogress"
    assert set(payload["included_providers"]) == {
        "wowhead",
        "method",
        "icy-veins",
        "raiderio",
        "warcraftlogs",
        "warcraft-wiki",
        "wowprogress",
    }
    assert {row["provider"] for row in payload["excluded_providers"]} == {"simc", "raidbots", "blizzard-api"}


def test_warcraft_search_prefers_profile_provider_for_structured_guild_queries(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr(
        "method_cli.main.MethodClient.sitemap_guides",
        lambda self: [{"slug": "liquid-guide", "name": "Liquid Guide", "url": "https://www.method.gg/guides/liquid-guide"}],
    )
    monkeypatch.setattr(
        "icy_veins_cli.main.IcyVeinsClient.sitemap_guides",
        lambda self: [{"slug": "liquid-guide", "name": "Liquid Guide", "url": "https://www.icy-veins.com/wow/liquid-guide"}],
    )
    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, limit: (0, []))
    monkeypatch.setattr(
        "wowprogress_cli.main.WowProgressClient.probe_search_route",
        lambda self, *, region, realm, name, obj_type: {
            "_search_kind": "guild",
            "guild": {
                "name": "Liquid",
                "region": "us",
                "realm": "illidan",
                "faction": "Horde",
                "page_url": "https://www.wowprogress.com/guild/us/illidan/Liquid",
            },
        }
        if obj_type == "guild"
        else None,
    )

    result = runner.invoke(warcraft_app, ["search", "guild us illidan Liquid", "--limit", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["results"][0]["provider"] == "wowprogress"
    assert any(
        "intent:structured_profile:family:profile" in reason
        for reason in payload["results"][0]["wrapper_ranking"]["reasons"]
    )


def test_warcraft_search_compact_and_ranking_debug(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, limit: (0, []))
    monkeypatch.setattr(
        "wowprogress_cli.main.WowProgressClient.probe_search_route",
        lambda self, *, region, realm, name, obj_type: {
            "_search_kind": "guild",
            "guild": {
                "name": "Liquid",
                "region": "us",
                "realm": "illidan",
                "faction": "Horde",
                "page_url": "https://www.wowprogress.com/guild/us/illidan/Liquid",
            },
        }
        if obj_type == "guild"
        else None,
    )

    result = runner.invoke(warcraft_app, ["search", "guild us illidan Liquid", "--limit", "3", "--compact", "--ranking-debug"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["providers"] == []
    assert payload["results"][0]["provider"] == "wowprogress"
    assert payload["ranking_debug"][0]["wrapper_ranking"]["provider_family"] == "profile"


def test_warcraft_search_adds_synthetic_wowprogress_leaderboard_candidate(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, limit: (0, []))

    result = runner.invoke(warcraft_app, ["search", "leaderboard us illidan", "--compact", "--ranking-debug"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["results"][0]["provider"] == "wowprogress"
    assert payload["results"][0]["kind"] == "leaderboard"
    assert payload["results"][0]["follow_up_command"] == "wowprogress leaderboard pve us --realm illidan"


def test_warcraft_resolve_prefers_stronger_later_provider(monkeypatch) -> None:
    def fake_wowhead_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 100, "id": 2594, "name": "Warlords of Draenor Mistweaver Monk Guide", "typeName": "Guide", "popularity": 8},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_wowhead_search)
    monkeypatch.setattr(
        "method_cli.main.MethodClient.sitemap_guides",
        lambda self: [{"slug": "mistweaver-monk", "name": "Mistweaver Monk", "url": "https://www.method.gg/guides/mistweaver-monk"}],
    )
    monkeypatch.setattr(
        "icy_veins_cli.main.IcyVeinsClient.sitemap_guides",
        lambda self: [{"slug": "mistweaver-monk-pve-healing-guide", "name": "Mistweaver Monk PvE Healing Guide",
                       "url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide"}],
    )
    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (1, [{"title": "Mistweaver Monk", "pageid": 1, "snippet": "Reference page",
                                    "url": "https://warcraft.wiki.gg/wiki/Mistweaver_Monk"}]),
    )

    result = runner.invoke(warcraft_app, ["resolve", "mistweaver monk guide"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["resolved"] is True
    assert payload["provider"] == "icy-veins"
    assert payload["confidence"] == "high"
    assert payload["next_command"] == "icy-veins guide mistweaver-monk-pve-healing-guide"


def test_warcraft_resolve_expansion_filter_blocks_retail_only_resolution(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.search_suggestions",
        lambda self, query: {"search": query, "results": []},
    )
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: (
        _ for _ in ()).throw(AssertionError("method should be excluded")))
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: (
        _ for _ in ()).throw(AssertionError("icy-veins should be excluded")))
    monkeypatch.setattr(
        "raiderio_cli.main.RaiderIOClient.search",
        lambda self, *, term, kind=None: (_ for _ in ()).throw(AssertionError("raiderio should be excluded")),
    )
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, limit: (_ for _ in ()).throw(AssertionError("warcraft-wiki should be excluded")),
    )
    monkeypatch.setattr(
        "wowprogress_cli.main.WowProgressClient.probe_search_route",
        lambda self, *, region, realm, name, obj_type: (_ for _ in ()).throw(AssertionError("wowprogress should be excluded")),
    )

    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "resolve", "guild us illidan Liquid"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["requested_expansion"] == "wotlk"
    assert payload["expansion_filter_active"] is True
    assert payload["resolved"] is False
    assert payload["provider"] is None
    assert payload["included_providers"] == ["wowhead"]
    assert {row["provider"] for row in payload["excluded_providers"]} == {
        "method",
        "icy-veins",
        "raiderio",
        "warcraftlogs",
        "warcraft-wiki",
        "wowprogress",
        "simc",
        "raidbots",
        "blizzard-api",
    }


def test_warcraft_resolve_expansion_debug(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: (
        _ for _ in ()).throw(AssertionError("method should be excluded")))
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: (
        _ for _ in ()).throw(AssertionError("icy-veins should be excluded")))
    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.search", lambda self, *, term,
                        kind=None: (_ for _ in ()).throw(AssertionError("raiderio should be excluded")))
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query,
                        limit: (_ for _ in ()).throw(AssertionError("warcraft-wiki should be excluded")))
    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.probe_search_route", lambda self, *, region, realm,
                        name, obj_type: (_ for _ in ()).throw(AssertionError("wowprogress should be excluded")))

    result = runner.invoke(
        warcraft_app,
        ["--expansion", "wotlk", "resolve", "guild us illidan Liquid", "--compact", "--expansion-debug"],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    snapshot = {row["provider"]: row["expansion_support"] for row in payload["expansion_debug"]}
    assert snapshot["wowhead"]["allowed"] is True
    assert snapshot["simc"]["allowed"] is False
    assert snapshot["simc"]["exclusion_reason"] == "provider_has_no_expansion_support"
    assert snapshot["simc"]["review_status"] == "deferred"
    assert "Local repo analysis" in snapshot["simc"]["policy_note"]


def test_warcraft_resolve_prefers_ready_provider(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 5, "id": 86739, "name": "Fairbreeze Favors", "typeName": "Quest", "popularity": 7},
            ],
        }

    monkeypatch.setattr(
        "method_cli.main.MethodClient.sitemap_guides",
        lambda self: [{"slug": "mistweaver-monk", "name": "Mistweaver Monk", "url": "https://www.method.gg/guides/mistweaver-monk"}],
    )
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, limit: (0, []))
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(warcraft_app, ["resolve", "fairbreeze favors"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["resolved"] is True
    assert payload["provider"] == "wowhead"
    assert payload["next_command"] == "wowhead entity quest 86739"


def test_warcraft_resolve_can_select_raiderio(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr(
        "raiderio_cli.main.RaiderIOClient.search",
        lambda self, *, term, kind=None: {
            "matches": [
                {
                    "type": "character",
                    "name": "Roguecane",
                    "data": {
                        "id": 39943,
                        "name": "Roguecane",
                        "faction": "horde",
                        "region": {"slug": "us", "name": "United States & Oceania"},
                        "realm": {"slug": "illidan", "name": "Illidan"},
                        "class": {"name": "Rogue", "slug": "rogue"},
                    },
                }
            ]
        },
    )
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, limit: (0, []))

    result = runner.invoke(warcraft_app, ["resolve", "Roguecane"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["resolved"] is True
    assert payload["provider"] == "raiderio"
    assert payload["next_command"] == "raiderio character us illidan Roguecane"


def test_warcraft_resolve_prefers_raiderio_for_character_queries_when_both_resolve(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr(
        "raiderio_cli.main.RaiderIOClient.search",
        lambda self, *, term, kind=None: {
            "matches": [
                {
                    "type": "character",
                    "name": "Roguecane",
                    "data": {
                        "id": 39943,
                        "name": "Roguecane",
                        "faction": "horde",
                        "region": {"slug": "us", "name": "United States & Oceania"},
                        "realm": {"slug": "illidan", "name": "Illidan"},
                        "class": {"name": "Rogue", "slug": "rogue"},
                    },
                }
            ]
        },
    )
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, limit: (0, []))
    monkeypatch.setattr(
        "wowprogress_cli.main.WowProgressClient.probe_search_route",
        lambda self, *, region, realm, name, obj_type: {
            "_search_kind": "character",
            "character": {
                "name": "Roguecane",
                "region": "us",
                "realm": "illidan",
                "guild_name": "Liquid",
                "class_name": "Rogue",
                "page_url": "https://www.wowprogress.com/character/us/illidan/Roguecane",
            },
        }
        if obj_type == "char"
        else None,
    )

    result = runner.invoke(warcraft_app, ["resolve", "character us illidan Roguecane", "--ranking-debug"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["resolved"] is True
    assert payload["provider"] == "raiderio"
    assert payload["next_command"] == "raiderio character us illidan Roguecane"
    assert payload["ranking_debug"][0]["provider"] == "raiderio"


def test_warcraft_resolve_can_select_wowprogress(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, limit: (0, []))
    monkeypatch.setattr(
        "wowprogress_cli.main.WowProgressClient.probe_search_route",
        lambda self, *, region, realm, name, obj_type: {
            "_search_kind": "guild",
            "guild": {
                "name": "Liquid",
                "region": "us",
                "realm": "illidan",
                "faction": "Horde",
                "page_url": "https://www.wowprogress.com/guild/us/illidan/Liquid",
            },
        }
        if obj_type == "guild"
        else None,
    )

    result = runner.invoke(warcraft_app, ["resolve", "guild us illidan Liquid"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["resolved"] is True
    assert payload["provider"] == "wowprogress"
    assert payload["next_command"] == "wowprogress guild us illidan Liquid"
    assert payload["match"]["wrapper_ranking"]["provider_family"] == "profile"


def test_warcraft_resolve_can_select_warcraftlogs_for_explicit_report_reference(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, limit: (0, []))
    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.probe_search_route", lambda self, *, region, realm, name, obj_type: None)

    result = runner.invoke(warcraft_app, ["resolve", "https://www.warcraftlogs.com/reports/abcd1234#fight=3"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["resolved"] is True
    assert payload["provider"] == "warcraftlogs"
    assert payload["next_command"] == "warcraftlogs report-encounter abcd1234 --fight-id 3"
    assert payload["match"]["wrapper_ranking"]["provider_family"] == "logs"


def test_warcraft_resolve_compact_and_ranking_debug(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, limit: (0, []))
    monkeypatch.setattr(
        "wowprogress_cli.main.WowProgressClient.probe_search_route",
        lambda self, *, region, realm, name, obj_type: {
            "_search_kind": "guild",
            "guild": {
                "name": "Liquid",
                "region": "us",
                "realm": "illidan",
                "faction": "Horde",
                "page_url": "https://www.wowprogress.com/guild/us/illidan/Liquid",
            },
        }
        if obj_type == "guild"
        else None,
    )

    result = runner.invoke(warcraft_app, ["resolve", "guild us illidan Liquid", "--compact", "--ranking-debug"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["providers"] == []
    assert payload["match"]["provider"] == "wowprogress"
    assert payload["ranking_debug"][0]["provider"] == "wowprogress"


def test_warcraft_resolve_does_not_fabricate_synthetic_wowprogress_leaderboard_route(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, limit: (0, []))

    result = runner.invoke(warcraft_app, ["resolve", "leaderboard us illidan", "--compact", "--ranking-debug"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["resolved"] is False
    assert payload["provider"] is None
    assert payload["next_command"] is None
    assert payload["match"] is None


def test_warcraft_passthrough_to_wowhead(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 3, "id": 19019, "name": "Thunderfury", "typeName": "Item"},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(warcraft_app, ["wowhead", "search", "thunderfury", "--limit", "1"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["query"] == "thunderfury"
    assert payload["results"][0]["name"] == "Thunderfury"


def test_warcraft_passthrough_to_wowhead_injects_global_expansion(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 3, "id": 19019, "name": "Thunderfury", "typeName": "Item"},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "wowhead", "search", "thunderfury", "--limit", "1"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["expansion"] == "wotlk"


def test_warcraft_passthrough_rejects_unsupported_provider_expansion() -> None:
    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "method", "guide", "mistweaver-monk"])
    assert result.exit_code == 1

    payload = json.loads(result.output)
    assert payload["error"]["code"] == "unsupported_provider_expansion"
    assert payload["provider"] == "method"
    assert payload["requested_expansion"] == "wotlk"


def test_warcraft_passthrough_rejects_duplicate_wowhead_expansion() -> None:
    result = runner.invoke(
        warcraft_app,
        ["--expansion", "wotlk", "wowhead", "--expansion", "retail", "search", "thunderfury", "--limit", "1"],
    )
    assert result.exit_code == 1

    payload = json.loads(result.output)
    assert payload["error"]["code"] == "duplicate_expansion_argument"


def test_warcraft_passthrough_to_method(monkeypatch) -> None:
    def fake_fetch(self, guide_ref):  # noqa: ANN001
        return {
            "guide": {
                "slug": "mistweaver-monk",
                "page_url": "https://www.method.gg/guides/mistweaver-monk",
                "section_slug": "introduction",
                "section_title": "Introduction",
                "author": "Tincell",
                "last_updated": "Last Updated: 26th Feb, 2026",
                "patch": "Patch 12.0.1",
            },
            "page": {
                "title": "Method Mistweaver Monk Guide - Introduction - Midnight 12.0.1",
                "description": "Learn the Mistweaver Monk basics.",
                "canonical_url": "https://www.method.gg/guides/mistweaver-monk",
            },
            "navigation": [],
            "article": {"html": "<p>Intro</p>", "text": "Intro", "headings": [], "sections": []},
            "linked_entities": [],
        }

    monkeypatch.setattr("method_cli.main.MethodClient.fetch_guide_page", fake_fetch)
    result = runner.invoke(warcraft_app, ["method", "guide", "mistweaver-monk"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["guide"]["slug"] == "mistweaver-monk"
    assert payload["guide"]["author"] == "Tincell"


def test_warcraft_passthrough_to_icy_veins(monkeypatch) -> None:
    def fake_fetch(self, guide_ref):  # noqa: ANN001
        return {
            "guide": {
                "slug": "mistweaver-monk-pve-healing-guide",
                "page_url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
                "section_slug": "mistweaver-monk-pve-healing-guide",
                "section_title": "Mistweaver Monk Guide",
                "author": "Dhaubbs",
                "last_updated": "2026-03-05T05:19:00+00:00",
                "published_at": "2012-09-13T02:17:00+00:00",
            },
            "page": {
                "title": "Mistweaver Monk Healing Guide - Midnight (12.0.1)",
                "description": "This guide contains everything you need to know to be an excellent Mistweaver Monk.",
                "canonical_url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
                "page_type": "guides",
            },
            "navigation": [],
            "page_toc": [],
            "article": {"html": "<p>Intro</p>", "text": "Intro", "intro_text": "General Information", "headings": [], "sections": []},
            "linked_entities": [],
            "citations": {"page": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide"},
        }

    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.fetch_guide_page", fake_fetch)
    result = runner.invoke(warcraft_app, ["icy-veins", "guide", "mistweaver-monk-pve-healing-guide"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["guide"]["slug"] == "mistweaver-monk-pve-healing-guide"
    assert payload["guide"]["author"] == "Dhaubbs"


def test_warcraft_passthrough_to_simc(monkeypatch, tmp_path) -> None:
    profile = tmp_path / "example.simc"
    profile.write_text('monk="example"\n')

    monkeypatch.setattr(
        "simc_cli.main.run_profile",
        lambda paths, profile_path, simc_args: type("Result", (), {"command": [str(paths.build_simc), str(
            profile_path)], "returncode": 0, "stdout": "Iterations: 1\n", "stderr": ""})(),
    )
    monkeypatch.setattr(
        "simc_cli.main.binary_version",
        lambda paths: type("VersionInfo", (), {"binary_path": paths.build_simc, "available": True,
                           "version_line": "SimulationCraft 1201", "returncode": 1})(),
    )

    result = runner.invoke(warcraft_app, ["simc", "run", str(profile)])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["provider"] == "simc"
    assert payload["status"] == "completed"
    assert payload["version"] == "SimulationCraft 1201"


def test_warcraft_passthrough_to_simc_validate_talent_transport(monkeypatch) -> None:
    monkeypatch.setattr(
        "simc_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": None,
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": "druid",
                "spec": "balance",
            },
        },
    )

    result = runner.invoke(
        warcraft_app,
        [
            "simc",
            "validate-talent-transport",
            "--actor-class",
            "druid",
            "--spec",
            "balance",
            "--talent-row",
            "103324:82244:1",
            "--talent-row",
            "109839:88206:1",
        ],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["provider"] == "simc"
    assert payload["kind"] == "validate_talent_transport"
    assert payload["transport_status"] == "validated"
    assert payload["transport_forms"]["simc_split_talents"]["spec_talents"] == "109839:1"


def test_warcraft_packet_handoff_from_warcraftlogs_to_simc(monkeypatch, tmp_path: Path) -> None:
    raw_packet_path = tmp_path / "raw-packet.json"
    validated_packet_path = tmp_path / "validated-packet.json"

    class _PacketClient:
        def report_player_details(self, **kwargs):  # noqa: ANN003, ANN201
            return {}

        def close(self) -> None:
            return None

    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _PacketClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main._resolve_encounter_scope",
        lambda ctx, *, client, reference, fight_id, allow_unlisted: (
            type("Ref", (), {"code": "abcd1234", "fight_id": 1, "source_url": None})(),
            {"code": "abcd1234", "title": "Test Report", "startTime": 1, "endTime": 2},
            {"id": 1, "encounterID": 3012, "name": "Dimensius", "kill": True, "difficulty": 5, "startTime": 0, "endTime": 1},
            {"id": 3012, "journalID": 3001, "name": "Dimensius", "zone": {
                "id": 38, "name": "Nerub-ar Palace", "expansion": {"id": 10, "name": "Retail"}}},
        ),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main._report_player_details_payload",
        lambda payload, *, report_code=None, fight_id=None: {
            "player_details": {
                "roles": {
                    "dps": [
                        {
                            "id": 9,
                            "name": "gubkfc",
                            "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                            "combatant_info": {
                                "talentTree": [
                                    {"id": 103324, "nodeID": 82244, "rank": 1},
                                    {"id": 109839, "nodeID": 88206, "rank": 1},
                                ]
                            },
                        }
                    ]
                }
            }
        },
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {},
            "validation": {
                "status": "not_validated",
                "reason": "simc_trait_resolution_incomplete",
            },
        },
    )
    monkeypatch.setattr(
        "simc_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": None,
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": "druid",
                "spec": "balance",
            },
        },
    )

    export_result = runner.invoke(
        warcraft_app,
        [
            "warcraftlogs",
            "report-player-talents",
            "abcd1234",
            "--fight-id",
            "1",
            "--actor-id",
            "9",
            "--out",
            str(raw_packet_path),
        ],
    )
    assert export_result.exit_code == 0
    export_payload = json.loads(export_result.stdout)
    assert export_payload["written_packet_path"] == str(raw_packet_path.resolve())

    validate_result = runner.invoke(
        warcraft_app,
        [
            "simc",
            "validate-talent-transport",
            "--build-packet",
            str(raw_packet_path),
            "--out",
            str(validated_packet_path),
        ],
    )
    assert validate_result.exit_code == 0
    validate_payload = json.loads(validate_result.stdout)
    assert validate_payload["transport_status"] == "validated"
    assert validate_payload["written_packet_path"] == str(validated_packet_path.resolve())

    written_packet = json.loads(validated_packet_path.read_text())
    assert written_packet["transport_status"] == "validated"
    assert written_packet["source"] == {"provider": "warcraftlogs", "source": "warcraftlogs_talent_tree"}
    assert written_packet["scope"] == {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9}
    assert written_packet["transport_forms"]["simc_split_talents"]["spec_talents"] == "109839:1"


def test_warcraft_talent_packet_routes_explicit_wowhead_ref(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append((provider, args))
        assert provider == "wowhead"
        return {
            "provider": "wowhead",
            "exit_code": 0,
            "payload": {
                "provider": "wowhead",
                "kind": "talent_calc_packet",
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "exact",
                    "transport_forms": {
                        "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                    },
                    "build_identity": {
                        "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                    },
                    "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                    "validation": {},
                    "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "warcraft"
    assert payload["kind"] == "talent_transport"
    assert payload["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}
    assert payload["source_packet_status"] == "exact"
    assert payload["upgrade_attempted"] is False
    assert payload["upgraded"] is False
    assert payload["talent_transport_packet"]["transport_forms"]["wowhead_talent_calc_url"] == "https://www.wowhead.com/talent-calc/druid/balance/ABC123"
    assert calls == [("wowhead", ["talent-calc-packet", "druid/balance/ABC123", "--listed-build-limit", "10"])]


def test_warcraft_talent_packet_routes_explicit_wowhead_ref_without_client_init(monkeypatch) -> None:
    _disable_wowhead_client_init(monkeypatch)

    result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/ABC123", "--no-validate"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}
    assert payload["talent_transport_packet"]["transport_status"] == "exact"
    assert "listed_builds" not in payload["producer_result"]["payload"]


def test_warcraft_talent_packet_passes_wowhead_listed_build_limit_and_expansion(monkeypatch) -> None:
    calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append((provider, args, expansion))
        return {
            "provider": "wowhead",
            "exit_code": 0,
            "payload": {
                "provider": "wowhead",
                "kind": "talent_calc_packet",
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "exact",
                    "transport_forms": {
                        "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                    },
                    "build_identity": {
                        "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                    },
                    "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                    "validation": {},
                    "scope": {"type": "wowhead_talent_calc", "expansion": "wotlk"},
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["--expansion", "wotlk", "talent-packet", "druid/balance/ABC123", "--listed-build-limit", "3"],
    )
    assert result.exit_code == 0
    assert calls == [("wowhead", ["talent-calc-packet", "druid/balance/ABC123", "--listed-build-limit", "3"], "wotlk")]


def test_warcraft_talent_packet_routes_expansion_prefixed_wowhead_ref(monkeypatch) -> None:
    calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append((provider, args, expansion))
        return {
            "provider": "wowhead",
            "exit_code": 0,
            "payload": {
                "provider": "wowhead",
                "kind": "talent_calc_packet",
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "exact",
                    "transport_forms": {
                        "wowhead_talent_calc_url": "https://www.wowhead.com/cata/talent-calc/hunter/beast-mastery/XYZ987",
                    },
                    "build_identity": {
                        "class_spec_identity": {"identity": {"actor_class": "hunter", "spec": "beast_mastery"}},
                    },
                    "raw_evidence": {"reference_url": "https://www.wowhead.com/cata/talent-calc/hunter/beast-mastery/XYZ987"},
                    "validation": {},
                    "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["talent-packet", "cata/talent-calc/hunter/beast-mastery/XYZ987", "--no-validate"],
    )
    assert result.exit_code == 0
    assert calls == [
        ("wowhead", ["talent-calc-packet", "cata/talent-calc/hunter/beast-mastery/XYZ987", "--listed-build-limit", "10"], None)
    ]
    payload = json.loads(result.stdout)
    assert payload["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}


def test_warcraft_talent_packet_routes_expansion_prefixed_class_spec_wowhead_ref(monkeypatch) -> None:
    calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append((provider, args, expansion))
        return {
            "provider": "wowhead",
            "exit_code": 0,
            "payload": {
                "provider": "wowhead",
                "kind": "talent_calc_packet",
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "exact",
                    "transport_forms": {
                        "wowhead_talent_calc_url": "https://www.wowhead.com/classic/talent-calc/druid/balance/ABC123",
                    },
                    "build_identity": {
                        "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                    },
                    "raw_evidence": {"reference_url": "https://www.wowhead.com/classic/talent-calc/druid/balance/ABC123"},
                    "validation": {},
                    "scope": {"type": "wowhead_talent_calc", "expansion": "classic"},
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["talent-packet", "classic/druid/balance/ABC123", "--no-validate"],
    )
    assert result.exit_code == 0
    assert calls == [
        ("wowhead", ["talent-calc-packet", "classic/druid/balance/ABC123", "--listed-build-limit", "10"], None)
    ]
    payload = json.loads(result.stdout)
    assert payload["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}


def test_warcraft_talent_packet_passes_allow_unlisted_to_warcraftlogs(monkeypatch) -> None:
    calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append((provider, args, expansion))
        return {
            "provider": "warcraftlogs",
            "exit_code": 0,
            "payload": {
                "provider": "warcraftlogs",
                "kind": "report_player_talents",
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "raw_only",
                    "build_identity": {},
                    "transport_forms": {},
                    "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                    "validation": {"status": "not_validated"},
                    "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["talent-packet", "abcd1234", "--actor-id", "9", "--fight-id", "1", "--allow-unlisted", "--no-validate"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["route"]["allow_unlisted"] is True
    assert calls == [("warcraftlogs", ["report-player-talents", "abcd1234",
                      "--actor-id", "9", "--fight-id", "1", "--allow-unlisted"], None)]


def test_warcraft_talent_packet_routes_scheme_less_warcraftlogs_report_ref(monkeypatch) -> None:
    calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append((provider, args, expansion))
        return {
            "provider": "warcraftlogs",
            "exit_code": 0,
            "payload": {
                "provider": "warcraftlogs",
                "kind": "report_player_talents",
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "raw_only",
                    "build_identity": {},
                    "transport_forms": {},
                    "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                    "validation": {"status": "not_validated"},
                    "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["talent-packet", "warcraftlogs.com/reports/abcd1234#fight=1", "--actor-id", "9", "--no-validate"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["route"] == {
        "kind": "warcraftlogs_report_actor",
        "provider": "warcraftlogs",
        "actor_id": 9,
        "fight_id": None,
        "allow_unlisted": False,
    }
    assert calls == [
        (
            "warcraftlogs",
            ["report-player-talents", "https://warcraftlogs.com/reports/abcd1234#fight=1", "--actor-id", "9"],
            None,
        )
    ]


def test_warcraft_talent_packet_routes_alpha_only_warcraftlogs_report_code(monkeypatch) -> None:
    calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append((provider, args, expansion))
        return {
            "provider": "warcraftlogs",
            "exit_code": 0,
            "payload": {
                "provider": "warcraftlogs",
                "kind": "report_player_talents",
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "raw_only",
                    "build_identity": {},
                    "transport_forms": {},
                    "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                    "validation": {"status": "not_validated"},
                    "scope": {"type": "report_fight_actor", "report_code": "abcdefgh", "fight_id": 1, "actor_id": 9},
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["talent-packet", "abcdefgh", "--actor-id", "9", "--fight-id", "1", "--no-validate"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["route"]["provider"] == "warcraftlogs"
    assert calls == [
        ("warcraftlogs", ["report-player-talents", "abcdefgh", "--actor-id", "9", "--fight-id", "1"], None)
    ]


def test_warcraft_talent_packet_routes_warcraftlogs_and_upgrades(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append((provider, args))
        if provider == "warcraftlogs":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "provider": provider,
                    "kind": "report_player_talents",
                    "talent_transport_packet": {
                        "kind": "talent_transport_packet",
                        "transport_status": "raw_only",
                        "build_identity": {
                            "class_spec_identity": {
                                "identity": {"actor_class": "druid", "spec": "balance"},
                            }
                        },
                        "transport_forms": {},
                        "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                        "validation": {"status": "not_validated"},
                        "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
                    },
                },
                "stdout": "",
            }
        packet = json.loads(Path(args[2]).read_text())
        assert provider == "simc"
        assert args[:2] == ["validate-talent-transport", "--build-packet"]
        assert packet["transport_status"] == "raw_only"
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "provider": provider,
                "kind": "validate_talent_transport",
                "input": {"source": "build_packet", "build_packet": args[2]},
                "updated_packet": {
                    **packet,
                    "transport_status": "validated",
                    "transport_forms": {"simc_split_talents": {"class_talents": "103324:1"}},
                    "validation": {
                        "status": "validated",
                        "source": "simc_trait_data_round_trip",
                        "actor_class": "druid",
                        "spec": "balance",
                    },
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "abcd1234", "--fight-id", "1", "--actor-id", "9"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["route"] == {
        "kind": "warcraftlogs_report_actor",
        "provider": "warcraftlogs",
        "actor_id": 9,
        "fight_id": 1,
        "allow_unlisted": False,
    }
    assert payload["source_packet_status"] == "raw_only"
    assert payload["upgrade_attempted"] is True
    assert payload["upgraded"] is True
    assert payload["talent_transport_packet"]["transport_status"] == "validated"
    assert payload["talent_transport_packet"]["transport_forms"]["simc_split_talents"]["class_talents"] == "103324:1"
    assert "build_packet" not in payload["upgrade_result"]["payload"]["input"]
    assert calls[0] == ("warcraftlogs", ["report-player-talents", "abcd1234", "--actor-id", "9", "--fight-id", "1"])
    assert calls[1][0] == "simc"
    assert calls[1][1][:2] == ["validate-talent-transport", "--build-packet"]


def test_warcraft_talent_packet_preserves_missing_talent_tree_for_non_dict_rows(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _EndToEndWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main._player_detail_actor",
        lambda details_payload, actor_id: {
            "id": actor_id,
            "combatant_info": {
                "talentTree": [
                    {"id": 103324, "nodeID": 82244, "rank": 1},
                    "bad-row",
                ]
            },
            "class_spec_identity": {
                "identity": {"actor_class": "paladin", "spec": "retribution"},
            },
        },
    )

    result = runner.invoke(
        warcraft_app,
        ["talent-packet", "abcd1234", "--fight-id", "1", "--actor-id", "9"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "missing_talent_tree"
    assert "incomplete combatant_info.talentTree rows" in payload["error"]["message"]


def test_warcraft_talent_packet_upgrades_packet_file_and_writes_output(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "raw-packet.json"
    out_path = tmp_path / "validated-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "druid", "spec": "balance"},
                    }
                },
                "transport_forms": {},
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                "validation": {"status": "not_validated"},
                "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
            }
        )
    )

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        packet = json.loads(Path(args[2]).read_text())
        assert provider == "simc"
        assert packet["transport_status"] == "raw_only"
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "input": {"source": "build_packet", "build_packet": args[2]},
                "updated_packet": {
                    **packet,
                    "transport_status": "validated",
                    "transport_forms": {"simc_split_talents": {"class_talents": "103324:1"}},
                    "validation": {
                        "status": "validated",
                        "source": "simc_trait_data_round_trip",
                        "actor_class": "druid",
                        "spec": "balance",
                    },
                }
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path), "--out", str(out_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["route"] == {"kind": "packet_file", "provider": None, "packet_path": str(packet_path.resolve())}
    assert payload["written_packet_path"] == str(out_path.resolve())
    assert payload["talent_transport_packet"]["transport_status"] == "validated"
    assert "build_packet" not in payload["upgrade_result"]["payload"]["input"]
    written = json.loads(out_path.read_text())
    assert written["transport_status"] == "validated"


def test_warcraft_talent_packet_reemits_unknown_packet_file_without_auto_upgrade(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "unknown-packet.json"
    out_path = tmp_path / "unknown-out.json"
    packet = {
        "kind": "talent_transport_packet",
        "transport_status": "unknown",
        "build_identity": {
            "class_spec_identity": {
                "identity": {"actor_class": "druid", "spec": "balance"},
            }
        },
        "transport_forms": {},
        "raw_evidence": {
            "talent_tree_entries": [{"entry": None, "node_id": None, "rank": None}],
        },
        "validation": {"status": "not_validated"},
        "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
    }
    packet_path.write_text(json.dumps(packet))

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        raise AssertionError(f"unexpected provider call: {provider} {args}")

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path), "--out", str(out_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["source_packet_status"] == "unknown"
    assert payload["upgrade_attempted"] is False
    assert payload["upgraded"] is False
    assert payload["talent_transport_packet"] == packet
    assert json.loads(out_path.read_text()) == packet


def test_warcraft_talent_packet_requires_explicit_source_contract() -> None:
    result = runner.invoke(warcraft_app, ["talent-packet", "abcd1234"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unsupported_talent_source"


def test_warcraft_talent_packet_rejects_non_wowhead_absolute_url() -> None:
    result = runner.invoke(
        warcraft_app,
        ["talent-packet", "https://notwowhead.com/talent-calc/druid/balance/ABC123"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unsupported_talent_source"


def test_warcraft_talent_packet_rejects_wowhead_ref_without_build_code() -> None:
    result = runner.invoke(
        warcraft_app,
        ["talent-packet", "druid/balance"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc packet refs must include an explicit build code."


def test_warcraft_talent_packet_rejects_buried_real_wowhead_talent_calc_path() -> None:
    result = runner.invoke(
        warcraft_app,
        ["talent-packet", "https://www.wowhead.com/items/talent-calc/druid/balance/ABC123"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"


def test_warcraft_talent_packet_rejects_nested_talent_calc_non_url_ref() -> None:
    result = runner.invoke(
        warcraft_app,
        ["talent-packet", "talent-calc/foo/talent-calc/druid/balance/ABC123"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must be a Wowhead talent-calc path or class/spec ref."


def test_warcraft_talent_packet_rejects_fake_host_wowhead_like_input() -> None:
    result = runner.invoke(
        warcraft_app,
        ["talent-packet", "notwowhead.com/talent-calc/druid/balance/ABC123"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must be a Wowhead talent-calc path or class/spec ref."


def test_warcraft_talent_packet_accepts_hyphenated_wowhead_class_slug(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "wowhead"
        assert args == ["talent-calc-packet", "death-knight/frost/ABC123", "--listed-build-limit", "10"]
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "exact",
                    "build_identity": {
                        "class_spec_identity": {"identity": {"actor_class": "deathknight", "spec": "frost"}}
                    },
                    "transport_forms": {
                        "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/death-knight/frost/ABC123"
                    },
                    "raw_evidence": {
                        "reference_url": "https://www.wowhead.com/talent-calc/death-knight/frost/ABC123"
                    },
                    "validation": {},
                    "scope": {},
                }
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "death-knight/frost/ABC123", "--no-validate"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}
    assert payload["talent_transport_packet"]["transport_status"] == "exact"


def test_warcraft_talent_packet_rejects_invalid_packet_file(tmp_path: Path) -> None:
    packet_path = tmp_path / "broken-packet.json"
    packet_path.write_text("{not json}\n")

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"
    assert payload["source"] == str(packet_path)


def test_warcraft_talent_packet_rejects_missing_packet_path_like_input(tmp_path: Path) -> None:
    packet_path = tmp_path / "missing-packet.json"

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"
    assert payload["error"]["message"] == f"Talent transport packet file was not found: {packet_path}"


def test_warcraft_talent_packet_rejects_missing_file_like_class_spec_json_path() -> None:
    result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/missing-packet.json"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"
    assert payload["error"]["message"] == "Talent transport packet file was not found: druid/balance/missing-packet.json"


def test_warcraft_talent_packet_rejects_unscoped_relative_packet_like_input() -> None:
    result = runner.invoke(warcraft_app, ["talent-packet", "tmp/foo"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"
    assert payload["error"]["message"] == "Talent transport packet file was not found: tmp/foo"


def test_warcraft_talent_packet_rejects_path_containing_talent_calc_segment() -> None:
    result = runner.invoke(warcraft_app, ["talent-packet", "tmp/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must be a Wowhead talent-calc path or class/spec ref."


def test_warcraft_talent_packet_fails_when_provider_omits_packet(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "wowhead"
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {"provider": provider, "kind": "talent_calc_packet"},
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "missing_transport_packet"
    assert payload["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}


def test_warcraft_talent_packet_preserves_wowhead_invalid_transport_packet_error(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "wowhead"
        return {
            "provider": provider,
            "exit_code": 1,
            "payload": {
                "ok": False,
                "error": {
                    "code": "invalid_transport_packet",
                    "message": "wowhead talent-calc-packet produced an invalid talent transport packet: invalid test packet",
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"
    assert payload["error"]["message"] == "wowhead talent-calc-packet produced an invalid talent transport packet: invalid test packet"
    assert payload["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}
    assert payload["provider_result"]["provider"] == "wowhead"


def test_warcraft_talent_packet_preserves_warcraftlogs_invalid_transport_packet_error(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "warcraftlogs"
        return {
            "provider": provider,
            "exit_code": 1,
            "payload": {
                "ok": False,
                "error": {
                    "code": "invalid_transport_packet",
                    "message": "warcraftlogs report-player-talents produced an invalid talent transport packet: invalid test packet",
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "abcd1234", "--fight-id", "1", "--actor-id", "9"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"
    assert payload["error"]["message"] == "warcraftlogs report-player-talents produced an invalid talent transport packet: invalid test packet"
    assert payload["route"] == {
        "kind": "warcraftlogs_report_actor",
        "provider": "warcraftlogs",
        "actor_id": 9,
        "fight_id": 1,
        "allow_unlisted": False,
    }
    assert payload["provider_result"]["provider"] == "warcraftlogs"


def test_warcraft_talent_packet_preserves_provider_error_codes(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "warcraftlogs"
        return {
            "provider": provider,
            "exit_code": 1,
            "payload": {
                "ok": False,
                "error": {
                    "code": "missing_public_auth",
                    "message": "Public Warcraft Logs API access requires client credentials.",
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "abcd1234", "--fight-id", "1", "--actor-id", "9"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "missing_public_auth"
    assert payload["error"]["message"] == "Public Warcraft Logs API access requires client credentials."


def test_warcraft_talent_packet_preserves_warcraftlogs_not_found(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "warcraftlogs"
        return {
            "provider": provider,
            "exit_code": 1,
            "payload": {
                "ok": False,
                "error": {
                    "code": "not_found",
                    "message": "Actor ID 999 was not present in the selected fight.",
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "abcd1234", "--fight-id", "1", "--actor-id", "999"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["message"] == "Actor ID 999 was not present in the selected fight."
    assert payload["route"] == {
        "kind": "warcraftlogs_report_actor",
        "provider": "warcraftlogs",
        "actor_id": 999,
        "fight_id": 1,
        "allow_unlisted": False,
    }


def test_warcraft_talent_packet_preserves_ok_false_provider_errors(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "wowhead"
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "ok": False,
                "error": {
                    "code": "invalid_query",
                    "message": "Buildless Wowhead ref cannot produce an exact packet.",
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert payload["error"]["message"] == "Buildless Wowhead ref cannot produce an exact packet."
    assert payload["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}


def test_warcraft_talent_packet_rejects_malformed_packet_status(tmp_path: Path) -> None:
    packet_path = tmp_path / "mismatched-status.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "build_identity": {},
                "transport_forms": {},
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "rank": 1}]},
                "validation": {},
                "scope": {},
            }
        )
    )

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"
    assert "does not match packet contents" in payload["error"]["message"]


def test_warcraft_talent_packet_rejects_malformed_transport_forms(tmp_path: Path) -> None:
    packet_path = tmp_path / "bad-forms.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "validated",
                "build_identity": {},
                "transport_forms": {"simc_split_talents": []},
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                "validation": {"status": "validated"},
                "scope": {},
            }
        )
    )

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"
    assert "simc_split_talents" in payload["error"]["message"]


def test_warcraft_talent_packet_rejects_invalid_provider_packet(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "wowhead"
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "validated",
                    "build_identity": {},
                    "transport_forms": {"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                    "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                    "validation": {},
                    "scope": {},
                }
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"
    assert payload["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}
    assert payload["provider_result"]["provider"] == "wowhead"


def test_warcraft_talent_packet_rejects_invalid_upgraded_packet(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "raw-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {},
                "transport_forms": {},
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                "validation": {"status": "not_validated"},
                "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
            }
        )
    )

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "simc"
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "updated_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "validated",
                    "build_identity": {},
                    "transport_forms": {"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                    "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                    "validation": {},
                    "scope": {},
                }
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "packet_upgrade_failed"
    assert "invalid upgraded packet" in payload["error"]["message"]


def test_warcraft_talent_packet_reports_upgrade_failure(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "raw-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {},
                "transport_forms": {},
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                "validation": {"status": "not_validated"},
                "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
            }
        )
    )

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "simc"
        return {
            "provider": provider,
            "exit_code": 1,
            "payload": {
                "ok": False,
                "error": {
                    "code": "invalid_build_packet",
                    "message": "Build packet did not contain a validated transport form.",
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_build_packet"
    assert payload["error"]["message"] == "Build packet did not contain a validated transport form."
    assert payload["route"] == {"kind": "packet_file", "provider": None, "packet_path": str(packet_path.resolve())}
    assert payload["provider_result"]["provider"] == "simc"


def test_warcraft_talent_packet_preserves_upgrade_failure_with_malformed_updated_packet(
    monkeypatch,
    tmp_path: Path,
) -> None:
    packet_path = tmp_path / "raw-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {},
                "transport_forms": {},
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                "validation": {"status": "not_validated"},
                "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
            }
        )
    )

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "simc"
        return {
            "provider": provider,
            "exit_code": 1,
            "payload": {
                "ok": False,
                "error": {
                    "code": "invalid_build_packet",
                    "message": "Build packet did not contain a validated transport form.",
                },
                "updated_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "validated",
                    "build_identity": {},
                    "transport_forms": {"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                    "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                    "validation": {},
                    "scope": {},
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_build_packet"
    assert payload["error"]["message"] == "Build packet did not contain a validated transport form."
    assert payload["provider_result"]["provider"] == "simc"


def test_warcraft_talent_packet_rejects_successful_validate_without_updated_packet(
    monkeypatch,
    tmp_path: Path,
) -> None:
    packet_path = tmp_path / "raw-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {},
                "transport_forms": {},
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                "validation": {"status": "not_validated"},
                "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
            }
        )
    )

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "simc"
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "provider": provider,
                "kind": "validate_talent_transport",
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "packet_upgrade_failed"
    assert payload["error"]["message"] == "simc validate-talent-transport did not return an upgraded talent transport packet."
    assert payload["provider_result"]["provider"] == "simc"


def test_warcraft_talent_describe_preserves_wowhead_invalid_transport_packet_error(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "wowhead"
        return {
            "provider": provider,
            "exit_code": 1,
            "payload": {
                "ok": False,
                "error": {
                    "code": "invalid_transport_packet",
                    "message": "wowhead talent-calc-packet produced an invalid talent transport packet: invalid test packet",
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-describe", "druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"
    assert payload["error"]["message"] == "wowhead talent-calc-packet produced an invalid talent transport packet: invalid test packet"
    assert payload["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}
    assert payload["provider_result"]["provider"] == "wowhead"


def test_warcraft_talent_describe_preserves_warcraftlogs_invalid_transport_packet_error(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "warcraftlogs"
        return {
            "provider": provider,
            "exit_code": 1,
            "payload": {
                "ok": False,
                "error": {
                    "code": "invalid_transport_packet",
                    "message": "warcraftlogs report-player-talents produced an invalid talent transport packet: invalid test packet",
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-describe", "abcd1234", "--fight-id", "1", "--actor-id", "9"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"
    assert payload["error"]["message"] == "warcraftlogs report-player-talents produced an invalid talent transport packet: invalid test packet"
    assert payload["route"] == {
        "kind": "warcraftlogs_report_actor",
        "provider": "warcraftlogs",
        "actor_id": 9,
        "fight_id": 1,
        "allow_unlisted": False,
    }
    assert payload["provider_result"]["provider"] == "warcraftlogs"


def test_warcraft_talent_describe_preserves_warcraftlogs_not_found(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "warcraftlogs"
        return {
            "provider": provider,
            "exit_code": 1,
            "payload": {
                "ok": False,
                "error": {
                    "code": "not_found",
                    "message": "Actor ID 999 was not present in the selected fight.",
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-describe", "abcd1234", "--fight-id", "1", "--actor-id", "999"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["message"] == "Actor ID 999 was not present in the selected fight."
    assert payload["route"] == {
        "kind": "warcraftlogs_report_actor",
        "provider": "warcraftlogs",
        "actor_id": 999,
        "fight_id": 1,
        "allow_unlisted": False,
    }


def test_warcraft_talent_describe_reports_simc_failure(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "exact-packet.json"
    apl_path = tmp_path / "balance.simc"
    apl_path.write_text("actions=wrath\n")
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "transport_forms": {
                    "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                },
                "build_identity": {
                    "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                },
                "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "validation": {},
                "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
            }
        )
    )

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "simc"
        return {
            "provider": provider,
            "exit_code": 1,
            "payload": {
                "ok": False,
                "error": {
                    "code": "apl_not_found",
                    "message": "APL path did not exist.",
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", str(packet_path), "--no-validate", "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "apl_not_found"
    assert payload["error"]["message"] == "APL path did not exist."
    assert payload["kind"] == "talent_describe"
    assert payload["route"] == {"kind": "packet_file", "provider": None, "packet_path": str(packet_path.resolve())}
    assert payload["provider_result"]["provider"] == "simc"


def test_warcraft_talent_describe_preserves_ok_false_simc_failure(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "exact-packet.json"
    apl_path = tmp_path / "balance.simc"
    apl_path.write_text("actions=wrath\n")
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "transport_forms": {
                    "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                },
                "build_identity": {
                    "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                },
                "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "validation": {},
                "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
            }
        )
    )

    monkeypatch.setattr(
        "warcraft_cli.main.provider_invoke",
        lambda provider, args, *, expansion=None: {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "ok": False,
                "error": {
                    "code": "describe_build_failed",
                    "message": "Unable to resolve build against the supplied APL.",
                },
            },
            "stdout": "",
        },
    )

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", str(packet_path), "--no-validate", "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "describe_build_failed"
    assert payload["error"]["message"] == "Unable to resolve build against the supplied APL."
    assert payload["kind"] == "talent_describe"
    assert payload["provider_result"]["provider"] == "simc"


def test_warcraft_talent_describe_does_not_write_packet_out_on_failure(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "exact-packet.json"
    apl_path = tmp_path / "balance.simc"
    out_path = tmp_path / "described-packet.json"
    apl_path.write_text("actions=wrath\n")
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "transport_forms": {
                    "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                },
                "build_identity": {
                    "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                },
                "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "validation": {},
                "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
            }
        )
    )

    monkeypatch.setattr(
        "warcraft_cli.main.provider_invoke",
        lambda provider, args, *, expansion=None: {
            "provider": provider,
            "exit_code": 1,
            "payload": {
                "ok": False,
                "error": {
                    "code": "apl_not_found",
                    "message": "APL path did not exist.",
                },
            },
            "stdout": "",
        },
    )

    result = runner.invoke(
        warcraft_app,
        [
            "talent-describe",
            str(packet_path),
            "--no-validate",
            "--apl-path",
            str(apl_path),
            "--packet-out",
            str(out_path),
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "apl_not_found"
    assert not out_path.exists()


def test_warcraft_talent_describe_uses_stable_error_kind_for_route_failures() -> None:
    result = runner.invoke(warcraft_app, ["talent-describe", "abcd1234"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["kind"] == "talent_describe"
    assert payload["error"]["code"] == "unsupported_talent_source"


def test_warcraft_talent_describe_rejects_non_wowhead_absolute_url() -> None:
    result = runner.invoke(
        warcraft_app,
        ["talent-describe", "https://notwowhead.com/talent-calc/druid/balance/ABC123"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["kind"] == "talent_describe"
    assert payload["error"]["code"] == "unsupported_talent_source"


def test_warcraft_talent_describe_rejects_wowhead_ref_without_build_code(tmp_path: Path) -> None:
    apl_path = tmp_path / "balance.simc"
    apl_path.write_text("actions=wrath\n")

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", "druid/balance", "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["kind"] == "talent_describe"
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc packet refs must include an explicit build code."


def test_warcraft_talent_describe_rejects_buried_real_wowhead_talent_calc_path(tmp_path: Path) -> None:
    apl_path = tmp_path / "balance.simc"
    apl_path.write_text("actions=wrath\n")

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", "https://www.wowhead.com/items/talent-calc/druid/balance/ABC123", "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["kind"] == "talent_describe"
    assert payload["error"]["code"] == "invalid_tool_ref"


def test_warcraft_talent_describe_rejects_nested_talent_calc_non_url_ref(tmp_path: Path) -> None:
    apl_path = tmp_path / "balance.simc"
    apl_path.write_text("actions=wrath\n")

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", "talent-calc/foo/talent-calc/druid/balance/ABC123", "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["kind"] == "talent_describe"
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must be a Wowhead talent-calc path or class/spec ref."


def test_warcraft_talent_describe_rejects_empty_segment_wowhead_ref(tmp_path: Path) -> None:
    apl_path = tmp_path / "balance.simc"
    apl_path.write_text("actions=wrath\n")

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", "druid//balance/ABC123", "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["kind"] == "talent_describe"
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must not include empty path segments."


def test_warcraft_talent_describe_rejects_wowhead_ref_with_trailing_extra_segment(tmp_path: Path) -> None:
    apl_path = tmp_path / "balance.simc"
    apl_path.write_text("actions=wrath\n")

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", "https://www.wowhead.com/talent-calc/druid/balance/ABC123/extra", "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["kind"] == "talent_describe"
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "Talent calculator URL must use /talent-calc/<class>/<spec>[/<build-code>]."


def test_warcraft_talent_describe_rejects_fake_host_wowhead_like_input(tmp_path: Path) -> None:
    apl_path = tmp_path / "balance.simc"
    apl_path.write_text("actions=wrath\n")

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", "notwowhead.com/talent-calc/druid/balance/ABC123", "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["kind"] == "talent_describe"
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must be a Wowhead talent-calc path or class/spec ref."


def test_warcraft_talent_describe_accepts_hyphenated_wowhead_class_slug(monkeypatch, tmp_path: Path) -> None:
    apl_path = tmp_path / "deathknight_frost.simc"
    apl_path.write_text("actions=obliterate\n")

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        if provider == "wowhead":
            assert args == ["talent-calc-packet", "death-knight/frost/ABC123", "--listed-build-limit", "10"]
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "talent_transport_packet": {
                        "kind": "talent_transport_packet",
                        "transport_status": "exact",
                        "build_identity": {
                            "class_spec_identity": {"identity": {"actor_class": "deathknight", "spec": "frost"}}
                        },
                        "transport_forms": {
                            "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/death-knight/frost/ABC123"
                        },
                        "raw_evidence": {
                            "reference_url": "https://www.wowhead.com/talent-calc/death-knight/frost/ABC123"
                        },
                        "validation": {},
                        "scope": {},
                    }
                },
                "stdout": "",
            }
        assert provider == "simc"
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "provider": "simc",
                "kind": "describe_build",
                "build_spec": {
                    "transport_packet": {
                        "transport_form": "wowhead_talent_calc_url",
                        "transport_status": "exact",
                    }
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", "death-knight/frost/ABC123", "--apl-path", str(apl_path), "--no-validate"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}
    assert payload["describe_result"]["payload"]["kind"] == "describe_build"


def test_warcraft_talent_describe_rejects_path_containing_talent_calc_segment(tmp_path: Path) -> None:
    apl_path = tmp_path / "balance.simc"
    apl_path.write_text("actions=wrath\n")

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", "tmp/talent-calc/druid/balance/ABC123", "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["kind"] == "talent_describe"
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must be a Wowhead talent-calc path or class/spec ref."


def test_warcraft_talent_packet_normalizes_packet_write_failure(monkeypatch, tmp_path: Path) -> None:
    out_dir = tmp_path / "out-dir"
    out_dir.mkdir()
    monkeypatch.setattr(
        "warcraft_cli.main.provider_invoke",
        lambda provider, args, *, expansion=None: {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "provider": "wowhead",
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "exact",
                    "transport_forms": {
                        "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                    },
                    "build_identity": {
                        "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                    },
                    "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                    "validation": {},
                    "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
                },
            },
            "stdout": "",
        },
    )

    result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/ABC123", "--out", str(out_dir)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "transport_packet_write_failed"


def test_warcraft_talent_describe_normalizes_packet_write_failure(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "exact-packet.json"
    apl_path = tmp_path / "balance.simc"
    out_dir = tmp_path / "out-dir"
    apl_path.write_text("actions=wrath\n")
    out_dir.mkdir()
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "transport_forms": {
                    "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                },
                "build_identity": {
                    "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                },
                "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "validation": {},
                "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
            }
        )
    )
    monkeypatch.setattr(
        "warcraft_cli.main.provider_invoke",
        lambda provider, args, *, expansion=None: {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "ok": True,
                "build_spec": {
                    "transport_packet": {
                        "path": "/tmp/deleted-packet.json",
                        "transport_form": "wowhead_talent_calc_url",
                        "transport_status": "exact",
                    }
                },
            },
            "stdout": "",
        },
    )

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", str(packet_path), "--no-validate", "--apl-path", str(apl_path), "--packet-out", str(out_dir)],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "transport_packet_write_failed"


def test_warcraft_talent_describe_routes_wowhead_ref_to_simc(monkeypatch) -> None:
    provider_calls: list[tuple[str, list[str]]] = []
    simc_calls: list[dict[str, object]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        provider_calls.append((provider, args))
        if provider == "wowhead":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "provider": provider,
                    "kind": "talent_calc_packet",
                    "talent_transport_packet": {
                        "kind": "talent_transport_packet",
                        "transport_status": "exact",
                        "transport_forms": {
                            "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                        },
                        "build_identity": {
                            "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                        },
                        "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                        "validation": {},
                        "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
                    },
                },
                "stdout": "",
            }
        assert provider == "simc"
        simc_calls.append(_simc_build_input_summary(args))
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "provider": provider,
                "kind": "describe_build",
                "summary": {"active_action_count": 5},
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", "druid/balance/ABC123", "--apl-path", "/tmp/druid_balance.simc"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "warcraft"
    assert payload["kind"] == "talent_describe"
    assert payload["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}
    assert payload["source_packet_status"] == "exact"
    assert payload["upgrade_attempted"] is False
    assert payload["packet_written_path"] is None
    assert payload["describe_result"]["payload"]["kind"] == "describe_build"
    assert provider_calls[0] == ("wowhead", ["talent-calc-packet", "druid/balance/ABC123", "--listed-build-limit", "10"])
    assert simc_calls[0]["command"] == "describe-build"
    assert simc_calls[0]["packet_transport_status"] == "exact"
    assert simc_calls[0]["packet_transport_url"] == "https://www.wowhead.com/talent-calc/druid/balance/ABC123"
    assert simc_calls[0]["args"][:4] == ["describe-build", "--targets", "1", "--aoe-targets"]
    assert "--apl-path" in simc_calls[0]["args"]


def test_warcraft_talent_describe_hides_deleted_temp_packet_source_notes(monkeypatch, tmp_path: Path) -> None:
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        if provider == "wowhead":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "provider": provider,
                    "kind": "talent_calc_packet",
                    "talent_transport_packet": {
                        "kind": "talent_transport_packet",
                        "transport_status": "exact",
                        "transport_forms": {
                            "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                        },
                        "build_identity": {
                            "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                        },
                        "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                        "validation": {},
                        "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
                    },
                },
                "stdout": "",
            }
        assert provider == "simc"
        packet_path = args[args.index("--build-packet") + 1]
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "provider": provider,
                "kind": "describe_build",
                "build_spec": {
                    "source_notes": [f"build packet: {packet_path}", "talent transport packet"],
                    "transport_packet": {
                        "path": packet_path,
                        "transport_form": "wowhead_talent_calc_url",
                        "transport_status": "exact",
                    },
                },
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", "druid/balance/ABC123", "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 0
    build_spec = json.loads(result.stdout)["describe_result"]["payload"]["build_spec"]
    assert "path" not in build_spec["transport_packet"]
    assert build_spec["source_notes"] == ["talent transport packet"]


def test_warcraft_talent_describe_passes_wowhead_listed_build_limit(monkeypatch) -> None:
    provider_calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        provider_calls.append((provider, args, expansion))
        if provider == "wowhead":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "provider": provider,
                    "kind": "talent_calc_packet",
                    "talent_transport_packet": {
                        "kind": "talent_transport_packet",
                        "transport_status": "exact",
                        "transport_forms": {
                            "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                        },
                        "build_identity": {
                            "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                        },
                        "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                        "validation": {},
                        "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
                    },
                },
                "stdout": "",
            }
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "provider": provider,
                "kind": "describe_build",
                "summary": {"active_action_count": 3},
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", "druid/balance/ABC123", "--listed-build-limit", "4"],
    )
    assert result.exit_code == 0
    assert provider_calls[0] == ("wowhead", ["talent-calc-packet", "druid/balance/ABC123", "--listed-build-limit", "4"], None)


def test_warcraft_talent_describe_passes_expansion_to_wowhead_route(monkeypatch) -> None:
    provider_calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        provider_calls.append((provider, args, expansion))
        if provider == "wowhead":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "provider": provider,
                    "kind": "talent_calc_packet",
                    "talent_transport_packet": {
                        "kind": "talent_transport_packet",
                        "transport_status": "exact",
                        "transport_forms": {
                            "wowhead_talent_calc_url": "https://www.wowhead.com/cata/talent-calc/hunter/beast-mastery/XYZ987",
                        },
                        "build_identity": {
                            "class_spec_identity": {"identity": {"actor_class": "hunter", "spec": "beast_mastery"}},
                        },
                        "raw_evidence": {"reference_url": "https://www.wowhead.com/cata/talent-calc/hunter/beast-mastery/XYZ987"},
                        "validation": {},
                        "scope": {"type": "wowhead_talent_calc", "expansion": "cata"},
                    },
                },
                "stdout": "",
            }
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "provider": provider,
                "kind": "describe_build",
                "summary": {"active_action_count": 3},
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["--expansion", "wotlk", "talent-describe", "cata/talent-calc/hunter/beast-mastery/XYZ987"],
    )
    assert result.exit_code == 0
    assert provider_calls[0] == (
        "wowhead",
        ["talent-calc-packet", "cata/talent-calc/hunter/beast-mastery/XYZ987", "--listed-build-limit", "10"],
        "wotlk",
    )


def test_warcraft_talent_describe_routes_expansion_prefixed_class_spec_wowhead_ref(monkeypatch) -> None:
    provider_calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        provider_calls.append((provider, args, expansion))
        if provider == "wowhead":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "provider": provider,
                    "kind": "talent_calc_packet",
                    "talent_transport_packet": {
                        "kind": "talent_transport_packet",
                        "transport_status": "exact",
                        "transport_forms": {
                            "wowhead_talent_calc_url": "https://www.wowhead.com/classic/talent-calc/druid/balance/ABC123",
                        },
                        "build_identity": {
                            "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                        },
                        "raw_evidence": {"reference_url": "https://www.wowhead.com/classic/talent-calc/druid/balance/ABC123"},
                        "validation": {},
                        "scope": {"type": "wowhead_talent_calc", "expansion": "classic"},
                    },
                },
                "stdout": "",
            }
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "provider": provider,
                "kind": "describe_build",
                "summary": {"active_action_count": 3},
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", "classic/druid/balance/ABC123"],
    )
    assert result.exit_code == 0
    assert provider_calls[0] == (
        "wowhead",
        ["talent-calc-packet", "classic/druid/balance/ABC123", "--listed-build-limit", "10"],
        None,
    )


def test_warcraft_talent_describe_routes_warcraftlogs_and_upgrades(monkeypatch) -> None:
    provider_calls: list[tuple[str, list[str]]] = []
    simc_calls: list[dict[str, object]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        provider_calls.append((provider, args))
        if provider == "warcraftlogs":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "provider": provider,
                    "kind": "report_player_talents",
                    "talent_transport_packet": {
                        "kind": "talent_transport_packet",
                        "transport_status": "raw_only",
                        "build_identity": {
                            "class_spec_identity": {
                                "identity": {"actor_class": "druid", "spec": "balance"},
                            }
                        },
                        "transport_forms": {},
                        "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                        "validation": {"status": "not_validated"},
                        "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
                    },
                },
                "stdout": "",
            }
        assert provider == "simc"
        simc_calls.append(_simc_build_input_summary(args))
        packet = json.loads(Path(args[args.index("--build-packet") + 1]).read_text())
        if args[0] == "validate-talent-transport":
            assert packet["transport_status"] == "raw_only"
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "provider": provider,
                    "kind": "validate_talent_transport",
                    "input": {"source": "build_packet", "build_packet": args[args.index("--build-packet") + 1]},
                    "updated_packet": {
                        **packet,
                        "transport_status": "validated",
                        "transport_forms": {"simc_split_talents": {"class_talents": "103324:1"}},
                        "validation": {
                            "status": "validated",
                            "source": "simc_trait_data_round_trip",
                            "actor_class": "druid",
                            "spec": "balance",
                        },
                    },
                },
                "stdout": "",
            }
        assert args[0] == "describe-build"
        assert packet["transport_status"] == "validated"
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "provider": provider,
                "kind": "describe_build",
                "summary": {"active_action_count": 7},
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", "abcd1234", "--fight-id", "1", "--actor-id", "9"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["route"] == {
        "kind": "warcraftlogs_report_actor",
        "provider": "warcraftlogs",
        "actor_id": 9,
        "fight_id": 1,
        "allow_unlisted": False,
    }
    assert payload["source_packet_status"] == "raw_only"
    assert payload["upgrade_attempted"] is True
    assert payload["upgraded"] is True
    assert payload["talent_transport_packet"]["transport_status"] == "validated"
    assert payload["describe_result"]["payload"]["kind"] == "describe_build"
    assert provider_calls[0] == ("warcraftlogs", ["report-player-talents", "abcd1234", "--actor-id", "9", "--fight-id", "1"])
    assert [row["command"] for row in simc_calls] == ["validate-talent-transport", "describe-build"]
    assert simc_calls[1]["packet_transport_status"] == "validated"
    assert simc_calls[1]["packet_transport_form_keys"] == ["simc_split_talents"]
    assert "build_packet" not in payload["upgrade_result"]["payload"]["input"]


def test_warcraft_talent_describe_passes_allow_unlisted_and_expansion(monkeypatch) -> None:
    provider_calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        provider_calls.append((provider, args, expansion))
        if provider == "warcraftlogs":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "provider": provider,
                    "kind": "report_player_talents",
                    "talent_transport_packet": {
                        "kind": "talent_transport_packet",
                        "transport_status": "exact",
                        "build_identity": {},
                        "transport_forms": {"wow_talent_export": "ABC123"},
                        "raw_evidence": {"reference_type": "wow_talent_export"},
                        "validation": {},
                        "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
                    },
                },
                "stdout": "",
            }
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "provider": provider,
                "kind": "describe_build",
                "summary": {"active_action_count": 6},
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["--expansion", "retail", "talent-describe", "abcd1234", "--actor-id", "9", "--fight-id", "1", "--allow-unlisted", "--no-validate"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["route"]["allow_unlisted"] is True
    assert provider_calls[0] == (
        "warcraftlogs",
        ["report-player-talents", "abcd1234", "--actor-id", "9", "--fight-id", "1", "--allow-unlisted"],
        "retail",
    )
    assert provider_calls[1][2] == "retail"


def test_warcraft_talent_describe_routes_scheme_less_warcraftlogs_report_ref(monkeypatch, tmp_path: Path) -> None:
    apl_path = tmp_path / "balance.simc"
    apl_path.write_text("actions=wrath\n")
    provider_calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        provider_calls.append((provider, args, expansion))
        if provider == "warcraftlogs":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "provider": provider,
                    "kind": "report_player_talents",
                    "talent_transport_packet": {
                        "kind": "talent_transport_packet",
                        "transport_status": "raw_only",
                        "build_identity": {},
                        "transport_forms": {},
                        "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                        "validation": {"status": "not_validated"},
                        "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
                    },
                },
                "stdout": "",
            }
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "provider": provider,
                "kind": "describe_build",
                "summary": {"active_action_count": 6},
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        [
            "talent-describe",
            "warcraftlogs.com/reports/abcd1234#fight=1",
            "--actor-id",
            "9",
            "--apl-path",
            str(apl_path),
            "--no-validate",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["route"] == {
        "kind": "warcraftlogs_report_actor",
        "provider": "warcraftlogs",
        "actor_id": 9,
        "fight_id": None,
        "allow_unlisted": False,
    }
    assert provider_calls[0] == (
        "warcraftlogs",
        ["report-player-talents", "https://warcraftlogs.com/reports/abcd1234#fight=1", "--actor-id", "9"],
        None,
    )
    assert provider_calls[1][0] == "simc"


def test_warcraft_talent_describe_uses_packet_file_and_can_write_output(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "exact-packet.json"
    out_path = tmp_path / "described-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "transport_forms": {
                    "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                },
                "build_identity": {
                    "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                },
                "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "validation": {},
                "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
            }
        )
    )
    simc_calls: list[dict[str, object]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "simc"
        simc_calls.append(_simc_build_input_summary(args))
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "provider": provider,
                "kind": "describe_build",
                "summary": {"active_action_count": 4},
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", str(packet_path), "--packet-out", str(out_path)],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["route"] == {"kind": "packet_file", "provider": None, "packet_path": str(packet_path.resolve())}
    assert payload["packet_written_path"] == str(out_path.resolve())
    assert payload["describe_result"]["payload"]["kind"] == "describe_build"
    assert simc_calls[0]["command"] == "describe-build"
    assert simc_calls[0]["packet_transport_status"] == "exact"
    written = json.loads(out_path.read_text())
    assert written["transport_status"] == "exact"


def test_warcraft_talent_describe_skips_auto_upgrade_for_unknown_packet_file(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "unknown-packet.json"
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")
    packet = {
        "kind": "talent_transport_packet",
        "transport_status": "unknown",
        "build_identity": {
            "class_spec_identity": {
                "identity": {"actor_class": "druid", "spec": "balance"},
            }
        },
        "transport_forms": {},
        "raw_evidence": {
            "talent_tree_entries": [{"entry": None, "node_id": None, "rank": None}],
        },
        "validation": {"status": "not_validated"},
        "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
    }
    packet_path.write_text(json.dumps(packet))
    simc_calls: list[dict[str, object]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "simc"
        simc_calls.append(_simc_build_input_summary(args))
        assert args[0] == "describe-build"
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "provider": provider,
                "kind": "describe_build",
                "summary": {"active_action_count": 4},
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", str(packet_path), "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["source_packet_status"] == "unknown"
    assert payload["upgrade_attempted"] is False
    assert payload["upgraded"] is False
    assert payload["talent_transport_packet"] == packet
    assert [row["command"] for row in simc_calls] == ["describe-build"]


def test_warcraft_talent_packet_preserves_wowhead_provider_packet(monkeypatch) -> None:
    _disable_wowhead_page_fetch(monkeypatch)

    direct_result = runner.invoke(wowhead_app, ["talent-calc-packet", "druid/balance/ABC123"])
    assert direct_result.exit_code == 0
    direct_payload = json.loads(direct_result.stdout)

    wrapper_result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/ABC123"])
    assert wrapper_result.exit_code == 0
    wrapper_payload = json.loads(wrapper_result.stdout)

    assert wrapper_payload["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}
    assert wrapper_payload["talent_transport_packet"] == direct_payload["talent_transport_packet"]
    assert wrapper_payload["talent_transport_packet"]["transport_status"] == "exact"


def test_warcraft_talent_packet_out_matches_wowhead_provider_file(monkeypatch, tmp_path: Path) -> None:
    direct_path = tmp_path / "wowhead-direct.json"
    wrapper_path = tmp_path / "wowhead-wrapper.json"

    _disable_wowhead_page_fetch(monkeypatch)

    direct_result = runner.invoke(wowhead_app, ["talent-calc-packet", "druid/balance/ABC123", "--out", str(direct_path)])
    assert direct_result.exit_code == 0
    wrapper_result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/ABC123", "--out", str(wrapper_path)])
    assert wrapper_result.exit_code == 0

    assert direct_path.read_text() == wrapper_path.read_text()


def test_warcraft_talent_packet_preserves_warcraftlogs_provider_packet(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _EndToEndWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": "117176:1",
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": "paladin",
                "spec": "retribution",
            },
        },
    )

    direct_result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "9"],
    )
    assert direct_result.exit_code == 0
    direct_payload = json.loads(direct_result.stdout)

    wrapper_result = runner.invoke(
        warcraft_app,
        ["talent-packet", "abcd1234", "--fight-id", "1", "--actor-id", "9"],
    )
    assert wrapper_result.exit_code == 0
    wrapper_payload = json.loads(wrapper_result.stdout)

    assert wrapper_payload["route"] == {
        "kind": "warcraftlogs_report_actor",
        "provider": "warcraftlogs",
        "actor_id": 9,
        "fight_id": 1,
        "allow_unlisted": False,
    }
    assert wrapper_payload["upgrade_attempted"] is False
    assert wrapper_payload["talent_transport_packet"] == direct_payload["talent_transport_packet"]
    assert wrapper_payload["talent_transport_packet"]["transport_status"] == "validated"


def test_warcraft_talent_packet_out_matches_warcraftlogs_provider_file(monkeypatch, tmp_path: Path) -> None:
    direct_path = tmp_path / "warcraftlogs-direct.json"
    wrapper_path = tmp_path / "warcraftlogs-wrapper.json"

    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _EndToEndWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": "117176:1",
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": "paladin",
                "spec": "retribution",
            },
        },
    )

    direct_result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "9", "--out", str(direct_path)],
    )
    assert direct_result.exit_code == 0
    wrapper_result = runner.invoke(
        warcraft_app,
        ["talent-packet", "abcd1234", "--fight-id", "1", "--actor-id", "9", "--out", str(wrapper_path)],
    )
    assert wrapper_result.exit_code == 0

    assert direct_path.read_text() == wrapper_path.read_text()


def test_warcraft_talent_describe_packet_out_matches_wowhead_provider_file(monkeypatch, tmp_path: Path) -> None:
    direct_path = tmp_path / "wowhead-direct.json"
    wrapper_path = tmp_path / "wowhead-described.json"
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")

    _disable_wowhead_page_fetch(monkeypatch)
    _patch_simc_describe_pipeline(
        monkeypatch,
        transport_form="wowhead_talent_calc_url",
        transport_status="exact",
    )

    direct_result = runner.invoke(wowhead_app, ["talent-calc-packet", "druid/balance/ABC123", "--out", str(direct_path)])
    assert direct_result.exit_code == 0
    wrapper_result = runner.invoke(
        warcraft_app,
        ["talent-describe", "druid/balance/ABC123", "--apl-path", str(apl_path), "--packet-out", str(wrapper_path)],
    )
    assert wrapper_result.exit_code == 0

    assert direct_path.read_text() == wrapper_path.read_text()


def test_warcraft_talent_describe_packet_out_changes_after_validation_upgrade(monkeypatch, tmp_path: Path) -> None:
    direct_path = tmp_path / "warcraftlogs-direct.json"
    wrapper_path = tmp_path / "warcraftlogs-described.json"
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")

    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _EndToEndWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {},
            "validation": {
                "status": "not_validated",
                "reason": "simc_trait_resolution_incomplete",
            },
        },
    )
    monkeypatch.setattr(
        "simc_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": "117176:1",
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": "paladin",
                "spec": "retribution",
            },
        },
    )
    _patch_simc_describe_pipeline(
        monkeypatch,
        transport_form="simc_split_talents",
        transport_status="validated",
    )

    direct_result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "9", "--out", str(direct_path)],
    )
    assert direct_result.exit_code == 0
    wrapper_result = runner.invoke(
        warcraft_app,
        ["talent-describe", "abcd1234", "--fight-id", "1", "--actor-id", "9",
            "--apl-path", str(apl_path), "--packet-out", str(wrapper_path)],
    )
    assert wrapper_result.exit_code == 0
    wrapper_payload = json.loads(wrapper_result.stdout)

    direct_packet = json.loads(direct_path.read_text())
    wrapper_packet = json.loads(wrapper_path.read_text())
    assert direct_path.read_text() != wrapper_path.read_text()
    assert direct_packet["transport_status"] == "raw_only"
    assert wrapper_packet["transport_status"] == "validated"
    assert wrapper_packet["transport_forms"]["simc_split_talents"]["spec_talents"] == "109839:1"
    assert "build_packet" not in wrapper_payload["upgrade_result"]["payload"]["input"]
    assert wrapper_payload["describe_result"]["payload"]["build_spec"]["transport_packet"]["path"] == str(wrapper_path.resolve())


def test_warcraft_talent_packet_file_reuse_stays_exact_without_validation(monkeypatch, tmp_path: Path) -> None:
    source_path = tmp_path / "wowhead-source.json"
    routed_path = tmp_path / "wowhead-routed.json"
    described_path = tmp_path / "wowhead-described.json"
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")

    _disable_wowhead_page_fetch(monkeypatch)
    _patch_simc_describe_pipeline(
        monkeypatch,
        transport_form="wowhead_talent_calc_url",
        transport_status="exact",
    )

    producer_result = runner.invoke(
        wowhead_app,
        ["talent-calc-packet", "druid/balance/ABC123", "--out", str(source_path)],
    )
    assert producer_result.exit_code == 0

    packet_result = runner.invoke(
        warcraft_app,
        ["talent-packet", str(source_path), "--no-validate", "--out", str(routed_path)],
    )
    assert packet_result.exit_code == 0
    describe_result = runner.invoke(
        warcraft_app,
        [
            "talent-describe",
            str(source_path),
            "--no-validate",
            "--apl-path",
            str(apl_path),
            "--packet-out",
            str(described_path),
        ],
    )
    assert describe_result.exit_code == 0

    packet_payload = json.loads(packet_result.stdout)
    describe_payload = json.loads(describe_result.stdout)
    assert packet_payload["upgrade_attempted"] is False
    assert describe_payload["upgrade_attempted"] is False
    assert source_path.read_text() == routed_path.read_text()
    assert source_path.read_text() == described_path.read_text()


def test_warcraft_talent_packet_file_reuse_upgrades_raw_packet_consistently(monkeypatch, tmp_path: Path) -> None:
    raw_path = tmp_path / "warcraftlogs-raw.json"
    routed_path = tmp_path / "warcraftlogs-routed.json"
    described_path = tmp_path / "warcraftlogs-described.json"
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")

    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _EndToEndWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {},
            "validation": {
                "status": "not_validated",
                "reason": "simc_trait_resolution_incomplete",
            },
        },
    )
    raw_result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "9", "--out", str(raw_path)],
    )
    assert raw_result.exit_code == 0

    monkeypatch.setattr(
        "simc_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": "117176:1",
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": "paladin",
                "spec": "retribution",
            },
        },
    )
    _patch_simc_describe_pipeline(
        monkeypatch,
        transport_form="simc_split_talents",
        transport_status="validated",
    )

    packet_result = runner.invoke(
        warcraft_app,
        ["talent-packet", str(raw_path), "--out", str(routed_path)],
    )
    assert packet_result.exit_code == 0
    describe_result = runner.invoke(
        warcraft_app,
        [
            "talent-describe",
            str(raw_path),
            "--apl-path",
            str(apl_path),
            "--packet-out",
            str(described_path),
        ],
    )
    assert describe_result.exit_code == 0

    packet_payload = json.loads(packet_result.stdout)
    describe_payload = json.loads(describe_result.stdout)
    raw_packet = json.loads(raw_path.read_text())
    routed_packet = json.loads(routed_path.read_text())
    described_packet = json.loads(described_path.read_text())

    assert packet_payload["upgrade_attempted"] is True
    assert describe_payload["upgrade_attempted"] is True
    assert raw_packet["transport_status"] == "raw_only"
    assert routed_packet == described_packet
    assert routed_packet["transport_status"] == "validated"
    assert raw_path.read_text() != routed_path.read_text()


def test_warcraft_talent_round_trip_wowhead_packet_to_describe(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "wowhead-packet.json"
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")
    _disable_wowhead_page_fetch(monkeypatch)
    _patch_simc_describe_pipeline(
        monkeypatch,
        transport_form="wowhead_talent_calc_url",
        transport_status="exact",
    )

    packet_result = runner.invoke(
        warcraft_app,
        ["talent-packet", "druid/balance/ABC123", "--out", str(packet_path)],
    )
    assert packet_result.exit_code == 0
    packet_payload = json.loads(packet_result.stdout)
    assert packet_payload["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}
    written_packet = json.loads(packet_path.read_text())
    assert written_packet["transport_status"] == "exact"
    assert written_packet["transport_forms"]["wowhead_talent_calc_url"] == "https://www.wowhead.com/talent-calc/druid/balance/ABC123"

    describe_result = runner.invoke(
        warcraft_app,
        ["talent-describe", str(packet_path), "--apl-path", str(apl_path)],
    )
    assert describe_result.exit_code == 0
    payload = json.loads(describe_result.stdout)
    assert payload["route"] == {"kind": "packet_file", "provider": None, "packet_path": str(packet_path.resolve())}
    transport_packet = payload["describe_result"]["payload"]["build_spec"]["transport_packet"]
    assert transport_packet["transport_form"] == "wowhead_talent_calc_url"
    assert transport_packet["transport_status"] == "exact"
    assert transport_packet["path"] == str(packet_path.resolve())


def test_warcraft_talent_round_trip_warcraftlogs_packet_to_describe(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "warcraftlogs-packet.json"
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _EndToEndWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {},
            "validation": {
                "status": "not_validated",
                "reason": "simc_trait_resolution_incomplete",
            },
        },
    )
    monkeypatch.setattr(
        "simc_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": "117176:1",
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": "paladin",
                "spec": "retribution",
            },
        },
    )
    _patch_simc_describe_pipeline(
        monkeypatch,
        transport_form="simc_split_talents",
        transport_status="validated",
    )

    packet_result = runner.invoke(
        warcraft_app,
        ["talent-packet", "abcd1234", "--fight-id", "1", "--actor-id", "9", "--out", str(packet_path)],
    )
    assert packet_result.exit_code == 0
    packet_payload = json.loads(packet_result.stdout)
    assert packet_payload["route"] == {
        "kind": "warcraftlogs_report_actor",
        "provider": "warcraftlogs",
        "actor_id": 9,
        "fight_id": 1,
        "allow_unlisted": False,
    }
    assert packet_payload["source_packet_status"] == "raw_only"
    assert packet_payload["talent_transport_packet"]["transport_status"] == "validated"
    written_packet = json.loads(packet_path.read_text())
    assert written_packet["transport_forms"]["simc_split_talents"]["spec_talents"] == "109839:1"

    describe_result = runner.invoke(
        warcraft_app,
        ["talent-describe", str(packet_path), "--apl-path", str(apl_path)],
    )
    assert describe_result.exit_code == 0
    payload = json.loads(describe_result.stdout)
    assert payload["route"] == {"kind": "packet_file", "provider": None, "packet_path": str(packet_path.resolve())}
    assert payload["talent_transport_packet"]["transport_status"] == "validated"
    transport_packet = payload["describe_result"]["payload"]["build_spec"]["transport_packet"]
    assert transport_packet["transport_form"] == "simc_split_talents"
    assert transport_packet["transport_status"] == "validated"
    assert transport_packet["path"] == str(packet_path.resolve())


def test_warcraft_talent_describe_hides_stale_packet_path_after_in_memory_upgrade(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "raw-packet.json"
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "transport_forms": {},
                "build_identity": {
                    "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                },
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                "validation": {"status": "not_validated"},
                "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
            }
        )
    )

    _patch_simc_describe_pipeline(
        monkeypatch,
        transport_form="simc_split_talents",
        transport_status="validated",
    )
    monkeypatch.setattr(
        "simc_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": "117176:1",
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": "druid",
                "spec": "balance",
            },
        },
    )

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", str(packet_path), "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    transport_packet = payload["describe_result"]["payload"]["build_spec"]["transport_packet"]
    assert transport_packet["transport_form"] == "simc_split_talents"
    assert transport_packet["transport_status"] == "validated"
    assert "path" not in transport_packet


def test_warcraft_talent_describe_preserves_packet_path_after_raw_only_refresh(
    monkeypatch,
    tmp_path: Path,
) -> None:
    packet_path = tmp_path / "raw-packet.json"
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "transport_forms": {},
                "build_identity": {
                    "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                },
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                "validation": {"status": "not_validated"},
                "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
            }
        )
    )

    _patch_simc_describe_pipeline(
        monkeypatch,
        transport_form="talent_transport_packet",
        transport_status="raw_only",
    )
    monkeypatch.setattr(
        "simc_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {},
            "validation": {
                "status": "raw_only",
                "reason": "unresolved_talent_entries",
                "source": "simc_trait_data_round_trip",
            },
        },
    )

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", str(packet_path), "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["source_packet_status"] == "raw_only"
    assert payload["upgraded"] is False
    assert payload["talent_transport_packet"]["transport_status"] == "raw_only"
    assert payload["talent_transport_packet"]["validation"] == {
        "status": "raw_only",
        "reason": "unresolved_talent_entries",
        "source": "simc_trait_data_round_trip",
    }
    transport_packet = payload["describe_result"]["payload"]["build_spec"]["transport_packet"]
    assert transport_packet["transport_form"] == "talent_transport_packet"
    assert transport_packet["transport_status"] == "raw_only"
    assert transport_packet["path"] == str(packet_path.resolve())


def test_warcraft_talent_packet_rejects_home_relative_missing_packet_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))

    result = runner.invoke(warcraft_app, ["talent-packet", "~/missing-packet.json"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"
    assert payload["error"]["message"] == "Talent transport packet file was not found: ~/missing-packet.json"


def test_warcraft_talent_describe_rejects_backslash_packet_like_input() -> None:
    result = runner.invoke(warcraft_app, ["talent-describe", r"tmp\missing-packet.json"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"
    assert payload["error"]["message"] == r"Talent transport packet file was not found: tmp\missing-packet.json"


def test_warcraft_passthrough_to_raiderio(monkeypatch) -> None:
    def fake_profile(self, *, region: str, realm: str, name: str, fields: str = ""):  # noqa: ANN001
        return {
            "name": "Roguecane",
            "region": "us",
            "realm": "Illidan",
            "race": "Blood Elf",
            "class": "Rogue",
            "active_spec_name": "Subtlety",
            "faction": "horde",
            "profile_url": "https://raider.io/characters/us/illidan/Roguecane",
            "thumbnail_url": "https://example.test/thumb.jpg",
            "guild": {"name": "Liquid", "realm": "Illidan", "region": "us"},
            "raid_progression": {},
            "mythic_plus_scores_by_season": [],
            "mythic_plus_ranks": {},
            "mythic_plus_recent_runs": [],
        }

    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.character_profile_variants", fake_profile)
    result = runner.invoke(warcraft_app, ["raiderio", "character", "us", "illidan", "Roguecane"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["character"]["name"] == "Roguecane"


def test_warcraft_passthrough_to_warcraft_wiki(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.fetch_article_page",
        lambda self, article_ref: {
            "article": {
                "title": "World of Warcraft API",
                "slug": "world-of-warcraft-api",
                "display_title": "World of Warcraft API",
                "page_url": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API",
                "section_slug": "world-of-warcraft-api",
                "section_title": "World of Warcraft API",
                "page_count": 1,
            },
            "page": {
                "title": "World of Warcraft API",
                "description": "Programming reference",
                "canonical_url": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API",
            },
            "navigation": {"count": 0, "items": []},
            "article_content": {"html": "<p>FrameXML</p>", "text": "FrameXML", "headings": [], "sections": []},
            "linked_entities": [],
            "citations": {"page": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API"},
        },
    )
    result = runner.invoke(warcraft_app, ["warcraft-wiki", "article", "World of Warcraft API"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["article"]["title"] == "World of Warcraft API"


def test_warcraft_passthrough_to_wowprogress(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowprogress_cli.main.WowProgressClient.fetch_guild_page_variants",
        lambda self, *, region, realm, name: {
            "guild": {
                "name": "Liquid",
                "region": "us",
                "realm": "US-Illidan",
                "faction": "Horde",
                "page_url": "https://www.wowprogress.com/guild/us/illidan/Liquid",
                "armory_url": "https://worldofwarcraft.com/en-us/guild/illidan/liquid",
            },
            "progress": {"summary": "8/8 (M)", "ranks": {"world": "1", "region": "1", "realm": "1"}},
            "item_level": {"average": 724.51, "group_size": "20-man", "ranks": {"world": "9026", "region": "4149", "realm": "238"}},
            "encounters": {"count": 0, "items": []},
            "citations": {"page": "https://www.wowprogress.com/guild/us/illidan/Liquid"},
        },
    )
    result = runner.invoke(warcraft_app, ["wowprogress", "guild", "us", "illidan", "Liquid"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["guild"]["name"] == "Liquid"


def test_warcraft_passthrough_to_warcraftlogs() -> None:
    result = runner.invoke(
        warcraft_app,
        ["warcraftlogs", "resolve", "https://www.warcraftlogs.com/reports/abcd1234#fight=3"],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["provider"] == "warcraftlogs"
    assert payload["resolved"] is True
    assert payload["next_command"] == "warcraftlogs report-encounter abcd1234 --fight-id 3"


def test_warcraft_guild_merges_sources_and_normalizes_query(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert args[1:] == ["us", "mal-ganis", "gn"]
        if provider == "raiderio":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "guild": {
                        "name": "gn",
                        "region": "us",
                        "realm": "Mal'Ganis",
                        "faction": "horde",
                        "profile_url": "https://raider.io/guilds/us/malganis/gn",
                        "member_count": 1,
                    },
                    "raiding": {
                        "progression": [{"raid_slug": "tier-mn-1", "summary": "0/9 N", "total_bosses": 9}],
                        "rankings": [{"raid_slug": "tier-mn-1", "normal": {"world": 0, "region": 0, "realm": 0}}],
                    },
                    "roster_preview": [
                        {
                            "name": "Fharg",
                            "class_name": "Shaman",
                            "active_spec_name": "Enhancement",
                            "profile_url": "https://raider.io/characters/us/malganis/Fharg",
                        }
                    ],
                    "citations": {"profile": "https://raider.io/guilds/us/malganis/gn"},
                },
                "stdout": "",
            }
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "guild": {
                    "name": "gn",
                    "region": "us",
                    "realm": "Mal'Ganis",
                    "faction": "Horde",
                    "page_url": "https://www.wowprogress.com/guild/us/mal-ganis/gn",
                },
                "progress": {
                    "raid": "Liberation of Undermine",
                    "tier_key": "tier34",
                    "summary": "8/8 (M)",
                    "ranks": {"world": "19", "region": "6", "realm": "2"},
                },
                "item_level": {
                    "average": 732.1,
                    "ranks": {"world": "1", "region": "1", "realm": "1"},
                },
                "encounters": {"count": 8, "items": [{"encounter": "Chrome King Gallywix"}]},
                "citations": {"page": "https://www.wowprogress.com/guild/us/mal-ganis/gn"},
            },
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["guild", "na", "Mal'Ganis", "gn"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["query"] == {"region": "us", "realm": "mal-ganis", "name": "gn"}
    assert payload["guild"]["name"] == "gn"
    assert payload["sources"]["raiderio"]["status"] == "ok"
    assert payload["sources"]["wowprogress"]["status"] == "ok"
    assert (
        payload["sources"]["raiderio"]["payload"]["guild"]["profile_url"]
        == "https://raider.io/guilds/us/malganis/gn"
    )
    assert payload["sources"]["wowprogress"]["payload"]["progress"]["summary"] == "8/8 (M)"
    assert payload["conflicts"]["different_tier_window_detected"] is True


def test_warcraft_guild_history_and_ranks_use_wowprogress(monkeypatch) -> None:
    history_payload = {
        "provider": "wowprogress",
        "kind": "guild_history",
        "guild": {"name": "gn", "region": "us", "realm": "Mal'Ganis"},
        "count": 1,
        "tiers": [
            {
                "tier_key": "tier34",
                "raid": "Liberation of Undermine",
                "current": True,
                "progress": "8/8 (M)",
                "progress_ranks": {"world": "19", "region": "6", "realm": "2"},
                "item_level_average": 732.1,
                "item_level_ranks": {"world": "1", "region": "1", "realm": "1"},
                "last_kill_at": "Apr 4, 2025 02:03",
                "page_url": "https://www.wowprogress.com/guild/us/mal-ganis/gn/rating.tier34",
            }
        ],
        "citations": {"page": "https://www.wowprogress.com/guild/us/mal-ganis/gn"},
    }

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "wowprogress"
        assert args[1:] == ["us", "mal-ganis", "gn"]
        payload = (
            history_payload
            if args[0] == "guild-history"
            else {**history_payload, "kind": "guild_ranks"}
        )
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": payload,
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    history_result = runner.invoke(warcraft_app, ["guild-history", "us", "Mal'Ganis", "gn"])
    assert history_result.exit_code == 0
    history = json.loads(history_result.stdout)
    assert history["ok"] is True
    assert history["source"] == "wowprogress"
    assert history["tiers"][0]["raid"] == "Liberation of Undermine"
    assert history["provider_payload"]["kind"] == "guild_history"

    ranks_result = runner.invoke(warcraft_app, ["guild-ranks", "us", "Mal'Ganis", "gn"])
    assert ranks_result.exit_code == 0
    ranks = json.loads(ranks_result.stdout)
    assert ranks["ok"] is True
    assert ranks["tiers"][0]["progress_ranks"]["world"] == "19"
    assert ranks["provider_payload"]["kind"] == "guild_ranks"
