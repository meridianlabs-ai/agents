#!/usr/bin/env bash
# Fast path of /promote (see SKILL.md): fork issue -> upstream PR + full
# tracking bookkeeping, in one invocation. Every write is check-before-write
# (idempotent), so re-running heals an already-promoted issue.
#
# Usage: promote.sh <issue-number> [--dry-run]
# Exit codes: 0 ok; 3 no open same-repo fork PR chip (resolve inputs via the
# skill's slow path); 4 branch not on the fork; 5 preflight hard failure.
set -euo pipefail

FORK=meridianlabs-ai/inspect_ai
UPSTREAM=UKGovernmentBEIS/inspect_ai
TSMONO=meridianlabs-ai/ts-mono
PROJECT=PVT_kwDOC7YMCM4BU68p
STAGE_FIELD=PVTSSF_lADOC7YMCM4BU68pzhYZEwY   # Sign-off option below
SIGNOFF_OPT=da6137e6
STATUS_FIELD=PVTSSF_lADOC7YMCM4BU68pzhKizZM  # In progress option below
INPROGRESS_OPT=47fc9ee4
UPSTREAM_PR_FIELD=PVTF_lADOC7YMCM4BU68pzhYZp9Q
REVIEWER=dragonstyle

N=${1:?usage: promote.sh <issue-number> [--dry-run]}
DRY=${2:-}

write() {  # guard every mutation; --dry-run prints instead
  if [ "$DRY" = "--dry-run" ]; then echo "DRY-RUN: $*"; else "$@"; fi
}

# ---- one GraphQL round trip: chips (with PR bodies) + board item + fields
JSON=$(gh api graphql -f query='query($n:Int!){repository(owner:"meridianlabs-ai",name:"inspect_ai"){issue(number:$n){id title state body
  closedByPullRequestsReferences(first:10,includeClosedPrs:true){nodes{number state isDraft title body headRefName repository{nameWithOwner}}}
  projectItems(first:5){nodes{id project{number}
    stage: fieldValueByName(name:"Stage"){... on ProjectV2ItemFieldSingleSelectValue{name}}
    up: fieldValueByName(name:"Upstream PR"){... on ProjectV2ItemFieldTextValue{text}}}}}}}' -F n="$N")

ISSUE_NODE=$(jq -r '.data.repository.issue.id' <<<"$JSON")
ISSUE_TITLE=$(jq -r '.data.repository.issue.title' <<<"$JSON")
ISSUE_STATE=$(jq -r '.data.repository.issue.state' <<<"$JSON")
# Imported issues (skills/import) carry an "Upstream issue:" body line; the
# upstream PR then gets a bare `Fixes #<up>` too, so the upstream issue links
# and auto-closes on merge (bare refs resolve there — upstream PRs base on
# upstream main).
UP_ISSUE=$(jq -r '.data.repository.issue.body // ""' <<<"$JSON" \
  | grep -oE 'Upstream issue: https://github.com/UKGovernmentBEIS/inspect_ai/issues/[0-9]+' \
  | head -1 | grep -oE '[0-9]+$' || true)
# Prefer the OPEN fork PR; fall back to a closed one (the heal path — a
# promoted issue's fork PR is already superseded and closed).
PICK=$(jq -c --arg repo "$FORK" '([.data.repository.issue.closedByPullRequestsReferences.nodes[]
  | select(.repository.nameWithOwner==$repo)] | (map(select(.state=="OPEN")) + .))[0] // empty' <<<"$JSON")
if [ -z "$PICK" ]; then
  echo "NO FORK PR CHIP for issue #$N — resolve inputs via the slow path. Chips seen:" >&2
  jq -r '.data.repository.issue.closedByPullRequestsReferences.nodes[]
         | "  #\(.number) \(.state) \(.repository.nameWithOwner) head=\(.headRefName)"' <<<"$JSON" >&2
  exit 3
