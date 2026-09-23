# CurseForge CLI

`curseforge` is an **experimental** provider whose host, `x-api-key` auth, mod search, mod lookup,
and changelog endpoints were confirmed against the live API on 2026-09-13, so payloads carry
`provenance.verified: true`. `doctor` reports `tier: experimental` because the command surface is
thin (four commands) and addon metadata sits at the edge of the product's scope. Core API keys
without search access can only resolve numeric mod ids; the slug path then fails with
`auth_failed` and a message pointing at the numeric form.

Design history and the go/no-go record live in
[docs/architecture/history/curseforge.md](../architecture/history/curseforge.md).

## Auth

Commands that call the API need `CURSEFORGE_API_KEY` (a static `x-api-key` header). Discovery order,
highest first:

1. repo `.env.local`
2. `~/.config/warcraft/providers/curseforge.env`
3. process environment

`doctor` reports whether a key is configured and which source supplied it; it never prints the key.

## Global Flags

Every command accepts the shared output flags, which go **before** the subcommand:
`--pretty`, `--compact`, `--compact-max-chars <n>`, `--fields <dot.path>`, `--fields-strict`,
`--profile agent|human|debug`.

## Commands

### `curseforge doctor`

Reports install state, API-key auth posture (`api_key` flow, `CURSEFORGE_API_KEY`, credential source
and lookup order), the `experimental` tier, and capability metadata: `doctor` and `addon` are
`ready`, `search` and `resolve` are `coming_soon`.

### `curseforge addon <slug-or-id>`

Resolves one WoW addon and returns its metadata, latest files, and latest changelog.

- A numeric argument is a mod id, validated to be a WoW project via `gameId`. Anything else is a
  `gameId=1` slug search whose result is matched to the exact slug client-side, so an ignored or
  renamed server-side filter can never bind the wrong mod.
- `data.metadata` is the raw CurseForge mod record, `data.latest_files` is its `latestFiles`, and
  `data.changelog` covers the newest file.
- Changelog is best-effort and never fails the lookup. Top-level `null` means the addon has no
  files. Otherwise it is an object keyed by `file_id` carrying `body` (changelog HTML, or `null`
  when that file exposes no notes) plus `source_url`, or an explicit `{file_id, error}` marker when
  that one request fails. Detect empty notes via `changelog.body`, not `changelog is null`.
- `provenance` carries `game_id`, `mod_id`, `slug`, `resolved_by`, `source_urls`, `verified: true`,
  and `verification_note`.

### `curseforge search <query>` and `curseforge resolve <query>`

Not implemented. Both accept `--limit <1-50>` (currently unused) and return a structured
`kind: coming_soon` envelope with `ok: true`, empty `results`, and a `suggested_command`, so probing
them is a stable contract rather than a Click "no such command" error.

## Output And Exit Codes

Every command emits the shared envelope (`ok`, `provider`, `command`, `kind`, `schema_version`,
`query`, `provenance`, `data`) on stdout, and nothing else at the top level. Failures write
`{ok: false, ..., error: {code, message}}` to stderr and exit nonzero — never a traceback.

| Error code | Exit |
|---|---|
| `missing_api_key`, `auth_failed` | 3 |
| `addon_not_found` | 4 |
| `http_error`, `network_error` | 5 |
| `invalid_response` | 1 |

See [ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md) for the shared vocabulary.

## Wrapper Routing

`warcraft curseforge ...` routes to this provider. It registers with `expansion_mode = "none"` —
addon game-version compatibility lives inside file records, not the wrapper's expansion axis — so it
stays out of expansion fanout (like `blizzard-api` and `simc`).

```
warcraft curseforge doctor
warcraft curseforge addon deadly-boss-mods
```

## Re-verifying

```
make test-e2e E2E_ARGS="tests/e2e/test_curseforge.py"
```

with a real `CURSEFORGE_API_KEY`. The end-to-end journey covers both resolution paths (numeric id
and slug search) plus `latest-files` and `changelog`.

## Analytics And Provenance Posture

Per [SAFE_ANALYTICS_RULES.md](../foundation/SAFE_ANALYTICS_RULES.md): raw source identifiers and
URLs (addon id, slug, project/file URLs) stay alongside any normalized output, normalization is
additive, and the CLI does not synthesize "best addon" answers.

## Source Links

- `https://www.curseforge.com/wow/addons`
- [Roadmap](../ROADMAP.md)
