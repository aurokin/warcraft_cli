"""Shared helpers for the cross-provider contract tests.

These tests treat every binary as one surface: the same flags, the same envelope, the same error
contract. Anything provider-specific belongs in that provider's own test file, not here.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import sys
import tomllib
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import typer
import warcraft_cli.main
from warcraft_cli.providers import PROVIDERS

REPO_ROOT = Path(__file__).resolve().parents[1]

# Provider client seams that keep an otherwise-live surface offline. Each entry is
# (dotted target, replacement); the replacement returns the provider's empty-result shape so the
# surface still builds a full envelope. Targets are class attributes so every reference sees them.
PROVIDER_STUBS: dict[str, tuple[tuple[str, Any], ...]] = {
    "wowhead": (("wowhead_cli.wowhead_client.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []}),),
    "method": (("method_cli.client.MethodClient.sitemap_guides", lambda self: []),),
    "icy-veins": (("icy_veins_cli.client.IcyVeinsClient.sitemap_guides", lambda self: []),),
    "raiderio": (("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []}),),
    "warcraft-wiki": (("warcraft_wiki_cli.client.WarcraftWikiClient.search_articles", lambda self, query, *, limit: (0, [])),),
    "lorrgs": (
        ("lorrgs_cli.client.LorrgsClient.specs", lambda self: {"payload": {"specs": []}, "source_url": "https://api.lorrgs.io/api/spec"}),
        ("lorrgs_cli.client.LorrgsClient.bosses", lambda self: {"payload": {"bosses": []}, "source_url": "https://api.lorrgs.io/api/boss"}),
    ),
}

# An explicit report reference keeps the Warcraft Logs surfaces on their offline parsing path.
WARCRAFTLOGS_REPORT_QUERY = "https://www.warcraftlogs.com/reports/abcd1234EFGH5678#fight=3"


def all_cli_apps() -> dict[str, typer.Typer]:
    """Every installed binary name mapped to its Typer app, wrapper first."""
    return {"warcraft": warcraft_cli.main.app} | {registration.command: registration.app for registration in PROVIDERS}


def subcommands(command: Any) -> dict[str, Any]:
    """The child commands of a Click group, or {} for a leaf.

    Typer vendors its own Click shim (``typer._click``), so ``isinstance(cmd, click.Group)`` is
    False for the objects ``typer.main.get_command`` returns. Duck-type on ``commands`` instead.
    """
    children = getattr(command, "commands", None)
    return children if isinstance(children, dict) else {}


def walk_commands(command: Any, prefix: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    """Yield every (command path, command) pair in a Click tree, including intermediate groups."""
    if prefix:
        yield prefix, command
    for name, child in subcommands(command).items():
        yield from walk_commands(child, (*prefix, name))


def is_argument(param: Any) -> bool:
    """True for positional parameters. See ``subcommands`` for why this is not an isinstance check."""
    return getattr(param, "param_type_name", None) == "argument"


def apply_provider_stubs(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise a provider's network seam so its pure surface can be exercised offline."""
    for target, replacement in PROVIDER_STUBS.get(name, ()):
        monkeypatch.setattr(target, replacement)


class BinaryResult(NamedTuple):
    exit_code: int
    stdout: str
    stderr: str


@cache
def console_scripts() -> dict[str, Any]:
    """Every declared console script resolved to its callable, the way pip wires it up."""
    scripts = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    resolved = {}
    for binary, target in scripts.items():
        module_name, _, attribute = target.partition(":")
        resolved[binary] = getattr(importlib.import_module(module_name), attribute)
    return resolved


def run_binary(binary: str, args: list[str]) -> BinaryResult:
    """Run a binary through its real ``run()`` entry point.

    ``typer.testing.CliRunner`` invokes the Typer app directly and so skips ``guarded_run``, which
    is exactly the layer that turns an escaping exception into an error envelope. Error-contract
    assertions have to go through here to mean anything.
    """
    entry_point = console_scripts()[binary]
    out, err = io.StringIO(), io.StringIO()
    argv = sys.argv
    sys.argv = [binary, *args]
    exit_code = 0
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            entry_point()
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else int(bool(exc.code))
    finally:
        sys.argv = argv
    return BinaryResult(exit_code, out.getvalue(), err.getvalue())
