# Wowhead normalization

Additive normalized fields for agent consumption. Raw `tooltip`, HTML-derived blocks, and provider payloads remain unchanged.

## Schema versions

| Command | `schema_version` | When present |
| --- | --- | --- |
| `entity` | `wowhead.entity.v1` | `entity` type is `item` and at least one normalized field is available |
| `entity-page` | `wowhead.entity_page.v1` | `entity` type is `item` and page metadata supplies normalized fields |

Bump the version when field meaning or shape changes. Document changes in `CHANGELOG.md` under `[Unreleased]`.

## Field shape

Each normalized field is an object:

```json
{
  "value": 5,
  "provenance": {
    "source": "tooltip",
    "detail": "optional source hint"
  }
}
```

### Provenance sources (v1)

| `source` | Meaning |
| --- | --- |
| `tooltip` | Nether tooltip JSON for the entity |
| `page` | Entity page HTML metadata (`og:title`, canonical URL context) |

### Null and unknown semantics

- **Omitted field** — not extracted in this version; do not infer gameplay facts.
- **`value: null`** — not used in v1; omitted preferred over explicit null.
- Conflicting sources — when page title differs from tooltip name, `name` keeps tooltip provenance and `page_title` carries the page value.

## Item fields (v1)

| Field | Typical source |
| --- | --- |
| `name` | Tooltip `name`, else page `og:title` |
| `quality` | Tooltip `quality` |
| `icon` | Tooltip `icon` |
| `item_level` | Tooltip `itemLevel` |
| `binding` | Tooltip `binding` / `bindType` (ints mapped to labels when known) |
| `inventory_type` | Tooltip `inventoryType` / `slot` |
| `page_title` | Page `og:title` when it differs from tooltip `name` |

## Non-goals (v1)

- Quest, NPC, and spell adapters (later slices).
- Objectives, rewards, drops, and gatherer block extraction.
- Replacing or removing raw tooltip HTML/text.

## Related

- [../foundation/SAFE_ANALYTICS_RULES.md](../foundation/SAFE_ANALYTICS_RULES.md)
- [EXPANSION_RESEARCH.md](EXPANSION_RESEARCH.md)
