"""The cross-binary contract: everything every binary in this repo promises, checked on all of them.

Provider-specific behaviour belongs in that provider's journey file. What lives here is the shared
surface from docs/foundation/ERROR_CONTRACT.md — one envelope, the same exit codes, the same global
flags, plain-text help — plus the cache behaviour the binaries advertise in ``doctor``. Every check
runs on every installed binary, so this file is also the harness self-check the suite used to keep
in a separate smoke module.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import typer

from tests.cli_testkit import all_cli_apps, console_scripts, subcommands, walk_commands
from tests.e2e.harness import EXIT_NETWORK, EXIT_USAGE, Result, dead_proxy_env, run, run_raw, run_text
from tests.e2e.pins import (
    CHARACTER_NAME,
    CURSEFORGE_ADDON_ID,
    GUILD_REALM,
    GUILD_REGION,
    ITEM_ID,
    ITEM_SEARCH_QUERY,
    REALM_SLUG,
    WIKI_API_FUNCTION,
)

# Every installed binary, read from the console scripts pip wires up, so a new binary joins every
# journey below without anyone editing a list. The wrapper comes first because its doctor is the
# tier registry the provider journeys read.
BINARIES: tuple[str, ...] = ("warcraft", *sorted(set(console_scripts()) - {"warcraft"}))

# Binary -> provider name in the envelope and in `warcraft doctor`. Only blizzard differs.
PROVIDER_BY_BINARY: dict[str, str] = {binary: binary for binary in BINARIES} | {"blizzard": "blizzard-api"}

# One cheap network command per binary, for the dead-proxy journey. simc is a local process and has
# no network command to fail, so it is not listed.
NETWORK_COMMAND: dict[str, tuple[str, ...]] = {
    "warcraft": ("wowhead", "entity", "item", str(ITEM_ID)),
    "wowhead": ("entity", "item", str(ITEM_ID)),
    "warcraftlogs": ("regions",),
    "raiderio": ("search", ITEM_SEARCH_QUERY),
    "warcraft-wiki": ("search", "CreateFrame"),
    "icy-veins": ("search", "mistweaver monk"),
    "method": ("search", "mistweaver monk"),
    "lorrgs": ("specs",),
    "raidbots": ("inspect-report", "warcraftcliE2Emissing"),
    "blizzard": ("realm", REALM_SLUG),
    "curseforge": ("addon", CURSEFORGE_ADDON_ID),
}

# Providers with a file-backed HTTP cache and a cheap repeatable read. lorrgs is deliberately absent:
# its client talks straight to the API with no cache store, so there is no hit to observe.
CACHED_READ: dict[str, tuple[str, ...]] = {
    "raiderio": ("character", GUILD_REGION, GUILD_REALM, CHARACTER_NAME),
    "warcraft-wiki": ("article", WIKI_API_FUNCTION),
    "wowhead": ("entity", "item", str(ITEM_ID)),
}
# The CACHED_READ commands whose payload reports cache state in a `freshness` block.
REPORTS_FRESHNESS = frozenset({"raiderio"})

COMPACT_MAX_CHARS = 40


def _provider(binary: str) -> str:
    return PROVIDER_BY_BINARY[binary]


def _command_rows(help_text: str) -> list[tuple[str, str]]:
    """``(name, description)`` for every row of a Typer ``--help`` Commands panel."""
    rows: list[tuple[str, str]] = []
    inside = False
    for line in help_text.splitlines():
        stripped = line.rstrip()
        if not inside:
            inside = stripped.startswith("╭─") and " Commands " in stripped
            continue
        if stripped.startswith("╰"):
            break
        if not (stripped.startswith("│") and stripped.endswith("│")):
            continue
        body = stripped[1:-1]
        if not body.strip():
            continue
        if body.startswith("  ") and rows:
            # The name column is blank: this wraps the previous row's description.
            rows[-1] = (rows[-1][0], f"{rows[-1][1]} {body.strip()}".strip())
            continue
        name, _, description = body.strip().partition("  ")
        rows.append((name, description.strip()))
    return rows


def _group_paths(binary: str) -> list[tuple[str, ...]]:
    """Command paths that own a Commands panel, read from the installed Typer app.

    Detecting groups by running ``--help`` on all ~250 commands would dominate the suite's runtime,
    and the app object is the same tree the binary exposes, so it is the cheap exact answer for
    *where* to look. The descriptions themselves still come from the real binary's help page.
    """
    command = typer.main.get_command(all_cli_apps()[binary])
    return [(), *(path for path, child in walk_commands(command) if subcommands(child))]


def _help_descriptions(binary: str) -> dict[tuple[str, ...], str]:
    """Every ``command path -> description`` the binary's help pages advertise."""
    described: dict[tuple[str, ...], str] = {}
    for group in _group_paths(binary):
        for name, description in _command_rows(run_text(binary, *group, "--help").stdout):
            described[(*group, name)] = description
    return described


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for row in value for text in _strings(row)]
    if isinstance(value, dict):
        return [text for row in value.values() for text in _strings(row)]
    return []


