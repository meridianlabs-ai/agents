#!/usr/bin/env bash
# Fast path of /checkout (see SKILL.md): chip lookup -> VS Code PR-extension
# config -> checkout, in one invocation and one network call.
#
# Chip selection: an OPEN same-repo chip wins; otherwise a single OPEN
# cross-repo chip (an External proxy's upstream PR, or a promotion) is
# checked out against ITS repo — gh pr checkout handles personal-fork heads
# and wires the push remote when maintainerCanModify.
#
# Trust rule (applied BEFORE any PR text — title, body, branch name — is
# consulted): a linked PR qualifies only when its head repository is this
# org repo AND its author is in TRUSTED_LOGINS or has write access here.
# Anyone can link a PR from a personal fork to an issue (GitHub creates the
# chip natively for `Fixes #N` on a default-branch PR), so the chip alone
# proves nothing; a branch inside the org repo can only be pushed by a
# write-access account, which makes the head-repository check the
# load-bearing one and the author check defence in depth. A cross-repo chip
# qualifies as a promotion (its head is this repo) or as a genuine External
# proxy's upstream PR (the issue was written by a trusted login and carries
# the `External` label). The body's `Upstream PR:` line is honoured only for
# such a proxy and only under UPSTREAM.
#
# Usage: checkout.sh <issue-number> [--dry-run]
#   --dry-run  print the candidates, each verdict and the decision; exit
#              before any write (no branch config, no fetch, no checkout).
# Exit codes: 0 ok; 2 dirty tree (never switch over uncommitted work);
# 3 no qualifying open chip (stderr lists every candidate and why it was
# refused; fall back to the slow path); 4 repo unresolvable.
set -euo pipefail

# Trusted identities (comma-separated). Phase 2 (GitHub App identity) changes
# this ONE value: marvin's login becomes `<app-slug>[bot]`.
TRUSTED_LOGINS="i-am-marvin"
# The only repo an External proxy's upstream PR may live in.
UPSTREAM=UKGovernmentBEIS/inspect_ai

N=""
DRY=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    -*) echo "usage: checkout.sh <issue-number> [--dry-run]" >&2; exit 1 ;;
    *) N=$arg ;;
  esac
done
[ -n "$N" ] || { echo "usage: checkout.sh <issue-number> [--dry-run]" >&2; exit 1; }

if [ -z "$DRY" ] && [ -n "$(git status --porcelain)" ]; then
  echo "DIRTY TREE — not switching:" >&2
  git status --short >&2
  exit 2
fi

# The issue lives in the meridianlabs-ai-owned repo — NOT necessarily origin
# (fork clones point origin at upstream, whose issue numbers are unrelated).
REPO=$(git remote -v | grep -om1 'meridianlabs-ai/[A-Za-z0-9._-]*' | head -1 | sed 's/\.git$//' || true)
[ -n "$REPO" ] || REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner) ||
  { echo "no meridianlabs-ai remote and no gh default repo" >&2; exit 4; }

# trusted_login <login>: 0 when <login> is in TRUSTED_LOGINS or has write
# access on $REPO (admin/maintain/write from the collaborator permission
# API — a public repo answers `read` for everyone else). A failed or
# unexpected lookup is untrusted (fail closed). Cached per login for the
# run; call it in the main shell, not inside $(...), or the cache is lost.
PERM_CACHE=""
trusted_login() {
  local login perm
  login=$(tr 'A-Z' 'a-z' <<<"$1")
  [ -n "$login" ] || return 1
  case ",$(tr 'A-Z' 'a-z' <<<"$TRUSTED_LOGINS")," in *",$login,"*) return 0 ;; esac
  case "$PERM_CACHE" in *"|$login=ok|"*) return 0 ;; *"|$login=no|"*) return 1 ;; esac
  perm=$(gh api "repos/$REPO/collaborators/$login/permission" --jq .permission 2>/dev/null || true)
  case "$perm" in
    admin|maintain|write) PERM_CACHE="$PERM_CACHE|$login=ok|"; return 0 ;;
    *) PERM_CACHE="$PERM_CACHE|$login=no|"; return 1 ;;
  esac
}
# check_pr <pr-json>: applies the trust rule; sets REASON to "" when the PR
# qualifies, else to why it was refused. Reads only headRepository and
# author — never the PR's title, body or branch name.
check_pr() {
  local head login
  head=$(jq -r '.headRepository.nameWithOwner // ""' <<<"$1")
  login=$(jq -r '.author.login // ""' <<<"$1")
  REASON=""
  if [ "$head" != "$REPO" ]; then
    REASON="head repository is '${head:-unknown}', not $REPO"
  elif ! trusted_login "$login"; then
    REASON="author '${login:-unknown}' is not in TRUSTED_LOGINS ($TRUSTED_LOGINS) and has no write access on $REPO"
  fi
}

