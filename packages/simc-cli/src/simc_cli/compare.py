from __future__ import annotations

import hashlib
import json
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from warcraft_core.paths import provider_data_root

from simc_cli.build_input import BuildSpec, build_profile_text
from simc_cli.repo import RepoPaths
from simc_cli.run import CommandResult, repo_git_status, run_profile


@dataclass(slots=True)
class ValidationResult:
    profile_path: Path
    result: CommandResult


@dataclass(slots=True)
class VariantSummary:
    label: str
    apl_path: Path
    profile_path: Path
    json_path: Path
    dps: float
    dps_error: float | None
    fight_length: float | None
    action_counts: dict[str, int]
    action_cpm: dict[str, float]


def default_harness_dir() -> Path:
    return provider_data_root("simc") / "harnesses"


def default_compare_dir() -> Path:
    return provider_data_root("simc") / "compare"


def write_harness(
    build_spec: BuildSpec,
    *,
    lines: list[str],
    out_path: str | Path | None = None,
) -> Path:
    payload = _render_harness_text(build_spec, lines=lines)
    if out_path is None:
        target_dir = default_harness_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        actor = build_spec.actor_class or "actor"
        spec = build_spec.spec or "spec"
        target = target_dir / f"{actor}_{spec}_harness.simc"
    else:
        target = Path(out_path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload)
    return target


def validate_profile_file(paths: RepoPaths, profile_path: str | Path, *, simc_args: list[str] | None = None) -> ValidationResult:
    args = ["iterations=1", "threads=1", "target_error=0"] + list(simc_args or [])
    result = run_profile(paths, profile_path, simc_args=args)
    return ValidationResult(profile_path=Path(profile_path).expanduser().resolve(), result=result)


def build_variant_profile(harness_path: str | Path, apl_path: str | Path, *, label: str, out_dir: str | Path | None = None) -> Path:
    harness = Path(harness_path).expanduser().resolve()
    apl = Path(apl_path).expanduser().resolve()
    if not harness.exists():
        raise FileNotFoundError(f"Harness profile not found: {harness}")
    if not apl.exists():
        raise FileNotFoundError(f"APL file not found: {apl}")
    if out_dir is None:
        target_dir = Path(tempfile.mkdtemp(prefix="simc-cli-variant-"))
    else:
        target_dir = Path(out_dir).expanduser().resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{label}.simc"
    target.write_text(_merge_harness_and_apl(harness.read_text(), apl.read_text()))
    return target


def compare_apl_variants(
    paths: RepoPaths,
    *,
    harness_path: str | Path,
    base_label: str,
    base_apl_path: str | Path,
    variant_specs: list[tuple[str, str | Path]],
    iterations: int,
    threads: int,
    out_dir: str | Path | None = None,
    validate_first: bool = True,
) -> dict[str, Any]:
    compare_dir = _resolve_compare_dir(out_dir)
    base_profile = build_variant_profile(harness_path, base_apl_path, label=base_label, out_dir=compare_dir)
    profiles: list[tuple[str, Path, Path]] = [(base_label, Path(base_apl_path).expanduser().resolve(), base_profile)]
    for label, apl_path in variant_specs:
        profiles.append((label, Path(apl_path).expanduser().resolve(), build_variant_profile(
            harness_path, apl_path, label=label, out_dir=compare_dir)))

    validations: list[dict[str, Any]] = []
    if validate_first:
        for label, _apl_path, profile_path in profiles:
            validation = validate_profile_file(paths, profile_path)
            validations.append(
                {
                    "label": label,
                    "profile_path": str(profile_path),
                    "returncode": validation.result.returncode,
                    "stdout_preview": _preview_lines(validation.result.stdout),
                    "stderr_preview": _preview_lines(validation.result.stderr),
                    "valid": validation.result.returncode == 0,
                }
            )
            if validation.result.returncode != 0:
                raise RuntimeError(f"Validation failed for {label}: {validation.result.stderr.strip() or validation.result.stdout.strip()}")

    summaries = [
        _simulate_variant(paths, label=label, apl_path=apl_path, profile_path=profile_path,
                          iterations=iterations, threads=threads, out_dir=compare_dir)
        for label, apl_path, profile_path in profiles
    ]
    base = summaries[0]
    ranking = sorted(summaries, key=lambda item: item.dps, reverse=True)
    return {
        "kind": "apl_comparison",
        "compare_dir": str(compare_dir),
        "harness_path": str(Path(harness_path).expanduser().resolve()),
        "iterations": iterations,
        "threads": threads,
        "validations": validations,
        "base": _summary_payload(base),
        "ranking": [_summary_payload(row) for row in ranking],
        "comparisons": [_comparison_payload(base, row) for row in summaries[1:]],
    }


