# CurseForge CLI

`curseforge` is an **experimental** provider whose host, `x-api-key` auth, mod search, mod lookup,
and changelog endpoints were confirmed against the live API on 2026-09-13, so payloads carry
`provenance.verified: true`. `doctor` reports `tier: experimental` because the command surface is
thin (addon lookup and doctor) and addon metadata sits at the edge of the product's scope. Core API keys
without search access can only resolve numeric mod ids; the slug path then fails with
`auth_failed` and a message pointing at the numeric form.

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
`--profile agent|human`.

## Commands

### `curseforge doctor`

Reports install state, API-key auth posture (`api_key` flow, `CURSEFORGE_API_KEY`, credential source
and lookup order), the `experimental` tier, the cache configuration, and capability metadata:
`doctor` is `ready`, `addon` is `ready` with a key and `requires_api_key` without one, and `search`
and `resolve` are `not_supported`. `status` is `ready` with a key and `degraded` without one, or when the cache config does not parse or a Redis cache backend does not
answer (`cache.available: false`; an unparsable config also carries `cache.error.code: "invalid_cache_config"`).

### `curseforge addon <slug-or-id>`

Resolves one WoW addon and returns its metadata, latest files, and latest changelog.

- A numeric argument is a mod id, validated to be a WoW project via `gameId`. Anything else is a
  `gameId=1` slug search whose result is matched to the exact slug client-side, so an ignored or
  renamed server-side filter can never bind the wrong mod.
- A slug with no exact match (`weakauras`, whose slug is `weakauras-2`) is `addon_not_found` (exit 4)
  whose `error.details.candidates` lists up to five addons a name search finds for it, most popular
  first (`slug`, `id`, `name`), and whose message names their slugs; retry with
  `curseforge addon <slug>`. The candidates are best effort and empty when that search fails.
- `data.metadata` is the raw CurseForge mod record. `data.latest_files` is its `latestFiles` sorted
  newest first by `fileDate`; CurseForge's own order (kept in `metadata.latestFiles`) can put
  years-old betas first. The list mixes game flavors and release types (`releaseType` 1 release,
  2 beta, 3 alpha), so the current stable version is the first row with `releaseType` 1 for the
  flavor you want (`gameVersions`), not row 0.
- `data.changelog` covers the newest file by date, which can be an alpha or beta: its `display_name`
  and `release_type` say which file the notes belong to.
- Changelog is best-effort and never fails the lookup. Top-level `null` means the addon has no
  files. Otherwise it is an object keyed by `file_id` carrying `body` (changelog HTML, or `null`
  when that file exposes no notes) plus `source_url`, or an explicit `{file_id, error}` marker when
  that one request fails. Every form also carries `display_name` and `release_type`. Detect empty notes via `changelog.body`, not `changelog is null`.
- `provenance` carries `game_id`, `mod_id`, `slug`, `resolved_by`, `source_urls`, `verified: true`,
  `verification_note`, and the shared `cache` block over the lookup's responses (see
  [USAGE.md](../USAGE.md#reading-cache-state-provenancecache)).
- Responses are cached on disk under the XDG cache root (`curseforge/http`) for an hour, keyed on
  path and query, never on the key, because the API key is rate-limited. Override with
  `CURSEFORGE_CACHE_TTL_SECONDS`, `CURSEFORGE_CACHE_DIR`, or `CURSEFORGE_CACHE_BACKEND=file|redis|none`
  (Redis takes `CURSEFORGE_REDIS_URL` and `CURSEFORGE_REDIS_PREFIX`).

### `curseforge search <query>` and `curseforge resolve <query>`

Both fail with `unsupported_operation` (exit 2), with an explicit `addon` lookup as the
available operation. `--limit` remains a compatibility parameter. The wrapper excludes these
operations from discovery.

The provider remains experimental for explicit addon lookup. Additional discovery and addon
management are paused until a compatible stable-release workflow justifies expanding this scope.


## Output And Exit Codes

Every command emits the shared envelope (`ok`, `provider`, `command`, `kind`, `schema_version`,
`query`, `provenance`, `data`) on stdout, and nothing else at the top level. Failures write
`{ok: false, ..., error: {code, message}}` to stderr and exit nonzero — never a traceback.

| Error code | Exit |
|---|---|
| `missing_api_key`, `auth_failed` | 3 |
| `addon_not_found` | 4 |
| `rate_limited` (429), `upstream_error` (other HTTP errors), `timeout`, `network_error` | 5 |
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
make test-e2e E2E_PATHS="tests/e2e/test_curseforge.py"
```

with a real `CURSEFORGE_API_KEY`. The end-to-end journey covers both resolution paths (numeric id
and slug search) plus the `data.latest_files` ordering and `data.changelog` of `addon`.

## Analytics And Provenance Posture

Per [SAFE_ANALYTICS_RULES.md](../foundation/SAFE_ANALYTICS_RULES.md): raw source identifiers and
URLs (addon id, slug, project/file URLs) stay alongside any normalized output, normalization is
additive, and the CLI does not synthesize "best addon" answers.

## Source Links

- `https://www.curseforge.com/wow/addons`
- [Roadmap](../ROADMAP.md)
