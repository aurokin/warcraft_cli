from __future__ import annotations

import os
from dataclasses import dataclass

from warcraft_core.env import find_env_file, read_env_keys
from warcraft_core.paths import provider_env_path

PROVIDER = "blizzard-api"

CLIENT_ID_ENV = "BLIZZARD_CLIENT_ID"
CLIENT_SECRET_ENV = "BLIZZARD_CLIENT_SECRET"  # noqa: S105 — env var name, not a credential
REGION_ENV = "BLIZZARD_REGION"

MANAGED_ENV_KEYS = (CLIENT_ID_ENV, CLIENT_SECRET_ENV, REGION_ENV)


@dataclass(frozen=True, slots=True)
class BlizzardAuthConfig:
    client_id: str | None
    client_secret: str | None
    region: str | None
    # Where the credential pair came from: a config-file path, "environment" for the process
    # environment, or None when no single source holds both halves.
    credential_source: str | None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


def blizzard_provider_env_path() -> str:
    return str(provider_env_path(PROVIDER))


def load_blizzard_auth_config(*, start_dir: str | None = None) -> BlizzardAuthConfig:
    """Resolve the credentials from .env.local, then the provider env file, then the process environment.

    A client ID and secret belong to one OAuth client, so the first layer holding both wins and halves
    from different layers are never combined (as in warcraftlogs). ``BLIZZARD_REGION`` is a separate
    setting and resolves on its own. Every layer is a pure read; ``os.environ`` is never mutated.
    """
    local_path = find_env_file(start_dir=start_dir)
    # Each layer is (source_label, values); source_label is None for the process environment.
    layers: list[tuple[str | None, dict[str, str]]] = []
    if local_path is not None:
        layers.append((str(local_path), read_env_keys(local_path, MANAGED_ENV_KEYS)))
    provider_path = blizzard_provider_env_path()
    layers.append((provider_path, read_env_keys(provider_path, MANAGED_ENV_KEYS)))
    layers.append((None, {key: os.environ[key] for key in MANAGED_ENV_KEYS if os.environ.get(key)}))

    region = next((value for _, values in layers if (value := values.get(REGION_ENV, "").strip())), None)
    for source, values in layers:
        client_id = values.get(CLIENT_ID_ENV, "").strip()
        client_secret = values.get(CLIENT_SECRET_ENV, "").strip()
        if client_id and client_secret:
            return BlizzardAuthConfig(
                client_id=client_id, client_secret=client_secret, region=region, credential_source=source or "environment"
            )
    return BlizzardAuthConfig(client_id=None, client_secret=None, region=region, credential_source=None)