def variant_report_payload(report: dict[str, Any]) -> dict[str, Any]:
    ranking = report.get("ranking") if isinstance(report.get("ranking"), list) else []
    best = ranking[0] if ranking else None
    base = report.get("base") if isinstance(report.get("base"), dict) else None
    comparisons = report.get("comparisons") if isinstance(report.get("comparisons"), list) else []
    return {
        "kind": "apl_variant_report",
        "base_label": base.get("label") if base else None,
        "best_label": best.get("label") if best else None,
        "best_dps": best.get("dps") if best else None,
        "ranking": [
            {
                "label": row.get("label"),
                "dps": row.get("dps"),
                "delta_vs_base": (
                    0.0
                    if base and row.get("label") == base.get("label")
                    else _comparison_delta_for(row.get("label"), comparisons)
                ),
                "percent_vs_base": (
                    0.0
                    if base and row.get("label") == base.get("label")
                    else _comparison_percent_for(row.get("label"), comparisons)
                ),
            }
            for row in ranking
        ],
        "comparisons": comparisons,
    }


def verify_clean_payload(paths: RepoPaths, *, hash_binary: bool) -> dict[str, Any]:
    git = repo_git_status(paths)
    binary = paths.build_simc
    binary_info: dict[str, Any] = {
        "path": str(binary),
        "exists": binary.exists(),
        "mtime": binary.stat().st_mtime if binary.exists() else None,
        "size": binary.stat().st_size if binary.exists() else None,
    }
    if hash_binary and binary.exists():
        binary_info["sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    return {
        "kind": "verify_clean",
        "repo_root": str(paths.root),
        "git": git,
        "binary": binary_info,
    }


def _render_harness_text(build_spec: BuildSpec, *, lines: list[str]) -> str:
    payload = build_profile_text(build_spec).rstrip() + "\n"
    normalized_lines = _normalized_lines(lines)
    if not any(line.startswith("load_default_gear=") for line in normalized_lines):
        normalized_lines.append("load_default_gear=1")
    if not any(line.startswith("load_default_talents=") for line in normalized_lines):
        normalized_lines.append("load_default_talents=1")
    if not any(line.startswith("allow_experimental_specializations=") for line in normalized_lines):
        normalized_lines.append("allow_experimental_specializations=1")
    return payload + "".join(f"{line}\n" for line in normalized_lines)


def _normalized_lines(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if line.strip()]


def _merge_harness_and_apl(harness_text: str, apl_text: str) -> str:
    return harness_text.rstrip() + "\n" + apl_text.lstrip()


