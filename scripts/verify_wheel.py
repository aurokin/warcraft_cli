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

# The guard is inherited by real console-script subprocesses, including attempts swallowed by retries.
NETWORK_GUARD = """
import os
import socket
import sys
from pathlib import Path

def blocked(*args, **kwargs):
    with Path(os.environ["VERIFY_NETWORK_LOG"]).open("a") as log:
        log.write(repr(sys.argv) + ": " + repr(args) + "\\n")
    raise RuntimeError("Installed verification must not contact providers")

socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.getaddrinfo = blocked
socket.gethostbyname = blocked
socket.gethostbyname_ex = blocked
socket.socket.sendto = blocked
"""


def install_network_guard(root: Path, env: dict[str, str]) -> None:
    guard = root / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(NETWORK_GUARD)
    env["PYTHONPATH"] = str(guard)
    env["VERIFY_GUARD_PATH"] = str(guard / "sitecustomize.py")
    env["VERIFY_NETWORK_LOG"] = str(root / "network-attempts.log")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env[name] = "http://127.0.0.1:9"
    env["NO_PROXY"] = ""


def run_installed_probe(python: Path, distribution: str, scripts: dict[str, str], cwd: Path, env: dict[str, str]) -> None:
    probe = Path(__file__).with_name("verify_installed.py").resolve()
    subprocess.run([str(python), "-I", str(probe), distribution, json.dumps(scripts)],
                   cwd=cwd, env=env, check=True, timeout=180)


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
        install_network_guard(root, env)
        run_installed_probe(python, "warcraft", expected_scripts, cwd, env)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    verify_wheel(parser.parse_args().wheel)


if __name__ == "__main__":
    main()
