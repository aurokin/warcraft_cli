from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_generate_provider_skills_creates_consumer_facing_outputs(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / "scripts" / "generate_provider_skills.py"
    output_dir = tmp_path / "skills"

    result = subprocess.run(
        [sys.executable, str(script), "--provider", "wowhead", "--provider", "raiderio", "--output-dir", str(output_dir)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )

    assert str(output_dir / "wowhead") in result.stdout
    assert str(output_dir / "raiderio") in result.stdout

    wowhead_skill = (output_dir / "wowhead" / "SKILL.md").read_text(encoding="utf-8")
    raiderio_skill = (output_dir / "raiderio" / "SKILL.md").read_text(encoding="utf-8")
    wowhead_yaml = (output_dir / "wowhead" / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert "name: wowhead" in wowhead_skill
    assert "# Wowhead" in wowhead_skill
    assert "For source selection across providers, start with `warcraft` first." in wowhead_skill
    assert "Do not mention generation" not in wowhead_skill
    assert "generated" not in wowhead_skill.lower()

    assert "name: raiderio" in raiderio_skill
    assert "# Raider.IO" in raiderio_skill
    assert "sample mythic-plus-runs" in raiderio_skill
    assert 'display_name: "Wowhead"' in wowhead_yaml


def test_generator_covers_every_wrapper_provider() -> None:
    """The consumer subskills must not silently lag the wrapper's provider registry."""
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root / "scripts"))
    try:
        from generate_provider_skills import PROVIDERS as SKILLS
    finally:
        sys.path.remove(str(repo_root / "scripts"))

    from warcraft_cli.providers import PROVIDERS

    assert set(SKILLS) == {registration.name for registration in PROVIDERS}
