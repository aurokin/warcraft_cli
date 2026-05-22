from __future__ import annotations

from wowprogress_cli.main import WowProgressClient


def test_wowprogress_main_reexports_client() -> None:
    assert WowProgressClient is not None
