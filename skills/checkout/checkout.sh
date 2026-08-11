#!/usr/bin/env bash
# Fast path of /checkout (see SKILL.md): chip lookup -> VS Code PR-extension
# config -> checkout, in one invocation and two network calls.
#
# Chip selection: an OPEN same-repo chip wins; otherwise a single OPEN
# cross-repo chip (an External proxy's upstream PR, or a promotion) is
# checked out against ITS repo — gh pr checkout handles personal-fork heads
# and wires the push remote when maintainerCanModify.
#
# Exit codes: 0 ok; 2 dirty tree (never switch over uncommitted work);
# 3 no usable chip (fall back to the slow path); 4 repo unresolvable.
set -euo pipefail

N=${1:?usage: checkout.sh <issue-number>}

if [ -n "$(git status --porcelain)" ]; then
  echo "DIRTY TREE — not switching:" >&2
  git status --short >&2
  exit 2
fi

# The issue lives in the meridianlabs-ai-owned repo — NOT necessarily origin
# (fork clones point origin at upstream, whose issue numbers are unrelated).
REPO=$(git remote -v | grep -om1 'meridianlabs-ai/[A-Za-z0-9._-]*' | head -1 | sed 's/\.git$//' || true)
[ -n "$REPO" ] || REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner) ||
  { echo "no meridianlabs-ai remote and no gh default repo" >&2; exit 4; }

# One GraphQL round trip: title + linked-PR chips WITH head/base refs, so no
# separate `gh pr view` is needed.
JSON=$(gh api graphql \
  -f query='query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){issue(number:$n){title closedByPullRequestsReferences(first:10,includeClosedPrs:true){nodes{number state headRefName baseRefName repository{nameWithOwner}}}}}}' \
  -F o="${REPO%%/*}" -F r="${REPO##*/}" -F n="$N")

TITLE=$(jq -r '.data.repository.issue.title' <<<"$JSON")
OPEN=$(jq -c '[.data.repository.issue.closedByPullRequestsReferences.nodes[] | select(.state=="OPEN")]' <<<"$JSON")
PICK=$(jq -c --arg repo "$REPO" '[.[] | select(.repository.nameWithOwner==$repo)][0] // empty' <<<"$OPEN")
CROSS=""
if [ -z "$PICK" ]; then
  # No same-repo chip: accept a SINGLE open cross-repo chip (ambiguity goes
  # to the slow path's judgment).
  if [ "$(jq length <<<"$OPEN")" = "1" ]; then
    PICK=$(jq -c '.[0]' <<<"$OPEN")
    CROSS=1
  fi
fi
if [ -z "$PICK" ]; then
  echo "NO USABLE OPEN CHIP for issue #$N — use the slow path. Chips seen:" >&2
  jq -r '.data.repository.issue.closedByPullRequestsReferences.nodes[]
         | "  #\(.number) \(.state) \(.repository.nameWithOwner)"' <<<"$JSON" >&2
  exit 3
fi
M=$(jq -r .number <<<"$PICK")
BRANCH=$(jq -r .headRefName <<<"$PICK")
BASE_REF=$(jq -r .baseRefName <<<"$PICK")
PR_REPO=$(jq -r .repository.nameWithOwner <<<"$PICK")

# Merge base = the PR's base branch on the BASE repo's remote (origin is
# upstream in fork clones — wrong diff), and baseRefName, not the default
# branch (fork PRs base on meridian).
BASE_REMOTE=$(git remote -v | grep -im1 "github.com[:/]${PR_REPO}" | cut -f1 || true)
[ -n "$BASE_REMOTE" ] || BASE_REMOTE=origin

# Config BEFORE the checkout: the PR extension re-reads branch config on the
# HEAD-change event; written after, it is only noticed on the NEXT switch.
git config --replace-all "branch.$BRANCH.github-pr-owner-number" "${PR_REPO%%/*}#${PR_REPO##*/}#$M"
git config --replace-all "branch.$BRANCH.vscode-merge-base" "$BASE_REMOTE/$BASE_REF"

# No submodule recursion, for fetch OR checkout (clones set
# submodule.recurse=true): switching branches only moves the gitlink
# pointer; a later `git submodule update` fetches/updates on demand if the
# submodule contents are ever needed.
git fetch -q --no-recurse-submodules "$BASE_REMOTE" "$BASE_REF"
GIT_CONFIG_COUNT=2 \
  GIT_CONFIG_KEY_0=fetch.recurseSubmodules GIT_CONFIG_VALUE_0=false \
  GIT_CONFIG_KEY_1=submodule.recurse GIT_CONFIG_VALUE_1=false \
  gh pr checkout "$M" -R "$PR_REPO"

NOTE=""
[ -n "$CROSS" ] && NOTE=" [cross-repo: $PR_REPO — upstream PR, don't push without cause]"
echo "OK branch=$(git branch --show-current) pr=$PR_REPO#$M issue=#$N ($TITLE)$NOTE"
