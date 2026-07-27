---
name: checkout
description: Check out the PR branch for an issue — /checkout <issue-number> finds the issue's PR (linked-PR chip fast path, then agent comments, then the claude/issue-N-* branch convention) and checks its branch out in the current repo clone.
---

# Check out an issue's PR branch

Given an issue number (`/checkout 97`), find the right PR for that issue and
check out its branch. This is a mechanical skill: the common case is THREE
commands — run them without narration and report one line at the end.

**Which repo owns the issue:** the remote owned by `meridianlabs-ai` — NOT
necessarily `origin`. Fork clones of inspect_ai have `origin` pointing at
upstream (UKGovernmentBEIS) and the fork under another remote name; upstream's
issue numbers are unrelated, so never query the issue there.

## Fast path (usual case: issue has a linked-PR chip)

**Command 1** — dirty-check, resolve the meridian repo, chip query, all at once:

```sh
git status --porcelain
REPO=$(git remote -v | grep -om1 'meridianlabs-ai/[A-Za-z0-9._-]*' | head -1 | sed 's/\.git$//')
gh api graphql -f query='query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){issue(number:$n){title closedByPullRequestsReferences(first:10,includeClosedPrs:true){nodes{number state repository{nameWithOwner}}}}}}' \
  -F o="${REPO%%/*}" -F r="${REPO##*/}" -F n=<N>
```

- Dirty tree → STOP, show the files (never switch over uncommitted work).
- No meridian remote → fall back to `gh repo view --json nameWithOwner`.

**Command 2** — when the chip shows an OPEN same-repo PR `M` (prefer open
over closed; ignore cross-repo entries here — see fallbacks):

```sh
gh pr checkout <M> -R "$REPO" && git log --oneline -3 && git branch --show-current
```

**Command 3** — wire up the VS Code GitHub PR extension, which `gh pr
checkout` doesn't: without `github-pr-owner-number` the extension never
associates the branch with the PR, and VS Code guesses `vscode-merge-base`
as the branch's own upstream (branch vs itself = empty diff). The merge base
must be the PR's **base branch on the base repo's remote** — i.e. `$REPO`'s
remote, NOT `origin` (in fork clones `origin` is upstream, and an upstream
base gives a wrong/stale diff), and the PR's `baseRefName`, NOT the default
branch (on the inspect_ai fork PRs base on `meridian`; a `main` merge base
would show every meridian-only file as changed). Derive both and fetch so
the ref is current:

```sh
BRANCH=$(git branch --show-current)
BASE_REMOTE=$(git remote -v | grep -im1 "github.com[:/]${REPO}" | cut -f1)
BASE_REF=$(gh pr view <M> -R "$REPO" --json baseRefName -q .baseRefName)
git fetch "$BASE_REMOTE" "$BASE_REF"
git config "branch.$BRANCH.github-pr-owner-number" "${REPO%%/*}#${REPO##*/}#<M>"
git config "branch.$BRANCH.vscode-merge-base" "$BASE_REMOTE/$BASE_REF"
```

Report one line: branch, PR, issue title. Done.

## Slow path (no chip, or only closed/cross-repo entries)

b. **Agent comments** — scan ALL `i-am-marvin` comments on the issue for
   `/pull/<M>` refs (not just the last comment; superseded PRs sit next to
   live ones). Keep the open one, newest if several.

c. **Branch convention** — match PR heads against `claude/issue-<N>-*`:

   ```sh
   gh api "repos/$REPO/pulls?state=all&per_page=100" \
     --jq '.[] | select(.head.ref|test("^claude/issue-<N>-")) | {number, state, head:.head.ref}'
   ```

If candidates disagree: open chip > open comment-ref > open branch-match >
most recently updated. Say which rule matched.

## Fallbacks — tell the user instead of guessing

- **No PR but a branch exists** (a "Claude finished" comment names a
  `claude/issue-N-*` branch never turned into a PR): fetch + switch to it from
  the meridian remote; say there's no PR. Still set `vscode-merge-base`
  (Command 3, minus the PR association line; with no PR to read the base
  from, use the branch's fork point — `meridian` on the fork, the default
  branch elsewhere).
- **Only a cross-repo (upstream) chip**: the work was promoted (possibly
  merged). Offer the upstream PR's head branch from the meridian remote and
  say it's under upstream review / already merged.
- **Nothing found**: list what was scanned so the user can point at the right
  thing.
