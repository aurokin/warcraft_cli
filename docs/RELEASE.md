# Releases

The workspace ships under one version. Every `pyproject.toml` in the repo (root + packages) bumps together, and `CHANGELOG.md` at the repo root is the source of truth for what shipped.

## During Normal Development

When a PR introduces a user-visible change (CLI flag, output shape, behavior, removed command, fixed bug), add an entry under the matching `### Added/Changed/Fixed/Removed/Deprecated` heading inside `## [Unreleased]` in `CHANGELOG.md`. Same PR — not later. The point of the in-tree changelog is to avoid reconstructing release notes from memory at tag time.

Internal refactors, doc-only edits, and test-only changes don't need a changelog entry.

## Cutting a Release

1. **Confirm `[Unreleased]` covers what's about to ship.** Skim `git log` since the previous tag and reconcile against the changelog. While you are there, reconcile the docs that carry release-coupled facts:
   - [ROADMAP.md](ROADMAP.md) — tiers, `## Next`, and deferred candidates still true?
   - `README.md` — is the install block still true?
2. **Move `[Unreleased]` content into a new versioned section.**
   - Rename the heading: `## [Unreleased]` → `## [X.Y.Z] - YYYY-MM-DD` (today's date, ISO).
   - Add a fresh empty `## [Unreleased]` block above it with the standard subheads.
   - Drop empty subheads (e.g. delete `### Deprecated` if there's nothing under it).
   - Update the compare links at the bottom of the file:
     - `[Unreleased]: …/compare/vX.Y.Z...HEAD`
     - `[X.Y.Z]: …/compare/v<previous>...vX.Y.Z`
3. **Bump every `pyproject.toml`.**

   ```bash
   make release VERSION=X.Y.Z
   ```

   This runs `scripts/bump_version.py`, which validates the new version is semver, errors if the current versions across the workspace disagree, and rewrites every workspace `pyproject.toml` in place. It does not stage or commit.

   Refresh the lockfile after every version bump, even when dependencies are unchanged. It records the editable root package version, and release CI installs with `uv sync --frozen`:

   ```bash
   uv lock
   uv lock --check
   make check
   make build
   ```
4. **Review and commit.**

   ```bash
   git diff
   git add CHANGELOG.md README.md docs/ROADMAP.md docs/RELEASE.md Makefile pyproject.toml packages/*/pyproject.toml uv.lock
   git commit -m "Release vX.Y.Z" -m "Co-Authored-By: OpenAI Codex <noreply@openai.com>"
   git push
   ```
5. **Tag and publish.**

   Copy the exact `## [X.Y.Z]` section, stopping before the next version heading, into a scratch notes file such as `/tmp/warcraft-release-notes.md`. Review that file before publishing; the release body must match the changelog section.

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   if gh release view vX.Y.Z >/dev/null 2>&1; then
     gh release edit vX.Y.Z --notes-file /tmp/warcraft-release-notes.md
   else
     gh release create vX.Y.Z --notes-file /tmp/warcraft-release-notes.md
   fi
   ```

   Pushing the tag triggers `.github/workflows/release.yml`, which runs `make check` on the tagged commit and only then builds and attaches the wheel. A failed check publishes no wheel. The workflow can create the release before the notes command runs, so edit an existing release or create one when absent. Wait for the workflow to finish and confirm the attachment before checking installation.

6. **Verify the published wheel.**

   ```bash
   pipx install --force https://github.com/aurokin/warcraft_cli/releases/download/vX.Y.Z/warcraft-X.Y.Z-py3-none-any.whl
   uvx --from https://github.com/aurokin/warcraft_cli/releases/download/vX.Y.Z/warcraft-X.Y.Z-py3-none-any.whl warcraft doctor
   ```

   `warcraft doctor` on a clean machine is the check that the wheel actually ships every provider package.

## Picking the Version

- **Patch (`0.3.0` → `0.3.1`)**: bug fixes only, no new flags or output changes.
- **Minor (`0.3.0` → `0.4.0`)**: new commands, new flags, new fields in JSON output, additive behavior.
- **Major (`0.3.0` → `1.0.0`)**: removed/renamed commands or flags, JSON output shape changes that break consumers, auth scope changes that require re-login.

"Output shape" means the shared envelope in [foundation/ERROR_CONTRACT.md](foundation/ERROR_CONTRACT.md) plus the documented `data` payload of a command.

Pre-1.0 we still try to follow the spirit of semver — flag the breaking part of a release in `### Removed`/`### Changed` so consumers know what to update.
