# Error and Envelope Contract

Every binary in this repo (`warcraft`, `wowhead`, `warcraftlogs`, `simc`, ...) speaks the same JSON
envelope, the same exit codes, and the same global output flags. The implementation lives in
`warcraft_core.envelope`, `warcraft_core.exit_codes`, `warcraft_core.provider`, and
`warcraft_core.cli`; this page is the agent-facing description.

## Envelope

Every command writes exactly one JSON object to stdout on success or to stderr on failure. The one
opt-in exception is `wowhead --stream`: when the payload has a row collection (`data.results`,
`data.comments`, or `data.linked_entities.items`), stdout is JSON Lines, a header line that is the
envelope with that collection emptied and `data.stream: {"field", "count"}` naming it, then one
`{"record": <row>}` line per row. Its failures are still one envelope on stderr.

| Key | Type | Meaning |
| --- | --- | --- |
| `ok` | bool | `true` on success, `false` on failure |
| `provider` | string | Binary/provider name (`wowhead`, `warcraftlogs`, `warcraft`, ...) |
| `command` | string | Full subcommand path that produced the payload (`search`, `entity`, `distribution mythic-plus-runs`, ...) |
| `kind` | string | Payload kind inside `data` (`search_results`, `entity`, `doctor`, `error`, ...) |
| `schema_version` | string | Envelope schema version. Currently `"1"`. |
| `query` | string, object, or null | On success, the normalized input when the command reports one, otherwise `null` (many commands that take a report, build or file answer with `null` here and describe their input inside `data`); on failure, see [Error object](#error-object) |
| `provenance` | object | Source URLs, fetch timestamps, cache state, upstream warnings about the source (for example `warcraftlogs graphql`'s `graphql_warnings`), and `compacted_paths` under `--compact`. `{}` when the command reports none; some commands keep their source URLs in `data` instead (for example `wowhead entity`'s `data.entity.page_url` and `data.citations`). |
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
provider owns their contents).

### Nothing else at the top level

The top level holds only the keys in the table above. Every payload field lives under `data`, once.
Older releases also copied payload keys (`results`, `count`, `entity`, ...) to the top level; those
copies are removed, so read `data`. `warcraft_core.cli.emit` (and the `wowhead --stream` writer)
enforces this: it refuses a payload with a missing, mistyped, or extra top-level key, so the command
fails with `internal_error` (exit 1) rather than printing it.

## Error object

```json
{"ok": false, "provider": "wowhead", "command": "entity", "kind": "error", "schema_version": "1",
 "query": null, "provenance": {}, "data": {},
 "error": {"code": "not_found", "message": "No item with id 0", "details": {"status_code": 404, "url": "https://..."}}}
```

`code` is a stable snake_case identifier for programs; `message` is for humans; `details` is
optional structured context.

A failure's `query` is the command's parsed parameters, so the rejected request is machine-readable
instead of only spelled out in `message`. `warcraft_core.cli.fail()` sets it by default from the Click
context (a command may pass its own `query=` instead), and the `--fields-strict` `missing_fields`
failure uses the same rule. The parameters are named as the command declares them, so a failure's
`query` can differ in shape from the same command's success `query`, which is the normalized input:
a `warcraftlogs report-events --fight-id 1` failure echoes `"fight_id": [1]`. Two kinds of parameter
are never echoed: an OAuth authorization code (`--code`), and the global output flags (`--pretty`,
`--fields`, ...). `query` is `null` when no command parsed its input: a usage error or an unexpected
exception caught by the process guard, the same two caught by the `warcraft` wrapper while it runs a
provider command in-process, a provider failure the wrapper builds from its in-process `doctor`
surface call, the `unsupported_provider_expansion` refusal a wrapper composite gets before a
provider command runs, and `warcraft guide-builds-simc`'s `simc_handoff_failed`, whose `query` is
`null` on success too. The wrapper's in-process `search` and `resolve` surface failures, including
their `unsupported_provider_expansion` refusal, echo the query text, and a `warcraft <provider> ...`
passthrough refusal echoes `{provider, expansion}`.

