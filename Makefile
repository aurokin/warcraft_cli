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

IMPORT_LINTER := $(VENV)/bin/lint-imports
PRE_COMMIT := $(VENV)/bin/pre-commit

.PHONY: install dev-deploy dev-deploy-no-link worktree-env test test-fast test-e2e test-canary \
	check lint lint-boundaries lint-all complexity complexity-gate typecheck coverage deadcode \
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
# credentials in ~/.config/warcraft/providers. CI runs only the keyless journey files, weekly
# (.github/workflows/live-contracts.yml). Exclude providers with
# WARCRAFT_E2E_SKIP=curseforge; pass extra pytest args with E2E_ARGS="-k wowhead".
test-e2e:
	WARCRAFT_E2E=1 $(PYTEST) -q -m e2e tests/e2e --durations=25 $(E2E_ARGS)

check: lint typecheck lint-boundaries complexity-gate deadcode coverage

# The one live test outside tests/e2e: pinned Wowhead pages through the parsers (weekly in CI).
test-canary:
	WOWHEAD_LIVE_TESTS=1 $(PYTEST) -q -m live tests/test_wowhead_parser_canaries.py

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

# The fast suite with coverage. The floor is the total measured in a clean environment (empty HOME,
# no SimC checkout, as in CI; 91.8% on 2026-09-24) rounded down, so a drop fails; raise it when
# coverage rises. -rs lists the skipped tests, such as the opt-in real-binary SimC tests.
coverage:
	$(PYTEST) -q -rs -m "not live and not e2e" --cov=packages --cov-report=term-missing --cov-fail-under=91

# tests/ is not scanned, so production code only a test uses counts as dead. The allowlist
# (scripts/vulture_allowlist.py) is picked up with scripts/.
deadcode:
	$(VULTURE) packages scripts --min-confidence 60 --ignore-decorators "@*.command,@*.callback"

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
