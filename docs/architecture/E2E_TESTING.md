# End-to-End Testing

`make test-e2e` runs the journeys under `tests/e2e/` against the real providers with the
credentials on this machine. It is the local gate for "does the toolset still work", and it is
meant to be run before merging anything that touches a provider, the wrapper, or the shared
contract. CI runs only the keyless half, weekly (see [CI](#ci)).

## What a journey is

Every journey executes an installed binary (`.venv/bin/<name>`) as a real subprocess through
`tests/e2e/harness.py` and holds it to the contract in
[ERROR_CONTRACT.md](../foundation/ERROR_CONTRACT.md):

- exactly one JSON envelope on stdout on success, or on stderr on failure, and nothing else;
- the expected exit code (0, 2 usage, 3 auth, 4 not found, 5 network, 1 generic);
- no traceback, ever;
- `envelope_violations()` empty, so nothing at the top level but the envelope keys, and every field
  a journey reads taken from `data`.

On top of that, journeys assert real content: names, ids, counts, files on disk, and agreement
between commands (search, resolve, and entity must name the same thing, and the page a typed
lookup returns must be the page the query names). A journey follows an agent workflow end to end
rather than probing one endpoint, so the wrapper composites (`guide-compare-query`,
`guide-builds-simc`, `talent-packet`, `talent-describe`, `cooldown-packet`, `actor-profile`) run
against the local SimulationCraft checkout rather than a stub.

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
  skip. The only journeys that skip are the optional ones under [Coverage](#coverage), and they
  say so in the skip reason.
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

Every command in `docs/reference/` has at least one journey except the three Warcraft Logs auth
mutations below, and some commands need an optional input to reach their success path. Six
commands are deliberately kept off their success path, because a success would change this
machine:

| Command | Coverage and why |
| --- | --- |
| `warcraftlogs auth login`, `auth pkce-login`, `auth logout` | no journey at all, not even an error path. Each one rewrites or deletes the saved user token that the private-report journeys depend on, so a success would log you out of your own account mid-run. The read-only `auth status`, `client`, `token`, and `whoami` are covered |
| `simc sync`, `simc build`, `simc checkout` | error path only. A success would pull, recompile, or clone the SimulationCraft checkout that every other simc journey reads. `build` is reached through its missing-build-dir guard, `sync` through its dirty-worktree and missing-repo guards, `checkout` through a temporary `XDG_DATA_HOME` whose managed root is not a git repo |

Optional inputs:

| Variable | Enables |
| --- | --- |
| `WARCRAFT_E2E_RAIDBOTS_REPORT` | the Raidbots `inspect-report` / `input` round trip (reports expire, so there is no stable public pin) |
| `WARCRAFT_E2E_REDIS_URL` | the Redis cache journey |
| `WARCRAFT_E2E_SKIP` | comma-separated providers to exclude, plus `redis`; anything not named here must run |
| `WARCRAFT_E2E_PACE_SECONDS` | minimum gap between consecutive runs of the same binary (default 0.75s; Wowhead is held to 1.5s because it answers bursts with an IP-level 403) |

## Known limits

What a green run does **not** prove:

- **The auth mutations and the SimC update commands never succeed here.** See Coverage.
- **Wowhead's PTR and beta datasets are untested.** Whether a PTR dataset is live is upstream
  state no command can discover, so `--normalize-canonical-to-expansion` and the `ptr` expansion
  profile have no journey; the five classic-era profiles cover expansion routing instead.
- **Some Wowhead flags have no journey.** `comments --hydrate-missing-replies`, the `guides
  <query>` text filter, and `compare` across mixed entity types (quest, npc, spell) each need extra
  live Wowhead requests per run and stays out of the suite to keep it under Wowhead's rate limit.
- **No write path anywhere.** Nothing logs in, rotates a token, uploads a sim, or mutates a
  provider account, so those code paths are only covered by the fast tests.
- **Volatile upstreams.** The journeys assert titles, ids, and counts from live pages. Upstream
  renaming a page or changing a listing turns a journey red, which is the intent: a suite that
  followed upstream quietly would pass while the CLI returned the wrong page.
- **One machine, one account.** Credentials, the SimulationCraft checkout, and the guild and
  character pins are the maintainer's; a green run on another machine needs the same inputs.

## Known gaps

Open weaknesses a green run does not rule out, beyond the limits above:

- **Coverage is hand-maintained.** "Every command in `docs/reference/` has a journey" is checked by
  reading, not by a test, and many documented flags appear in no journey.
- **One journey is red against the current product**:
  `test_wrapper_core.py::test_passthrough_refuses_an_expansion_it_cannot_apply` expects exit 2 for
  `unsupported_provider_expansion` and `duplicate_expansion_argument`; the wrapper exits 1, which
  `docs/warcraft/README.md` documents. One of the two has to change.
- **Wowhead guide exports carry no build references**, so `warcraft guide-builds-simc` hands simc
  only the Method and Icy Veins builds, and the packet has no per-bundle count showing that the
  Wowhead bundle contributed none. The guide journey pins the contributing providers, so it goes
  red, not quiet, if that changes.
- **Merged search order between providers** is not checked end to end: the journeys check each
  provider's rows against its own payload, and the cross-provider order is covered only by
  `tests/test_provider_contract.py`.
- **The Wowhead guide-order rule** (Wowhead's guide ranking counts only when the query or
  `--entity-type` asks for a guide) has no journey; no live query found so far tells the two rules apart.
- **Raider.IO leaderboard citations** are compared as strings only: raider.io answers 200 for any
  `?realm=` value, so fetching the citation would prove nothing.
- **Wowhead suggestion type 112** (Companion) has never appeared in a live response, so its label is
  unverified.

## CI

`.github/workflows/live-contracts.yml` runs weekly (Mondays 06:00 UTC) and on demand, with no
secrets. It runs the keyless journey files (`test_wowhead.py`, `test_method.py`,
`test_icy_veins.py`, `test_raiderio.py`, `test_warcraft_wiki.py`, `test_lorrgs.py`,
`test_raidbots.py`, with `WARCRAFT_E2E_SKIP=raidbots-report`) and `make test-canary`, the Wowhead
parser canary (`tests/test_wowhead_parser_canaries.py`, gated by `WOWHEAD_LIVE_TESTS=1`). The keyed
providers, SimulationCraft, the wrapper composites, and `test_contract.py` stay local. It never
gates a pull request. When a job fails, is cancelled, or is skipped, a scheduled run (or a manual
run with `open_issue` set) opens or comments on the `live-failure` tracking issue.