# One GraphQL round trip: title, issue author + labels (the External-proxy
# test), body (the `Upstream PR:` fallback) and the linked-PR chips WITH
# head/base refs, author and head repository, so no separate `gh pr view`
# or `gh issue view` is needed.
JSON=$(gh api graphql \
  -f query='query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){issue(number:$n){title body author{login} labels(first:20){nodes{name}} closedByPullRequestsReferences(first:10,includeClosedPrs:true){nodes{number state headRefName baseRefName author{login} repository{nameWithOwner} headRepository{nameWithOwner}}}}}}' \
  -F o="${REPO%%/*}" -F r="${REPO##*/}" -F n="$N")

TITLE=$(jq -r '.data.repository.issue.title' <<<"$JSON")
ISSUE_AUTHOR=$(jq -r '.data.repository.issue.author.login // ""' <<<"$JSON")
ISSUE_LABELS=$(jq -r '[.data.repository.issue.labels.nodes[].name] | join(",")' <<<"$JSON")
# A genuine External proxy: written by a trusted login (the sync, as marvin)
# AND labelled External. Membership in TRUSTED_LOGINS only — write access
# is not enough here, a collaborator's own issue is not a proxy.
PROXY=""
case ",$(tr 'A-Z' 'a-z' <<<"$TRUSTED_LOGINS")," in
  *",$(tr 'A-Z' 'a-z' <<<"$ISSUE_AUTHOR"),"*)
    case ",$ISSUE_LABELS," in *",External,"*) PROXY=1 ;; esac ;;
esac

# Classify every chip. SAME_OK / CROSS_OK collect qualifying OPEN chips
# (one compact JSON object per line); LISTING is the human record of every
# candidate and its verdict, printed on refusal and in --dry-run.
SAME_OK=""
CROSS_OK=""
LISTING=""
while IFS= read -r chip; do
  [ -n "$chip" ] || continue
  line=$(jq -r '"  #\(.number) \(.state) \(.repository.nameWithOwner) head=\(.headRepository.nameWithOwner // "?"):\(.headRefName) author=\(.author.login // "?")"' <<<"$chip")
  if [ "$(jq -r .state <<<"$chip")" != "OPEN" ]; then
    LISTING="$LISTING$line — not open"$'\n'
    continue
  fi
  check_pr "$chip"
  if [ "$(jq -r .repository.nameWithOwner <<<"$chip")" = "$REPO" ]; then
    if [ -z "$REASON" ]; then
      SAME_OK="$SAME_OK$chip"$'\n'
      LISTING="$LISTING$line — qualifies"$'\n'
    else
      LISTING="$LISTING$line — REFUSED: $REASON"$'\n'
    fi
  elif [ -z "$REASON" ]; then
    CROSS_OK="$CROSS_OK$chip"$'\n'
    LISTING="$LISTING$line — qualifies (promotion: head is $REPO)"$'\n'
  elif [ -n "$PROXY" ] && [ "$(jq -r .repository.nameWithOwner <<<"$chip")" = "$UPSTREAM" ]; then
    CROSS_OK="$CROSS_OK$chip"$'\n'
    LISTING="$LISTING$line — qualifies (External proxy: issue by $ISSUE_AUTHOR, label External)"$'\n'
  else
    LISTING="$LISTING$line — REFUSED: $REASON; issue is not an External proxy (author=${ISSUE_AUTHOR:-?} labels=${ISSUE_LABELS:-none})"$'\n'
  fi
done <<<"$(jq -c '.data.repository.issue.closedByPullRequestsReferences.nodes[]' <<<"$JSON")"

