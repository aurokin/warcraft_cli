# warcraft

A monorepo of World of Warcraft data CLIs built for AI agents. The `warcraft` wrapper routes and composes; each provider binary speaks the same JSON envelope, the same exit codes, and the same global output flags, so an agent can compose guides, reference content, rankings, logs, and local SimulationCraft analysis without learning a new output shape per source. See [docs/foundation/PRODUCT_PRINCIPLES.md](docs/foundation/PRODUCT_PRINCIPLES.md) for what the repo will and will not do.

## Install

```bash
# release wheel — attached to each GitHub release from the first release after the release workflow lands
pipx install https://github.com/aurokin/warcraft_cli/releases/download/v0.5.0/warcraft-0.5.0-py3-none-any.whl
uvx --from https://github.com/aurokin/warcraft_cli/releases/download/v0.5.0/warcraft-0.5.0-py3-none-any.whl warcraft doctor
# from a checkout (editable)
uv sync --all-extras     # or: make install, or: pip install -e '.[dev,redis]'
make dev-deploy-no-link  # refresh the checkout-local .venv for branch work
```

## Providers

| Command | Tier | Best for | Docs |
|---------|------|----------|------|
| `warcraft` | core | routing, discovery, cross-provider composition | [docs/warcraft](docs/warcraft/README.md) |
| `wowhead` | core | entities, guides, comments, talent calc | [docs/wowhead](docs/wowhead/README.md) |
| `warcraftlogs` | core | official log/report analysis (OAuth) | [docs/warcraftlogs](docs/warcraftlogs/README.md) |
| `simc` | core | local SimulationCraft inspection and runs | [docs/simc](docs/simc/README.md) |
| `raiderio` | supported | character/guild profiles, Mythic+ analytics | [docs/raiderio](docs/raiderio/README.md) |
| `warcraft-wiki` | supported | reference, lore, API/event articles | [docs/warcraft-wiki](docs/warcraft-wiki/README.md) |
| `icy-veins` | supported | guide extraction and local guide query | [docs/icy-veins](docs/icy-veins/README.md) |
| `method` | supported | guide extraction and local guide query | [docs/method](docs/method/README.md) |
| `lorrgs` | experimental | top-parse cooldown timelines, comp rankings | [docs/lorrgs](docs/lorrgs/README.md) |
| `raidbots` | experimental | public report consumption, SimC handoff | [docs/raidbots](docs/raidbots/README.md) |
| `blizzard` | experimental (verified live for us/eu/kr/tw) | Battle.net Game Data and Profile reads | [docs/blizzard-api](docs/blizzard-api/README.md) |
| `curseforge` | experimental (verified live) | addon metadata and changelogs | [docs/curseforge](docs/curseforge/README.md) |

## Quick Start

```bash
warcraft doctor  # start here when the source is unclear; call a provider binary directly once you know it
warcraft --pretty resolve "thunderfury"
warcraft search "defias"
wowhead entity item 19019
```

## Docs

- [docs/README.md](docs/README.md) (map) · [docs/USAGE.md](docs/USAGE.md) (workflows) · [docs/reference/README.md](docs/reference/README.md) (generated per-command flags)
- [docs/foundation/ERROR_CONTRACT.md](docs/foundation/ERROR_CONTRACT.md) — envelope, exit codes, and the global flags (`--pretty`, `--compact`, `--fields`, `--fields-strict`, `--profile`) that go before the subcommand
- [docs/ROADMAP.md](docs/ROADMAP.md) · [CHANGELOG.md](CHANGELOG.md)

## Development

```bash
make check       # lint, typecheck, import boundaries, complexity, dead code, fast tests
make test-live   # opt-in live provider suites (network; per-suite flags in docs/USAGE.md)
make reference   # regenerate docs/reference/; make skills regenerates provider subskills
```
