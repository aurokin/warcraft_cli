from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from warcraft_core.env import find_env_file
from warcraft_core.paths import provider_env_path

PROVIDER = "blizzard-api"

CLIENT_ID_ENV = "BLIZZARD_CLIENT_ID"
CLIENT_SECRET_ENV = "BLIZZARD_CLIENT_SECRET"
REGION_ENV = "BLIZZARD_REGION"

MANAGED_ENV_KEYS = (CLIENT_ID_ENV, CLIENT_SECRET_ENV, REGION_ENV)


def _read_env_keys(path: Path, keys: tuple[str, ...]) -> dict[str, str]:
    # Pure-read parse of the managed keys from a .env file, with no os.environ mutation, so doctor can
    # attribute each credential half to the source that supplied it. Kept local to this provider: only
    # one consumer needs it today, and the repo extracts shared helpers after a second caller appears.
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
class BlizzardAuthConfig:
    client_id: str | None
    client_secret: str | None
    region: str | None
    # Where the active credential pair came from: a config-file path when both halves resolved from
    # the same file, "environment" when both came from the process environment, "mixed" when the two
    # halves came from different sources, or None when unconfigured.
    credential_source: str | None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


def blizzard_provider_env_path() -> str:
    return str(provider_env_path(PROVIDER))


def load_blizzard_auth_config(*, start_dir: str | None = None) -> BlizzardAuthConfig:
    # Discovery precedence (highest wins): repo .env.local > provider env file > process environment,
    # per docs/architecture/AUTH_ARCHITECTURE.md. Resolve each managed key independently across the
    # chain (pure reads, no os.environ mutation) so a value is attributed to the source that actually
    # supplied it -- even when keys are split across layers.
    local_path = find_env_file(start_dir=start_dir)
    provider_path = Path(blizzard_provider_env_path())
    # Each layer is (source_label, values); source_label is None for the process environment.
    layers: list[tuple[str | None, dict[str, str]]] = []
    if local_path is not None:
        layers.append((str(local_path), _read_env_keys(local_path, MANAGED_ENV_KEYS)))
    if provider_path.is_file():
        layers.append((str(provider_path), _read_env_keys(provider_path, MANAGED_ENV_KEYS)))
    layers.append((None, {key: os.environ[key] for key in MANAGED_ENV_KEYS if os.environ.get(key)}))

    def resolve(key: str) -> tuple[str | None, str | None]:
        for source, values in layers:
            value = values.get(key)
            if value is not None and value.strip():
                return value.strip(), source
        return None, None

    client_id, id_source = resolve(CLIENT_ID_ENV)
    client_secret, secret_source = resolve(CLIENT_SECRET_ENV)
    region, _ = resolve(REGION_ENV)
    if not (client_id and client_secret):
        credential_source: str | None = None
    elif id_source == secret_source:
        credential_source = id_source if id_source is not None else "environment"
    else:
        credential_source = "mixed"
    return BlizzardAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        region=region,
        credential_source=credential_source,
    )