fi
FPR_STATE=$(jq -r .state <<<"$PICK")
FPR=$(jq -r .number <<<"$PICK")
BRANCH=$(jq -r .headRefName <<<"$PICK")
FPR_TITLE=$(jq -r .title <<<"$PICK")
FPR_BODY=$(jq -r .body <<<"$PICK")

ITEM=$(jq -r '[.data.repository.issue.projectItems.nodes[] | select(.project.number==1)][0].id // empty' <<<"$JSON")
CUR_STAGE=$(jq -r '[.data.repository.issue.projectItems.nodes[] | select(.project.number==1)][0].stage.name // empty' <<<"$JSON")
CUR_UP=$(jq -r '[.data.repository.issue.projectItems.nodes[] | select(.project.number==1)][0].up.text // empty' <<<"$JSON")

# ---- preflight (branch pushed; review verdict + CI are advisory)
gh api "repos/$FORK/branches/$BRANCH" --silent 2>/dev/null ||
  { echo "branch $BRANCH not on the fork" >&2; exit 4; }
# --paginate: busy @auto issues/PRs exceed 100 comments, and the API returns
# oldest-first — a single page never sees recent comments.
VERDICT=$(gh api --paginate "repos/$FORK/issues/$FPR/comments?per_page=100" \
  --jq '.[] | select(.body | contains("claude-review-verdict")) | .body' 2>/dev/null \
  | tail -1 | grep -o 'verdict:[a-z]*' || echo "verdict:none")
CI=$(gh pr checks "$FPR" -R "$FORK" 2>&1 | awk -F'\t' '{print $2}' | sort | uniq -c | tr '\n' ' ' || true)
echo "ADVISORY: fork PR #$FPR review $VERDICT; CI: ${CI:-unknown}"

# ---- upstream PR: adopt or create. Adoption detection uses the issue's own
# cross-repo chip (same branch, upstream repo) — the REST `pulls?head=org:br`
# filter silently returns [] for this org-fork pair (verified against a known
# merged PR), so it cannot be trusted.
UP_PICK=$(jq -c --arg up "$UPSTREAM" --arg br "$BRANCH" \
  '[.data.repository.issue.closedByPullRequestsReferences.nodes[]
    | select(.repository.nameWithOwner==$up) | select(.headRefName==$br)][0] // empty' <<<"$JSON")
M=$(jq -r '.number // empty' <<<"$UP_PICK")
if [ -n "$M" ]; then
  UP_URL="https://github.com/$UPSTREAM/pull/$M"
  echo "ADOPTED existing upstream PR #$M ($(jq -r .state <<<"$UP_PICK"))"
