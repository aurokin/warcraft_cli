# CurseForge

## Best For

- looking up a World of Warcraft addon by slug or numeric mod id
- the packaged addon surface users actually install: metadata, latest files, changelog
- complementing `warcraft-wiki` (programming/usage context) with the real addon project

## Start With

- readiness + auth posture: `warcraft curseforge doctor`
- an addon by slug: `warcraft curseforge addon deadly-boss-mods`
- an addon by mod id: `warcraft curseforge addon 3358`

## Auth

- needs a CurseForge API key: set `CURSEFORGE_API_KEY` (discovered from `.env.local`, the provider
  env file, or the environment)
- with no key, `addon` returns a clean `missing_api_key` error; `doctor` reports whether the key is
  configured

## Effective Use

- `addon` returns `data.metadata` (the raw mod record), `data.latest_files`, and `data.changelog`
  for the newest file in one call
- a numeric argument is treated as a mod id and validated to be a WoW project; a non-numeric
  argument is matched to the exact addon slug, so a near-miss returns `addon_not_found` rather than
  the wrong addon
- changelog is best-effort: when a file exposes none it is `null`, and a failed fetch becomes an
  explicit `{file_id, error}` marker — the lookup still returns metadata + files
- every payload carries `provenance` (mod id, slug, resolved-by, source URLs) and
  `provenance.verified: false` — endpoints/shapes follow the documented public CurseForge API but
  are pending a one-time live confirmation, so treat results as best-effort until verified

## Boundaries

- `search` / `resolve` are coming soon: use `addon` with a known slug or id
- deferred: dependency graphs, game-version compatibility, file downloads
- registered with `expansion_mode=none`: addon game-version compatibility lives inside file
  records, so `curseforge` stays out of `--expansion` fanout
