#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WARCRAFT_REFERENCES_DIR = REPO_ROOT / "skills" / "warcraft" / "references"
DEFAULT_OUTPUT_DIR = REPO_ROOT / ".generated-skills"


@dataclass(frozen=True)
class ProviderSkill:
    key: str
    display_name: str
    description: str
    short_description: str
    default_prompt: str
    intro: str


PROVIDERS: dict[str, ProviderSkill] = {
    "wowhead": ProviderSkill(
        key="wowhead",
        display_name="Wowhead",
        description=(
            "Use the local `wowhead` CLI for structured WoW lookups on Wowhead. "
            "Best for entities, guides, comments, timelines, and stable tool-state inspection "
            "when the caller already wants the Wowhead source."
        ),
        short_description="Use the local wowhead command for structured WoW lookups and timelines.",
        default_prompt=(
            "Use wowhead for entity, guide, comments, news, blue-tracker, or tool-state lookups "
            "when the caller already wants the Wowhead source."
        ),
        intro=(
            "Use `wowhead` when the caller already wants the Wowhead source or when `warcraft` "
            "has already routed you here."
        ),
    ),
    "method": ProviderSkill(
        key="method",
        display_name="Method.gg",
        description=(
            "Use the local `method` CLI for supported Method.gg guide and article lookups. "
            "Best for article-style guide content, export, and local query when the caller "
            "already wants Method.gg."
        ),
        short_description="Use the local method command for supported Method.gg guides and articles.",
        default_prompt=(
            "Use method for supported guide and article lookups when the caller already wants "
            "Method.gg or warcraft has already routed you there."
        ),
        intro=(
            "Use `method` when the caller already wants Method.gg or when `warcraft` has already "
            "routed you here."
        ),
    ),
    "icy-veins": ProviderSkill(
        key="icy-veins",
        display_name="Icy Veins",
        description=(
            "Use the local `icy-veins` CLI for structured guide-family lookups on Icy Veins. "
            "Best for spec guides, hubs, and guide subpages when the caller already wants Icy Veins."
        ),
        short_description="Use the local icy-veins command for Icy Veins guide lookups.",
        default_prompt=(
            "Use icy-veins for guide-family search, resolve, and guide fetch when the caller "
            "already wants Icy Veins or warcraft has already routed you there."
        ),
        intro=(
            "Use `icy-veins` when the caller already wants Icy Veins or when `warcraft` has already "
            "routed you here."
        ),
    ),
    "raiderio": ProviderSkill(
        key="raiderio",
        display_name="Raider.IO",
        description=(
            "Use the local `raiderio` CLI for Raider.IO profiles, Mythic+ runs, and sampled run "
            "analytics when the caller already wants the Raider.IO source."
        ),
        short_description="Use the local raiderio command for Raider.IO profiles and Mythic+ analytics.",
        default_prompt=(
            "Use raiderio for structured character or guild lookups and sampled Mythic+ analytics "
            "when the caller already wants Raider.IO or warcraft has routed you there."
        ),
        intro=(
            "Use `raiderio` when the caller already wants Raider.IO or when `warcraft` has already "
            "routed you here."
        ),
    ),
    "warcraft-wiki": ProviderSkill(
        key="warcraft-wiki",
        display_name="Warcraft Wiki",
        description=(
            "Use the local `warcraft-wiki` CLI for addon/API documentation, systems pages, lore, "
            "and reference lookups when the caller already wants Warcraft Wiki."
        ),
        short_description="Use the local warcraft-wiki command for Warcraft Wiki reference lookups.",
        default_prompt=(
            "Use warcraft-wiki for API, event, systems, lore, and reference lookups when the caller "
            "already wants Warcraft Wiki or warcraft has already routed you there."
        ),
        intro=(
            "Use `warcraft-wiki` when the caller already wants Warcraft Wiki or when `warcraft` "
            "has already routed you here."
        ),
    ),
    "wowprogress": ProviderSkill(
        key="wowprogress",
        display_name="WowProgress",
        description=(
            "Use the local `wowprogress` CLI for rankings, progression, guild profiles, and "
            "sample-backed analytics when the caller already wants WowProgress."
        ),
        short_description="Use the local wowprogress command for rankings, progression, and analytics.",
        default_prompt=(
            "Use wowprogress for guild, character, leaderboard, and sampled progression analytics "
            "when the caller already wants WowProgress or warcraft has already routed you there."
        ),
        intro=(
            "Use `wowprogress` when the caller already wants WowProgress or when `warcraft` has "
            "already routed you here."
        ),
    ),
    "simc": ProviderSkill(
        key="simc",
        display_name="SimulationCraft",
        description=(
            "Use the local `simc` CLI for SimulationCraft repo inspection, readonly analysis, "
            "build decoding, and local sim execution when the caller already wants SimulationCraft."
        ),
        short_description="Use the local simc command for SimulationCraft inspection and runs.",
        default_prompt=(
            "Use simc for local SimulationCraft inspection, APL analysis, build decoding, and sim "
            "execution when the caller already wants SimulationCraft or warcraft has routed you there."
        ),
        intro=(
            "Use `simc` when the caller already wants SimulationCraft or when `warcraft` has already "
            "routed you here."
        ),
    ),
}


def _reference_path(provider_key: str) -> Path:
    return WARCRAFT_REFERENCES_DIR / f"{provider_key}.md"


def _reference_body(provider_key: str) -> str:
    path = _reference_path(provider_key)
    text = path.read_text(encoding="utf-8").strip()
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).rstrip() + "\n"


def _skill_markdown(provider: ProviderSkill) -> str:
    body = _reference_body(provider.key)
    return (
        f"---\n"
        f"name: {provider.key}\n"
        f"description: {provider.description}\n"
        f"---\n\n"
        f"# {provider.display_name}\n\n"
        f"{provider.intro}\n\n"
        f"For source selection across providers, start with `warcraft` first.\n\n"
        f"{body}"
    )


def _openai_yaml(provider: ProviderSkill) -> str:
    return (
        "interface:\n"
        f'  display_name: "{provider.display_name}"\n'
        f'  short_description: "{provider.short_description}"\n'
        f'  default_prompt: "{provider.default_prompt}"\n'
    )


def generate_provider_skill(provider: ProviderSkill, *, output_dir: Path) -> Path:
    skill_dir = output_dir / provider.key
    agents_dir = skill_dir / "agents"
    skill_dir.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(_skill_markdown(provider), encoding="utf-8")
    (agents_dir / "openai.yaml").write_text(_openai_yaml(provider), encoding="utf-8")
    return skill_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate standalone provider consumer skills from the warcraft skill references."
    )
    parser.add_argument(
        "--provider",
        action="append",
        choices=sorted(PROVIDERS),
        help="Generate only the selected provider skill. Repeat to generate multiple.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory where generated skills will be written. Default: {DEFAULT_OUTPUT_DIR}",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    selected = args.provider or list(PROVIDERS)
    for key in selected:
        path = generate_provider_skill(PROVIDERS[key], output_dir=output_dir)
        print(path)


if __name__ == "__main__":
    main()