else
  # Fully-qualified Fixes ref: qualify any bare same-repo ref, else prepend.
  # For imported issues, also prepend the bare upstream `Fixes #<up>`.
  BODY=$(ISSUE_N="$N" UP_ISSUE="$UP_ISSUE" FPR_BODY="$FPR_BODY" python3 -c '
import os, re
n = os.environ["ISSUE_N"]
body = os.environ["FPR_BODY"]
body = re.sub(r"\b(Fixes|Closes|Resolves)\s+#%s\b" % n,
              r"\1 meridianlabs-ai/inspect_ai#%s" % n, body)
if ("meridianlabs-ai/inspect_ai#%s" % n) not in body:
    body = "Fixes meridianlabs-ai/inspect_ai#%s\n\n" % n + body
up = os.environ["UP_ISSUE"]
if up and not re.search(r"\b(Fixes|Closes|Resolves)\s+#%s\b" % up, body):
    body = "Fixes #%s\n" % up + body
print(body)')
  # Org forks cannot be resolved as `owner:branch` heads — PR creation needs
  # an explicit head_repo, which `gh pr create` lacks (cli/cli#6462; see the
  # fork's AGENTS.md → "Opening an upstream PR from an org fork").
  if [ "$DRY" = "--dry-run" ]; then
    echo "DRY-RUN: gh api repos/$UPSTREAM/pulls -X POST -f base=main -f head=$BRANCH -f head_repo=$FORK -f title=\"$FPR_TITLE\" -f body=<transformed fork-PR body>"
    UP_URL="(dry-run)"; M=0
  else
    CREATED=$(gh api "repos/$UPSTREAM/pulls" -X POST \
      -f base=main -f head="$BRANCH" -f head_repo="$FORK" \
      -f title="$FPR_TITLE" -f body="$BODY" --jq '{number, html_url}')
    M=$(jq -r .number <<<"$CREATED")
    UP_URL=$(jq -r .html_url <<<"$CREATED")
    echo "CREATED upstream PR #$M"
  fi
fi

# ---- assignee + review request (skip whichever is already set)
if [ "$M" != "0" ]; then
  UP_STATE=$(gh api "repos/$UPSTREAM/pulls/$M" --jq '{a: [.assignees[].login], r: [.requested_reviewers[].login], state}' 2>/dev/null || echo '{}')
  if [ "$(jq -r .state <<<"$UP_STATE")" = "open" ]; then
    jq -e --arg u "$REVIEWER" '.a | index($u)' <<<"$UP_STATE" >/dev/null 2>&1 ||
      write gh api "repos/$UPSTREAM/issues/$M/assignees" -X POST -f "assignees[]=$REVIEWER" --silent
    jq -e --arg u "$REVIEWER" '.r | index($u)' <<<"$UP_STATE" >/dev/null 2>&1 ||
      write gh api "repos/$UPSTREAM/pulls/$M/requested_reviewers" -X POST -f "reviewers[]=$REVIEWER" --silent
  fi
fi

# ---- ts-mono companion: the viewer half needs the SAME human reviewer.
# Companions share the branch name (dev-agent convention; the sync and the
# chip sweep key on it). ts-mono has no promotion step — its PR merges in
# place — so sign-off review is requested here, at promotion time.
COMPANION=$(gh pr list --repo "$TSMONO" --head "$BRANCH" --state open   --json number --jq '.[0].number // empty' 2>/dev/null || true)
if [ -n "$COMPANION" ]; then
  C_STATE=$(gh api "repos/$TSMONO/pulls/$COMPANION" --jq '{a: [.assignees[].login], r: [.requested_reviewers[].login]}' 2>/dev/null || echo '{}')
  jq -e --arg u "$REVIEWER" '.a | index($u)' <<<"$C_STATE" >/dev/null 2>&1 ||
    write gh api "repos/$TSMONO/issues/$COMPANION/assignees" -X POST -f "assignees[]=$REVIEWER" --silent
  jq -e --arg u "$REVIEWER" '.r | index($u)' <<<"$C_STATE" >/dev/null 2>&1 ||
    write gh api "repos/$TSMONO/pulls/$COMPANION/requested_reviewers" -X POST -f "reviewers[]=$REVIEWER" --silent
  echo "companion $TSMONO#$COMPANION: $REVIEWER assigned + review requested"
fi

# ---- board bookkeeping (the #90 lesson: the Upstream PR field is the sync's join key)
if [ -z "$ITEM" ]; then
  ITEM=$(write gh api graphql -f query='mutation($p:ID!,$c:ID!){addProjectV2ItemById(input:{projectId:$p,contentId:$c}){item{id}}}' \
    -f p="$PROJECT" -f c="$ISSUE_NODE" --jq '.data.addProjectV2ItemById.item.id' || echo "")
  echo "board: item added"
fi
if [ "$CUR_UP" != "$UP_URL" ] && [ -n "$ITEM" ] && [ "$UP_URL" != "(dry-run)" ]; then
  write gh api graphql -f query='mutation($p:ID!,$i:ID!,$f:ID!,$t:String!){updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,value:{text:$t}}){projectV2Item{id}}}' \
    -f p="$PROJECT" -f i="$ITEM" -f f="$UPSTREAM_PR_FIELD" -f t="$UP_URL" --silent
  echo "board: Upstream PR field set"
else
  echo "board: Upstream PR field already current (or dry-run)"
fi
# Never re-stage a closed (Done) issue — healing must not resurrect it.
[ "$ISSUE_STATE" = "CLOSED" ] && CUR_STAGE="__issue-closed__"
case "$CUR_STAGE" in
  __issue-closed__) echo "board: issue is CLOSED — stage left alone" ;;
  Sign-off|Merge) echo "board: stage already $CUR_STAGE — left alone" ;;
  *)
    if [ -n "$ITEM" ]; then
      write gh api graphql -f query='mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){projectV2Item{id}}}' \
        -f p="$PROJECT" -f i="$ITEM" -f f="$STAGE_FIELD" -f o="$SIGNOFF_OPT" --silent
      write gh api graphql -f query='mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){projectV2Item{id}}}' \
        -f p="$PROJECT" -f i="$ITEM" -f f="$STATUS_FIELD" -f o="$INPROGRESS_OPT" --silent
      echo "board: stage -> Sign-off (was ${CUR_STAGE:-unset})"
    fi ;;
