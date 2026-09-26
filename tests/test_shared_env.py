from __future__ import annotations

import os
from pathlib import Path

from warcraft_core.env import find_env_file, read_env_keys


def test_find_env_file_stops_at_git_repo_root(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    repo = outer / "repo"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()
    (outer / ".env.local").write_text("OUTER=1\n")

    assert find_env_file(start_dir=sub) is None

    (repo / ".env.local").write_text("REPO=1\n")
    assert find_env_file(start_dir=sub) == repo / ".env.local"


def test_find_env_file_without_git_ancestor_checks_only_start_dir(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / ".env.local").write_text("PARENT=1\n")

    assert find_env_file(start_dir=child) is None

    (child / ".env.local").write_text("CHILD=1\n")
    assert find_env_file(start_dir=child) == child / ".env.local"


def test_read_env_keys_handles_quotes_and_export_without_touching_environ(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "\n".join(
            [
                "# comment",
                "export WCL_ID='abc'",
                'WCL_SECRET="s3cr3t"',
                "OTHER_KEY=ignored",
                "MALFORMED",
            ]
        )
        + "\n"
    )
    for key in ("WCL_ID", "WCL_SECRET", "OTHER_KEY"):
        monkeypatch.delenv(key, raising=False)
    before = dict(os.environ)

    values = read_env_keys(env_file, ["WCL_ID", "WCL_SECRET", "MISSING"])

    assert values == {"WCL_ID": "abc", "WCL_SECRET": "s3cr3t"}
    assert dict(os.environ) == before


def test_read_env_keys_returns_empty_for_missing_file(tmp_path: Path) -> None:
    assert read_env_keys(tmp_path / "absent.env", ["A"]) == {}
