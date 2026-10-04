# warcraft

A monorepo of World of Warcraft data CLIs built for AI agents. The `warcraft` wrapper routes and composes; each provider binary speaks the same JSON envelope, the same exit codes, and the same global output flags, so an agent can compose guides, reference content, rankings, logs, and local SimulationCraft analysis without learning a new output shape per source. See [docs/foundation/PRODUCT_PRINCIPLES.md](docs/foundation/PRODUCT_PRINCIPLES.md) for what the repo will and will not do.

## Install

```bash
git clone https://github.com/aurokin/warcraft_cli && cd warcraft_cli
uv sync --all-extras     # or: make install, or: pip install -e '.[dev,redis]'
make dev-deploy-no-link  # refresh the checkout-local .venv for branch work
source .warcraft/worktree-env.sh
```

The release workflow attaches a wheel to the [GitHub release](https://github.com/aurokin/warcraft_cli/releases)
after checks pass. Use the published asset URL with `pipx install <wheel-url>` or
`uvx --from <wheel-url> warcraft doctor`; v0.5.0 and earlier have no wheel.

## Providers

| Command | Tier | Best for | Docs |
|---------|------|----------|------|
| `warcraft` | core | routing, discovery, cross-provider composition | [docs/warcraft](docs/warcraft/README.md) |
| `wowhead` | core | entities, guides, comments, talent calc | [docs/wowhead](docs/wowhead/README.md) |
| `warcraftlogs` | core | official log/report analysis (OAuth) | [docs/warcraftlogs](docs/warcraftlogs/README.md) |
| `simc` | core | local SimulationCraft inspection and runs | [docs/simc](docs/simc/README.md) |
| `raiderio` | supported | character/guild profiles, Mythic+ analytics | [docs/raiderio](docs/raiderio/README.md) |
| `warcraft-wiki` | supported | reference, lore, API/event articles | [docs/warcraft-wiki](docs/warcraft-wiki/README.md) |
| `icy-veins` | supported | guides, local queries, calculator build imports | [docs/icy-veins](docs/icy-veins/README.md) |
| `method` | supported | guide extraction and local guide query | [docs/method](docs/method/README.md) |
| `lorrgs` | supported | top-parse cooldown timelines, comp rankings | [docs/lorrgs](docs/lorrgs/README.md) |
| `raidbots` | experimental | public report consumption, SimC handoff | [docs/raidbots](docs/raidbots/README.md) |
| `blizzard` | experimental (verified live for us/eu/kr/tw) | official profiles, PvP, collections and market snapshots | [docs/blizzard-api](docs/blizzard-api/README.md) |
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
make check       # lint, typecheck, import boundaries, complexity, dead code, fast tests + coverage floor
make test-e2e    # end-to-end journeys against the real providers (network; docs/architecture/E2E_TESTING.md)
make reference   # regenerate docs/reference/; make skills regenerates provider subskills
```
