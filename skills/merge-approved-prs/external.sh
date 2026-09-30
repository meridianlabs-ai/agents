#!/usr/bin/env bash
# External path of /merge-approved-prs (SKILL.md → External PRs): check out
# an approved contributor PR, merge upstream main into it and push the merge
# to the contributor's branch, with every git command on the tree pinned.
#
# The tree is an outsider's, so it is data (skills/THREAT_MODEL.md rule
# (c)). Every git call here runs with pin_git_config's pins (Claude Security
# 4773883): hooks pointed at an empty directory, core.fsmonitor off, every
# filter driver git would read in this worktree emptied and made optional,
# submodule recursion off — a relative command in the clone's configuration
# would otherwise resolve to the contributor's files during the checkout,
# merge, status, add, commit, diff or push. Diffs pass --no-ext-diff and
# --no-textconv. And nothing the contributor names becomes a local ref or
# config key (Claude Security 4773882): the approved commit is checked out
# detached, no local branch is created or moved, no `branch.*` config is
# written, and the push names its destination in full, `git push <fork-url>
# HEAD:refs/heads/<branch>`. A head named `main`, or after a local branch,
# changes nothing in the clone.
#
# Run it from the queue's throwaway worktree (a linked, detached worktree of
# ~/git/inspect_ai, `origin` = UKGovernmentBEIS/inspect_ai), at its root.
#
# Usage:
#   external.sh start <pr> <approved-sha>
#       fetch origin main and the PR head; refuse unless the head is the
#       approved commit; check that commit out detached; merge origin/main
#       without committing. Conflicts are listed with what main changed in
#       each (conflicts.sh).
#   external.sh merge
#       fetch origin main again and merge it into HEAD without committing
#       (main moved: BEHIND or DIRTY).
#   external.sh conflicts
#       conflicts.sh again: main's log and diff for each conflicted file.
#   external.sh git <args>...
#       any other git command on the worktree, pinned (diff, log and show
#       also get --no-ext-diff --no-textconv).
#   external.sh commit [--trailer <line>]
#       stage the resolution (every conflicted path and every tracked
#       change), refuse a conflict marker anywhere in it (git diff --check,
#       any marker size), refuse a net change to
#       the ts-mono gitlink, commit the merge with git's merge message plus
#       the trailer, then run check.
#   external.sh check
#       the CHANGELOG invariant (changelog_check.sh) and no net ts-mono
#       gitlink change, on HEAD.
#   external.sh push <pr> <approved-sha>
#       check, then push HEAD to the contributor's branch, read from the PR
#       (headRepositoryOwner/headRepository/headRefName; maintainerCanModify
#       must be true), refusing unless the approved commit is an ancestor of
#       HEAD and every commit on top of it is a merge, and HEAD adds no
#       conflict marker to the approved commit. Never forced: a
#       rejection means the contributor pushed.
# Exit codes: 0 ok; 1 usage, or not run from a linked worktree's root (or a
# refused value); 2 dirty tree or a merge in progress when none is expected;
# 3 conflicts to resolve (start/merge), unresolved markers (commit), or a
# failed invariant (commit/check/push); 5 the PR head is not the approved
# commit (start), or the push was rejected (push) — SKIP and report.
set -euo pipefail
HERE=$(dirname "$(realpath "$0")")
. "$HERE/../lib/common.sh"

UPSTREAM=UKGovernmentBEIS/inspect_ai
GITLINK=src/inspect_ai/_view/ts-mono

