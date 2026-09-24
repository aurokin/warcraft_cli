# Vulture allowlist for `make deadcode` (vulture over packages/ and scripts/, --min-confidence 60).
#
# Typer commands and callbacks are excluded in the Makefile with --ignore-decorators. Each name
# below is used by something vulture cannot see. vulture matches by name, so one entry covers
# every occurrence. tests/ is not scanned, so code only a test uses is reported as dead.
#
# This file is never imported; the bare names exist only for vulture to bind.
# ruff: noqa: F821, B018

# Called by the Makefile's `schema` target.
envelope_schema_document

# Context-manager __exit__(exc_type, exc, tb) parameters the protocol forces on us.
exc_type
tb

# TypedDict keys: callers read them by subscript, which vulture cannot see.
schema_version
