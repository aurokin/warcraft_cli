# Vulture allowlist for `make deadcode` (vulture --min-confidence 60).
#
# Typer commands/callbacks and pytest fixtures are excluded in the Makefile with
# --ignore-decorators, so they need no entry here. Each name below is read by a framework,
# not by our code; vulture matches by name, so one entry covers every occurrence.
#
# This file is never imported; the bare names exist only for vulture to bind.
# ruff: noqa: F821, B018

# pytest reads these module-level names and hook functions by convention.
pytestmark
pytest_collection_modifyitems

# Attributes we set for a library to read: unittest.mock (on a Mock) and shlex (on a lexer).
return_value
side_effect
whitespace_split

# Signature parameters the caller forces on us but the body never reads: context-manager
# __exit__(exc_type, exc, tb), and monkeypatched test stand-ins that must accept the real
# callee's arguments (tests/test_simc_cli.py, tests/test_simc_repo.py, tests/test_warcraftlogs_cli.py).
exc_type
tb
a
kw
capture_output
verifier

# TypedDict keys: callers read them by subscript, which vulture cannot see.
schema_version