usage() { sed -n '/^# Usage:/,/^# Exit codes:/p' "$HERE/external.sh" | sed '$d; s/^# \{0,1\}//' >&2; exit 1; }
[ $# -gt 0 ] || usage
CMD=$1
shift

# A linked worktree, at its root: the queue's scratch worktree, never the
# primary clone (whose HEAD and working tree are Ransom's).
phys() { (cd -P -- "$1" 2>/dev/null && pwd); }
GIT_DIR_P=$(phys "$(git rev-parse --git-dir 2>/dev/null)") || { echo "external.sh: not in a git worktree" >&2; exit 1; }
COMMON_P=$(phys "$(git rev-parse --git-common-dir)")
if [ "$GIT_DIR_P" = "$COMMON_P" ]; then
  echo "external.sh: run it from the queue's throwaway worktree (git worktree add --detach <scratch>/merge-queue origin/main), not from the primary clone" >&2
  exit 1
fi
[ -z "$(git rev-parse --show-cdup)" ] || { echo "external.sh: run it from the worktree's root" >&2; exit 1; }

NOHOOKS=$(mktemp -d)
trap 'rm -rf "$NOHOOKS"' EXIT
pin_git_config "$NOHOOKS" "$PWD"
# File names come from the contributor's tree, and git reads a pathspec's
# magic (`:(glob)…`, `*`) even after `--`: every pathspec is literal here.
export GIT_LITERAL_PATHSPECS=1

sha_arg() {  # $1 = value: a full hex SHA, or usage
  grep -qE '^[0-9a-f]{40}$' <<<"$1" || { echo "external.sh: '$1' is not a full commit SHA" >&2; exit 1; }
}
pr_arg() {
  case "$1" in ""|*[!0-9]*) echo "external.sh: '$1' is not a PR number" >&2; exit 1 ;; esac
}
in_merge() { git rev-parse -q --verify MERGE_HEAD >/dev/null; }
require_clean() {
  if in_merge; then
    echo "external.sh: a merge is in progress — finish it with 'external.sh commit' (or abort it with 'external.sh git merge --abort')" >&2
    exit 2
  fi
  if [ -n "$(git status --porcelain --ignore-submodules=dirty)" ]; then
    echo "DIRTY TREE — not continuing:" >&2
    git status --short --ignore-submodules=dirty >&2
    exit 2
  fi
}
# merge_main: merge origin/main into HEAD without committing; list the
# conflicts when there are any (exit 3).
merge_main() {
  if git merge -q --no-ff --no-commit origin/main; then
    if in_merge; then
      echo "merged origin/main cleanly (not committed): check the invariants, then 'external.sh commit'"
    else
      echo "HEAD already contains origin/main — nothing to merge"
    fi
    return 0
  fi
  if [ -n "$(git diff --name-only --diff-filter=U)" ]; then
    echo "CONFLICTS merging origin/main — what main changed in each file since the branch diverged:"
    bash "$HERE/conflicts.sh"
    echo "Resolve each file with your editor (never paste a file name from the tree into a command), then 'external.sh commit'."
    exit 3
  fi
  echo "external.sh: git merge origin/main failed" >&2
  exit 1
}
# gitlink_unchanged <rev-or---cached>: 0 when the ts-mono gitlink matches origin/main's.
gitlink_unchanged() {
  if [ "$1" = "--cached" ]; then
    git diff --quiet --no-ext-diff --cached origin/main -- "$GITLINK"
  else
    git diff --quiet --no-ext-diff origin/main "$1" -- "$GITLINK"
  fi
}
# markers_vs <base> <result>: set MARKERS to the conflict markers git finds
# in <result> (`--cached` for the index, or a commit) that <base> does not
# have — `git diff --check`, which honours a path's conflict-marker-size
# attribute — one `path:line` per line, "" when none. A diff that fails is
# exit 1, never "none".
markers_vs() {
  local out rc=0
  if [ "$2" = "--cached" ]; then
    out=$(git diff --no-ext-diff --check --cached "$1" 2>&1) || rc=$?
  else
    out=$(git diff --no-ext-diff --check "$1" "$2" 2>&1) || rc=$?
  fi
  case "$rc" in
    0|2) MARKERS=$({ grep -F ': leftover conflict marker' <<<"$out" || true; } | sed 's/: leftover conflict marker$//') ;;
    *) echo "external.sh: git diff --check failed:" >&2; printf '%s\n' "$out" >&2; exit 1 ;;
  esac
}
# conflict_residue <parent1> <parent2> <result>: set MARKERS to the conflict
# markers in <result> that neither parent has — what an unfinished
# resolution leaves, of any marker size, staged or not — and not a marker
# line either side legitimately carries (a test fixture, a doc).
conflict_residue() {
  local a
  markers_vs "$1" "$3"; a=$MARKERS
  markers_vs "$2" "$3"
  MARKERS=$(comm -12 <(sort <<<"$a") <(sort <<<"$MARKERS") | grep . || true)
}
check() {
  local rc=0
  if ! gitlink_unchanged HEAD; then
    echo "INVARIANT: the merge carries a net change to the $GITLINK gitlink; restore it ('external.sh git checkout origin/main -- $GITLINK') and commit" >&2
    rc=3
  fi
  if ! bash "$HERE/changelog_check.sh"; then
    echo "INVARIANT: a CHANGELOG entry the branch adds is not under ## Unreleased (or is gone) — see the lines above" >&2
    rc=3
  fi
  return "$rc"
}

