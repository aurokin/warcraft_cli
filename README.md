# warcraft

Warcraft data CLI monorepo.

See [CHANGELOG.md](CHANGELOG.md) for what shipped in each release.

## Principles

- These CLIs exist to give agents a comprehensive World of Warcraft toolset across guides, reference content, rankings, logs, and local simulation workflows.
- The product goal is to make complex WoW questions easier for agents by returning accurate, structured, well-formatted data with clear provenance.
- When a workflow is not supported, the CLIs should fail clearly instead of pretending to answer it.
- Prefer trustworthy building blocks that agents can compose:
  - normalization
  - sampling
  - aggregation
  - provenance
  - freshness
- Normalization is an additive analysis layer. It should improve routing and comparison without replacing raw source detail.
- Do not bolt on fake universal "smart answers" where the underlying source contract is narrower than the question.

## Install

```bash
# branch-local editable environment
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

# optional: install Redis cache support
pip install -e '.[dev,redis]'
```

## Local Editable Deploy

```bash
# setup/update the current checkout as an editable branch-local environment
make dev-deploy-no-link

# optional: regenerate the shell env activation file for this worktree
make worktree-env

# optional: add the worktree venv to PATH in this shell
source .warcraft/worktree-env.sh

# deliberate exception: relink ~/.local/bin to this checkout
WARCRAFT_ALLOW_LINK_BIN=1 make dev-deploy
```

This project uses editable install mode (`pip install -e`) for local development, so code changes are immediately reflected without rebuilding.
Use `make dev-deploy-no-link` for normal branch work. It updates the checkout-local `.venv` and writes `.warcraft/worktree-env.sh` without rewriting host-level command wrappers.
The generated worktree env keeps credentials in the shared host config/state roots and isolates branch-local data/cache under `.warcraft/runtime/`.
Use `make worktree-env` whenever you want to regenerate or refresh that shell activation file explicitly.
Worktree creation and trunk hygiene are intentionally outside this repo and should be handled with `worktrunk`.
If `wowhead` is not found, add `~/.local/bin` to your `PATH`.

## Supported Providers

- [`warcraft`](docs/warcraft/README.md): root wrapper and orchestration CLI
- [`wowhead`](docs/wowhead/README.md): entity, guide, comments, and bundle workflows
- [`method`](docs/method/README.md): guide extraction and local guide workflows
- [`icy-veins`](docs/icy-veins/README.md): guide extraction and local guide workflows
- [`raiderio`](docs/raiderio/README.md): API-backed profile and leaderboard workflows
- [`warcraft-wiki`](docs/warcraft-wiki/README.md): reference, lore, and API/article workflows
- [`wowprogress`](docs/wowprogress/README.md): rankings and profile workflows
- [`warcraftlogs`](docs/warcraftlogs/README.md): official log/report workflows
- [`simc`](docs/simc/README.md): local SimulationCraft inspection and analysis workflows

## Quick Start

```bash
warcraft doctor
warcraft --pretty search "defias"
warcraft search "defias"
warcraft guide-compare-query "mistweaver monk guide"
warcraft warcraftlogs resolve "https://www.warcraftlogs.com/reports/abcd1234#fight=3"
warcraftlogs report-encounter abcd1234 --fight-id 3
simc analysis-packet <simc-root>/ActionPriorityLists/default/monk_mistweaver.simc --targets 1
```

Use `warcraft` when the source is unclear. Use `wowhead`, `method`, `icy-veins`, `raiderio`, `warcraft-wiki`, `wowprogress`, `warcraftlogs`, or `simc` directly once you know the provider you need.

## Docs

- [docs/README.md](docs/README.md)
- [docs/USAGE.md](docs/USAGE.md)
- [docs/ROADMAP.md](docs/ROADMAP.md)
- [docs/foundation/PRODUCT_PRINCIPLES.md](docs/foundation/PRODUCT_PRINCIPLES.md)
- [docs/foundation/SAFE_ANALYTICS_RULES.md](docs/foundation/SAFE_ANALYTICS_RULES.md)

## Testing

```bash
# fast local suite (fixture + unit tests)
pytest -q

# live contract checks against every opted-in provider suite
make test-live

# one provider at a time
WOWHEAD_LIVE_TESTS=1 pytest -q -m live tests/test_live_integration.py tests/test_live_endpoint_contracts.py
METHOD_LIVE_TESTS=1 pytest -q -m live tests/test_method_live.py
ICY_VEINS_LIVE_TESTS=1 pytest -q -m live tests/test_icy_veins_live.py
RAIDERIO_LIVE_TESTS=1 pytest -q -m live tests/test_raiderio_live.py
WARCRAFT_WIKI_LIVE_TESTS=1 pytest -q -m live tests/test_warcraft_wiki_live.py
WOWPROGRESS_LIVE_TESTS=1 pytest -q -m live tests/test_wowprogress_live.py
WARCRAFTLOGS_LIVE_TESTS=1 pytest -q -m live tests/test_warcraftlogs_live.py
WARCRAFT_WRAPPER_LIVE_TESTS=1 pytest -q -m live tests/test_warcraft_wrapper_live.py
```

Wowhead live checks can be run manually in GitHub Actions via `.github/workflows/live-wowhead-contracts.yml` (`workflow_dispatch`).
Wowhead live coverage includes mixed entity-type (`item`, `quest`, `npc`, `spell`) contracts and cross-entity compare checks.

## Health Checks

```bash
make fmt-check
make lint
make lint-all
make complexity
make typecheck
make coverage
```

Notes:
- `make lint-all` is report-only and keeps the current full-repo Ruff backlog visible without blocking local work.
- `make complexity` runs `radon` via `python -m` so stale console-script wrappers do not break the report.
- `make coverage` prefers `pytest-cov` when available and falls back to stdlib `trace` coverage for the shared packages when the active Python build lacks `sqlite3`.
