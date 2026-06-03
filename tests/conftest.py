from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SRC_DIRS = (
    ROOT / "packages" / "warcraft-core" / "src",
    ROOT / "packages" / "warcraft-api" / "src",
    ROOT / "packages" / "warcraft-content" / "src",
    ROOT / "packages" / "warcraft-cli" / "src",
    ROOT / "packages" / "wowhead-cli" / "src",
    ROOT / "packages" / "method-cli" / "src",
    ROOT / "packages" / "icy-veins-cli" / "src",
    ROOT / "packages" / "raiderio-cli" / "src",
    ROOT / "packages" / "warcraft-wiki-cli" / "src",
    ROOT / "packages" / "wowprogress-cli" / "src",
    ROOT / "packages" / "simc-cli" / "src",
    ROOT / "packages" / "warcraftlogs-cli" / "src",
    ROOT / "packages" / "raidbots-cli" / "src",
    ROOT / "packages" / "blizzard-api-cli" / "src",
    ROOT / "packages" / "curseforge-cli" / "src",
)
LIVE_TEST_ENV_BY_FILE = {
    "test_blizzard_api_live.py": "BLIZZARD_LIVE_TESTS",
    "test_curseforge_live.py": "CURSEFORGE_LIVE_TESTS",
    "test_icy_veins_live.py": "ICY_VEINS_LIVE_TESTS",
    "test_live_endpoint_contracts.py": "WOWHEAD_LIVE_TESTS",
    "test_live_integration.py": "WOWHEAD_LIVE_TESTS",
    "test_method_live.py": "METHOD_LIVE_TESTS",
    "test_raidbots_live.py": "RAIDBOTS_LIVE_TESTS",
    "test_raiderio_live.py": "RAIDERIO_LIVE_TESTS",
    "test_warcraft_wiki_live.py": "WARCRAFT_WIKI_LIVE_TESTS",
    "test_warcraft_wrapper_live.py": "WARCRAFT_WRAPPER_LIVE_TESTS",
    "test_warcraftlogs_live.py": "WARCRAFTLOGS_LIVE_TESTS",
    "test_live_command_matrix.py": "WARCRAFTLOGS_LIVE_TESTS",
    "test_wowhead_parser_canaries.py": "WOWHEAD_LIVE_TESTS",
    "test_wowprogress_live.py": "WOWPROGRESS_LIVE_TESTS",
}

TESTS_DIR = str(ROOT / "tests")
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

for package_src in reversed(PACKAGE_SRC_DIRS):
    path_str = str(package_src)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)


@pytest.fixture(autouse=True)
def disable_cache_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "none")


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _live_env_for_item(item: pytest.Item) -> str:
    file_name = Path(str(item.path)).name
    return LIVE_TEST_ENV_BY_FILE.get(file_name, "WOWHEAD_LIVE_TESTS")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    for item in items:
        if item.get_closest_marker("live") is None:
            continue
        env_name = _live_env_for_item(item)
        if not _env_enabled(env_name):
            item.add_marker(pytest.mark.skip(reason=f"Set {env_name}=1 to run this live test."))
