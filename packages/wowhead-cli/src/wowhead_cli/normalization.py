from __future__ import annotations

from typing import Any

ENTITY_PAYLOAD_SCHEMA_VERSION = "wowhead.entity.v1"
ENTITY_PAGE_PAYLOAD_SCHEMA_VERSION = "wowhead.entity_page.v1"

_ITEM_FIELD_SOURCES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("name", ("name",), "tooltip"),
    ("quality", ("quality",), "tooltip"),
    ("icon", ("icon",), "tooltip"),
    ("item_level", ("itemLevel", "item_level"), "tooltip"),
    ("binding", ("binding", "bindType", "bind_type"), "tooltip"),
    ("inventory_type", ("inventoryType", "inventory_type", "slot"), "tooltip"),
)

_BINDING_LABELS: dict[int, str] = {
    0: "none",
    1: "pickup",
    2: "equip",
    3: "use",
    4: "quest",
}


def _provenance(*, source: str, detail: str | None = None) -> dict[str, str]:
    row: dict[str, str] = {"source": source}
    if detail:
        row["detail"] = detail
    return row


def _normalize_binding(value: Any) -> str | int | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int):
        return _BINDING_LABELS.get(value, value)
    return None


def _pick_first(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _normalized_field(value: Any, *, source: str, detail: str | None = None) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return {
        "value": value,
        "provenance": _provenance(source=source, detail=detail),
    }


def build_normalized_item(
    *,
    tooltip: dict[str, Any] | None = None,
    page: dict[str, str | None] | None = None,
) -> dict[str, Any] | None:
    """Build additive normalized item fields with per-field provenance."""
    normalized: dict[str, Any] = {}
    tooltip_block = tooltip if isinstance(tooltip, dict) else {}

    for field_name, keys, source in _ITEM_FIELD_SOURCES:
        raw = _pick_first(tooltip_block, keys)
        if field_name == "binding":
            raw = _normalize_binding(raw)
        field = _normalized_field(raw, source=source)
        if field is not None:
            normalized[field_name] = field

    if page is not None:
        title = page.get("title")
        if isinstance(title, str) and title.strip():
            name_field = normalized.get("name")
            if name_field is None:
                normalized["name"] = _normalized_field(title.strip(), source="page", detail="og:title")
            elif name_field.get("value") != title.strip():
                normalized["page_title"] = _normalized_field(title.strip(), source="page", detail="og:title")

    return normalized or None


def attach_entity_normalization(
    payload: dict[str, Any],
    *,
    entity_type: str,
    tooltip: dict[str, Any] | None = None,
    page: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    if entity_type.strip().lower() != "item":
        return payload
    normalized_item = build_normalized_item(tooltip=tooltip, page=page)
    if normalized_item is None:
        return payload
    enriched = dict(payload)
    enriched["schema_version"] = ENTITY_PAYLOAD_SCHEMA_VERSION
    enriched["normalized"] = {"item": normalized_item}
    return enriched


def attach_entity_page_normalization(
    payload: dict[str, Any],
    *,
    entity_type: str,
    page: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    if entity_type.strip().lower() != "item":
        return payload
    normalized_item = build_normalized_item(page=page)
    if normalized_item is None:
        return payload
    enriched = dict(payload)
    enriched["schema_version"] = ENTITY_PAGE_PAYLOAD_SCHEMA_VERSION
    enriched["normalized"] = {"item": normalized_item}
    return enriched
