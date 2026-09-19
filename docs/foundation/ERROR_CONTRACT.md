# Error and Envelope Contract

Every binary in this repo (`warcraft`, `wowhead`, `warcraftlogs`, `simc`, ...) speaks the same JSON
envelope, the same exit codes, and the same global output flags. The implementation lives in
`warcraft_core.envelope`, `warcraft_core.exit_codes`, `warcraft_core.provider`, and
`warcraft_core.cli`; this page is the agent-facing description.

## Envelope

Every command writes exactly one JSON object to stdout on success or to stderr on failure.

| Key | Type | Meaning |
| --- | --- | --- |
| `ok` | bool | `true` on success, `false` on failure |
| `provider` | string | Binary/provider name (`wowhead`, `warcraftlogs`, `warcraft`, ...) |
| `command` | string | Subcommand that produced the payload (`search`, `entity`, `doctor`, ...) |
| `kind` | string | Payload kind inside `data` (`search_results`, `entity`, `doctor`, `error`, ...) |
| `schema_version` | string | Envelope schema version. Currently `"1"`. |
| `query` | string, object, or null | The normalized input the command acted on |
| `provenance` | object | Source URLs, fetch timestamps, cache state. `{}` when there is none. |
| `data` | object | Provider payload. `{}` on failure. |
| `error` | object | Present only when `ok` is `false`: `{"code": str, "message": str, "details"?: object}` |

`schema_version` describes the envelope only. Provider payloads inside `data` may carry their own
versions (for example Wowhead entity payloads).

### Machine-readable schema

The envelope is also published as a draft 2020-12 JSON Schema at
[`schemas/envelope.schema.json`](../../schemas/envelope.schema.json), and `warcraft schema` prints
the same document:

```bash
warcraft --pretty schema
```

It is derived from the `warcraft_core.envelope` TypedDicts by `warcraft_cli.schema`, so it cannot
drift from the implementation. `data`, `provenance`, and `error.details` are open objects (each
provider owns their contents), and the envelope itself allows additional properties because of the
deprecated legacy top-level keys below.

### Deprecated legacy top-level keys

Providers that historically emitted payload keys at the top level (`results`, `count`, `entity`,
`status`, ...) still emit them next to the envelope keys so existing agents keep working. `data`
always carries the same payload, so read `data`; the top-level copies are deprecated. Legacy keys
never shadow envelope keys.

## Error object

```json
{"ok": false, "provider": "wowhead", "command": "entity", "kind": "error", "schema_version": "1",
 "query": null, "provenance": {}, "data": {},
 "error": {"code": "not_found", "message": "No item with id 0", "details": {"status_code": 404, "url": "https://..."}}}
```

`code` is a stable snake_case identifier for programs; `message` is for humans; `details` is
optional structured context. Providers keep their existing code strings; the codes below have a
fixed repo-wide exit-code mapping. A provider may additionally map its own codes onto the same five
exit codes, and documents them in its provider README: for example `warcraftlogs` exits `2` for
`invalid_query` and its `missing_*` input codes, `curseforge` exits `3` for `missing_api_key` and
`4` for `addon_not_found`, and `blizzard` exits `2` for `unsupported_region`,
`unsupported_game_version`, and `classic_profile_unsupported`.

## Exit codes

| Exit | Meaning | Error codes mapped to it |
| --- | --- | --- |
| `0` | Success | |
| `1` | Generic failure, including uncaught exceptions (`internal_error`) | any code with neither a row below nor a provider-specific mapping |
| `2` | Usage error: bad flags or arguments | `invalid_query`, `invalid_argument`, `missing_fields` |
| `3` | Authentication required or rejected | `auth_required`, `auth_failed`, `unauthorized`, `forbidden` |
| `4` | Target not found | `not_found` |
| `5` | Network or upstream failure | `network_error`, `timeout`, `upstream_error`, `rate_limited`, `http_error` |

The repo-wide mapping is `warcraft_core.exit_codes.EXIT_CODE_BY_ERROR_CODE`; `exit_code_for(code)`
resolves it. Provider-specific mappings sit next to the code that raises them and pass an explicit
`exit_code`, so the five codes above stay the whole exit vocabulary.

## Process guard

Each binary's entry point is `warcraft_core.cli.guarded_run(app, provider=...)`. Anything that
escapes a command becomes an error envelope on stderr and never a traceback:

- `ProviderError` -> its own `code` and exit code (default from the table above)
- `httpx.TimeoutException` -> `timeout`, exit 5
- `httpx.HTTPStatusError` -> `auth_failed` (401/403, exit 3), `not_found` (404, exit 4), otherwise `upstream_error` (exit 5); `details` carries `status_code` and `url`
- any other `httpx.RequestError` -> `network_error`, exit 5
- argument-parsing failures (unknown flag, rejected option value, missing argument) -> `invalid_argument`, exit 2
- any other exception -> `internal_error` with `"<ExceptionType>: <message>"`, exit 1

There is no failure mode that writes human text instead of the envelope: `--help` is the only
non-JSON output, and it exits `0`. Explicit `typer.Exit` passes through with its own exit code. The
guard's error JSON is always compact because the global flags may not have been parsed yet.

When the failure happens before the command body runs, `command` is still the subcommand named on
the command line, never the value of a global flag (`warcraft --profile bogus schema` reports
`"command": "schema"`). Nested command groups are named in full, so the label matches the one the
success envelope would have carried (`raiderio distribution mythic-plus-runs --pages abc` reports
`"command": "distribution mythic-plus-runs"`). It is empty only when no subcommand was named at all.

## Global output flags

These flags exist on every binary and go before the subcommand:

| Flag | Effect |
| --- | --- |
| `--pretty` | Pretty-print JSON. Default output is compact JSON. |
| `--compact` | Truncate long string fields (default 280 chars, adds `...`) |
| `--compact-max-chars N` | Truncation length for `--compact` (40-10000) |
| `--fields a.b,c` | Keep only the listed dot paths (repeatable or comma-separated). `ok` and `error` are always kept on failures. |
| `--fields-strict` | Exit 2 with `missing_fields` when a requested path is absent |
| `--profile agent\|human` | Presets: `agent` compact JSON (default), `human` pretty JSON |

A `--fields` path the payload does not have is never dropped in silence. With `--fields-strict` it
is a `missing_fields` error (exit 2); without it the projection carries a `fields_missing` array of
the paths that did not resolve, so an empty or thin projection is distinguishable from an empty
result.

```bash
wowhead --fields data.results --compact search "thunderfury"
wowhead --profile human entity item 19019
```

## Provider surface

`warcraft_core.provider.ProviderSurface` is the in-process interface each provider package exports as
`PROVIDER` from `<pkg>/provider.py`:

```python
class ProviderSurface(Protocol):
    @property
    def name(self) -> str: ...  # read-only, so frozen dataclasses satisfy it
    def search(self, query: str, *, limit: int = 10, **options) -> Envelope: ...
    def resolve(self, target: str, **options) -> Envelope: ...
    def doctor(self, **options) -> Envelope: ...
```

Surface methods are pure: they never print and never raise `typer.Exit`. They return an `Envelope`
or raise `ProviderError(code, message, details=..., exit_code=...)`. Typer commands are thin
wrappers that call the surface and `emit` the result, and the `warcraft` wrapper calls `PROVIDER`
objects directly instead of spawning provider binaries.