def _resolve_compare_dir(out_dir: str | Path | None) -> Path:
    if out_dir is None:
        base_dir = default_compare_dir()
        base_dir.mkdir(parents=True, exist_ok=True)
        target = Path(tempfile.mkdtemp(prefix="simc-cli-compare-", dir=str(base_dir)))
    else:
        target = Path(out_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    return target


def _simulate_variant(
    paths: RepoPaths,
    *,
    label: str,
    apl_path: Path,
    profile_path: Path,
    iterations: int,
    threads: int,
    out_dir: Path,
) -> VariantSummary:
    json_path = out_dir / f"{label}.json"
    result = run_profile(
        paths,
        profile_path,
        simc_args=[
            f"iterations={iterations}",
            f"threads={threads}",
            "target_error=0",
            f"json2={json_path}",
        ],
    )
    if result.returncode != 0:
        raise RuntimeError(f"Simulation failed for {label}: {result.stderr.strip() or result.stdout.strip()}")
    report = json.loads(json_path.read_text())
    return _extract_summary(label=label, apl_path=apl_path, profile_path=profile_path, json_path=json_path, report=report)


def _extract_summary(*, label: str, apl_path: Path, profile_path: Path, json_path: Path, report: dict[str, Any]) -> VariantSummary:
    sim = report.get("sim") if isinstance(report, dict) else None
    players = sim.get("players") if isinstance(sim, dict) else None
    if not isinstance(players, list) or not players:
        raise RuntimeError("SimC JSON report did not contain players.")
    player = players[0]
    collected = player.get("collected_data") if isinstance(player, dict) else None
    if not isinstance(collected, dict):
        raise RuntimeError("SimC JSON report did not contain collected_data.")
    dps = _metric_mean(collected.get("dps"))
    dps_error = _metric_mean(collected.get("dpse"))
    fight_length = _metric_mean(collected.get("fight_length"))
    action_counts = _action_counts(collected.get("action_sequence"))
    action_cpm = _action_cpm(action_counts, fight_length)
    return VariantSummary(
        label=label,
        apl_path=apl_path,
        profile_path=profile_path,
        json_path=json_path,
        dps=dps or 0.0,
        dps_error=dps_error,
        fight_length=fight_length,
        action_counts=action_counts,
        action_cpm=action_cpm,
    )


def _metric_mean(metric: Any) -> float | None:
    if isinstance(metric, dict):
        value = metric.get("mean")
        if isinstance(value, (int, float)):
            return float(value)
    if isinstance(metric, (int, float)):
        return float(metric)
    return None


def _action_counts(sequence: Any) -> dict[str, int]:
    counts: Counter[str] = Counter()
    if not isinstance(sequence, list):
        return {}
    for row in sequence:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("spell_name") or "").strip().lower()
        if name:
            counts[name] += 1
    return dict(counts)


def _action_cpm(action_counts: dict[str, int], fight_length: float | None) -> dict[str, float]:
    if not fight_length or fight_length <= 0:
        return {}
    return {name: round(count * 60.0 / fight_length, 2) for name, count in action_counts.items()}


def _summary_payload(summary: VariantSummary) -> dict[str, Any]:
    return {
        "label": summary.label,
        "apl_path": str(summary.apl_path),
        "profile_path": str(summary.profile_path),
        "json_path": str(summary.json_path),
        "dps": round(summary.dps, 2),
        "dps_error": round(summary.dps_error, 2) if summary.dps_error is not None else None,
        "fight_length": round(summary.fight_length, 3) if summary.fight_length is not None else None,
        "action_counts": summary.action_counts,
        "action_cpm": summary.action_cpm,
    }


def _comparison_payload(base: VariantSummary, current: VariantSummary) -> dict[str, Any]:
    delta = current.dps - base.dps
    percent = (delta / base.dps * 100.0) if base.dps else 0.0
    return {
        "label": current.label,
        "base_label": base.label,
        "dps_delta": round(delta, 2),
        "percent_delta": round(percent, 2),
        "top_action_deltas": _top_action_deltas(base, current),
    }


def _top_action_deltas(base: VariantSummary, current: VariantSummary, *, limit: int = 8) -> list[dict[str, Any]]:
    names = set(base.action_cpm) | set(current.action_cpm)
    rows: list[dict[str, Any]] = []
    for name in names:
        base_cpm = float(base.action_cpm.get(name, 0.0))
        current_cpm = float(current.action_cpm.get(name, 0.0))
        delta = current_cpm - base_cpm
        if delta == 0:
            continue
        rows.append(
            {
                "action": name,
                "base_cpm": base_cpm,
                "current_cpm": current_cpm,
                "delta_cpm": round(delta, 2),
            }
        )
    rows.sort(key=lambda row: abs(float(row["delta_cpm"])), reverse=True)
    return rows[:limit]


def _comparison_delta_for(label: Any, comparisons: list[Any]) -> float | None:
    for row in comparisons:
        if isinstance(row, dict) and row.get("label") == label:
            value = row.get("dps_delta")
            return float(value) if isinstance(value, (int, float)) else None
    return None


def _comparison_percent_for(label: Any, comparisons: list[Any]) -> float | None:
    for row in comparisons:
        if isinstance(row, dict) and row.get("label") == label:
            value = row.get("percent_delta")
            return float(value) if isinstance(value, (int, float)) else None
    return None


def _preview_lines(text: str, *, max_lines: int = 20) -> list[str]:
    return text.splitlines()[:max_lines]
