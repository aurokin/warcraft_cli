# Operational boundaries

Repo-wide expectations for respectful provider use, safe logging, and failure response. This document is governance and operator guidance — not a substitute for each provider's terms of service or robots policy.

## Principles

- Prefer **fewer, cache-backed requests** over tight loops against live endpoints.
- Use **`doctor`** and **contract tests** to detect breakage before agents depend on broken parsers.
- Return **structured errors** (`ok: false`, `error.code`) instead of partial guesses when a source contract fails.
- Keep **credentials and tokens out of logs** and shared artifacts unless the operator explicitly opts in.

## Rate limiting and request volume

| Practice | Guidance |
| --- | --- |
| Default posture | Treat provider APIs and HTML endpoints as rate-limited. Back off when responses are slow, empty, or HTTP 429/503. |
| Caching | Use built-in CLI caches (`wowhead`, `warcraftlogs`, `wowprogress`, etc.) when repeating work. Clear or repair caches only when freshness requires it (`cache-clear`, `cache-repair`). |
| Concurrency | Wowhead `comments --hydrate-missing-replies` exposes `--max-concurrency`; keep values modest (default 4). Avoid unbounded parallel fanout across many CLIs. |
| Live matrices | Warcraft Logs `make test-live-matrix` and Wowhead live workflows are **operator-triggered** — do not schedule them as high-frequency CI against production without credentials and scope review. |
| Warcraft Logs | Query `warcraftlogs rate-limit` and inspect `doctor` / auth status before large report-scoped batch jobs. |

There is no repo-wide global throttle yet. Operators are responsible for pacing automation outside documented test entrypoints.

## User-Agent and transport identity

| Provider family | Current behavior |
| --- | --- |
| Wowhead / Method / Icy Veins / Raider.IO / Wiki | `httpx` with redirects; default library User-Agent unless overridden in client code. |
| WowProgress | Browser-style transport and impersonation settings (see `docs/wowprogress/README.md` and `wowprogress doctor`). |
| Warcraft Logs | Official GraphQL API with OAuth/client credentials — not HTML scraping. |

**Guidance:**

- Do not misrepresent automated traffic as end-user browser sessions except where a provider CLI documents that transport (for example WowProgress impersonation).
- When adding new HTTP clients, document the User-Agent and fingerprint choice in the provider's `docs/<cli>/README.md`.
- Prefer stable, honest identification over rotating spoofed identities.

## Legal, robots, and respectful scraping

- Follow each site's terms of use and robots rules. The CLIs read public or authenticated APIs/pages the same way a diligent operator would — they are not a license to bulk-harvest beyond those rules.
- **Wowhead / guide sites:** use documented commands; avoid hammering search or entity-page endpoints in tight loops. Prefer bundles and caches for repeat analysis (`guide-export`, `guide-bundle-*`).
- **Warcraft Logs:** stay within API scopes granted to your client credentials. Private reports require user auth and appropriate OAuth scopes.
- **Simulation / local tools (`simc`):** no network scraping; local checkout and binary execution only.

If unsure whether a workflow is allowed, narrow the scope or stop — see [SAFE_ANALYTICS_RULES.md](SAFE_ANALYTICS_RULES.md).

## Logging and redaction

When building automation on top of CLI JSON or shell output:

| Data class | Treatment |
| --- | --- |
| OAuth tokens, API keys, client secrets | Never commit; avoid echoing in CI logs. Store under worktree-local config paths documented in architecture auth docs. |
| `warcraftlogs auth` payloads | Treat access tokens and refresh tokens as secrets. |
| Report codes / private log URLs | May identify players or guilds — redact in public bug reports when not essential. |
| Raw guide/article HTML in bundles | Evidence artifacts; do not paste large HTML blocks into public issues without reason. |

**Optional future hook:** `WARCRAFT_TELEMETRY=1` may be introduced for opt-in, anonymized failure telemetry. It is **not implemented** in this repo today. Do not set this variable expecting behavior.

For local debugging, prefer `wowhead doctor --no-live` and recorded fixtures (`tests/fixtures/`, schema snapshots) before attaching live tokens to bug reports.

## Failure-mode playbook

Use this order when a command regresses or returns empty data.

### 1. Reproduce narrowly

```bash
<provider> doctor
<provider> doctor --no-live    # when live endpoints are flaky or credentials are missing
```

Wrapper:

```bash
warcraft doctor
warcraft --expansion retail doctor
```

### 2. Classify the failure

| Symptom | Likely cause | First actions |
| --- | --- | --- |
| HTTP 4xx/5xx | Endpoint change, auth, or rate limit | Retry once; check `doctor` probes; inspect `warcraftlogs rate-limit` for WCL. |
| Empty `results` / missing keys | Parser drift or envelope change | Run provider contract tests; compare raw payload vs normalized layer. |
| `parse_error` / `unexpected_response` | HTML or JSON shape change | Run Wowhead parser canaries or WCL live matrix row for the command. |
| Auth errors | Expired token or missing scope | `warcraftlogs auth status`; re-run OAuth bootstrap. |
| Wrong expansion / wrong links | Profile or URL routing bug | `wowhead expansion-detect <url>`; check `expansion_url_policy` in `wowhead doctor`. |

### 3. Run targeted tests

| Provider | Fast checks |
| --- | --- |
| Wowhead | `pytest -q tests/test_wowhead_schema_snapshots.py tests/test_wowhead_doctor.py` |
| Wowhead live (opt-in) | `WOWHEAD_LIVE_TESTS=1 pytest -q tests/test_live_integration.py` |
| Warcraft Logs matrix (opt-in) | `make test-live-matrix` with credentials |
| Monorepo CI | `make lint && make typecheck && pytest -q` |

### 4. Fix or narrow the contract

- Prefer **additive** JSON fields and `schema_version` bumps over silent breaking changes.
- If the source no longer supports a workflow, update `doctor` capability metadata and docs — do not invent filler analytics. See [SAFE_ANALYTICS_RULES.md](SAFE_ANALYTICS_RULES.md).
- File Linear issues with: command invoked, expansion/profile, fixture URL or report code (redacted), and the smallest failing pytest.

### 5. Ship with verification

- Update `CHANGELOG.md` for user-visible contract changes.
- Re-run `doctor` and the provider's contract tests before merge.

## Provider quick reference

| CLI | Doctor | Rate / auth signal | Contract tests |
| --- | --- | --- | --- |
| `wowhead` | `wowhead doctor` | Session dedupe; optional live probes | `tests/test_wowhead_schema_snapshots.py`, parser canaries |
| `warcraftlogs` | `warcraftlogs doctor` | `warcraftlogs rate-limit`, OAuth | `tests/test_warcraftlogs_cli.py`, `make test-live-matrix` |
| `wowprogress` | `wowprogress doctor` | Browser transport | `tests/test_wowprogress_cli.py` |
| `warcraft` (wrapper) | `warcraft doctor` | Aggregates provider doctors | `tests/test_warcraft_wrapper.py` |

## Related

- [PRODUCT_PRINCIPLES.md](PRODUCT_PRINCIPLES.md)
- [SAFE_ANALYTICS_RULES.md](SAFE_ANALYTICS_RULES.md)
- [../wowhead/CONTRACTS.md](../wowhead/CONTRACTS.md)
- [../architecture/AUTH_ARCHITECTURE.md](../architecture/AUTH_ARCHITECTURE.md)
- [../USAGE.md](../USAGE.md)
