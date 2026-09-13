"""Every shell example in the docs must resolve against the real Typer apps.

The parser validates command names and option names only, never prose and never positional values,
so docs can be rewritten freely as long as the commands they show still exist. Coverage is "every
simple example": lines with shell operators (pipes, redirects, ``&&``, substitutions) and lines
whose first token is not one of our binaries are skipped rather than validated.
"""

from __future__ import annotations

import re
import shlex
from functools import cache
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import typer
from cli_testkit import all_cli_apps, subcommands
from warcraft_cli.providers import PROVIDERS

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_APPS = all_cli_apps()
PASSTHROUGH_COMMANDS = {registration.command for registration in PROVIDERS}

# Provider docs directory -> binary name; they differ only for blizzard.
PROVIDER_DOC_DIRS = {
    "blizzard-api": "blizzard",
    "curseforge": "curseforge",
    "icy-veins": "icy-veins",
    "lorrgs": "lorrgs",
    "method": "method",
    "raidbots": "raidbots",
    "raiderio": "raiderio",
    "simc": "simc",
    "warcraft": "warcraft",
    "warcraft-wiki": "warcraft-wiki",
    "warcraftlogs": "warcraftlogs",
    "wowhead": "wowhead",
    "wowprogress": "wowprogress",
}
SHELL_INFO_STRINGS = frozenset({"", "bash", "sh", "shell", "zsh", "console"})
# Lines containing any of these are shell constructs, not a single command invocation.
SHELL_OPERATORS = ("|", "&&", "||", ";", ">", "<(", "$(", "`")
# "wowhead-cli" as a bare word means the command; inside a path or a pip/dependency name it is the
# distribution and stays legitimate, so require no adjacent path or word characters.
DISTRIBUTION_NAME_MISUSE = re.compile(r"(?<![\w/.-])wowhead-cli(?![\w/-])")


def _doc_files() -> list[Path]:
    files = [REPO_ROOT / "README.md", REPO_ROOT / "docs" / "USAGE.md", REPO_ROOT / "docs" / "README.md"]
    for doc_dir in PROVIDER_DOC_DIRS:
        files.extend(sorted((REPO_ROOT / "docs" / doc_dir).rglob("*.md")))
    files.extend(sorted((REPO_ROOT / "skills").rglob("*.md")))
    return [path for path in files if path.exists()]


DOC_FILES = _doc_files()
DOC_IDS = [str(path.relative_to(REPO_ROOT)) for path in DOC_FILES]


class Example(NamedTuple):
    line_no: int
    text: str


def _shell_examples(markdown: str) -> list[Example]:
    """Every runnable-looking line inside fenced shell blocks, with continuations joined."""
    examples: list[Example] = []
    fence_open = False
    in_shell_block = False
    pending: list[str] = []
    pending_line = 0
    for line_no, raw in enumerate(markdown.splitlines(), start=1):
        fence = re.match(r"^\s*```+\s*(\S*)", raw)
        if fence:
            # Track fence nesting separately from shell-ness so a ```json block's closing fence
            # is not mistaken for the opening fence of an unlabelled shell block.
            in_shell_block = not fence_open and fence.group(1).lower() in SHELL_INFO_STRINGS
            fence_open = not fence_open
            pending = []
            continue
        if not in_shell_block:
            continue
        line = raw.strip()
        if pending:
            pending.append(line.removesuffix("\\").strip())
            if not line.endswith("\\"):
                examples.append(Example(pending_line, " ".join(pending)))
                pending = []
            continue
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("$ ").strip()
        if line.endswith("\\"):
            pending_line = line_no
            pending = [line.removesuffix("\\").strip()]
            continue
        examples.append(Example(line_no, line))
    return examples


def _option_names(command: Any) -> set[str]:
    names: set[str] = set()
    for param in command.params:
        names.update(param.opts)
        names.update(param.secondary_opts)
    return {name for name in names if name.startswith("-")}


def _is_flag_token(token: str) -> bool:
    return token.startswith("-") and token != "-" and not re.fullmatch(r"-\d+(\.\d+)?", token)


def _takes_value(command: Any, flag: str) -> bool:
    for param in command.params:
        if flag in param.opts or flag in param.secondary_opts:
            return not getattr(param, "is_flag", False) and getattr(param, "nargs", 1) != 0
    return False


def _check_options(command: Any, tokens: list[str], where: str) -> list[str]:
    """Reject any ``--flag`` the command does not declare. Positional values are not validated."""
    known = _option_names(command)
    problems: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not _is_flag_token(token):
            continue
        flag, _, inline_value = token.partition("=")
        if flag not in known:
            problems.append(f"{where}: unknown option {flag}")
            continue
        if not inline_value and _takes_value(command, flag) and index < len(tokens):
            index += 1
    return problems


@cache
def _root_command(binary: str) -> Any:
    return typer.main.get_command(CLI_APPS[binary])


def _resolve(binary: str, tokens: list[str]) -> list[str]:
    """Walk a command line down the Typer tree, reporting unknown subcommands and options."""
    command = _root_command(binary)
    path = [binary]
    while True:
        children = subcommands(command)
        if not children:
            return _check_options(command, tokens, " ".join(path))
        # On a group, every leading flag must be one of that group's own options: this is what
        # enforces "global flags before the subcommand".
        known = _option_names(command)
        index = 0
        while index < len(tokens) and _is_flag_token(tokens[index]):
            flag, _, inline_value = tokens[index].partition("=")
            if flag not in known:
                return [f"{' '.join(path)}: unknown global option {flag} (global flags go before the subcommand)"]
            index += 1
            if not inline_value and _takes_value(command, flag):
                index += 1
        tokens = tokens[index:]
        if not tokens:
            return []
        name, tokens = tokens[0], tokens[1:]
        if name not in children:
            return [f"{' '.join(path)}: unknown subcommand {name!r}"]
        if path == ["warcraft"] and name in PASSTHROUGH_COMMANDS:
            return _resolve(name, tokens)
        path.append(name)
        command = children[name]


def _violations(path: Path) -> list[str]:
    problems: list[str] = []
    for example in _shell_examples(path.read_text()):
        if any(operator in example.text for operator in SHELL_OPERATORS):
            continue
        try:
            tokens = shlex.split(example.text)
        except ValueError:
            continue
        if not tokens or tokens[0] not in CLI_APPS:
            continue
        problems.extend(f"line {example.line_no}: {example.text!r} -> {reason}" for reason in _resolve(tokens[0], tokens[1:]))
    return problems


@pytest.mark.parametrize("doc", DOC_FILES, ids=DOC_IDS)
def test_documented_commands_and_flags_exist(doc: Path) -> None:
    problems = _violations(doc)
    assert not problems, f"{doc.relative_to(REPO_ROOT)} documents commands the CLIs do not have:\n" + "\n".join(problems)


@pytest.mark.parametrize("doc", DOC_FILES, ids=DOC_IDS)
def test_docs_use_the_binary_name_not_the_distribution_name(doc: Path) -> None:
    """``wowhead-cli`` is the package; ``wowhead`` is the command agents type."""
    offenders = [
        f"line {line_no}: {line.strip()!r}"
        for line_no, line in enumerate(doc.read_text().splitlines(), start=1)
        if DISTRIBUTION_NAME_MISUSE.search(line)
    ]
    assert not offenders, f"{doc.relative_to(REPO_ROOT)} uses 'wowhead-cli' where it means the 'wowhead' command:\n" + "\n".join(
        offenders
    )
