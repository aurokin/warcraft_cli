# Code review workflow

Automated reviewers (Codex, Cursor/Bugbot) catch different issues and often surface more findings on a second pass. Treat review as a **loop**, not a one-shot gate before merge.

## Before opening a PR

1. `make lint` and `make typecheck`
2. `pytest -q` for the areas you touched (provider package tests + any shared core tests)
3. If you changed live contracts: run the matching live target locally (`make test-live-matrix`, provider live flags — see `docs/USAGE.md`)

## Request reviews

Post **separate** review triggers (one comment each) so each bot runs independently:

```bash
make request-pr-reviews PR=42
```

Or manually:

```bash
gh pr comment 42 --body "@codex review"
gh pr comment 42 --body "@cursor review"
```

## Triage review feedback

List inline review comments and threads:

```bash
make review-comments PR=42
```

For each finding:

1. **Reproduce** — confirm on the branch (run the cited command/test or read the cited lines).
2. **Fix or dismiss** — fix valid bugs; reply on the thread with rationale when the bot is wrong or the change is intentional product policy.
3. **Resolve** the GitHub thread after fix or documented dismissal.
4. **Re-request** reviews after pushing fixes (`make request-pr-reviews PR=42` again).

Repeat until:

- No unresolved **high/medium** findings remain, or
- Remaining items are explicitly accepted with a short note on the thread.

## What not to rely on

- A single green CI run does not replace review triage.
- Review bots may miss issues on the first pass; a second `@codex review` / `@cursor review` after fixes often finds regressions or adjacent bugs.
- Do not merge with known high-severity inline comments still open unless the team explicitly accepts the risk on the thread.

## Agent checklist (copy/paste)

```text
- [ ] lint + typecheck green
- [ ] targeted pytest green
- [ ] @codex review posted
- [ ] @cursor review posted
- [ ] all review threads triaged (fix or reply + resolve)
- [ ] second review pass after fixes (if first pass had medium+ findings)
- [ ] CHANGELOG [Unreleased] updated for user-visible CLI/output changes
```
