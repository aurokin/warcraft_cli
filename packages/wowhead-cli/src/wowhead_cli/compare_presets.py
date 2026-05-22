from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ComparePreset:
    key: str
    label: str
    comparable_fields: tuple[str, ...]
    max_links_per_entity: int
    max_shared_links: int
    max_unique_links: int
    comment_sample: int
    comment_chars: int
    include_gatherer: bool


DEFAULT_COMPARE_PRESET = ComparePreset(
    key="default",
    label="General entity comparison",
    comparable_fields=("name", "quality", "icon", "title"),
    max_links_per_entity=150,
    max_shared_links=80,
    max_unique_links=120,
    comment_sample=3,
    comment_chars=320,
    include_gatherer=True,
)

COMPARE_PRESETS: dict[str, ComparePreset] = {
    "default": DEFAULT_COMPARE_PRESET,
    "gear": ComparePreset(
        key="gear",
        label="Item and gear comparison",
        comparable_fields=("name", "quality", "icon"),
        max_links_per_entity=150,
        max_shared_links=80,
        max_unique_links=120,
        comment_sample=2,
        comment_chars=280,
        include_gatherer=True,
    ),
    "quest": ComparePreset(
        key="quest",
        label="Quest comparison",
        comparable_fields=("name", "title", "description"),
        max_links_per_entity=200,
        max_shared_links=100,
        max_unique_links=150,
        comment_sample=3,
        comment_chars=400,
        include_gatherer=True,
    ),
    "spell": ComparePreset(
        key="spell",
        label="Spell comparison",
        comparable_fields=("name", "icon", "title"),
        max_links_per_entity=120,
        max_shared_links=60,
        max_unique_links=100,
        comment_sample=1,
        comment_chars=240,
        include_gatherer=False,
    ),
}


def resolve_compare_preset(name: str | None) -> ComparePreset:
    if name is None:
        return DEFAULT_COMPARE_PRESET
    key = name.strip().lower()
    if key not in COMPARE_PRESETS:
        supported = ", ".join(sorted(key for key in COMPARE_PRESETS if key != "default"))
        raise ValueError(f"--preset must be one of: {supported}")
    return COMPARE_PRESETS[key]


@dataclass(frozen=True, slots=True)
class ResolvedCompareOptions:
    preset: ComparePreset
    comparable_fields: tuple[str, ...]
    max_links_per_entity: int
    max_shared_links: int
    max_unique_links: int
    comment_sample: int
    comment_chars: int
    include_gatherer: bool


def resolve_compare_options(
    *,
    preset: str | None = None,
    max_links_per_entity: int | None = None,
    max_shared_links: int | None = None,
    max_unique_links: int | None = None,
    comment_sample: int | None = None,
    comment_chars: int | None = None,
    include_gatherer: bool | None = None,
) -> ResolvedCompareOptions:
    base = resolve_compare_preset(preset)
    return ResolvedCompareOptions(
        preset=base,
        comparable_fields=base.comparable_fields,
        max_links_per_entity=max_links_per_entity if max_links_per_entity is not None else base.max_links_per_entity,
        max_shared_links=max_shared_links if max_shared_links is not None else base.max_shared_links,
        max_unique_links=max_unique_links if max_unique_links is not None else base.max_unique_links,
        comment_sample=comment_sample if comment_sample is not None else base.comment_sample,
        comment_chars=comment_chars if comment_chars is not None else base.comment_chars,
        include_gatherer=include_gatherer if include_gatherer is not None else base.include_gatherer,
    )
