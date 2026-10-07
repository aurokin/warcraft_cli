from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.e2e import harness

AUTH_PROBE = '''
import json
import socket
socket.socket.connect = lambda *args: (_ for _ in ()).throw(RuntimeError("Network forbidden"))
from blizzard_api_cli.auth import load_blizzard_auth_config
from curseforge_cli.auth import load_curseforge_auth_config
from warcraftlogs_cli.client import load_warcraftlogs_auth_config
print(json.dumps([
    load_blizzard_auth_config().configured,
    load_curseforge_auth_config().configured,
    load_warcraftlogs_auth_config().client_id is not None,
]))
'''


def test_harness_cwd_isolates_missing_auth_from_checkout_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    (checkout / ".env.local").write_text(
        "BLIZZARD_CLIENT_ID=synthetic\nBLIZZARD_CLIENT_SECRET=synthetic\n"
        "WARCRAFTLOGS_CLIENT_ID=synthetic\nWARCRAFTLOGS_CLIENT_SECRET=synthetic\n"
        "CURSEFORGE_API_KEY=synthetic\n"
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(harness, "REPO_ROOT", checkout)
    monkeypatch.setattr(harness, "binary_path", lambda binary: Path(sys.executable))
    monkeypatch.setattr(harness, "_pace", lambda binary: None)
    monkeypatch.setattr(harness, "SESSION_ENV", {})
    blank = {
        "XDG_CONFIG_HOME": str(empty / "config"),
        "XDG_STATE_HOME": str(empty / "state"),
        "BLIZZARD_CLIENT_ID": "", "BLIZZARD_CLIENT_SECRET": "",
        "WARCRAFTLOGS_CLIENT_ID": "", "WARCRAFTLOGS_CLIENT_SECRET": "",
        "CURSEFORGE_API_KEY": "",
    }
    inherited_checkout = harness.run_raw("probe", "-c", AUTH_PROBE, env=blank)
    assert inherited_checkout.exit_code == 0, inherited_checkout.stderr
    assert json.loads(inherited_checkout.stdout) == [True, True, True]
    isolated = harness.run_raw("probe", "-c", AUTH_PROBE, env=blank, cwd=empty)
    assert isolated.exit_code == 0, isolated.stderr
    assert json.loads(isolated.stdout) == [False, False, False]
