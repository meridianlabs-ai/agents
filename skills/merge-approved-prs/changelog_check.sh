#!/usr/bin/env bash
# CHANGELOG invariant of /merge-approved-prs (SKILL.md → Conflict resolution
# invariants): every entry the branch adds to CHANGELOG.md must sit under
# `## Unreleased` after the merge. Run in the queue worktree, after the
# merge commit, for promotions and External PRs alike (external.sh runs it
# with its git pins).
#
# One line per added entry, `<section><TAB><entry>`, in diff order; exit
# non-zero when a section is not `## Unreleased`, an entry is gone from the
# file, or the diff itself failed. The entry text is the PR author's (an
# outsider's, for an External PR): awk gets it from the environment and
# compares it whole, never as a regex or on the command line (finding
# 4628737: an inline `awk '/<entry text>/'` ran a backticked phrase).
#
# Usage: changelog_check.sh [<main-ref>]   (default origin/main)
set -uo pipefail
main=${1:-origin/main}
bad=
added=
base=$(git merge-base "$main" HEAD) && added=$(git diff --no-color --no-ext-diff --no-textconv "$base" HEAD -- CHANGELOG.md) || bad=1
while IFS= read -r entry; do
  entry="$entry" awk 'BEGIN { e = substr(ENVIRON["entry"], 2); sec = "(above the first heading)" }
    /^## / { sec = $0 }
    $0 == e { print sec "\t" $0; n++; if (sec != "## Unreleased") bad = 1 }
    END { if (!n) { print "(not in CHANGELOG.md)\t" e; bad = 1 }; exit bad }' CHANGELOG.md || bad=1
done < <(grep '^+- ' <<<"$added")
test -z "$bad"
