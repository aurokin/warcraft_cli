# End-to-End Testing

`make test-e2e` runs the journeys under `tests/e2e/` against the real providers with the
credentials on this machine. It is the local gate for "does the toolset still work", it never runs
in CI, and it is meant to be run before merging anything that touches a provider, the wrapper, or
the shared contract.

## What a journey is

Every journey executes an installed binary (`.venv/bin/<name>`) as a real subprocess through
`tests/e2e/harness.py` and holds it to the contract in
[ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md):

- exactly one JSON envelope on stdout on success, or on stderr on failure, and nothing else;
- the expected exit code (0, 2 usage, 3 auth, 4 not found, 5 network, 1 generic);
- no traceback, ever;
- `envelope_violations()` empty, and every field a journey reads taken from `data`.

On top of that, journeys assert real content: names, ids, counts, files on disk, and agreement
between commands (search, resolve, and entity must name the same thing, and the page a typed
lookup returns must be the page the query names). A journey follows an agent workflow end to end
rather than probing one endpoint, so the wrapper composites (`guide-compare-query`,
`guide-builds-simc`, `talent-packet`, `talent-describe`, `cooldown-packet`, `actor-profile`) run
against the local SimulationCraft checkout rather than a stub. How far that gets depends on the
build: `identify-build` runs for real on every build handed off, while `decode-build` and
`describe-build` need a class and a spec, which the guide providers' build strings do not carry
(see Known limits).

A filter, a cap, or a sort is only exercised when the bound provably excludes something: journeys
read the unfiltered baseline first, derive the bound from it, and compare the filtered result
against the exact rows that bound keeps.

## Prerequisites

- `make dev-deploy-no-link` (or `make dev-deploy`), so `.venv/bin/<name>` is this checkout.
- A SimulationCraft binary at `<checkout>/build/simc` compiled from that checkout's *current*
  HEAD. A binary that lags the checkout makes the simc and talent-transport journeys answer with
  data that is wrong rather than missing; `simc doctor` reports `repo.build_ready: false` and
  prints the rebuild command, and the simc fixture turns that into a failure, not a skip.

## Policy

- **Skips are failures.** A provider that cannot be reached, a credential that is missing, or a
  parser that returns nothing fails the run. Upstream bot protection is a failure too, not a
  skip.
- **Exclude explicitly, never implicitly.** `WARCRAFT_E2E_SKIP=curseforge` skips
  that provider with a visible reason. `redis` is an optional component: set
  `WARCRAFT_E2E_REDIS_URL` to exercise it.
- **No stale pins.** Only permanent identifiers live in `tests/e2e/pins.py` (Thunderfury is item
  19019 forever). Anything that ages out, such as report codes, seasons, news slugs, or current
  tier bosses, is discovered at run time by the journey that needs it.
- **Real caches, isolated.** The session points `XDG_CACHE_HOME` at a temporary directory so
  journeys can assert cache hits without touching `~/.cache`. Config, state, and data roots stay
  real so credentials, saved tokens, guide bundles, and the local SimC checkout resolve exactly
  as they do for you.
- **Read-only against your accounts.** Journeys never log in, log out, rotate tokens, or upload.

## Running

```bash
make test-e2e                                   # everything, about ten minutes
make test-e2e E2E_ARGS="tests/e2e/test_wowhead.py"
make test-e2e E2E_ARGS="-k cooldown"
WARCRAFT_E2E_SKIP=curseforge make test-e2e
```

`make check` and `make test-fast` never run these; without `WARCRAFT_E2E=1` every journey is
collected and skipped with a reason.

## Credentials

The binaries read provider env files from `~/.config/warcraft/providers/` (mode 0600) after
`.env.local` and before the process environment.

| Provider | File | Keys | Where to get them |
| --- | --- | --- | --- |
| Warcraft Logs | `warcraftlogs.env` | `WARCRAFTLOGS_CLIENT_ID`, `WARCRAFTLOGS_CLIENT_SECRET`, plus a saved user token from `warcraftlogs auth login` | Warcraft Logs API clients page |
| Blizzard | `blizzard-api.env` | `BLIZZARD_CLIENT_ID`, `BLIZZARD_CLIENT_SECRET`, optional `BLIZZARD_REGION` | Battle.net Developer Portal, create a client; only the client-credentials flow is used |
| CurseForge | `curseforge.env` | `CURSEFORGE_API_KEY` | CurseForge developer console Core API key; read-only. Keys without search access can only resolve numeric mod ids |

Everything else is keyless. Store the canonical copies in your password manager and render the
files from there; never commit them. `warcraft doctor` reports which providers are configured
without printing secrets.

## Coverage

Every command in `docs/reference/` has at least one journey, and some need an optional input below
to reach their success path. Six are reached only on a deliberate failure path, because a success
path would change this machine:

| Command | Why it is error-path only |
| --- | --- |
| `warcraftlogs auth login`, `auth pkce-login`, `auth logout` | they rewrite the saved user token, so a success path would log you out of your own account mid-run |
| `simc sync`, `simc build`, `simc checkout` | a success path would pull, recompile, or clone the SimulationCraft checkout that every other simc journey reads. `build` is reached through its missing-build-dir guard, `sync` through its dirty-worktree and missing-repo guards, `checkout` through a temporary `XDG_DATA_HOME` whose managed root is not a git repo |

Optional inputs:

| Variable | Enables |
| --- | --- |
| `WARCRAFT_E2E_RAIDBOTS_REPORT` | the Raidbots `inspect-report` / `input` round trip (reports expire, so there is no stable public pin) |
| `WARCRAFT_E2E_REDIS_URL` | the Redis cache journey |
| `WARCRAFT_E2E_SKIP` | comma-separated providers to exclude, plus `redis`; anything not named here must run |
| `WARCRAFT_E2E_PACE_SECONDS` | minimum gap between consecutive runs of the same binary (default 0.75s; Wowhead is held to 1.5s because it answers bursts with an IP-level 403) |

## Known limits

What a green run does **not** prove:

- **Guide builds are not decoded.** Icy Veins and Method publish builds as bare
  `wow_talent_export` strings, which carry no class or spec, so `warcraft guide-builds-simc
  --decode/--describe` reports `simc_handoff_status: "partial"` with those legs empty. The journey
  pins that status; it does not prove a guide build can be simulated.
- **Wowhead's PTR and beta datasets are untested.** Whether a PTR dataset is live is upstream
  state no command can discover, so `--normalize-canonical-to-expansion` and the `ptr` expansion
  profile have no journey; the five classic-era profiles cover expansion routing instead.
- **No write path anywhere.** Nothing logs in, rotates a token, uploads a sim, or mutates a
  provider account, so those code paths are only covered by the fast tests.
- **Volatile upstreams.** The journeys assert titles, ids, and counts from live pages. Upstream
  renaming a page or changing a listing turns a journey red, which is the intent: a suite that
  followed upstream quietly would pass while the CLI returned the wrong page.
- **One machine, one account.** Credentials, the SimulationCraft checkout, and the guild and
  character pins are the maintainer's; a green run on another machine needs the same inputs.

## Relationship to the live suites

`tests/test_*_live.py` and the Warcraft Logs matrix remain the weekly CI canaries
(`.github/workflows/live-contracts.yml`): cheaper, endpoint-shaped, and allowed to skip when a
credential is absent. The end-to-end journeys are the stricter local gate and are the place new
coverage goes.
