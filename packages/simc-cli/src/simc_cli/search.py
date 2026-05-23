from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from simc_cli.repo import RepoPaths


@dataclass(slots=True)
class SearchHit:
    path: Path
    line_no: int
    text: str


def _fuzzy_glob(base: Path, needle: str, pattern: str = "*") -> list[Path]:
    compact = needle.replace("_", "").replace("-", "")
    matches: list[Path] = []
    for path in base.rglob(pattern):
        normalized = path.stem.lower().replace("_", "").replace("-", "")
        if compact in normalized:
            matches.append(path)
    return sorted(matches)


def _rg_files(needle: str, base: Path, pattern: str) -> list[Path]:
    proc = subprocess.run(["rg", "-l", "-i", needle, str(base), "-g", pattern], capture_output=True, text=True, check=False)
    return sorted(Path(line) for line in proc.stdout.splitlines() if line.strip())


def _run_rg(pattern: str, paths: list[Path]) -> list[SearchHit]:
    proc = subprocess.run(["rg", "-n", "--no-heading", pattern, *[str(path)
                          for path in paths if path.exists()]], capture_output=True, text=True, check=False)
    hits: list[SearchHit] = []
    for line in proc.stdout.splitlines():
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
    pattern = rf"\b{action}\b"
    return {name: _run_rg(pattern, roots) for name, roots in search_roots.items()}