Providers keep their existing code strings; the codes below have a fixed repo-wide exit-code
mapping. A provider may additionally map its own codes onto the same five
exit codes, and documents them in its provider README: for example `warcraftlogs` exits `2` for
`invalid_query` and its `missing_*` input codes, `curseforge` exits `3` for `missing_api_key` and
`4` for `addon_not_found`, `blizzard` exits `2` for `unsupported_region`,
`unsupported_game_version`, and `classic_profile_unsupported`, `lorrgs` exits `2` for
`invalid_report_ref` and `missing_fight`, `raidbots` exits `2` for `invalid_report_ref`, and
`warcraft` exits `2` for `unsupported_provider_expansion`, `duplicate_expansion_argument`, and
`invalid_report_ref`, and `4` for `cooldown-packet`'s `fight_not_found`, `actor_id_not_found`,
`actor_name_not_found`, and `lorrgs_fight_not_found`.

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
- `httpx.HTTPStatusError` -> `auth_failed` (401/403, exit 3), `not_found` (404, exit 4), `rate_limited` (429, exit 5), otherwise `upstream_error` (exit 5); `details` carries `status_code` and `url`. Providers that translate status errors themselves use the same mapping (`warcraft_core.exit_codes.error_code_for_http_status`)
- any other `httpx.RequestError` -> `network_error`, exit 5
- argument-parsing failures (unknown flag, rejected option value, missing argument) -> `invalid_argument`, exit 2
- any other exception -> `internal_error` with `"<ExceptionType>: <message>"`, exit 1

There is no failure mode that writes human text instead of the envelope: `--help` is the only
non-JSON output, and it exits `0`. Explicit `typer.Exit` passes through with its own exit code. The
guard's error JSON is always compact because the global flags may not have been parsed yet.

## The `command` label

There is one rule: `command` is the full subcommand path, so a nested group is named in full
(`raiderio distribution mythic-plus-runs --pages abc` reports
`"command": "distribution mythic-plus-runs"`). `warcraft_core.cli.command_path(ctx)` is the one
implementation.

Every failure envelope carries that path already, whether it came from `fail()`, from a usage error
or from an unexpected exception: the guard and `fail()` apply the rule themselves. A command that
builds its own success envelope is responsible for passing `command=command_path(ctx)`, so that its
success label is the one its failures would have carried.

The value of a global flag is never the label (`warcraft --profile bogus schema` reports
`"command": "schema"`), and the label is empty only when no subcommand was named at all. When the
failure happens before any command body runs, the path is resolved from the command line against
the app's own command tree, so an unknown subcommand is still reported by the name the caller
typed.

## Global output flags

These flags exist on every binary and go before the subcommand:

| Flag | Effect |
| --- | --- |
| `--pretty` | Pretty-print JSON. Default output is compact JSON. |
| `--compact` | Truncate long prose strings (default 280 chars, adds `...`) and list each cut dot path in `provenance.compacted_paths`. Strings without a space or tab (URLs, talent and transport strings, export codes, ids, a generated SimC profile) and `*command`/`*commands` values are never cut. With `--fields`, `provenance.compacted_paths` lists the cut paths the projection kept. |
| `--compact-max-chars N` | Truncation length for `--compact` (40-10000) |
| `--fields a.b,c` | Keep only the listed dot paths (repeatable or comma-separated) |
| `--fields-strict` | Exit 2 with `missing_fields` when a requested path is absent |
| `--profile agent\|human` | Presets: `agent` compact JSON (default), `human` pretty JSON |

A `--fields` path the payload does not have is never dropped in silence. With `--fields-strict` it
is a `missing_fields` error (exit 2); without it the projection carries a `fields_missing` array of
the paths that did not resolve, so an empty or thin projection is distinguishable from an empty
result.

`--fields` and `--compact` shape success envelopes only. A failure is always written whole, so its
`error` and `query` are never projected or cut away.

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
