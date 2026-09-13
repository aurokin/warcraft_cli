"""Normalized class/spec identity for Raider.IO payloads.

Search candidates, resolve matches, and sampled roster rows all decorate their rows with the shared
``class_spec_identity`` block; this is the one place that decides Raider.IO's confidence rule.
"""

from __future__ import annotations

from typing import Any

from warcraft_core.identity import IdentityConfidence, class_spec_identity_payload


def raiderio_class_spec_identity(class_name: Any, spec_name: Any, *, source: str) -> dict[str, Any]:
    # Additive normalized class/spec sibling, mirroring the `character` command: only a fully
    # resolved class+spec pair claims high confidence; class-only/partial degrades to none.
    actor_class = class_name if isinstance(class_name, str) and class_name.strip() else None
    spec = spec_name if isinstance(spec_name, str) and spec_name.strip() else None
    confidence: IdentityConfidence = "high" if actor_class and spec else "none"
    return class_spec_identity_payload(
        actor_class=actor_class,
        spec=spec,
        provider="raiderio",
        source=source,
        confidence=confidence,
    )
