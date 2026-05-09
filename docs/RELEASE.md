# Releases

The workspace ships under one version. Every `pyproject.toml` in the repo (root + 11 packages) bumps together, and `CHANGELOG.md` at the repo root is the source of truth for what shipped.

## During Normal Development

When a PR introduces a user-visible change (CLI flag, output shape, behavior, removed command, fixed bug), add an entry under the matching `### Added/Changed/Fixed/Removed/Deprecated` heading inside `## [Unreleased]` in `CHANGELOG.md`. Same PR — not later. The point of the in-tree changelog is to avoid reconstructing release notes from memory at tag time.

Internal refactors, doc-only edits, and test-only changes don't need a changelog entry.

## Cutting a Release

1. **Confirm `[Unreleased]` covers what's about to ship.** Skim `git log` since the previous tag and reconcile against the changelog.
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

   This runs `scripts/bump_version.py`, which validates the new version is semver, errors if the current versions across the workspace disagree, and rewrites all 12 files in place. It does not stage or commit.
4. **Review and commit.**

   ```bash
   git diff
   git add CHANGELOG.md pyproject.toml packages/*/pyproject.toml
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

## Picking the Version

- **Patch (`0.3.0` → `0.3.1`)**: bug fixes only, no new flags or output changes.
- **Minor (`0.3.0` → `0.4.0`)**: new commands, new flags, new fields in JSON output, additive behavior.
- **Major (`0.3.0` → `1.0.0`)**: removed/renamed commands or flags, JSON output shape changes that break consumers, auth scope changes that require re-login.

Pre-1.0 we still try to follow the spirit of semver — flag the breaking part of a release in `### Removed`/`### Changed` so consumers know what to update.
