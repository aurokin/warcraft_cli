# Raidbots

## Best For

- reading a Raidbots report someone shared (URL or bare report ID)
- pulling apart what was simulated: actor, metrics, run settings, ranked gear/drop options
- bridging a report's SimC input into local `simc` analysis
- explaining what Raidbots would do with a pasted SimC addon block

## Start With

- readiness + URL templates: `raidbots doctor`
- parse a report: `raidbots inspect-report <url-or-id>`
- extract the report's SimC input + handoff: `raidbots input <url-or-id>`
- classify pasted SimC text locally: `raidbots explain-input --text "..."` (or `--file`, or stdin)

`<url-or-id>` accepts a bare report ID or any URL containing `/report/{ID}`.

## Effective Use

- `inspect-report` returns a kind-aware summary:
  - `quick_sim`: the actor (name/spec/role/class) plus DPS and core metrics
  - `multi_profile` (Top Gear / Droptimizer): ranked profileset results by mean; per-actor damage/buff
    detail is not present in these report types, so reason from the ranked rows, not from a single actor
- pass `--no-raw` to `inspect-report` for large multi-profile reports so you get the summary without the
  full `data.json` payload
- every report response carries `freshness`, `citations` (report/data/input URLs), and `scope` so you can
  cite the source and tell how fresh it is
- use `input` when you want to continue the analysis locally: it returns the SimC input plus suggested
  `simc` commands (run the full profile with `simc sim -`, or decode/describe the talents)
- use `explain-input` when the user pastes a `/simc` addon block and asks "what would Raidbots do with this?"
  — it classifies the sim type (quick sim vs Top Gear/Droptimizer vs advanced) entirely offline

## Boundaries

- report reading only: there is no sanctioned Raidbots submission API, so this CLI never submits sims
- to run on the Raidbots cloud, paste the emitted input into raidbots.com; to run locally, use the
  suggested `simc` commands
- `raidbots` does not run SimC itself and does not call other providers — local analysis is a handoff,
  not an internal call
- no `search` / `resolve`: Raidbots is explicit-report consumption, so the wrapper does not fan out
  search/resolve to it
