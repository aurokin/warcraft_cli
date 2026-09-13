# Releases

The workspace ships under one version. Every `pyproject.toml` in the repo (root + packages) bumps together, and `CHANGELOG.md` at the repo root is the source of truth for what shipped.

## During Normal Development

When a PR introduces a user-visible change (CLI flag, output shape, behavior, removed command, fixed bug), add an entry under the matching `### Added/Changed/Fixed/Removed/Deprecated` heading inside `## [Unreleased]` in `CHANGELOG.md`. Same PR — not later. The point of the in-tree changelog is to avoid reconstructing release notes from memory at tag time.

Internal refactors, doc-only edits, and test-only changes don't need a changelog entry.

## Cutting a Release

1. **Confirm `[Unreleased]` covers what's about to ship.** Skim `git log` since the previous tag and reconcile against the changelog. While you are there, reconcile the docs that carry release-coupled facts:
   - [ROADMAP.md](ROADMAP.md) — tiers, `## Next`, and deferred candidates still true?
   - `README.md` — the wheel URL in the install block carries a literal version; bump it to the version you are about to tag.
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

   If dependencies changed in this release, refresh the lockfile too so the CI install (`uv sync --frozen`) resolves the same set:

   ```bash
   uv lock
   ```
4. **Review and commit.**

   ```bash
   git diff
   git add CHANGELOG.md README.md docs/ROADMAP.md pyproject.toml packages/*/pyproject.toml uv.lock
   git commit -m "Release vX.Y.Z"
   git push
   ```
5. **Tag and publish.**

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   gh release create vX.Y.Z --notes-file <(awk '/^## \[X\.Y\.Z\]/,/^## \[/{print}' CHANGELOG.md | sed '$d')
   ```

   Or simpler: copy the `## [X.Y.Z]` section into a scratch file and pass it via `--notes-file`. The GitHub release body should match the changelog section verbatim so the two never drift.

   Pushing the tag triggers `.github/workflows/release.yml`, which builds the wheel and attaches it to the release. It works in either order: run before `gh release create` and the wheel lands on the release the workflow creates; run after and the wheel is added to the existing release.

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

"Output shape" means the shared envelope in [foundation/ERROR_CONTRACT.md](foundation/ERROR_CONTRACT.md) plus the documented `data` payload of a command. The deprecated top-level copies of payload keys are not part of the stable shape; removing them is still a major-version change, but adding envelope keys next to them is not.

Pre-1.0 we still try to follow the spirit of semver — flag the breaking part of a release in `### Removed`/`### Changed` so consumers know what to update.
