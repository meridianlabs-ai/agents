#!/usr/bin/env bash
# Conflict inspection of /merge-approved-prs (SKILL.md → Conflict
# resolution invariants → Code conflicts): for each conflicted file, what
# main changed in it since the branch diverged, to resolve against. Run in
# the queue worktree during the merge (external.sh runs it with its git
# pins). A file name is the PR author's (an outsider's, for an External
# PR) and may carry spaces, quotes, `$(` or pathspec magic: the paths come
# from git NUL-delimited and reach git only as a quoted variable (finding
# 4628737), matched literally.
#
# Usage: conflicts.sh [<main-ref>]   (default origin/main)
set -euo pipefail
# git reads a pathspec's magic (`:(literal)…`, `*`) even after `--`; a file
# name from the tree is matched literally.
export GIT_LITERAL_PATHSPECS=1
main=${1:-origin/main}
base=$(git merge-base HEAD "$main")
while IFS= read -r -d '' file; do
  printf '=== %q\n' "$file"
  git log --oneline "$base..$main" -- "$file"
  git diff --no-ext-diff --no-textconv "$base..$main" -- "$file"
done < <(git diff --name-only --diff-filter=U -z)