def _cache_snapshot(root: Path) -> dict[str, tuple[float, int]]:
    """Every cache file under ``root`` with its mtime and size; a re-fetch rewrites the entry."""
    return {str(path): (path.stat().st_mtime, path.stat().st_size) for path in root.rglob("*") if path.is_file()}


def _without_freshness(data: dict[str, Any]) -> dict[str, Any]:
    """The payload minus the block that reports cache state, which two reads may legitimately differ on."""
    return {key: value for key, value in data.items() if key != "freshness"}


def _json_stdout(result: Result) -> dict[str, Any]:
    """Parse stdout for the flag journeys, which run outside ``run()`` because ``--fields`` prunes
    the envelope keys ``run()`` validates."""
    assert result.exit_code == 0, result.describe()
    assert "Traceback" not in result.stderr, result.describe()
    payload: dict[str, Any] = json.loads(result.stdout)
    return payload


@pytest.mark.parametrize("binary", BINARIES)
def test_doctor_reports_the_provider_its_tier_and_its_capabilities(
    binary: str, require, doctor_rows: dict[str, dict[str, Any]]
) -> None:
    provider = _provider(binary)
    if binary != "warcraft":
        require(provider)
    result = run(binary, "doctor")
    assert result.payload["provider"] == provider
    assert result.payload["kind"] == "doctor"

    if binary == "warcraft":
        # The wrapper's doctor is the tier registry every provider journey reads.
        assert {row["provider"] for row in result.data["providers"]} == set(doctor_rows)
        return

    capabilities = result.data["capabilities"]
    assert isinstance(capabilities, dict) and capabilities, result.describe()
    assert all(isinstance(state, str) and state for state in capabilities.values()), result.describe()
    assert doctor_rows[provider]["tier"] in {"core", "supported", "experimental"}


@pytest.mark.parametrize("binary", BINARIES)
def test_help_is_plain_text_and_describes_every_subcommand(binary: str) -> None:
    top = run_text(binary, "--help")
    assert "Usage:" in top.stdout, top.describe()
    assert not top.stdout.lstrip().startswith("{"), "help must be plain text, not an envelope"

    described = _help_descriptions(binary)
    # Every command the app defines has to appear on a help page, or the walk below is not a
    # complete check — a parser that quietly matched nothing would otherwise pass.
    expected = {path for path, _ in walk_commands(typer.main.get_command(all_cli_apps()[binary]))}
    assert set(described) == expected, f"{binary} help pages and command tree disagree"

    undescribed = sorted(" ".join(path) for path, description in described.items() if not description)
    assert not undescribed, f"{binary} subcommands with no --help description: {undescribed}"


@pytest.mark.parametrize("binary", BINARIES)
@pytest.mark.parametrize("argv", [("definitely-not-a-command",), ("--definitely-not-a-flag", "doctor")])
def test_a_usage_error_is_an_exit_2_envelope_on_stderr(binary: str, argv: tuple[str, ...]) -> None:
    # An unknown command and an unknown flag both have to reach the error contract, not a Rich
    # usage panel: `run` proves stdout stays empty and stderr holds exactly one valid envelope.
    result = run(binary, *argv, expect=EXIT_USAGE, error_code="invalid_argument")
    assert "Traceback" not in result.stderr, result.describe()
    assert result.payload["provider"] == _provider(binary), result.describe()


def test_the_removed_debug_profile_is_rejected_as_a_usage_error() -> None:
    # `--profile debug` was removed; it must fail loudly rather than be accepted and ignored.
    result = run("warcraft", "--profile", "debug", "doctor", expect=EXIT_USAGE, error_code="invalid_argument")
    assert "agent" in result.payload["error"]["message"], result.describe()
    assert "debug" not in run_text("warcraft", "--help").stdout, "help still advertises the debug profile"


