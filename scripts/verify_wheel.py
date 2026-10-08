#!/usr/bin/env python3
"""Install and exercise the release wheel outside the checkout, without user configuration."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

PRODUCT_PREFIXES = (
    "BLIZZARD_", "CURSEFORGE_", "ICY_VEINS_", "LORRGS_", "METHOD_", "RAIDBOTS_",
    "RAIDERIO_", "SIMC_", "WARCRAFT_", "WARCRAFTLOGS_", "WOWHEAD_",
)

# Executed with the clean environment's interpreter in isolated mode. Checking module origins also
# catches an editable install accidentally making the checkout available to this verification.
INSTALLED_PROBE = '''
import importlib
import importlib.metadata
import json
import pathlib
import socket
import sys
network_attempts = []
def blocked(*args, **kwargs):
    network_attempts.append(str(args))
    raise RuntimeError("Installed-wheel verification must not contact providers")
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.getaddrinfo = blocked
from icy_veins_cli.site_index import load_site_index
from warcraft_core.paths import worktree_root
expected = json.loads(sys.argv[1])
dist = importlib.metadata.distribution("warcraft")
actual = {entry.name: entry.value for entry in dist.entry_points if entry.group == "console_scripts"}
assert actual == expected, (actual, expected)
for target in actual.values():
    module = importlib.import_module(target.split(":")[0])
    origin = pathlib.Path(module.__file__).resolve()
    assert origin.is_relative_to(pathlib.Path(sys.prefix).resolve()), origin
assert worktree_root() is None, worktree_root()
index = load_site_index()
assert index is not None and index.bundled and index.pages, "Missing bundled Icy Veins index"
assert not network_attempts, network_attempts
print(f"Verified {len(actual)} installed console scripts and {len(index.pages)} bundled index pages")
'''


def isolated_env(root: Path) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(PRODUCT_PREFIXES) and key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}
    }
    env["HOME"] = str(root / "home")
    for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        env[name] = str(root / name.lower())
    env["WARCRAFT_HTTP_MIN_INTERVAL_SECONDS"] = "0"
    return env


def verify_wheel(wheel: Path) -> None:
    wheel = wheel.resolve(strict=True)
    project_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    expected_scripts = tomllib.loads(project_path.read_text())["project"]["scripts"]
    with tempfile.TemporaryDirectory(prefix="warcraft-wheel-") as directory:
        root = Path(directory).resolve()
        if root.is_relative_to(project_path.parent):
            raise RuntimeError("Wheel verification needs a temporary directory outside the checkout; check TMPDIR")
        env = isolated_env(root)
        venv = root / "venv"
        cwd = root / "work"
        cwd.mkdir()
        subprocess.run(["uv", "venv", str(venv), "--python", sys.executable], cwd=cwd, env=env, check=True)
        python = venv / "bin" / "python"
        subprocess.run(["uv", "pip", "install", "--python", str(python), str(wheel)], cwd=cwd, env=env, check=True)
        # Provider requests must fail locally if a help command accidentally starts one.
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env[name] = "http://127.0.0.1:9"
        env["NO_PROXY"] = ""
        for binary in expected_scripts:
            for args in (["--help"], ["doctor", "--help"]):
                result = subprocess.run([str(venv / "bin" / binary), *args], cwd=cwd, env=env,
                                        capture_output=True, text=True, check=True, timeout=30)
                if not result.stdout.strip():
                    raise RuntimeError(f"{binary} {' '.join(args)} produced no help")
        result = subprocess.run([str(venv / "bin" / "warcraft"), "schema"], cwd=cwd, env=env,
                                capture_output=True, text=True, check=True, timeout=30)
        payload = json.loads(result.stdout)
        if payload.get("ok") is not True or payload.get("kind") != "envelope_schema" or not payload["data"]["schema"]["required"]:
            raise RuntimeError("Installed warcraft schema did not return the envelope schema")
        subprocess.run([str(python), "-I", "-c", INSTALLED_PROBE, json.dumps(expected_scripts)],
                       cwd=cwd, env=env, check=True, timeout=30)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    verify_wheel(parser.parse_args().wheel)


if __name__ == "__main__":
    main()
