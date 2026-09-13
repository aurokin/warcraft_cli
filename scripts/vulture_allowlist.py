# Vulture allowlist for `make deadcode` (vulture --min-confidence 80).
#
# Every entry below is a *signature* name that the language or a test double forces us to
# declare but never reads: `__exit__(exc_type, exc, tb)` context managers, and monkeypatched
# stand-ins that must accept the real callee's parameters. Vulture matches these by name, so
# one entry covers every occurrence. Regenerate the raw list with:
#   .venv/bin/vulture packages scripts tests --min-confidence 80 --make-whitelist
#
# This file is never imported; the bare names exist only for vulture to bind.
# ruff: noqa: F821, B018

a  # unused signature parameter (tests/test_simc_cli.py, tests/test_warcraftlogs_cli.py)
capture_output  # unused signature parameter (tests/test_simc_repo.py)
check  # unused signature parameter (tests/test_simc_repo.py)
exc_type  # unused signature parameter (every provider client __exit__)
k  # unused signature parameter (tests/test_warcraftlogs_cli.py)
kw  # unused signature parameter (tests/test_simc_cli.py)
s  # unused signature parameter (tests/test_simc_cli.py)
tb  # unused signature parameter (every provider client __exit__)
verifier  # unused signature parameter (tests/test_warcraftlogs_cli.py)
