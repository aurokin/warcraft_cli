from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


def find_env_file(filename: str = ".env.local", *, start_dir: str | Path | None = None) -> Path | None:
    """Locate ``filename`` in ``start_dir`` or its ancestors, never above the enclosing git repository root.

    Without a ``.git`` ancestor only ``start_dir`` itself is checked, so an env file in an unrelated
    parent checkout is never picked up.
    """
    root = Path(start_dir or Path.cwd()).expanduser().resolve()
    candidate_dirs: list[Path] = []
    for directory in (root, *root.parents):
        candidate_dirs.append(directory)
        if (directory / ".git").exists():
            break
    else:
        candidate_dirs = [root]
    for candidate_dir in candidate_dirs:
        candidate = candidate_dir / filename
        if candidate.is_file():
            return candidate
    return None


def _parse_env_line(raw_line: str) -> tuple[str, str] | None:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[7:].lstrip()
    key, separator, value = line.partition("=")
    if separator != "=":
        return None
    env_key = key.strip()
    if not env_key:
        return None
    env_value = value.strip()
    if len(env_value) >= 2 and env_value[0] == env_value[-1] and env_value[0] in {"'", '"'}:
        env_value = env_value[1:-1]
    return env_key, env_value


def read_env_keys(path: str | Path, keys: Iterable[str]) -> dict[str, str]:
    """Return the requested keys from an env file without touching ``os.environ``."""
    candidate = Path(path).expanduser()
    wanted = set(keys)
    found: dict[str, str] = {}
    if not candidate.is_file():
        return found
    for raw_line in candidate.read_text().splitlines():
        parsed = _parse_env_line(raw_line)
        if parsed is not None and parsed[0] in wanted:
            found[parsed[0]] = parsed[1]
    return found
