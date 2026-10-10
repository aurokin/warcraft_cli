#!/usr/bin/env python3
"""Exercise installed CLI contracts with empty runtime roots and forbidden provider networking."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

AUTH_COMMANDS = {
    "blizzard": (("item", "19019"), {"missing_client_credentials"}),
    "curseforge": (("addon", "3358"), {"missing_api_key"}),
    "warcraftlogs": (("zones",), {"missing_public_auth", "missing_client_credentials"}),
}
AUTH_HINTS = {
    "blizzard": ("BLIZZARD_CLIENT_ID", "BLIZZARD_CLIENT_SECRET"),
    "curseforge": ("CURSEFORGE_API_KEY",),
    "warcraftlogs": ("WARCRAFTLOGS_CLIENT_ID",),
}


def checked_envelope(result: subprocess.CompletedProcess[str], *, provider: str, exit_code: int,
                     error_codes: set[str] | None = None) -> dict[str, Any]:
    """Validate stream placement as well as shape: extra stdout/stderr is a contract failure."""
    from warcraft_core.envelope import envelope_violations

    context = f"{result.args}: exit={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
    assert result.returncode == exit_code, context
    text, other = (result.stdout, result.stderr) if exit_code == 0 else (result.stderr, result.stdout)
    assert not other, context
    assert "Traceback" not in text, context
    payload = json.loads(text)
    assert isinstance(payload, dict), context
    assert not envelope_violations(payload), context
    assert payload["ok"] is (exit_code == 0), context
    assert payload["provider"] == provider, context
    if error_codes is not None:
        assert payload["error"]["code"] in error_codes, context
    return payload


def run_command(binary: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(Path(sys.prefix) / "bin" / binary), *args],
                          capture_output=True, text=True, timeout=30, check=False)


def check_doctor(binary: str) -> None:
    provider = "blizzard-api" if binary == "blizzard" else binary
    doctor_args = ("doctor", "--no-live") if binary == "wowhead" else ("doctor",)
    payload = checked_envelope(run_command(binary, *doctor_args), provider=provider, exit_code=0)
    assert payload["kind"] == "doctor" and payload["command"] == "doctor", payload
    data = payload["data"]
    if binary == "warcraft":
        from warcraft_cli.providers import PROVIDERS

        rows = {row["provider"]: row for row in data["providers"]}
        expected = {registration.name for registration in PROVIDERS}
        assert set(rows) == expected, rows
        for name in ("blizzard-api", "curseforge", "warcraftlogs"):
            assert rows[name]["auth"]["configured"] is False, rows[name]
    else:
        assert data["capabilities"] and isinstance(data["capabilities"], dict), payload
        if binary in AUTH_COMMANDS:
            assert data["auth"]["configured"] is False, payload
            assert data["status"] == "degraded", payload


def verify_installed(distribution: str, expected: dict[str, str]) -> None:
    from warcraft_core.paths import worktree_root

    dist = importlib.metadata.distribution(distribution)
    actual = {entry.name: entry.value for entry in dist.entry_points if entry.group == "console_scripts"}
    assert actual == expected, (actual, expected)
    if "warcraft" in actual:
        from warcraft_cli.providers import PROVIDERS

        installed_scripts = {entry.name: entry.value for entry in importlib.metadata.entry_points(group="console_scripts")}
        for registration in PROVIDERS:
            assert registration.command in installed_scripts, registration.command
            actual[registration.command] = installed_scripts[registration.command]
    for target in actual.values():
        module = importlib.import_module(target.split(":")[0])
        origin = Path(module.__file__).resolve()
        assert origin.is_relative_to(Path(sys.prefix).resolve()), origin
    # Shared dependencies must also resolve from this installation, never an editable checkout.
    for name in ("warcraft_core", "warcraft_api", "warcraft_content"):
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        assert Path(module.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), module.__file__
    assert worktree_root() is None, worktree_root()
    if "icy-veins" in actual:
        from icy_veins_cli.site_index import load_site_index

        index = load_site_index()
        assert index is not None and index.bundled and index.pages, "Missing bundled Icy Veins index"
    for binary in actual:
        for args in (("--help",), ("doctor", "--help")):
            result = run_command(binary, *args)
            assert result.returncode == 0 and result.stdout.strip() and not result.stderr, result
        check_doctor(binary)
        provider = "blizzard-api" if binary == "blizzard" else binary
        for args in (("definitely-not-a-command",), ("--definitely-not-a-flag", "doctor")):
            checked_envelope(run_command(binary, *args), provider=provider, exit_code=2, error_codes={"invalid_argument"})
        doctor_args = ("doctor", "--no-live") if binary == "wowhead" else ("doctor",)
        checked_envelope(run_command(binary, "--fields", "data.no_such_path", "--fields-strict", *doctor_args),
                         provider=provider, exit_code=2, error_codes={"missing_fields"})
    for binary, (args, codes) in AUTH_COMMANDS.items():
        if binary not in actual:
            continue
        provider = "blizzard-api" if binary == "blizzard" else binary
        direct = checked_envelope(run_command(binary, *args), provider=provider, exit_code=3, error_codes=codes)
        assert all(hint in direct["error"]["message"] for hint in AUTH_HINTS[binary]), direct
        if "warcraft" in actual:
            passed = checked_envelope(run_command("warcraft", binary, *args), provider=provider, exit_code=3, error_codes=codes)
            assert passed == direct, (passed, direct)
    if "warcraftlogs" in actual:
        checked_envelope(run_command("warcraftlogs", "auth", "whoami"), provider="warcraftlogs", exit_code=3,
                         error_codes={"missing_user_auth"})
    if "warcraft" in actual:
        schema = checked_envelope(run_command("warcraft", "schema"), provider="warcraft", exit_code=0)
        assert schema["kind"] == "envelope_schema" and schema["data"]["schema"]["required"], schema
    log = Path(os.environ["VERIFY_NETWORK_LOG"])
    assert not log.exists(), f"Installed verification attempted network access: {log.read_text()}"
    print(f"Verified {distribution}: {len(actual)} installed binaries, offline doctor/usage/auth contracts")


def main() -> None:
    # -I ignores PYTHONPATH, so the parent interpreter loads the same guard explicitly.
    runpy.run_path(os.environ["VERIFY_GUARD_PATH"])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("distribution")
    parser.add_argument("scripts")
    args = parser.parse_args()
    verify_installed(args.distribution, json.loads(args.scripts))


if __name__ == "__main__":
    main()
