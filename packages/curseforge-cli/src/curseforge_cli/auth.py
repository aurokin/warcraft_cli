from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from warcraft_core.env import find_env_file
from warcraft_core.paths import provider_env_path

PROVIDER = "curseforge"

API_KEY_ENV = "CURSEFORGE_API_KEY"

MANAGED_ENV_KEYS = (API_KEY_ENV,)


def _read_env_keys(path: Path, keys: tuple[str, ...]) -> dict[str, str]:
    # Pure-read parse of the managed keys from a .env file, with no os.environ mutation, so doctor can
    # attribute the credential to the source that supplied it. Kept local to this provider, mirroring
    # blizzard_api_cli.auth: the repo extracts shared helpers only after a second caller appears.
    if not path.is_file():
        return {}
    allowed = set(keys)
    values: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        env_key = key.strip()
        if separator != "=" or env_key not in allowed:
            continue
        env_value = value.strip()
        if len(env_value) >= 2 and env_value[0] == env_value[-1] and env_value[0] in {"'", '"'}:
            env_value = env_value[1:-1]
        values[env_key] = env_value
    return values


@dataclass(frozen=True, slots=True)
class CurseForgeAuthConfig:
    api_key: str | None
    # Where the key came from: a config-file path, "environment" for the process environment, or None
    # when unconfigured.
    credential_source: str | None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


def curseforge_provider_env_path() -> str:
    return str(provider_env_path(PROVIDER))


def load_curseforge_auth_config(*, start_dir: str | None = None) -> CurseForgeAuthConfig:
    # Discovery precedence (highest wins): repo .env.local > provider env file > process environment,
    # per docs/architecture/AUTH_ARCHITECTURE.md. Pure reads (no os.environ mutation) so the key is
    # attributed to the source that actually supplied it.
    local_path = find_env_file(start_dir=start_dir)
    provider_path = Path(curseforge_provider_env_path())
    # Each layer is (source_label, values); source_label is None for the process environment.
    layers: list[tuple[str | None, dict[str, str]]] = []
    if local_path is not None:
        layers.append((str(local_path), _read_env_keys(local_path, MANAGED_ENV_KEYS)))
    if provider_path.is_file():
        layers.append((str(provider_path), _read_env_keys(provider_path, MANAGED_ENV_KEYS)))
    layers.append((None, {key: os.environ[key] for key in MANAGED_ENV_KEYS if os.environ.get(key)}))

    api_key: str | None = None
    source: str | None = None
    for layer_source, values in layers:
        value = values.get(API_KEY_ENV)
        if value is not None and value.strip():
            api_key = value.strip()
            source = layer_source
            break

    if not api_key:
        credential_source: str | None = None
    else:
        credential_source = source if source is not None else "environment"
    return CurseForgeAuthConfig(api_key=api_key, credential_source=credential_source)
