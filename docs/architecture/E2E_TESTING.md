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
- `envelope_violations()` empty, and the payload mirrored into `data`.

On top of that, journeys assert real content: names, ids, counts, files on disk, and agreement
between commands (search, resolve, and entity must name the same thing). A journey follows an
agent workflow end to end rather than probing one endpoint, so the wrapper composites
(`guide-compare-query`, `guide-builds-simc`, `talent-packet`, `talent-describe`,
`cooldown-packet`, `actor-profile`) are exercised with real SimulationCraft runs.

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

Every command in `docs/reference/` has at least one journey, except the three Warcraft Logs auth
mutations (`auth login`, `auth pkce-login`, `auth logout`), which need a dedicated opt-in journey
because they rewrite the saved token. Optional inputs:

| Variable | Enables |
| --- | --- |
| `WARCRAFT_E2E_RAIDBOTS_REPORT` | the Raidbots `inspect-report` / `input` round trip (reports expire, so there is no stable public pin) |
| `WARCRAFT_E2E_REDIS_URL` | the Redis cache journey |
| `WARCRAFT_E2E_PACE_SECONDS` | minimum gap between consecutive runs of the same binary (default 0.75s; Wowhead is held to 1.5s because it answers bursts with an IP-level 403) |

## Relationship to the live suites

`tests/test_*_live.py` and the Warcraft Logs matrix remain the weekly CI canaries
(`.github/workflows/live-contracts.yml`): cheaper, endpoint-shaped, and allowed to skip when a
credential is absent. The end-to-end journeys are the stricter local gate and are the place new
coverage goes.
