"""Guards for the generated CLI reference under docs/reference/.

The reference is the single source of flag/default/help truth for agents, so a command surface
change that skips `make reference` must fail the fast suite rather than drift silently.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = REPO_ROOT / "docs" / "reference"


def _generator() -> ModuleType:
    scripts_dir = str(REPO_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    return importlib.import_module("generate_command_reference")


def test_reference_docs_are_current() -> None:
    generator = _generator()
    for path, expected in generator.generate(REFERENCE_DIR).items():
        assert path.exists(), f"{path} is missing. Run `make reference`."
        assert path.read_text(encoding="utf-8") == expected, f"{path} is stale. Run `make reference`."


def test_every_console_script_has_a_reference_file() -> None:
    generator = _generator()
    for name in generator.console_scripts():
        assert (REFERENCE_DIR / f"{name}.md").exists(), f"docs/reference/{name}.md is missing. Run `make reference`."


def test_every_command_has_help() -> None:
    """Every command, argument, and option an agent can reach must document itself."""
    generator = _generator()
    missing: list[str] = []

    def visit(path: str, command: object) -> None:
        if generator._is_group(command):
            if not generator._command_help(command):
                missing.append(f"{path} (group help)")
            for name, child in generator._subcommands(command):
                visit(f"{path} {name}", child)
            return
        if not generator._command_help(command):
            missing.append(f"{path} (command help)")
        arguments, options = generator._split_params(command)
        missing.extend(f"{path} argument {arg.name}" for arg in arguments if not getattr(arg, "help", None))
        missing.extend(f"{path} option {'/'.join(opt.opts)}" for opt in options if not opt.help)

    for script_name, entry_point in generator.console_scripts().items():
        group = generator._load_group(entry_point)
        if not generator._command_help(group):
            missing.append(f"{script_name} (root help)")
        _, global_options = generator._split_params(group)
        missing.extend(
            f"{script_name} global option {'/'.join(opt.opts)}" for opt in global_options if not opt.help
        )
        for name, command in generator._subcommands(group):
            if generator._is_passthrough(command):
                continue
            visit(f"{script_name} {name}", command)

    assert not missing, "Commands/arguments/options without help text:\n" + "\n".join(missing)
