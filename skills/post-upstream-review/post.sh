#!/usr/bin/env bash
# Post step of /post-upstream-review (see SKILL.md): send the review the
# agent composed to the contributor's upstream PR, as the maintainer, then
# note the relay on the proxy and move it to Contributor.
#
# The review text is derived from the findings comment, which quotes the
# contributor's code and text, so it never passes through a command line
# (Claude Security 4773877; skills/THREAT_MODEL.md rule (a)): the agent
# writes it as a JSON file, {body, event, comments: [{path, line, side,
# start_line, start_side, body}]}, and this script sends a copy it
# rebuilt from the checked fields with `gh api --input` — no `-f` body or
# path fields. Before anything is sent (rule (d)): only those keys are
# accepted, `event` is COMMENT or REQUEST_CHANGES (never APPROVE), the
# agents' trigger phrases are backticked (outbound.py defang-review), the
# AI-disclosure footer (AI-generated, wording not reviewed by the
# maintainer) is appended when the body does not end with it,
# every text is rendered by GitHub in upstream's context and refused when
# it references an upstream issue or PR other than this one (the findings
# were written on the fork, where a bare `#N` means a fork issue), and the
# review is printed as it will be posted. The review is pinned to the head
# gather.sh saw (`commit_id`), and gather.sh runs again first: a newer
# trusted findings comment, an edit to the chosen one, a moved head, a
# closed PR or a review of yours that already relayed it refuses the post.
#
# Usage: post.sh <context.json> <review.json> [--dry-run] [--allow-ref <N>]...
#   context.json  written by gather.sh.
#   --dry-run     run every check and print the review; post nothing.
#   --allow-ref   an upstream issue or PR the maintainer confirmed the review
#                 may reference (repeatable).
# Exit codes: 0 posted (or dry-run); 1 usage, or the review file is
# malformed (stderr says why); 2 a GitHub call failed before the review was
# posted (nothing was posted); 3 the state changed since gather.sh (a newer
# findings comment, a moved head) or gather.sh refused now (its message is
# relayed) — re-run gather.sh; 4 guard refusal: a text references another
# upstream issue or PR, or could not be rendered to check; 6 the review was
# posted but the proxy bookkeeping failed (stderr says which step; do it by
# hand).
set -euo pipefail
HERE=$(dirname "$(realpath "$0")")
. "$HERE/../lib/common.sh"

PROJECT=PVT_kwDOC7YMCM4BU68p
STAGE_FIELD=PVTSSF_lADOC7YMCM4BU68pzhYZEwY
CONTRIBUTOR_OPT=39c05a50
FOOTER=$'---\n*This review was AI-generated from findings a maintainer chose to relay; the maintainer did not review its wording.*'
# The footer this skill used before 2026-09-30, which claimed a review of
# the wording that never happens: dropped from the end of a body that
# still carries it, so it is never posted.
OLD_FOOTER=$'---\n*This review was AI-generated, and reviewed by a maintainer before posting.*'

usage() { echo "usage: post.sh <context.json> <review.json> [--dry-run] [--allow-ref <N>]..." >&2; exit 1; }
CTX=""
REVIEW=""
DRY=""
ALLOW=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --allow-ref)
      case "${2:-}" in ""|*[!0-9]*) usage ;; esac
      ALLOW+=("$2"); shift ;;
    -*) usage ;;
    *) if [ -z "$CTX" ]; then CTX=$1; elif [ -z "$REVIEW" ]; then REVIEW=$1; else usage; fi ;;
  esac
  shift
done
[ -n "$CTX" ] && [ -n "$REVIEW" ] || usage
[ -f "$CTX" ] && [ -f "$REVIEW" ] || { echo "post.sh: $CTX or $REVIEW is not a file" >&2; exit 1; }
PROXY=$(jq -r '.proxy // empty | tostring' "$CTX" 2>/dev/null || true)
case "$PROXY" in ""|*[!0-9]*) echo "post.sh: $CTX has no proxy number — write it with gather.sh" >&2; exit 1 ;; esac

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# Gather again: every check it makes still has to hold, and it must choose
# the same findings comment at the same head.
if ! bash "$HERE/gather.sh" "$PROXY" --out "$WORK/now" >"$WORK/gather.out" 2>"$WORK/gather.err"; then
  echo "REFUSED: gather.sh no longer passes for proxy #$PROXY — nothing was posted:" >&2
  cat "$WORK/gather.err" >&2
  exit 3
