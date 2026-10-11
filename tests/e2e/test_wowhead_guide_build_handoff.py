"""A guide's published loadout references survive bundle export and reach SimC unchanged."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.e2e.harness import run

TALENT_GUIDE = "https://www.wowhead.com/guide/classes/monk/mistweaver/talent-builds-pve-healer"
PUBLISHED_BLIZZARD_CODE = re.compile(r"/talent-calc/blizzard/([A-Za-z0-9+/]{40,})")


@pytest.fixture
def published_build_bundle(require, tmp_path_factory) -> tuple[Path, list[dict]]:
    require("wowhead")
    output = tmp_path_factory.mktemp("published-wowhead-builds") / "bundle"
    result = run("wowhead", "guide-export", TALENT_GUIDE, "--out", str(output), "--max-links", "25")
    manifest = json.loads((output / "manifest.json").read_text())
    refs = [json.loads(line) for line in (output / manifest["files"]["build_references_jsonl"]).read_text().splitlines()]
    # Read the provider-authored guide body independently of the normalized-reference extractor.
    published = set(PUBLISHED_BLIZZARD_CODE.findall((output / "body.markup.txt").read_text()))
    assert published, "the guide no longer publishes native Blizzard calculator references"
    actual = {row["build_code"] for row in refs if row["reference_type"] == "wow_talent_export"}
    assert published <= actual, result.describe()
    assert manifest["counts"]["build_references"] == len(refs), result.describe()
    return output, refs


def test_guide_bundle_preserves_published_codes_and_original_urls(published_build_bundle):
    output, refs = published_build_bundle
    guide = json.loads((output / "guide.json").read_text())
    assert guide["build_references"]["items"] == refs
    for row in refs:
        if row["reference_type"] == "wow_talent_export" and "original_ref" in row["source"]:
            assert row["build_identity"]["status"] == "unknown"
            assert row["build_code"] in row["source"]["original_ref"]
            assert row["source_url"] == TALENT_GUIDE
            assert any(row["build_code"] in citation["url"] for citation in row["citations"])


def test_published_guide_build_reaches_simc_without_inferred_guide_identity(require, published_build_bundle):
    require("wowhead", "simc")
    _, refs = published_build_bundle
    row = next(row for row in refs if row["reference_type"] == "wow_talent_export" and "original_ref" in row["source"])
    identified = run("simc", "identify-build", "--build-text", row["build_code"])
    assert identified.data["build_spec"]["talents"] == row["build_code"], identified.describe()
    assert identified.data["identity"]["actor_class"] == "monk", identified.describe()
    assert identified.data["identity"]["spec"] == "mistweaver", identified.describe()
    assert row["build_identity"]["status"] == "unknown", "guide extraction must not invent SimC's decoded identity"
