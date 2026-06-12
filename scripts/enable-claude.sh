#!/usr/bin/env bash
# Enable Claude Code in a Meridian repo by opening a PR that adds the
# workflow stub from examples/claude-stub.yml.
#
# Usage: scripts/enable-claude.sh <org/repo>
set -euo pipefail

REPO="${1:?usage: enable-claude.sh <org/repo>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STUB="$SCRIPT_DIR/../examples/claude-stub.yml"
BRANCH="enable-claude-code"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

gh repo clone "$REPO" "$tmp" -- --depth 1

if [[ -f "$tmp/.github/workflows/claude.yml" ]]; then
  echo "$REPO already has .github/workflows/claude.yml — skipping"
  exit 0
fi

mkdir -p "$tmp/.github/workflows"
cp "$STUB" "$tmp/.github/workflows/claude.yml"

git -C "$tmp" checkout -b "$BRANCH"
git -C "$tmp" add .github/workflows/claude.yml
git -C "$tmp" commit -m "Enable Claude Code via shared workflow"
git -C "$tmp" push -u origin "$BRANCH"

gh pr create --repo "$REPO" --head "$BRANCH" \
  --title "Enable Claude Code" \
  --body "Adds the Claude Code workflow stub from [meridianlabs-ai/agents](https://github.com/meridianlabs-ai/agents).

Once merged, trigger Claude by:
- Mentioning \`@claude\` in an issue or PR comment
- Adding the \`claude\` label to an issue"

echo "PR opened for $REPO"
