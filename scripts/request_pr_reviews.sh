#!/usr/bin/env bash
set -euo pipefail

pr="${1:-${PR:-}}"
if [[ -z "${pr}" ]]; then
  echo "usage: $0 <pr-number>   (or set PR=)" >&2
  exit 1
fi

gh pr comment "${pr}" --body "@codex review"
gh pr comment "${pr}" --body "@cursor review"
echo "Requested Codex and Cursor reviews on PR #${pr}"