fi
NOW="$WORK/now/context.json"
for key in findings.url findings.updated_at head_sha pr; do
  was=$(jq -r ".$key" "$CTX")
  now=$(jq -r ".$key" "$NOW")
  if [ "$was" != "$now" ]; then
    echo "REFUSED: $key changed since gather.sh ($was, now $now) — re-run gather.sh, re-read the findings and re-map the lines; nothing was posted" >&2
    exit 3
  fi
done
FORK=$(jq -r .fork "$NOW")
UPSTREAM=$(jq -r .upstream "$NOW")
PR=$(jq -r .pr "$NOW")
HEAD_SHA=$(jq -r .head_sha "$NOW")
F_URL=$(jq -r .findings.url "$NOW")

# The review: checked field by field and rebuilt, so nothing but these
# fields reaches the API.
if ! PAYLOAD=$(jq -c '
    def fail($m): error($m);
    if type != "object" then fail("the review is not a JSON object") else . end
    | (keys - ["body", "event", "comments"]) as $x
    | if ($x | length) > 0 then fail("unexpected key(s) \($x | join(", ")); a review has body, event and comments") else . end
    | if (.body | type) != "string" or (.body | length) == 0 then fail("body must be a non-empty string") else . end
    | if (.event | IN("COMMENT", "REQUEST_CHANGES")) | not then fail("event must be COMMENT or REQUEST_CHANGES, not \(.event | tostring); this skill never approves") else . end
    | if ((.comments // []) | type) != "array" then fail("comments must be an array") else . end
    | {body, event, comments: [(.comments // [])[]
        | if type != "object" then fail("an inline comment is not an object") else . end
        | (keys - ["path", "line", "side", "start_line", "start_side", "body"]) as $x
        | if ($x | length) > 0 then fail("unexpected inline-comment key(s) \($x | join(", "))") else . end
        | if (.path | type) != "string" or (.path | length) == 0 then fail("an inline comment has no path") else . end
        | if (.line | type) != "number" or .line < 1 or (.line | floor) != .line then fail("\(.path): line must be a positive integer") else . end
        | .side = (.side // "RIGHT")
        | if (.side | IN("RIGHT", "LEFT")) | not then fail("\(.path):\(.line): side must be RIGHT or LEFT") else . end
        | if has("start_line") then
            (if (.start_line | type) != "number" or .start_line < 1 or (.start_line | floor) != .start_line or .start_line >= .line
             then fail("\(.path):\(.line): start_line must be a positive integer below line") else . end)
            | .start_side = (.start_side // .side)
            | (if (.start_side | IN("RIGHT", "LEFT")) | not then fail("\(.path):\(.line): start_side must be RIGHT or LEFT") else . end)
          else . end
        | if (.body | type) != "string" or (.body | length) == 0 then fail("\(.path):\(.line): body must be a non-empty string") else . end
        | {path, line, side, body} + (if has("start_line") then {start_line, start_side} else {} end)]}
    ' "$REVIEW" 2>"$WORK/jq.err"); then
  echo "post.sh: $REVIEW is not a valid review: $(sed 's/^jq: error[^:]*: //' "$WORK/jq.err")" >&2
  exit 1
fi
PAYLOAD=$(python3 "$SKILLS_LIB/outbound.py" defang-review <<<"$PAYLOAD" \
  | jq -c --arg footer "$FOOTER" --arg old "$OLD_FOOTER" --arg head "$HEAD_SHA" \
      '.body |= (sub("\\s+$"; "")
        | if endswith($old) then .[: length - ($old | length)] | sub("\\s+$"; "") else . end
        | if endswith($footer) then . else . + "\n\n" + $footer end)
       | .commit_id = $head')
printf '%s\n' "$PAYLOAD" >"$WORK/payload.json"

# References: every text, rendered in upstream's context.
BAD=""
check_refs() {  # $1 = where, $2 = markdown
  local html stray
  if ! html=$(render_markdown "$2" "$UPSTREAM"); then
    echo "REFUSED: could not render the $1 with GitHub's Markdown API — its references cannot be checked; re-run. Nothing was posted." >&2
    exit 4
  fi
  stray=$(rendered_refs "$html" "$UPSTREAM" "$PR" ${ALLOW[@]+"${ALLOW[@]}"})
  [ -z "$stray" ] || BAD="$BAD  $1: ${stray% }"$'\n'
}
check_refs "review body" "$(jq -r .body <<<"$PAYLOAD")"
n=$(jq '.comments | length' <<<"$PAYLOAD")
i=0
while [ "$i" -lt "$n" ]; do
  check_refs "inline comment $(jq -r --argjson i "$i" '.comments[$i] | "\(.path):\(.line)"' <<<"$PAYLOAD")" \
    "$(jq -r --argjson i "$i" '.comments[$i].body' <<<"$PAYLOAD")"
  i=$((i + 1))
done
if [ -n "$BAD" ]; then
  echo "REFUSED: the review references $UPSTREAM issues or PRs other than #$PR (GitHub links them there under your name; the findings were written on the fork, where a bare #N is a fork issue):" >&2
  printf '%s' "$BAD" >&2
  echo "Reword each (drop the #, or name what it is), or re-run with --allow-ref <N> for one the maintainer confirmed. Nothing was posted." >&2
  exit 4
fi

echo "review as it will be posted on https://github.com/$UPSTREAM/pull/$PR ($(jq -r .event <<<"$PAYLOAD"), at $HEAD_SHA):"
jq -r .body <<<"$PAYLOAD" | sed 's/^/  | /'
echo "inline comments ($n):"
jq -r '.comments[] | "  \(.path):\(if .start_line then "\(.start_line)-" else "" end)\(.line) (\(.side))", (.body | split("\n")[] | "    | " + .)' <<<"$PAYLOAD"
echo "relays: $F_URL"
if [ -n "$DRY" ]; then
  echo "DRY-RUN: nothing was posted"
  exit 0
fi

if ! POSTED=$(gh api "repos/$UPSTREAM/pulls/$PR/reviews" -X POST --input "$WORK/payload.json" --jq '.html_url' 2>"$WORK/post.err"); then
  echo "ABORT: posting the review failed — nothing was posted:" >&2
  cat "$WORK/post.err" >&2
  exit 2
fi
echo "posted: $POSTED"

RC=0
printf 'Relayed upstream as %s (%s inline) from the findings comment %s. Awaiting contributor.\n' "$POSTED" "$n" "$F_URL" >"$WORK/audit.md"
if gh issue comment "$PROXY" --repo "$FORK" --body-file "$WORK/audit.md" >/dev/null 2>&1; then
  echo "proxy: relay noted on #$PROXY"
else
  echo "WARN: the review is posted, but the audit comment on proxy #$PROXY failed — post it by hand:" >&2
  cat "$WORK/audit.md" >&2
  RC=6
fi
ITEM=$(gh api graphql -f query='query($n:Int!){repository(owner:"meridianlabs-ai",name:"inspect_ai"){issue(number:$n){projectItems(first:5){nodes{id project{number}}}}}}' \
  -F n="$PROXY" --jq '[.data.repository.issue.projectItems.nodes[] | select(.project.number==1)][0].id // empty' 2>/dev/null || true)
if [ -n "$ITEM" ] && gh api graphql -f query='mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){projectV2Item{id}}}' \
    -f p="$PROJECT" -f i="$ITEM" -f f="$STAGE_FIELD" -f o="$CONTRIBUTOR_OPT" --silent 2>/dev/null; then
  echo "board: proxy #$PROXY stage -> Contributor"
else
  echo "WARN: the review is posted, but moving proxy #$PROXY to Contributor failed${ITEM:+ (item $ITEM)}${ITEM:- (no Atlas item found)} — set the stage by hand" >&2
  RC=6
fi
echo "OK review=$POSTED inline=$n findings=$F_URL proxy=#$PROXY"
exit "$RC"