case "$CMD" in
  start)
    [ $# -eq 2 ] || usage
    pr_arg "$1"; sha_arg "$2"
    N=$1 APPROVED=$2
    if git symbolic-ref -q HEAD >/dev/null; then
      echo "external.sh: this worktree is on branch $(git symbolic-ref --short HEAD); run it from the queue's detached worktree" >&2
      exit 1
    fi
    require_clean
    git fetch -q --no-tags --no-recurse-submodules origin main
    # The PR head, fetched but NOT checked out: nothing from its tree runs.
    git fetch -q --no-tags --no-recurse-submodules origin "refs/pull/$N/head"
    GOT=$(git rev-parse FETCH_HEAD)
    if [ "$GOT" != "$APPROVED" ]; then
      echo "SKIP: $UPSTREAM#$N head is $GOT, not the approved $APPROVED — the contributor pushed since the check; nothing was checked out" >&2
      exit 5
    fi
    git checkout -q --detach "$APPROVED"
    echo "checked out the approved commit $APPROVED detached (no local branch, no branch config)"
    merge_main
    ;;
  merge)
    [ $# -eq 0 ] || usage
    require_clean
    git fetch -q --no-tags --no-recurse-submodules origin main
    merge_main
    ;;
  conflicts)
    [ $# -eq 0 ] || usage
    bash "$HERE/conflicts.sh"
    ;;
  git)
    [ $# -gt 0 ] || usage
    sub=$1
    shift
    case "$sub" in
      diff|log|show) exec git "$sub" --no-ext-diff --no-textconv "$@" ;;
      *) exec git "$sub" "$@" ;;
    esac
    ;;
  commit)
    TRAILER=""
    case "${1:-}" in
      "") ;;
      --trailer) [ $# -eq 2 ] || usage; TRAILER=$2 ;;
      *) usage ;;
    esac
    in_merge || { echo "external.sh: no merge in progress — nothing to commit"; exit 0; }
    # Stage the resolution (every conflicted path, then every tracked
    # change), then refuse any conflict marker in the whole staged result
    # that neither side has: markers of any configured size, and content
    # staged before this command, count the same.
    while IFS= read -r -d '' f; do
      git add -- "$f"
    done < <(git diff --name-only --diff-filter=U -z)
    git add -u
    conflict_residue HEAD MERGE_HEAD --cached
    if [ -n "$MARKERS" ]; then
      echo "UNRESOLVED: conflict markers remain (path:line) — resolve them and run commit again:" >&2
      sed 's/^/  /' <<<"$MARKERS" >&2
      exit 3
    fi
    if ! gitlink_unchanged --cached; then
      echo "INVARIANT: the merge carries a net change to the $GITLINK gitlink; restore it ('external.sh git checkout origin/main -- $GITLINK') and commit again" >&2
      exit 3
    fi
    MSG=$(mktemp)
    cp "$(git rev-parse --git-path MERGE_MSG)" "$MSG"
    [ -z "$TRAILER" ] || printf '\n%s\n' "$TRAILER" >>"$MSG"
    git commit -q --cleanup=strip -F "$MSG"
    rm -f "$MSG"
    echo "committed the merge $(git rev-parse HEAD)"
    check || exit 3
    ;;
  check)
    [ $# -eq 0 ] || usage
    check || exit 3
    echo "invariants hold at $(git rev-parse HEAD)"
    ;;
  push)
    [ $# -eq 2 ] || usage
    pr_arg "$1"; sha_arg "$2"
    N=$1 APPROVED=$2
    require_clean
    if ! git merge-base --is-ancestor "$APPROVED" HEAD; then
      echo "external.sh: HEAD does not descend from the approved commit $APPROVED — not pushing" >&2
      exit 1
    fi
    OURS=$(git rev-list --first-parent --no-merges "$APPROVED..HEAD")
    if [ -n "$OURS" ]; then
      echo "external.sh: HEAD carries non-merge commits on top of the approved commit — the queue adds merges only; not pushing:" >&2
      printf '%s\n' "$OURS" >&2
      exit 1
    fi
    check || exit 3
    conflict_residue "$APPROVED" origin/main HEAD
    if [ -n "$MARKERS" ]; then
      echo "UNRESOLVED: HEAD carries conflict markers neither the approved commit nor origin/main has (path:line) — not pushing:" >&2
      sed 's/^/  /' <<<"$MARKERS" >&2
      exit 3
    fi
    PRJ=$(gh pr view "$N" --repo "$UPSTREAM" --json headRefName,headRepository,headRepositoryOwner,maintainerCanModify)
    OWNER=$(jq -r '.headRepositoryOwner.login // ""' <<<"$PRJ")
    NAME=$(jq -r '.headRepository.name // ""' <<<"$PRJ")
    BRANCH=$(jq -r '.headRefName // ""' <<<"$PRJ")
    if [ "$(jq -r '.maintainerCanModify' <<<"$PRJ")" != "true" ]; then
      echo "SKIP: $UPSTREAM#$N does not allow edits by maintainers — ask the contributor to enable it; not pushing" >&2
      exit 1
    fi
    if ! grep -qE '^[A-Za-z0-9](-?[A-Za-z0-9])*$' <<<"$OWNER" || ! grep -qE '^[A-Za-z0-9._-]+$' <<<"$NAME"; then
      echo "external.sh: unexpected head repository '$OWNER/$NAME' — not pushing" >&2
      exit 1
    fi
    git check-ref-format "refs/heads/$BRANCH" ||
      { echo "external.sh: head branch name is not a valid ref — not pushing" >&2; exit 1; }
    FORK_URL="https://github.com/$OWNER/$NAME.git"
    if ! git push -q "$FORK_URL" "HEAD:refs/heads/$BRANCH"; then
      echo "SKIP: the push to $OWNER/$NAME was rejected — the contributor pushed (never pull their commits in and retry); re-run approval_at_head.py and report" >&2
      exit 5
    fi
    echo "pushed $(git rev-parse HEAD) to $OWNER/$NAME branch $(printf '%q' "$BRANCH")"
    ;;
  *) usage ;;
esac
