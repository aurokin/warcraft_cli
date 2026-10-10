"""Actual filesystem aliases must be refused before an APL workflow changes any input."""

from pathlib import Path

import pytest

from tests.test_simc_cli import _invoke


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
@pytest.mark.parametrize("command", ["validate-apl", "compare-apls"])
def test_apl_workflows_preserve_inputs_aliased_by_existing_outputs(tmp_path: Path, alias: str, command: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    harness = source / "harness.simc"
    harness.write_text('deathknight="actor"\nspec=frost\n')
    apl = source / "apl.simc"
    apl.write_text("actions=obliterate\n")
    output = out / "base.simc"
    if alias == "symlink":
        output.symlink_to(harness)
    else:
        output.hardlink_to(harness)
    before = {path: path.read_bytes() for path in (harness, apl, output)}
    args = [str(harness), str(apl), "--label", "base"] if command == "validate-apl" else [str(harness), "--base-apl", str(apl)]
    code, payload = _invoke(tmp_path, command, *args, "--out-dir", str(out))
    assert (code, payload["error"]["code"]) == (2, "invalid_query")
    assert all(path.read_bytes() == contents for path, contents in before.items())
    assert set(out.iterdir()) == {output}
    assert output.samefile(harness)
