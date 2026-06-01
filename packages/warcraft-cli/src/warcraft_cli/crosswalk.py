from __future__ import annotations

from typing import Any

from warcraft_core.wow_normalization import normalize_name, normalize_region, primary_realm_slug


def find_report_actors(player_details_payload: dict[str, Any], actor_name: str) -> list[dict[str, Any]]:
    """Return every report actor whose name matches ``actor_name`` (case-insensitive), across roles.

    Operates on a Warcraft Logs ``report-player-details`` payload (``player_details.roles`` with
    ``tanks``/``healers``/``dps`` lists). Each returned row is annotated with its ``role``. Matching is
    case-insensitive (an input concern, distinct from identity normalization). Callers must handle the
    multi-match case themselves — a report can legitimately contain two characters sharing a name.
    """
    player_details = player_details_payload.get("player_details") if isinstance(player_details_payload.get("player_details"), dict) else {}
    roles = player_details.get("roles") if isinstance(player_details.get("roles"), dict) else {}
    target = actor_name.casefold()
    matches: list[dict[str, Any]] = []
    for role in ("tanks", "healers", "dps"):
        rows = roles.get(role) if isinstance(roles.get(role), list) else []
        for row in rows:
            if isinstance(row, dict) and str(row.get("name") or "").casefold() == target:
                matches.append({**row, "role": role})
    return matches


def distinct_actor_targets(actors: list[dict[str, Any]]) -> list[dict[str, str | None]]:
    """Distinct (region, server) targets among matched rows, for ambiguity detection.

    Two rows for the same name on the same region+server are the same character (e.g. it played
    multiple roles); two rows on different servers are genuinely ambiguous and must not be silently
    cross-walked to one profile.
    """
    seen: list[tuple[str | None, str | None]] = []
    targets: list[dict[str, str | None]] = []
    for actor in actors:
        region = actor.get("region")
        server = actor.get("server")
        key = (
            region.strip().casefold() if isinstance(region, str) and region.strip() else None,
            server.strip().casefold() if isinstance(server, str) and server.strip() else None,
        )
        if key not in seen:
            seen.append(key)
            targets.append({
                "region": region if isinstance(region, str) else None,
                "server": server if isinstance(server, str) else None,
            })
    return targets


def distinct_class_spec_signatures(actors: list[dict[str, Any]]) -> list[tuple[str | None, str | None]]:
    """Distinct normalized (class, spec) signatures across matched rows.

    Used to detect a same-character match that resolves to more than one class/spec (e.g. a player
    listed in multiple role buckets across fights when ``--fight-id`` is omitted). More than one
    signature means we cannot pick a single spec to reconcile against without disambiguation.
    """
    seen: list[tuple[str | None, str | None]] = []
    for actor in actors:
        signature = _identity_pair(actor.get("class_spec_identity"))
        if signature not in seen:
            seen.append(signature)
    return seen


def actor_spec_ambiguous(actors: list[dict[str, Any]]) -> bool:
    """True when the matched rows do not resolve to a single definite class/spec.

    Two shapes both count as ambiguous and must not be cross-walked to one profile spec without
    ``--fight-id``: more than one distinct (class, spec) signature across rows (a player listed in
    multiple role buckets), or any single row Warcraft Logs already marked ``ambiguous`` because the
    player swapped specs within the aggregated row.
    """
    if len(distinct_class_spec_signatures(actors)) > 1:
        return True
    return any(
        isinstance(actor.get("class_spec_identity"), dict) and actor["class_spec_identity"].get("status") == "ambiguous"
        for actor in actors
    )


def report_actor_names(player_details_payload: dict[str, Any]) -> list[str]:
    """List the actor names present in a report-player-details payload (for not-found hints)."""
    player_details = player_details_payload.get("player_details") if isinstance(player_details_payload.get("player_details"), dict) else {}
    roles = player_details.get("roles") if isinstance(player_details.get("roles"), dict) else {}
    names: list[str] = []
    for role in ("tanks", "healers", "dps"):
        rows = roles.get(role) if isinstance(roles.get(role), list) else []
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("name"), str):
                names.append(row["name"])
    return names


def actor_lookup_identity(actor: dict[str, Any], *, region_override: str | None = None) -> dict[str, Any]:
    """Derive the Raider.IO ``character`` lookup key (region/realm/name) from a WCL actor.

    Returns ``{"ok": True, "identity": {region, realm, name}}`` when all three resolve, else
    ``{"ok": False, "missing": "region"|"realm"|"name"}`` naming the first field that blocks the
    lookup — so the caller reports an accurate reason instead of always blaming region. Region comes
    from ``region_override`` when supplied, else the actor's own ``region`` field.
    """
    raw_region = region_override if region_override is not None else actor.get("region")
    server = actor.get("server")
    name = actor.get("name")
    if not (isinstance(raw_region, str) and raw_region.strip()):
        return {"ok": False, "missing": "region"}
    if not (isinstance(server, str) and server.strip()):
        return {"ok": False, "missing": "realm"}
    if not (isinstance(name, str) and name.strip()):
        return {"ok": False, "missing": "name"}
    return {
        "ok": True,
        "identity": {
            "region": normalize_region(raw_region),
            "realm": primary_realm_slug(server),
            "name": normalize_name(name),
        },
    }


def _identity_pair(class_spec_identity: Any) -> tuple[str | None, str | None]:
    identity = class_spec_identity.get("identity") if isinstance(class_spec_identity, dict) else None
    if not isinstance(identity, dict):
        return None, None
    actor_class = identity.get("actor_class")
    spec = identity.get("spec")
    return (
        actor_class if isinstance(actor_class, str) else None,
        spec if isinstance(spec, str) else None,
    )


def reconcile_class_spec(log_identity: Any, profile_identity: Any) -> dict[str, Any]:
    """Compare two ``class_spec_identity`` blocks, reporting agreement only on fields both resolved.

    ``class_agree``/``spec_agree`` are ``None`` when either side lacks that field. ``agree`` is
    tri-state: ``True`` only when both class and spec were comparable and matched (a full identity
    match), ``False`` on any mismatch, and ``None`` when the comparison is partial (no conflict, but
    not every field could be checked). This is a soft reconciliation — it never claims a canonical
    match.
    """
    log_class, log_spec = _identity_pair(log_identity)
    profile_class, profile_spec = _identity_pair(profile_identity)
    reasons: list[str] = []
    class_agree: bool | None = None
    if log_class is not None and profile_class is not None:
        class_agree = log_class == profile_class
        if not class_agree:
            reasons.append("class_mismatch")
    spec_agree: bool | None = None
    if log_spec is not None and profile_spec is not None:
        spec_agree = log_spec == profile_spec
        if not spec_agree:
            reasons.append("spec_mismatch")
    comparable = class_agree is not None or spec_agree is not None
    if reasons:
        agree: bool | None = False
    elif class_agree is True and spec_agree is True:
        agree = True
    else:
        agree = None
    return {
        "comparable": comparable,
        "agree": agree,
        "class_agree": class_agree,
        "spec_agree": spec_agree,
        "reasons": reasons,
    }
