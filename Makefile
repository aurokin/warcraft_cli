VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
RUFF := $(VENV)/bin/ruff
MYPY := $(VENV)/bin/mypy
RADON := $(VENV)/bin/radon
VULTURE := $(VENV)/bin/vulture
WOWHEAD := $(VENV)/bin/wowhead
WARCRAFT := $(VENV)/bin/warcraft
METHOD := $(VENV)/bin/method
RAIDERIO := $(VENV)/bin/raiderio
WARCRAFT_WIKI := $(VENV)/bin/warcraft-wiki
WOWPROGRESS := $(VENV)/bin/wowprogress
SIMC := $(VENV)/bin/simc
LINT_PATHS := packages/warcraft-core packages/warcraft-api packages/warcraft-content
LINT_ALL_PATHS := packages tests scripts
LIVE_TEST_ENV := \
	WOWHEAD_LIVE_TESTS=1 \
	METHOD_LIVE_TESTS=1 \
	ICY_VEINS_LIVE_TESTS=1 \
	RAIDERIO_LIVE_TESTS=1 \
	WARCRAFT_WIKI_LIVE_TESTS=1 \
	WOWPROGRESS_LIVE_TESTS=1 \
	WARCRAFTLOGS_LIVE_TESTS=1 \
	WARCRAFT_WRAPPER_LIVE_TESTS=1

.PHONY: dev-deploy dev-deploy-no-link worktree-env test test-live fmt-check lint lint-all complexity typecheck coverage deadcode run release

dev-deploy:
	./scripts/dev_deploy.sh

dev-deploy-no-link:
	./scripts/dev_deploy.sh --no-link-bin

worktree-env:
	./scripts/setup_worktree_env.sh

test:
	$(PYTEST) -q

test-live:
	$(LIVE_TEST_ENV) $(PYTEST) -q -m live

fmt-check:
	$(PYTHON) -m compileall -q packages

lint:
	$(RUFF) check $(LINT_PATHS)

lint-all:
	@status=0; \
	$(RUFF) check $(LINT_ALL_PATHS) || status=$$?; \
	if [ $$status -eq 1 ]; then \
		echo "lint-all is report-only: keeping the existing full-repo Ruff backlog visible without failing the target."; \
	elif [ $$status -ne 0 ]; then \
		exit $$status; \
	fi

complexity:
	$(PYTHON) -m radon cc packages -s -a
	$(PYTHON) -m radon mi packages -s

typecheck:
	$(MYPY)

coverage:
	@if $(PYTHON) -c 'import sqlite3' >/dev/null 2>&1 && $(PYTHON) -m pip show pytest-cov >/dev/null 2>&1; then \
		$(PYTHON) -m pytest -q \
			--cov=packages/warcraft-core/src/warcraft_core \
			--cov=packages/warcraft-api/src/warcraft_api \
			--cov=packages/warcraft-content/src/warcraft_content \
			--cov-report=term-missing; \
	else \
		echo "Coverage fallback: using stdlib trace because sqlite3 and/or pytest-cov is unavailable."; \
		$(PYTHON) scripts/trace_coverage.py; \
	fi

deadcode:
	$(VULTURE) packages scripts tests --min-confidence 80

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
	@echo "  3. git diff && git add CHANGELOG.md pyproject.toml packages/*/pyproject.toml"
	@echo "  4. git commit -m 'Release v$(VERSION)' && git push"
	@echo "  5. git tag v$(VERSION) && git push origin v$(VERSION)"
	@echo "  6. gh release create v$(VERSION) --notes-file <changelog-section>"
