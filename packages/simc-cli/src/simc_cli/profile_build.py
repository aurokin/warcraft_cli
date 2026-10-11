"""Apply a validated build to a caller-supplied, standalone single-actor profile."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from warcraft_core.exit_codes import EXIT_USAGE
from warcraft_core.provider import ProviderError
from warcraft_core.wow_specs import lookup_class, lookup_spec

from simc_cli import build_services
from simc_cli.build_input import DEFAULT_RACE_BY_CLASS, PacketInput, extract_build_spec_from_text, has_talent_data
from simc_cli.repo import RepoPaths

_TALENT_KEYS = frozenset({"talents", "class_talents", "spec_talents", "hero_talents", "load_default_talents", "enable_all_talents"})
_UNEXPANDED_PLAYER_KEYS = frozenset({"armory", "guild", "local_json", "player_simplified", "pet", "guardian", "active"})


def _profile_identity(text: str) -> tuple[str, str]:
    actors = 0
    specs = 0
    actor_active = False
    spec_on_actor = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key in DEFAULT_RACE_BY_CLASS:
            actors += 1
            actor_active = True
        elif key in {"enemy", "tank_dummy"}:
            actor_active = False
        if key == "spec":
            specs += 1
            spec_on_actor = actor_active
        if key in _UNEXPANDED_PLAYER_KEYS:
            raise ProviderError(
                "unsupported_profile",
                f"Apply-build cannot establish a standalone actor with {key!r}; expand player imports and controls first.",
                exit_code=EXIT_USAGE,
            )
        if key in {"input", "copy"} or key.startswith("profileset"):
            raise ProviderError(
                "invalid_profile",
                "Apply-build requires a standalone profile without input, copy, or profileset directives.",
                exit_code=EXIT_USAGE,
            )
    parsed = extract_build_spec_from_text(text)
    actor_class = lookup_class(parsed.actor_class or "")
    spec = lookup_spec(parsed.spec or "", class_hint=actor_class)
    if actors != 1 or specs != 1 or not spec_on_actor or actor_class is None or spec is None or spec.class_key != actor_class:
        raise ProviderError(
            "invalid_profile",
            "Apply-build requires exactly one actor and one explicit matching spec.",
            exit_code=EXIT_USAGE,
        )
    return actor_class, spec.key


def _replace_talents(text: str, assignments: list[str]) -> str:
    """Keep unrelated profile lines, replacing all old talent forms after the explicit spec."""
    lines: list[str] = []
    for raw in text.splitlines(keepends=True):
        active = raw.split("#", 1)[0].strip()
        key = active.split("=", 1)[0].strip() if "=" in active else None
        if key is not None and key.removesuffix("+") in _TALENT_KEYS:
            continue
        lines.append(raw)
        if key == "spec":
            if not raw.endswith(("\n", "\r")):
                lines.append("\n")
            lines.extend(f"{assignment}\n" for assignment in assignments)
    return "".join(lines)


def _write_profile(destination: Path, text: str, *, overwrite: bool) -> None:
    """Publish a completed file atomically; exclusive linking prevents accidental replacement."""
    temporary: str | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, delete=False) as handle:
            temporary = handle.name
            handle.write(text)
        if overwrite:
            os.replace(temporary, destination)
        else:
            os.link(temporary, destination)
    except FileExistsError as exc:
        raise ProviderError(
            "output_exists", f"Output already exists: {destination}. Use --overwrite to replace it.", exit_code=EXIT_USAGE
        ) from exc
    except OSError as exc:
        raise ProviderError("profile_write_failed", f"Could not write profile: {exc}") from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def apply_build_payload(
    paths: RepoPaths,
    *,
    profile_path: Path,
    build: PacketInput | str,
    out: Path,
    overwrite: bool = False,
) -> dict[str, Any]:
    source = profile_path.expanduser().resolve()
    destination = out.expanduser().resolve()
    if source == destination or (destination.exists() and source.exists() and source.samefile(destination)):
        raise ProviderError("output_path_conflict", "The output must differ from the source profile.", exit_code=EXIT_USAGE)
    if destination.exists() and not overwrite:
        raise ProviderError("output_exists", f"Output already exists: {destination}. Use --overwrite to replace it.", exit_code=EXIT_USAGE)
    try:
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ProviderError("not_found", f"Profile not found: {source}") from exc
    except (OSError, UnicodeError) as exc:
        raise ProviderError("invalid_profile", f"Could not read profile: {exc}", exit_code=EXIT_USAGE) from exc
    profile_identity = _profile_identity(text)
    spec, identity = build_services._identified_build_or_raise(
        paths,
        apl_path=None,
        option_values=build_services._in_memory_build_option_values(build),
    )
    if not has_talent_data(spec):
        raise ProviderError("invalid_query", "An explicit talent build is required.", exit_code=EXIT_USAGE)
    if (spec.actor_class, spec.spec) != profile_identity:
        raise ProviderError(
            "build_identity_mismatch",
            "Build class/spec must match the supplied profile.",
            exit_code=EXIT_USAGE,
            details={
                "profile": {"actor_class": profile_identity[0], "spec": profile_identity[1]},
                "build": build_services._serialize_build_identity(identity),
            },
        )
    resolution = build_services._decode_or_raise(paths, spec, identity=identity)
    if (resolution.actor_class, resolution.spec) != profile_identity:
        raise ProviderError("build_identity_mismatch", "Decoded build class/spec differs from the supplied profile.", exit_code=EXIT_USAGE)
    assignments = [
        f"{key}={getattr(spec, key)}" for key in ("talents", "class_talents", "spec_talents", "hero_talents") if getattr(spec, key)
    ]
    _write_profile(destination, _replace_talents(text, assignments), overwrite=overwrite)
    return {
        "kind": "apply_build",
        "profile_path": str(source),
        "written_profile_path": str(destination),
        "build_spec": build_services._serialize_build_spec(spec),
        "identity": build_services._serialize_build_identity(identity),
        "validation": {"status": "decoded", "actor_class": resolution.actor_class, "spec": resolution.spec},
        "notes": [
            "Talent assignments replaced the profile's talent configuration, including overrides and default talent loading.",
            "Gear and other settings came from the supplied profile.",
            "Talent decoding was verified; the resulting geared profile has not been simulated.",
        ],
    }
