VENV := .venv
PYTHON := $(VENV)/bin/python
PYTEST := $(VENV)/bin/pytest
RUFF := $(VENV)/bin/ruff
MYPY := $(VENV)/bin/mypy
RADON := $(VENV)/bin/radon
XENON := $(VENV)/bin/xenon
VULTURE := $(VENV)/bin/vulture
WOWHEAD := $(VENV)/bin/wowhead
UV ?= uv
LINT_PATHS := packages tests scripts
# Keep in sync with LIVE_TEST_ENV_BY_FILE in tests/conftest.py
# (tests/test_repo_tooling.py::test_makefile_live_env_matches_conftest_registry enforces it).
LIVE_TEST_ENV := \
	WOWHEAD_LIVE_TESTS=1 \
	METHOD_LIVE_TESTS=1 \
	ICY_VEINS_LIVE_TESTS=1 \
	RAIDERIO_LIVE_TESTS=1 \
	WARCRAFT_WIKI_LIVE_TESTS=1 \
	WOWPROGRESS_LIVE_TESTS=1 \
	WARCRAFTLOGS_LIVE_TESTS=1 \
	RAIDBOTS_LIVE_TESTS=1 \
	LORRGS_LIVE_TESTS=1 \
	BLIZZARD_LIVE_TESTS=1 \
	CURSEFORGE_LIVE_TESTS=1 \
	WARCRAFT_WRAPPER_LIVE_TESTS=1

IMPORT_LINTER := $(VENV)/bin/lint-imports
PRE_COMMIT := $(VENV)/bin/pre-commit

.PHONY: install dev-deploy dev-deploy-no-link worktree-env test test-fast test-e2e test-live test-live-matrix \
	check fmt-check lint lint-boundaries lint-all complexity complexity-gate typecheck coverage deadcode \
	skills reference schema build pre-commit-install benchmark-cache fixture-refresh-hints run release

install:
	$(UV) sync --all-extras

dev-deploy:
	./scripts/dev_deploy.sh

dev-deploy-no-link:
	./scripts/dev_deploy.sh --no-link-bin

worktree-env:
	./scripts/setup_worktree_env.sh

test:
	$(PYTEST) -q

test-fast:
	$(PYTEST) -q -m "not live and not e2e"

# Local end-to-end journeys through the installed binaries against real providers, using the
# credentials in ~/.config/warcraft/providers. Never runs in CI. Exclude providers with
# WARCRAFT_E2E_SKIP=curseforge,wowprogress; pass extra pytest args with E2E_ARGS="-k wowhead".
test-e2e:
	WARCRAFT_E2E=1 $(PYTEST) -q -m e2e tests/e2e --durations=25 $(E2E_ARGS)

check: lint typecheck lint-boundaries complexity-gate deadcode test-fast

test-live:
	$(LIVE_TEST_ENV) $(PYTEST) -q -m live

test-live-matrix:
	WARCRAFTLOGS_LIVE_TESTS=1 $(PYTEST) -q -m live tests/test_live_command_matrix.py

fmt-check:
	$(PYTHON) -m compileall -q packages

lint:
	$(RUFF) check $(LINT_PATHS)

lint-boundaries:
	$(IMPORT_LINTER)

lint-all: lint

complexity:
	$(RADON) cc packages -s -a
	$(RADON) mi packages -s

complexity-gate:
	$(XENON) --max-absolute C packages

typecheck:
	$(MYPY)

coverage:
	@if $(PYTHON) -c 'import sqlite3, pytest_cov' >/dev/null 2>&1; then \
		$(PYTEST) -q -m "not live" --cov=packages --cov-report=term-missing; \
	else \
		echo "Coverage fallback: using stdlib trace because sqlite3 and/or pytest-cov is unavailable."; \
		$(PYTHON) scripts/trace_coverage.py; \
	fi

deadcode:
	$(VULTURE) packages scripts tests scripts/vulture_allowlist.py --min-confidence 80

skills:
	$(PYTHON) scripts/generate_provider_skills.py

reference:
	$(PYTHON) scripts/generate_command_reference.py

schema:
	$(PYTHON) -c "import pathlib; from warcraft_cli.schema import envelope_schema_document; pathlib.Path('schemas/envelope.schema.json').write_text(envelope_schema_document())"

build:
	$(UV) build --wheel

pre-commit-install:
	$(PRE_COMMIT) install

benchmark-cache:
	$(PYTHON) scripts/benchmark_wowhead_cache.py $(ARGS)

fixture-refresh-hints:
	$(PYTHON) scripts/fixture_refresh_hints.py $(ARGS)

run:
	@if [ -z "$(ARGS)" ]; then \
		echo 'Usage: make run ARGS="search defias"'; \
		exit 2; \
	fi
	$(WOWHEAD) $(ARGS)

release:
	@if [ -z "$(VERSION)" ]; then \
		echo 'Usage: make release VERSION=X.Y.Z'; \
		exit 2; \
	fi
	$(PYTHON) scripts/bump_version.py $(VERSION)
	@echo ""
	@echo "Next steps:"
	@echo "  1. Move [Unreleased] content into [$(VERSION)] - $$(date -u +%Y-%m-%d) in CHANGELOG.md"
	@echo "  2. Update the compare links at the bottom of CHANGELOG.md"
	@echo "  3. Refresh uv.lock (uv lock) if dependencies changed, and update docs/ROADMAP.md"
	@echo "  4. Update the wheel URL version in README.md"
	@echo "  5. git diff && git add CHANGELOG.md README.md uv.lock pyproject.toml packages/*/pyproject.toml"
	@echo "  6. git commit -m 'Release v$(VERSION)' && git push"
	@echo "  7. git tag v$(VERSION) && git push origin v$(VERSION)"
	@echo "     (the tag push triggers .github/workflows/release.yml, which builds the wheel"
	@echo "      and attaches it to the GitHub release)"
	@echo "  8. gh release create v$(VERSION) --notes-file <changelog-section>"
