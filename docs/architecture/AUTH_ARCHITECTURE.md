# Auth Architecture

Shared auth design for official OAuth providers, workflow/session products, and light-credential APIs. Open rollout items beyond Warcraft Logs phase 2 live in [Linear — Warcraft CLI](https://linear.app/aurokin/project/warcraft-cli-a9a133da0d88).

## Rollout Status

| Phase | Scope | Status |
|-------|--------|--------|
| 1 | XDG/env discovery, shared state paths, auth-status primitives | Shipped (`warcraft_core.auth`, WCL integration) |
| 2 | `warcraftlogs` user auth (`auth status`, login, PKCE, logout, token persistence) | Shipped |
| 3 | `blizzard-api` as second OAuth validation (regions, namespaces, scopes) | Not started (provider not implemented) |
| 4 | `raidbots` session/workflow auth only if product requires it | Deferred |

## Auth Consumer Classes

### Official OAuth API providers

Shape the shared design: `warcraftlogs`, future `blizzard-api`.

Shared: credential discovery, token persistence, auth status, callback helpers, refresh/expiry.

Provider-local: authorize/token URLs, scopes, grant types, endpoint splits, region/namespace rules.

### Session or workflow providers

`raidbots` — do not force OAuth abstractions; reuse storage/status only where it fits.

### Light-credential providers

API keys / static tokens — separate from OAuth; not the current driver.

## Shared Responsibilities

**Credential discovery order:** repo `.env.local` → `~/.config/warcraft/providers/<provider>.env` → process environment.

**Runtime state (not in `.env`):** `~/.local/state/warcraft/providers/<provider>.json` for tokens, expiry, session metadata.

**Status reporting:** `doctor` and provider `auth status` should report credential presence, source, token validity, expiry, active auth mode (redacted).

**Callbacks (future shared helpers):** loopback listener, URL validation, state/nonce, browser launch — generic; token exchange stays provider-local.

## Provider Posture

| Provider | Commands | Notes |
|----------|----------|-------|
| `warcraftlogs` | `auth status`, `auth login`, `auth pkce-login`, `auth logout`, `doctor` | OAuth + GraphQL; user vs client endpoints |
| `blizzard-api` | Plan explicit `auth` early when implemented | Battle.net OAuth required |
| `raidbots` | No auth commands until a real workflow exists | Report consumption / local SimC handoff only |

## What Not To Do

- One universal auth client for all providers
- Storing issued tokens in `.env` files
- Provider-specific token rules in `warcraft-core`
- Letting Raidbots distort the OAuth path for official APIs

## Testing

- Shared: discovery, state paths, redacted status
- Provider: token acquire/refresh, failure modes
- Live: public first, then user auth where implemented

## Source Links

- Warcraft Logs: https://www.warcraftlogs.com/api/docs
- Blizzard Battle.net OAuth: https://community.developer.battle.net/documentation/guides/using-oauth
- Raidbots product/support (no public API): https://www.raidbots.com/