@pytest.mark.parametrize("binary", sorted(NETWORK_COMMAND))
def test_a_network_failure_is_an_exit_5_envelope_on_stderr(binary: str, require, tmp_path: Path) -> None:
    if binary != "warcraft":
        require(_provider(binary))
    # A private cache root as well as the dead proxy, so a cached response cannot mask the failure.
    env = {**dead_proxy_env(), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    result = run(binary, *NETWORK_COMMAND[binary], expect=EXIT_NETWORK, env=env)
    assert result.stdout == "", result.describe()
    assert result.error_code in {"network_error", "timeout", "upstream_error"}, result.describe()
    # `warcraft <provider> ...` is a passthrough, so the proxied provider owns the error envelope.
    assert result.payload["provider"] == (NETWORK_COMMAND[binary][0] if binary == "warcraft" else _provider(binary))


@pytest.mark.parametrize("binary", BINARIES)
def test_fields_reports_what_it_could_not_select_and_fields_strict_rejects_it(binary: str, require) -> None:
    if binary != "warcraft":
        require(_provider(binary))
    full = run(binary, "doctor")
    selected = _json_stdout(run_raw(binary, "--fields", "data", "--fields-strict", "doctor"))
    # Key sets, not values: some doctors probe endpoints and report a fresh latency every run.
    assert set(selected) == {"data"}, selected
    assert set(selected["data"]) == set(full.data), selected

    # Without --fields-strict a path the payload does not have is reported, never silently dropped.
    partial = _json_stdout(run_raw(binary, "--fields", "data,data.no_such_path", "doctor"))
    assert partial["fields_missing"] == ["data.no_such_path"], partial
    assert set(partial["data"]) == set(full.data), partial

    bogus = run(binary, "--fields", "data.no_such_path", "--fields-strict", "doctor", expect=EXIT_USAGE, error_code="missing_fields")
    assert "no_such_path" in json.dumps(bogus.payload["error"]), bogus.describe()


@pytest.mark.parametrize("binary", BINARIES)
def test_compact_truncates_long_strings_and_never_grows_the_payload(binary: str, require) -> None:
    if binary != "warcraft":
        require(_provider(binary))
    full = run(binary, "doctor")
    compact = run_raw(binary, "--compact", "--compact-max-chars", str(COMPACT_MAX_CHARS), "doctor")
    payload = _json_stdout(compact)
    compact_strings = _strings(payload)
    too_long = [text for text in compact_strings if len(text) > COMPACT_MAX_CHARS]
    assert not too_long, f"{binary} --compact left strings longer than {COMPACT_MAX_CHARS}: {too_long[:3]}"

    # "No string is too long" also holds when --compact is ignored on a doctor that has no long
    # string, so every doctor must carry one (each reports a cache path, a repo path, or a note past the
    # limit) and the cut has to show: the payload shrinks and the cut strings end in an ellipsis.
    over_limit = [text for text in _strings(full.payload) if len(text) > COMPACT_MAX_CHARS]
    assert over_limit, f"{binary} doctor has no string over {COMPACT_MAX_CHARS} chars, so --compact cannot be observed"
    assert len(compact.stdout) < len(full.stdout), compact.describe()
    assert any(text.endswith("...") for text in compact_strings), compact.describe()


@pytest.mark.parametrize("binary", BINARIES)
def test_pretty_and_the_human_profile_produce_readable_json(binary: str, require) -> None:
    if binary != "warcraft":
        require(_provider(binary))
    agent = run(binary, "doctor")
    assert agent.stdout.count("\n") == 1, "the default profile is compact JSON on one line"

    pretty = _json_stdout(run_raw(binary, "--pretty", "doctor"))
    assert pretty.keys() == agent.payload.keys()
    assert set(pretty["data"]) == set(agent.data)

    human = run_raw(binary, "--profile", "human", "doctor")
    assert human.stdout.count("\n") > 1, human.describe()
    assert set(_json_stdout(human)["data"]) == set(agent.data)


@pytest.mark.parametrize("binary", BINARIES)
def test_global_flags_only_bind_before_the_subcommand(binary: str) -> None:
    # A global flag after the subcommand is rejected like any other unknown option, through the
    # error contract: `run` proves stdout stays empty and stderr holds exactly one valid envelope.
    result = run(binary, "doctor", "--pretty", expect=EXIT_USAGE, error_code="invalid_argument")
    assert "--pretty" in result.payload["error"]["message"], result.describe()
    assert result.payload["provider"] == _provider(binary), result.describe()


@pytest.mark.parametrize("binary", sorted(CACHED_READ))
def test_a_repeated_read_is_served_from_the_isolated_cache(binary: str, require, tmp_path: Path) -> None:
    require(_provider(binary))
    # A cache root of its own, so the first read is a miss whatever the session already fetched.
    cache_env = {"XDG_CACHE_HOME": str(tmp_path / "cache")}
    provider_cache = tmp_path / "cache" / "warcraft" / _provider(binary)

    first = run(binary, *CACHED_READ[binary], env=cache_env)
    after_first = _cache_snapshot(provider_cache)
    assert after_first, f"{binary} wrote no cache entry under {provider_cache}"

    # Behind a dead proxy the command can only succeed if every byte came from the cache.
    second = run(binary, *CACHED_READ[binary], env={**cache_env, **dead_proxy_env()})
    # `freshness` is the one block allowed to differ: it reports where the answer came from, so it
    # has to call the first read a miss and the second a hit. Everything else must be identical.
    assert _without_freshness(second.data) == _without_freshness(first.data)
    assert ("freshness" in second.data) is (binary in REPORTS_FRESHNESS), second.describe()
    if binary in REPORTS_FRESHNESS:
        hits = (first.data["freshness"]["cache_hit"], second.data["freshness"]["cache_hit"])
        assert hits == (False, True), second.describe()
    # A miss would also rewrite the entry; identical mtimes and sizes mean nothing was re-fetched.
    assert _cache_snapshot(provider_cache) == after_first, second.describe()


def test_wowhead_cache_inspect_reports_the_isolated_root(require, cache_root: Path) -> None:
    require("wowhead")
    run("wowhead", "entity", "item", str(ITEM_ID))
    result = run("wowhead", "cache-inspect")

    settings = result.data["settings"]
    assert settings["enabled"] is True
    assert settings["backend"] == "file"
    assert Path(settings["cache_dir"]).is_relative_to(cache_root), result.describe()

    stats = result.data["stats"]
    assert stats["exists"] is True
    assert Path(stats["root"]).is_relative_to(cache_root)
    assert stats["totals"]["active"] >= 1, result.describe()
    assert stats["totals"]["invalid"] == 0, result.describe()


def test_a_redis_backed_read_hits_the_shared_cache(require, optional, tmp_path: Path) -> None:
    require("wowhead")
    redis_url = optional("redis", "WARCRAFT_E2E_REDIS_URL")
    # A per-run prefix keeps this from reading or clobbering anything already in that Redis.
    env = {
        "WOWHEAD_CACHE_BACKEND": "redis",
        "WOWHEAD_REDIS_URL": redis_url,
        "WOWHEAD_REDIS_PREFIX": f"warcraft_e2e_{tmp_path.name}",
    }
    settings = run("wowhead", "cache-inspect", env=env).data["settings"]
    assert settings["backend"] == "redis"

    first = run("wowhead", "entity", "item", str(ITEM_ID), env=env)
    stats = run("wowhead", "cache-inspect", env=env).data["stats"]
    assert stats["kind"] == "redis", json.dumps(stats)[:400]
    assert stats["available"] is True, json.dumps(stats)[:400]
    assert stats["count"] >= 1, json.dumps(stats)[:400]

    # Redis is reached over loopback, so a dead HTTP proxy leaves only the cache as a source.
    second = run("wowhead", "entity", "item", str(ITEM_ID), env={**env, **dead_proxy_env()})
    assert second.data == first.data


def test_the_contract_tables_cover_every_installed_binary() -> None:
    # A new console script must not slip past the contract; these tables are the coverage list.
    assert set(NETWORK_COMMAND) == set(BINARIES) - {"simc"}, "simc runs locally; every other binary needs one"
    assert REPORTS_FRESHNESS <= set(CACHED_READ) <= set(BINARIES)


def test_the_help_row_parser_matches_the_typer_panel_shape() -> None:
    # _help_descriptions drives the description check on ~250 commands; a parser that silently
    # matched nothing would make that check vacuous, so pin its behaviour on a known panel.
    panel = "\n".join(
        [
            "╭─ Commands ───────────────────────────╮",
            "│ doctor     Report readiness and      │",
            "│            cache state.              │",
            "│ search     Search things.            │",
            "╰──────────────────────────────────────╯",
        ]
    )
    assert _command_rows(panel) == [("doctor", "Report readiness and cache state."), ("search", "Search things.")]
    assert _command_rows("no panel here") == []
