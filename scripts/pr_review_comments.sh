#!/usr/bin/env bash
set -euo pipefail

pr="${1:-${PR:-}}"
if [[ -z "${pr}" ]]; then
  echo "usage: $0 <pr-number>   (or set PR=)" >&2
  exit 1
fi

repo="$(gh repo view --json nameWithOwner -q .nameWithOwner)"

echo "# Review comments for ${repo}#${pr}"
echo

gh api "repos/${repo}/pulls/${pr}/comments" --paginate \
  --jq '.[] | "[\(.user.login)] \(.path // "general"):\(.line // "?")\n\(.body | split("\n")[0:8] | join("\n"))\n---"'

echo
echo "# Review threads (unresolved first)"
gh api graphql -f query='
  query($owner: String!, $name: String!, $number: Int!) {
    repository(owner: $owner, name: $name) {
      pullRequest(number: $number) {
        reviewThreads(first: 100) {
          nodes {
            isResolved
            comments(first: 1) {
              nodes {
                author { login }
                path
                line
                body
              }
            }
          }
        }
      }
    }
  }
' -f owner="${repo%%/*}" -f name="${repo##*/}" -F number="${pr}" \
  --jq '.data.repository.pullRequest.reviewThreads.nodes
    | sort_by(.isResolved)
    | .[]
    | select(.comments.nodes[0] != null)
    | .comments.nodes[0] as $c
    | "[\($c.author.login)] resolved=\(.isResolved) \($c.path // "general"):\($c.line // "?")\n\($c.body | split("\n")[0:6] | join("\n"))\n---"'