esac

# ---- fork-issue comment (skip if any comment already links the upstream PR)
if [ "$UP_URL" != "(dry-run)" ]; then
  # Match either spelling of the upstream PR ref — the comment this script
  # posts uses owner/repo#M, humans and chips use the full URL. Paginated:
  # oldest-first pages mean recent comments live beyond page 1 on busy
  # issues. Plain grep, not jq --arg: gh's --jq flag takes only an
  # expression and silently misparses jq arguments.
  HAVE=$(gh api --paginate "repos/$FORK/issues/$N/comments?per_page=100" \
    --jq '.[].body' 2>/dev/null | grep -cF -e "$UP_URL" -e "UKGovernmentBEIS/inspect_ai#$M" || true)
  if [ "${HAVE:-0}" -eq 0 ]; then
    NOTE="awaiting review"
    [ -n "$UP_PICK" ] && [ "$(jq -r .state <<<"$UP_PICK")" != "OPEN" ] && NOTE="already $(jq -r .state <<<"$UP_PICK" | tr 'A-Z' 'a-z')"
    write gh issue comment "$N" --repo "$FORK" \
      --body "Promoted upstream → UKGovernmentBEIS/inspect_ai#$M ($NOTE)."
    echo "issue: promotion comment posted"
  else
    echo "issue: promotion comment already present"
  fi
fi

# ---- supersede the fork PR if still open (review surface, never merged)
if [ "$FPR_STATE" = "OPEN" ]; then
  write gh pr comment "$FPR" --repo "$FORK" \
    --body "Superseded by the upstream PR: UKGovernmentBEIS/inspect_ai#$M"
  write gh pr close "$FPR" --repo "$FORK"
  echo "fork PR #$FPR: superseded and closed"
else
  echo "fork PR #$FPR: already closed"
fi

# ---- best-effort chip sweep: Development-panel links have no API and fork
# closing refs are inert (non-default base), so the Playwright sweep is the
# ONLY thing that ever creates the issue<->PR chips. Run it here so promote
# leaves no chip pending — but never let it block or fail the promotion:
# an expired browser login just gets reported for a later /resolve-board.
if [ "$DRY" != "--dry-run" ]; then
  SWEEP="$(cd "$(dirname "$(realpath "$0")")/../.." && pwd)/scripts/link-upstream-chips"
  if [ -d "$SWEEP" ]; then
    if (cd "$SWEEP" && node index.mjs) 2>&1 | tail -3; then
      echo "chips: sweep ran"
    else
      echo "chips: sweep failed (expired login? run /resolve-board later) — promotion unaffected"
    fi
  fi
fi

echo "OK issue=#$N forkPR=#$FPR upstream=$UP_URL ($ISSUE_TITLE)"
