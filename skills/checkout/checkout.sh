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
  # External-proxy fallback: the sync writes a canonical "Upstream PR:" line
  # into every proxy body, and chips only exist after the (browser) sweep
  # runs — so resolve the upstream PR from the body instead of requiring a
  # chip. Same shape as a cross-repo chip pick.
  UP_URL=$(gh issue view "$N" --repo "$REPO" --json body     --jq '.body' 2>/dev/null | grep -ioE 'Upstream PR:[[:space:]]*https://github\.com/[^[:space:]]+/pull/[0-9]+'     | head -1 | grep -oE 'https://[^[:space:]]+' || true)
  if [ -n "$UP_URL" ]; then
    UP_REPO=$(printf '%s' "$UP_URL" | sed -E 's#https://github\.com/([^/]+/[^/]+)/pull/.*#\1#')
    UP_NUM=$(printf '%s' "$UP_URL" | grep -oE '[0-9]+$')
    PICK=$(gh pr view "$UP_NUM" --repo "$UP_REPO"       --json number,state,headRefName,baseRefName 2>/dev/null       | jq -c --arg r "$UP_REPO" 'select(.state=="OPEN")
          | {number, state, headRefName, baseRefName, repository:{nameWithOwner:$r}}' || true)
    [ -n "$PICK" ] && { CROSS=1; echo "note: no chip — resolved via the proxy body's Upstream PR line" >&2; }
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

# The recursion-free switch moves gitlink pointers without touching submodule
# working trees, leaving them at the previous branch's commits (status shows
# them modified). Sync them to the recorded pointers; this only hits the
# network if a recorded commit is missing locally. --init is safe ONLY in the
# primary clone: in a linked worktree it writes a broken submodule .git
# pointer file that poisons every later git command, so skip the sync there
# (worktree submodules were never initialized; there is nothing to sync).
if [ "$(git rev-parse --git-dir)" = "$(git rev-parse --git-common-dir)" ]; then
  git submodule update --init --quiet

  # ts-mono companion: work that spans the viewer lives on a ts-mono branch
  # with the SAME name as this one (dev-agent convention; the parent gitlink
  # only bumps at merge time, so the recorded pointer is NOT the companion).
  # If an open companion PR exists, put the submodule on its branch so both
  # halves of the issue are editable together.
  TSMONO=$(git config --file .gitmodules --get-regexp '^submodule\..*\.path$' 2>/dev/null              | awk '$2 ~ /ts-mono/ {print $2; exit}' || true)
  # Companions exist for fork branches AND External contributors' upstream
  # branches (the agent names its ts-mono half after the primary branch), so
  # no cross-repo gate. Guard instead on authorship: contributor branch
  # names are arbitrary, and an unrelated ts-mono PR sharing a generic name
  # must not be treated as a companion.
  if [ -n "$TSMONO" ]; then
    COMP=$(gh pr list --repo meridianlabs-ai/ts-mono --head "$BRANCH" --state open              --json number,author,headRefName --jq '[.[] | select((.author.login == "i-am-marvin") or (.author.login == "app/claude") or (.headRefName | startswith("claude/issue-")))][0].number // empty' 2>/dev/null || true)
    if [ -n "$COMP" ]; then
      if [ -z "$(git -C "$TSMONO" status --porcelain 2>/dev/null)" ]; then
        git -C "$TSMONO" fetch -q origin "$BRANCH"           && git -C "$TSMONO" checkout -q -B "$BRANCH" "origin/$BRANCH"           && NOTE_TSMONO=" [ts-mono companion: PR meridianlabs-ai/ts-mono#$COMP — submodule on branch $BRANCH; parent gitlink intentionally differs until the merge-time bump]"           || NOTE_TSMONO=" [ts-mono companion PR #$COMP exists but submodule checkout failed — handle manually]"
      else
        NOTE_TSMONO=" [ts-mono companion PR #$COMP exists but the submodule tree is dirty — not switching it]"
      fi
    fi
  fi
fi

NOTE="${NOTE_TSMONO:-}"
[ -n "$CROSS" ] && NOTE="$NOTE [cross-repo: $PR_REPO — upstream PR, don't push without cause]"
echo "OK branch=$(git branch --show-current) pr=$PR_REPO#$M issue=#$N ($TITLE)$NOTE"
