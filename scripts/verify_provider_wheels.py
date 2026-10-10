#!/usr/bin/env python3
"""Build provider wheels and exercise each installation using its declared dependencies alone."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from verify_wheel import install_network_guard, isolated_env, run_installed_probe


def verify_provider_wheels(packages: list[str] | None = None) -> None:
    repo = Path(__file__).resolve().parents[1]
    manifests = {path.parent.name: tomllib.loads(path.read_text())["project"]
                 for path in sorted((repo / "packages").glob("*/pyproject.toml"))}
    providers = [name for name, project in manifests.items() if project.get("scripts") and name != "warcraft-cli"]
    selected = providers if packages is None else packages
    unknown = set(selected) - {*providers, "warcraft-cli"}
    if unknown:
        raise ValueError(f"Unknown provider packages: {sorted(unknown)}")
    with tempfile.TemporaryDirectory(prefix="warcraft-provider-wheels-") as directory:
        root = Path(directory).resolve()
        if root.is_relative_to(repo):
            raise RuntimeError("Provider verification needs a temporary directory outside the checkout")
        wheelhouse = root / "wheels"
        build_names = list(dict.fromkeys(("warcraft-core", "warcraft-api", "warcraft-content",
                                         *(providers if "warcraft-cli" in selected else ()), *selected)))
        for name in build_names:
            subprocess.run(["uv", "build", "--wheel", str(repo / "packages" / name), "--out-dir", str(wheelhouse)], check=True)
        constraints = root / "local-constraints.txt"
        built_wheels = {name: wheelhouse / f"{manifests[name]['name'].replace('-', '_')}-{manifests[name]['version']}-py3-none-any.whl"
                        for name in build_names}
        constraints.write_text("\n".join(f"{manifests[name]['name']} @ {wheel.as_uri()}" for name, wheel in built_wheels.items()))
        for name in selected:
            project = manifests[name]
            run_root = root / name
            run_root.mkdir()
            cwd = run_root / "work"
            cwd.mkdir()
            env = isolated_env(run_root)
            venv = run_root / "venv"
            subprocess.run(["uv", "venv", str(venv), "--python", sys.executable], cwd=cwd, env=env, check=True)
            python = venv / "bin" / "python"
            wheel = built_wheels[name]
            # Only the requested wheel is installed explicitly. Shared dependencies must be declared.
            subprocess.run(["uv", "pip", "install", "--python", str(python), "--find-links", str(wheelhouse),
                            "--constraint", str(constraints), str(wheel)],
                           cwd=cwd, env=env, check=True)
            subprocess.run(["uv", "pip", "check", "--python", str(python)], cwd=cwd, env=env, check=True)
            install_network_guard(run_root, env)
            run_installed_probe(python, project["name"], project["scripts"], cwd, env)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", action="append", help="Package directory name (including warcraft-cli); defaults to every provider")
    verify_provider_wheels(parser.parse_args().package)


if __name__ == "__main__":
    main()
