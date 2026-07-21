---
name: checkout
description: Check out the PR branch for an issue — /checkout <issue-number> finds the issue's PR (linked-PR chip, then agent comments, then the claude/issue-N-* branch convention) and checks its branch out in the current repo clone.
---

# Check out an issue's PR branch

Given an issue number (`/checkout 97`), find the right PR for that issue in
the **current directory's repo** and check out its branch.

## Steps

1. **Repo + safety.** `REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)`
   from the cwd. If `git status --porcelain` is non-empty, STOP and show the
   dirty files — never switch branches over uncommitted work without the
   user's say-so.

2. **Find the PR** (the design-doc reconcile rules, in order):

   a. **Linked-PR chip** — authoritative when present:

      ```sh
      gh api graphql -f query='query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){issue(number:$n){closedByPullRequestsReferences(first:10,includeClosedPrs:true){nodes{number state repository{nameWithOwner}}}}}}' \
        -F o=<owner> -F r=<repo> -F n=<N>
      ```

      Ignore cross-repo entries (an upstream promotion PR is not checkout-able
      here — but see step 4). Prefer OPEN; a merged/closed one is a fallback.

   b. **Agent comments** — scan ALL `i-am-marvin` comments on the issue for
      `/pull/<M>` references (not just the last comment: earlier runs may hold
      the PR; superseded PRs may sit next to the live one). Collect every ref,
      check each PR's state, keep the open one (newest if several).

   c. **Branch convention** — list open PRs and match heads against
      `claude/issue-<N>-*`:

      ```sh
      gh api "repos/$REPO/pulls?state=all&per_page=100" \
        --jq '.[] | select(.head.ref|test("^claude/issue-<N>-")) | {number, state, head:.head.ref}'
      ```

   If candidates disagree, prefer: open chip > open comment-ref > open
   branch-match > most recently updated. Say which rule matched.

3. **Check it out.**

   ```sh
   gh pr checkout <M>
   ```

   (`gh pr checkout` fetches and tracks the PR branch; works for same-repo
   heads, which all agent PRs are.) Confirm with `git log --oneline -3` and
   report: issue, PR, branch, and how the PR was found.

4. **Fallbacks — tell the user instead of guessing:**
   - **No PR but a branch exists** (a "Claude finished" comment names a
     `claude/issue-N-*` branch that was never turned into a PR):
     `git fetch origin <branch> && git switch <branch>` — say there's no PR.
   - **Only a cross-repo (upstream) PR exists**: the work was promoted; the
     branch usually still exists on the fork — check out the upstream PR's
     head branch name from origin. Note it's under upstream review.
   - **Nothing found**: list what WAS found (comments scanned, branches
     probed) so the user can point at the right thing.
