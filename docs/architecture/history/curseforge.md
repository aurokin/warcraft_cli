# CurseForge Provider — Design Record

Historical record of the go/no-go decision and the scaffold slice. For what the CLI does today, see
[docs/curseforge/README.md](../../curseforge/README.md).

## Go/No-Go (AUR-395 → AUR-499)

The [AUR-395](https://linear.app/aurokin/issue/AUR-395) gate — *revisit only when wrapper, official
API (Blizzard), and evidence-oriented analytics work are in a stronger place* — was met by wrapper
routing/doctor/registration (AUR-384), the official Blizzard Game Data/Profile slice (AUR-455), and
the evidence/trust analytics passes (AUR-386/387/388). CurseForge cleared go/no-go because it has a
documented public API, a read/metadata-first first slice needing no write or user-auth flows, and it
complements `warcraft-wiki`: the wiki gives programming/usage context while CurseForge gives the
packaged addon surface users install.

The scaffold shipped under
[AUR-499 — CurseForge provider scaffold (doctor + addon-lookup workflow)](https://linear.app/aurokin/issue/AUR-499):
package, wrapper registration, `doctor`, and addon lookup by slug or mod id.

## Shared vs Provider-Specific

Shared: output/error shaping, cache and HTTP primitives, the wrapper search/resolve/doctor contract.

Provider-specific: the CurseForge API client, addon/file/changelog parsing, and any future
compatibility/version normalization.

## Deferred

- dependency graph, release/channel filtering, game-version compatibility, file-download workflows
- `search` / `resolve` beyond the `coming_soon` stub
- any write or user-auth flows
- live verification of host, endpoints, and response shapes
