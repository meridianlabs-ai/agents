#!/usr/bin/env bash
# Fast path of /import (see SKILL.md): mirror an upstream inspect_ai issue
# into the fork so @auto can work it (the agents have no upstream write
# access; the workflows live on the fork). Deliberately does NOT kick @auto —
# the human usually adds guidance to that comment.
#
# Usage: import.sh <upstream-issue-number-or-url> [--dry-run]
# Exit codes: 0 ok (created, or already imported); 4 not an open upstream
# issue (closed, a PR, or missing).
set -euo pipefail

FORK=meridianlabs-ai/inspect_ai
UPSTREAM=UKGovernmentBEIS/inspect_ai
PROJECT=PVT_kwDOC7YMCM4BU68p
STATUS_FIELD=PVTSSF_lADOC7YMCM4BU68pzhKizZM  # Todo option below
TODO_OPT=f75ad846

ARG=${1:?usage: import.sh <upstream-issue-number-or-url> [--dry-run]}
DRY=${2:-}
N=$(grep -oE '[0-9]+' <<<"$ARG" | tail -1)  # accepts 2615 or .../issues/2615
UP_URL="https://github.com/$UPSTREAM/issues/$N"

# ---- upstream issue (the issues endpoint also serves PRs — reject those:
# upstream PRs are the External-proxy flow, not an import)
UP=$(gh api "repos/$UPSTREAM/issues/$N" 2>/dev/null) ||
  { echo "no upstream issue $UPSTREAM#$N" >&2; exit 4; }
jq -e 'has("pull_request") | not' <<<"$UP" >/dev/null ||
  { echo "$UPSTREAM#$N is a PR — upstream PRs get External proxies via the Atlas sync, not /import" >&2; exit 4; }
[ "$(jq -r .state <<<"$UP")" = "open" ] ||
  { echo "$UPSTREAM#$N is closed — nothing to work on" >&2; exit 4; }
TITLE=$(jq -r .title <<<"$UP")

# ---- dedupe on the machine-readable "Upstream issue:" body line, open AND
# closed (a Done import must never be recreated). Search-index lag (~1 min)
# is the only dup window — acceptable for a human-invoked skill.
EXISTING=$(gh search issues --repo "$FORK" "\"$UP_URL\"" --match body \
  --json url,state --jq '.[0] // empty' 2>/dev/null || true)
if [ -n "$EXISTING" ]; then
  echo "OK fork issue=$(jq -r .url <<<"$EXISTING") upstream=$UP_URL (already imported, $(jq -r .state <<<"$EXISTING"))"
  exit 0
fi

# ---- body: "Upstream issue:" key line (promote.sh reads it to add the
# upstream Fixes ref), then a snapshot with bare #refs qualified — in fork
# context they would rebind to unrelated fork issue numbers.
BODY=$(UP_BODY="$(jq -r '.body // ""' <<<"$UP")" UP_URL="$UP_URL" python3 -c '
import os, re
snap = re.sub(r"(?<![\w/])#(\d+)\b", r"UKGovernmentBEIS/inspect_ai#\1",
              os.environ["UP_BODY"])
print("""Upstream issue: %s

Imported from upstream so the agents can work it here (no upstream write
access). Canonical discussion stays upstream; below is a snapshot of the
upstream body at import time.

---

%s""" % (os.environ["UP_URL"], snap))')

if [ "$DRY" = "--dry-run" ]; then
  echo "DRY-RUN: gh issue create --repo $FORK --title \"$TITLE\" --body <'Upstream issue: $UP_URL' + snapshot>"
  echo "DRY-RUN: add to Atlas + Status -> Todo"
  echo "OK (dry-run) upstream=$UP_URL"
  exit 0
fi

NEW_URL=$(gh issue create --repo "$FORK" --title "$TITLE" --body "$BODY")
NEW_N=${NEW_URL##*/}
echo "created fork issue #$NEW_N"

# ---- board: add to Atlas, Status -> Todo. Stage stays unset — the agent
# post-step owns Todo -> Agent when @auto starts.
NODE=$(gh api "repos/$FORK/issues/$NEW_N" --jq .node_id)
ITEM=$(gh api graphql -f query='mutation($p:ID!,$c:ID!){addProjectV2ItemById(input:{projectId:$p,contentId:$c}){item{id}}}' \
  -f p="$PROJECT" -f c="$NODE" --jq '.data.addProjectV2ItemById.item.id')
gh api graphql -f query='mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){projectV2Item{id}}}' \
  -f p="$PROJECT" -f i="$ITEM" -f f="$STATUS_FIELD" -f o="$TODO_OPT" --silent
echo "board: item added, Status -> Todo"

echo "OK fork issue=$NEW_URL upstream=$UP_URL"
echo "next: comment '@auto <your guidance>' on the fork issue to kick it off"
