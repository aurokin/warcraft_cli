"""Every command and every positional argument on every binary must describe itself.

Agents discover this toolchain through ``--help``. A command with no help text is invisible to
them, so this is a contract, not a style rule.
"""

from __future__ import annotations

import pytest
import typer
from cli_testkit import all_cli_apps, is_argument, walk_commands

CLI_APPS = all_cli_apps()


def _has_text(value: str | None) -> bool:
    return bool((value or "").strip())


@pytest.mark.parametrize("binary", sorted(CLI_APPS), ids=sorted(CLI_APPS))
def test_every_command_has_help_text(binary: str) -> None:
    root = typer.main.get_command(CLI_APPS[binary])
    missing = [" ".join(path) for path, command in walk_commands(root) if not _has_text(command.help or command.short_help)]
    assert not missing, f"{binary}: commands without help text: {missing}. Add a docstring or help= to each."


@pytest.mark.parametrize("binary", sorted(CLI_APPS), ids=sorted(CLI_APPS))
def test_every_positional_argument_has_help_text(binary: str) -> None:
    root = typer.main.get_command(CLI_APPS[binary])
    missing = [
        f"{' '.join(path)} <{param.name}>"
        for path, command in walk_commands(root)
        for param in command.params
        if is_argument(param) and not _has_text(getattr(param, "help", None))
    ]
    assert not missing, f"{binary}: arguments without help text: {missing}. Add help= to each typer.Argument."


@pytest.mark.parametrize("binary", sorted(CLI_APPS), ids=sorted(CLI_APPS))
def test_root_help_describes_the_binary(binary: str) -> None:
    root = typer.main.get_command(CLI_APPS[binary])
    assert _has_text(root.help or root.short_help), f"{binary}: the root callback has no help text."
