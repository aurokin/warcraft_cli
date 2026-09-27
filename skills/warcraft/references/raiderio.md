# Raider.IO

## Best For

- character and guild profile lookup
- guild raid rankings (world, region, or realm) for a raid and difficulty
- Mythic+ runs
- sample-backed Mythic+ analytics

## Start With

- exact character: `raiderio character <region> <realm> <name>`
- exact guild: `raiderio guild <region> <realm> <name>`
- discovery: `raiderio search "<query>"`
- conservative match: `raiderio resolve "<query>"`
- raid slugs for the current expansion: `raiderio raids` (add `--expansion-id 10` for The War Within, `9` for Dragonflight)
- guild raid leaderboard: `raiderio leaderboard raids --raid <slug> --difficulty mythic --region us --limit 20`

## Effective Use

- prefer structured queries like `character us illidan Roguecane`; multi-word realms work too (`character eu tarren mill Cotti`)
- the `guild`/`character` word only counts as a hint when it leads the query; anywhere else it is part of the name (`resolve "guild us malganis Old Guild Order"`, `search "Liquid Guild"`)
- pass `--kind character` or `--kind guild` when only one kind will do: it overrides the query word, and an empty result means nothing of that kind matched
- discover the `--raid` slug from `raiderio raids` (or a guild profile's progression) before asking for raid rankings; slugs change every tier
- the raid a guild is currently progressing is the `raiderio raids` row whose per-region `starts`/`ends` window covers now; join it to the guild payload's progression rows by `raid_slug`
- add `--realm` with a standard `--region` (`us`, `eu`, `kr`, `tw`, `cn`, or an alias such as `na`) to answer "where does this guild rank on its realm"; `rank` is then the realm position and `region_rank` the region position
- realms may be written as a display name or a slug (`Tarren Mill`, `tarren-mill`, `mal'ganis`, `malganis`) anywhere a realm is accepted
- read `sample.limit_reached` on raid leaderboards: `false` means the scope has fewer ranked guilds than you asked for
- a rank of `0` in guild rankings (or in a character's `mythic_plus.ranks`) means unranked, not first place
- `freshness.fetched_at` is when the data came off the wire and `freshness.cache_hit` says whether it was replayed from cache, so quote the fetch time rather than the time you ran the command
- use `sample mythic-plus-runs` and `distribution mythic-plus-runs` for analytics questions
- use `sample mythic-plus-players` and `distribution mythic-plus-players` when you need participant-level slices instead of raw run rows
- narrow sampled analytics with filters like `--level-min`, `--contains-spec`, and `--player-region` when you need a tighter slice
- spec labels in analytics are class-qualified (`priest-holy`, `paladin-holy`) because spec names repeat across classes; pass `--contains-spec priest-holy` to select one class's spec, since `--contains-class` and `--contains-spec` each match any roster entry on their own
- use `threshold mythic-plus-runs` for sampled estimates around score or key level targets
- treat the analytics outputs as sampled leaderboard-derived summaries, not authoritative universal truths
- check the filtering counts when you narrow a slice so you do not over-trust a tiny sample
- check player truncation metadata when you use `--player-limit`, so you know whether you are looking at the full deduped participant set

## Boundaries

- strongest today on Mythic+ and profile data
- not the right primary source for raid-boss comp recommendations