# max_by(.number), not [0]: with two OPEN same-repo chips (an old
# generation not yet closed plus its replacement) the API's list order is
# not defined as newest — highest PR number is.
PICK=$(jq -sc 'max_by(.number) // empty' <<<"$SAME_OK")
CROSS=""
HOW="open same-repo chip"
if [ -z "$PICK" ]; then
  # No same-repo chip: accept a SINGLE qualifying open cross-repo chip
  # (ambiguity goes to the slow path's judgment).
  if [ "$(jq -sc length <<<"$CROSS_OK")" = "1" ]; then
    PICK=$(jq -sc '.[0]' <<<"$CROSS_OK")
    CROSS=1
    HOW="open cross-repo chip"
  fi
fi
if [ -z "$PICK" ]; then
  # External-proxy fallback: the sync writes a canonical "Upstream PR:" line
  # into every proxy body, and chips only exist after the (browser) sweep
  # runs — so resolve the upstream PR from the body instead of requiring a
  # chip. Honoured ONLY for a genuine proxy (trusted author + External
  # label) and ONLY for a URL under $UPSTREAM: the line is free text on any
  # other issue. Same shape as a cross-repo chip pick.
  UP_LINE=$(jq -r '.data.repository.issue.body // ""' <<<"$JSON" \
    | grep -ioE 'Upstream PR:[[:space:]]*https://github\.com/[^[:space:]]+/pull/[0-9]+' | head -1 || true)
  if [ -n "$UP_LINE" ]; then
    UP_URL=$(grep -oE 'https://[^[:space:]]+' <<<"$UP_LINE")
    if [ -z "$PROXY" ]; then
      LISTING="$LISTING  body line '$UP_LINE' — REFUSED: issue is not an External proxy (author=${ISSUE_AUTHOR:-?} labels=${ISSUE_LABELS:-none})"$'\n'
    elif ! grep -qE "^https://github\.com/$UPSTREAM/pull/[0-9]+$" <<<"$UP_URL"; then
      LISTING="$LISTING  body line '$UP_LINE' — REFUSED: not under $UPSTREAM"$'\n'
    else
      UP_NUM=$(grep -oE '[0-9]+$' <<<"$UP_URL")
      PICK=$(gh pr view "$UP_NUM" --repo "$UPSTREAM" \
          --json number,state,headRefName,baseRefName 2>/dev/null \
        | jq -c --arg r "$UPSTREAM" 'select(.state=="OPEN")
          | {number, state, headRefName, baseRefName, repository:{nameWithOwner:$r}}' || true)
      if [ -n "$PICK" ]; then
        CROSS=1
        HOW="proxy body's Upstream PR line"
        LISTING="$LISTING  body line '$UP_LINE' — qualifies (External proxy: issue by $ISSUE_AUTHOR, label External)"$'\n'
        echo "note: no chip — resolved via the proxy body's Upstream PR line" >&2
      else
        LISTING="$LISTING  body line '$UP_LINE' — upstream PR is not open (or not readable)"$'\n'
      fi
    fi
  fi
fi
if [ -z "$PICK" ]; then
  echo "NO QUALIFYING OPEN CHIP for issue #$N — use the slow path. Candidates seen (TRUSTED_LOGINS=$TRUSTED_LOGINS):" >&2
  printf '%s' "${LISTING:-  (none)
}" >&2
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

if [ -n "$DRY" ]; then
  echo "DRY-RUN: issue #$N in $REPO ($TITLE) author=${ISSUE_AUTHOR:-?} labels=${ISSUE_LABELS:-none} proxy=${PROXY:-no}; TRUSTED_LOGINS=$TRUSTED_LOGINS"
  printf '%s' "$LISTING"
  echo "DRY-RUN: DECISION — check out $PR_REPO#$M via $HOW: branch=$BRANCH base=$BASE_REMOTE/$BASE_REF${CROSS:+ [cross-repo]}"
  echo "DRY-RUN: would run: git config branch.$BRANCH.github-pr-owner-number ${PR_REPO%%/*}#${PR_REPO##*/}#$M; git config branch.$BRANCH.vscode-merge-base $BASE_REMOTE/$BASE_REF; git fetch $BASE_REMOTE $BASE_REF; gh pr checkout $M -R $PR_REPO"
  exit 0
fi
# A refused candidate sat next to the one we took — say so, the operator
# may want to look at it.
grep -F -- '— REFUSED' <<<"$LISTING" | sed 's/^ */note: refused /' >&2 || true

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
