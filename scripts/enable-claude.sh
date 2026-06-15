#!/usr/bin/env bash
# Enable the Meridian agents in a repo by opening a PR that adds the workflow
# stubs from examples/ — the dev agent (claude.yml) and the reviewer
# (claude-review.yml). Stubs already present are left untouched.
#
# Usage: scripts/enable-claude.sh <org/repo>
set -euo pipefail

REPO="${1:?usage: enable-claude.sh <org/repo>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXAMPLES="$SCRIPT_DIR/../examples"
BRANCH="enable-claude-code"

# stub source -> destination workflow file
STUBS=(
  "claude-stub.yml:claude.yml"
  "claude-review-stub.yml:claude-review.yml"
)

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

gh repo clone "$REPO" "$tmp" -- --depth 1
mkdir -p "$tmp/.github/workflows"

added=()
for entry in "${STUBS[@]}"; do
  src="${entry%%:*}"
  dst="${entry##*:}"
  if [[ -f "$tmp/.github/workflows/$dst" ]]; then
    echo "$REPO already has .github/workflows/$dst — skipping"
    continue
  fi
  cp "$EXAMPLES/$src" "$tmp/.github/workflows/$dst"
  added+=("$dst")
done

if [[ ${#added[@]} -eq 0 ]]; then
  echo "$REPO already has all agent workflows — nothing to do"
  exit 0
fi

git -C "$tmp" checkout -b "$BRANCH"
for dst in "${added[@]}"; do
  git -C "$tmp" add ".github/workflows/$dst"
done
git -C "$tmp" commit -m "Enable Meridian agents via shared workflows"
git -C "$tmp" push -u origin "$BRANCH"

gh pr create --repo "$REPO" --head "$BRANCH" \
  --title "Enable Meridian agents" \
  --body "Adds workflow stubs from [meridianlabs-ai/agents](https://github.com/meridianlabs-ai/agents): $(IFS=', '; echo "${added[*]}").

Once merged:
- **Dev agent** — mention \`@claude\` in an issue or PR comment, or add the \`claude\` label to an issue.
- **Reviewer** — auto-reviews PRs on open; or comment \`@review\` on a PR."

echo "PR opened for $REPO (added: ${added[*]})"
