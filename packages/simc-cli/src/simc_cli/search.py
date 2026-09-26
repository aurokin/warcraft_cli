from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from simc_cli.repo import RepoPaths

RIPGREP = "rg"


class MissingRipgrepError(RuntimeError):
    """ripgrep is not on PATH; the content-search fallbacks cannot run."""

    def __init__(self) -> None:
        super().__init__(
            "ripgrep (rg) is required for simc content search and is not on PATH. "
            "Install it with 'brew install ripgrep' or your package manager."
        )


def ripgrep_available() -> bool:
    return shutil.which(RIPGREP) is not None


@dataclass(slots=True)
class SearchHit:
    path: Path
    line_no: int
    text: str


def word_bounded_pattern(action: str) -> str:
    r"""Escape a user query and anchor it with ``\b`` only where a word boundary can exist.

    ``\b`` matches between a word and a non-word character, so ``\bcast\(x\)\b`` can never match
    ``cast(x)`` followed by a space: the query's own punctuation makes the trailing boundary
    unsatisfiable. Anchoring is skipped on whichever end starts or ends with punctuation.
    """
    escaped = re.escape(action)
    prefix = r"\b" if action[:1].isalnum() or action[:1] == "_" else ""
    suffix = r"\b" if action[-1:].isalnum() or action[-1:] == "_" else ""
    return f"{prefix}{escaped}{suffix}"


def _fuzzy_glob(base: Path, needle: str, pattern: str = "*") -> list[Path]:
    compact = needle.replace("_", "").replace("-", "")
    matches: list[Path] = []
    for path in base.rglob(pattern):
        normalized = path.stem.lower().replace("_", "").replace("-", "")
        if compact in normalized:
            matches.append(path)
    return sorted(matches)


def _rg(args: list[str]) -> str:
    """Run ripgrep with a fixed argv (no shell), turning a missing binary into a typed error."""
    if not ripgrep_available():
        raise MissingRipgrepError
    proc = subprocess.run(  # noqa: S603
        [RIPGREP, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    # ripgrep exits 1 when nothing matched; anything above that is a real failure.
    if proc.returncode > 1:
        raise RuntimeError(f"ripgrep failed: {proc.stderr.strip() or proc.returncode}")
    return proc.stdout


def _rg_files(needle: str, base: Path, pattern: str) -> list[Path]:
    if not base.exists():
        return []
    stdout = _rg(["-l", "-i", "--fixed-strings", needle, str(base), "-g", pattern])
    return sorted(Path(line) for line in stdout.splitlines() if line.strip())


def _run_rg(pattern: str, paths: list[Path]) -> list[SearchHit]:
    existing = [str(path) for path in paths if path.exists()]
    if not existing:
        return []
    stdout = _rg(["-n", "--no-heading", pattern, *existing])
    hits: list[SearchHit] = []
    for line in stdout.splitlines():
        file_name, line_no, text = line.split(":", 2)
        hits.append(SearchHit(path=Path(file_name), line_no=int(line_no), text=text))
    return hits


def spec_file_search(paths: RepoPaths, query: str | None) -> dict[str, list[Path]]:
    if not query:
        return {
            "default_apl": sorted(paths.apl_default.glob("*.simc")),
            "assisted_apl": sorted(paths.apl_assisted.glob("*.simc")),
            "cpp": [],
            "hpp": [],
            "spell_dump": [],
        }
    q = query.lower()
    results = {
        "default_apl": sorted(p for p in paths.apl_default.glob("*.simc") if q in p.stem.lower()),
        "assisted_apl": sorted(p for p in paths.apl_assisted.glob("*.simc") if q in p.stem.lower()),
        "cpp": sorted(p for p in paths.class_modules.rglob("*.cpp") if q in p.name.lower()) or _rg_files(q, paths.class_modules, "*.cpp"),
        "hpp": sorted(p for p in paths.class_modules.rglob("*.hpp") if q in p.name.lower()) or _rg_files(q, paths.class_modules, "*.hpp"),
        "spell_dump": sorted(p for p in paths.spell_dump.glob("*.txt") if q in p.name.lower()) or _rg_files(q, paths.spell_dump, "*.txt"),
    }
    if not any(results.values()):
        results["default_apl"] = _fuzzy_glob(paths.apl_default, q, "*.simc")
        results["assisted_apl"] = _fuzzy_glob(paths.apl_assisted, q, "*.simc")
        results["cpp"] = _fuzzy_glob(paths.class_modules, q, "*.cpp")
        results["hpp"] = _fuzzy_glob(paths.class_modules, q, "*.hpp")
        results["spell_dump"] = _fuzzy_glob(paths.spell_dump, q, "*.txt")
    return results


def find_action(paths: RepoPaths, action: str, wow_class: str | None = None) -> dict[str, list[SearchHit]]:
    search_roots: dict[str, list[Path]] = {
        "apl_default": [paths.apl_default],
        "apl_assisted": [paths.apl_assisted],
        "class_modules": [paths.class_modules],
        "spell_dump": [paths.spell_dump],
    }
    if wow_class:
        lowered = wow_class.lower()
        class_modules = [path for path in paths.class_modules.rglob("*") if path.is_file() and lowered in path.name.lower()]
        spell_dump = [path for path in paths.spell_dump.glob("*.txt") if lowered in path.name.lower()]
        if class_modules:
            search_roots["class_modules"] = class_modules
        if spell_dump:
            search_roots["spell_dump"] = spell_dump
    pattern = word_bounded_pattern(action)
    return {name: _run_rg(pattern, roots) for name, roots in search_roots.items()}
