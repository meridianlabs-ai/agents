---
name: promote
description: Promote a reviewed inspect_ai fork branch upstream — open the UKGovernmentBEIS PR with the fully-qualified Fixes ref, then do the tracking bookkeeping (Atlas Sign-off stage, Upstream PR field, issue comment, supersede the fork PR). Idempotent — also use it to heal the bookkeeping of an already-promoted issue.
---

# Promote fork work upstream

Promote work from `meridianlabs-ai/inspect_ai` (the fork) to
`UKGovernmentBEIS/inspect_ai` (upstream), performing the full tracking
contract from `meridianlabs-ai/agents` design/atlas-tracking.md → "The fork:
promotion and the terminal sync". Every step is idempotent: running this on an
already-promoted issue verifies and heals its bookkeeping instead of
duplicating anything.

Constants (from the design doc; stable):

- Atlas project: `PVT_kwDOC7YMCM4BU68p`
- `Stage` field `PVTSSF_lADOC7YMCM4BU68pzhYZEwY`; `Sign-off` option `da6137e6`
- `Status` field `PVTSSF_lADOC7YMCM4BU68pzhKizZM`; `In progress` option `47fc9ee4`
- `Upstream PR` field (text): `PVTF_lADOC7YMCM4BU68pzhYZp9Q`

## Inputs

Resolve from what the user gives (issue number, fork PR number, or branch):

- **fork issue `N`** — the unit of work on the board.
- **branch** — the work branch (convention `claude/issue-N-*`, but human
  branches happen; get it from the fork PR's head).
- **fork PR** — the fork's review-surface PR for the branch (may be absent).

From an issue: find the fork PR via its linked-PR chip
(`closedByPullRequestsReferences`) or by scanning machine-account comments for
`/pull/` refs (take the one still open; see the design doc's reconcile rules).
From a PR or branch: the issue number comes from the branch name, else a
same-repo `Fixes` ref in the PR body.

## Steps

1. **Preflight.** Confirm the branch exists on the fork and is pushed
   (`gh api repos/meridianlabs-ai/inspect_ai/branches/<branch>`). Advisory,
   not blocking: note whether the fork PR's review converged (last
   `claude-review-verdict` marker) and whether fork CI is green — surface
   anything unfinished to the user before proceeding.

2. **Open (or adopt) the upstream PR.** Check for an existing one first:

   ```sh
   gh api "repos/UKGovernmentBEIS/inspect_ai/pulls?head=meridianlabs-ai:<branch>&state=all" \
     --jq '.[] | {number, state, merged_at}'
   ```

   - If one exists (open or merged): **adopt it** — skip creation, continue to
     bookkeeping (this is the heal path).
   - Else create it, deriving title/body from the fork PR (or the issue when
     there is no fork PR). The body MUST contain the fully-qualified
     **`Fixes meridianlabs-ai/inspect_ai#N`** — this is what populates the
     fork issue's linked-PR chip cross-org and makes the PR discoverable to
     the sync; a bare `#N` would rebind to upstream's tracker. Keep the
     upstream PR-template checklist if the fork PR body carries one.

   ```sh
   gh pr create --repo UKGovernmentBEIS/inspect_ai \
     --head meridianlabs-ai:<branch> --title "<title>" --body "<body>"
   ```

   Then assign `dragonstyle` and request their review (idempotent — skip
   whichever is already set):

   ```sh
   gh api repos/UKGovernmentBEIS/inspect_ai/issues/<M>/assignees -X POST -f "assignees[]=dragonstyle"
   gh api repos/UKGovernmentBEIS/inspect_ai/pulls/<M>/requested_reviewers -X POST -f "reviewers[]=dragonstyle"
   ```

3. **Bookkeeping** (each idempotent — check before writing):

   a. **`Upstream PR` field** ← the upstream PR URL (the sync's join key; a
      promotion without it is invisible to the hourly sync — the #90 lesson):

      ```sh
      ITEM=$(gh api graphql -f query='{repository(owner:"meridianlabs-ai",name:"inspect_ai"){issue(number:N){projectItems(first:5){nodes{id project{number}}}}}}' \
        --jq '.data.repository.issue.projectItems.nodes[] | select(.project.number==1) | .id')
      # if empty: addProjectV2ItemById with the issue node_id first
      gh api graphql -f query='mutation($p:ID!,$i:ID!,$f:ID!,$t:String!){updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,value:{text:$t}}){projectV2Item{id}}}' \
        -f p=PVT_kwDOC7YMCM4BU68p -f i="$ITEM" -f f=PVTF_lADOC7YMCM4BU68pzhYZp9Q -f t="<upstream url>"
      ```

   b. **Stage → `Sign-off`** (and `Status → In progress`), same mutation shape
      with field `PVTSSF_lADOC7YMCM4BU68pzhYZEwY` option `da6137e6` (and
      `PVTSSF_lADOC7YMCM4BU68pzhKizZM` option `47fc9ee4`). Skip if already at
      Sign-off or beyond (Merge — the sync may have advanced it).

   c. **Comment the upstream link on the fork issue** (the human-visible
      pointer; the Development chip comes from the `Fixes` ref, not this):
      skip if any comment already contains the upstream PR URL; else:

      ```sh
      gh issue comment N --repo meridianlabs-ai/inspect_ai \
        --body "Promoted upstream → UKGovernmentBEIS/inspect_ai#M (awaiting review)."
      ```

   d. **Supersede the fork PR** (if one exists and is open): comment
      `Superseded by the upstream PR: UKGovernmentBEIS/inspect_ai#M` and close
      it — the fork PR is a review surface, never merged.

4. **Report.** Upstream PR link, issue link, stage set, and which bookkeeping
   steps were created vs. already present (healed vs. no-op). From here the
   hourly Atlas sync owns the tail: approval → Merge, merge → Done
   (it closes the fork issue), changes-requested → Review, re-request →
   Sign-off.

## Cautions

- Never push to `main`/`meridian`; promotion only opens a PR from the existing
  branch.
- Upstream is not ours: no labels and no Meridian-internal markers on the
  upstream PR beyond the `Fixes` ref. The one exception is the `dragonstyle`
  assignee + review request from step 2 (explicitly requested by Ransom).
- Do not merge anything — upstream merges are upstream's call; the fork issue
  closes via the sync when that happens.
