#!/usr/bin/env bash
# Gather step of /post-upstream-review (see SKILL.md): find the findings
# comment to relay from an External proxy issue, and the upstream PR it is
# about, by verified author rather than by recency (Claude Security
# 4773885; skills/THREAT_MODEL.md rule (b)).
#
# The proxy is public: anyone can comment on it, and a comment shaped like
# the reviewer's findings would otherwise be relayed upstream under the
# maintainer's name. So a findings comment counts only when its author is
# the machine account (TRUSTED_LOGINS) or has write access on the fork (a
# failed lookup is untrusted); every other comment is ignored, for the
# relay and for the double-relay check. When the NEWEST findings-shaped
# comment is not a trusted one, nothing is chosen: the script stops and
# names its author, for the maintainer to decide. The proxy itself must be
# genuine (written by the machine account AND labelled External), and its
# `Upstream PR:` line must name a PR of UKGovernmentBEIS/inspect_ai.
#
# A comment is findings-shaped when its body names a severity (blocking,
# non-blocking) or one of the reviewer's verdict lines (looks ready to
# approve, changes needed, needs discussion), and is not this skill's own
# `Relayed upstream as …` audit comment.
#
# Usage: gather.sh <proxy-issue-number> [--out <dir>]
#   --out <dir>  where to write findings.md (the chosen comment's body, as
#                data) and context.json (what post.sh needs); default a new
#                temporary directory.
# Prints `OK proxy=#N upstream=<url> head=<sha> findings=<url> by <login>
# at <time>` and the two paths. Exit codes: 0 ok; 1 usage; 2 a GitHub read
# failed (nothing was chosen); 3 nothing to relay (not a genuine External
# proxy, no Upstream PR line under upstream, the upstream PR is not open,
# or no trusted findings comment); 4 the newest findings-shaped comment is
# by an untrusted author (named on stderr) — stop and ask the maintainer;
# 5 already relayed: your own upstream review is newer than the trusted
# findings comment — stop and ask.
set -euo pipefail
. "$(dirname "$(realpath "$0")")/../lib/common.sh"

FORK=meridianlabs-ai/inspect_ai
UPSTREAM=UKGovernmentBEIS/inspect_ai
FINDINGS_RE='(^|[^[:alnum:]])(non-?blocking|blocking)([^[:alnum:]]|$)|looks ready to approve|changes needed|needs discussion'

usage() { echo "usage: gather.sh <proxy-issue-number> [--out <dir>]" >&2; exit 1; }
N=""
OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --out) [ -n "${2:-}" ] || usage; OUT=$2; shift ;;
    -*) usage ;;
    *) N=$1 ;;
  esac
  shift
done
case "$N" in ""|*[!0-9]*) usage ;; esac
if [ -z "$OUT" ]; then
  OUT=$(mktemp -d "${TMPDIR:-/tmp}/post-upstream-review.XXXXXX")
else
  mkdir -p -- "$OUT"
fi

fail_read() { echo "ABORT: $1 — nothing was chosen; re-run" >&2; exit 2; }

ISSUE=$(gh api "repos/$FORK/issues/$N" 2>/dev/null) || fail_read "could not read $FORK#$N"
AUTHOR=$(jq -r '.user.login // ""' <<<"$ISSUE")
LABELS=$(jq -r '[.labels[].name] | join(",")' <<<"$ISSUE")
if ! genuine_proxy "$AUTHOR" "$LABELS"; then
  echo "REFUSED: $FORK#$N is not a genuine External proxy (author=${AUTHOR:-?}, labels=${LABELS:-none}; a proxy is written by $TRUSTED_LOGINS and labelled External)" >&2
  exit 3
fi
proxy_upstream_pr "$(jq -r '.body // ""' <<<"$ISSUE")" "$UPSTREAM"
if [ -z "$UP_NUM" ]; then
  echo "REFUSED: proxy #$N has no 'Upstream PR:' line naming a PR of $UPSTREAM (${UP_LINE:-none})" >&2
  exit 3
fi
PR=$(gh api "repos/$UPSTREAM/pulls/$UP_NUM" 2>/dev/null) || fail_read "could not read $UPSTREAM#$UP_NUM"
if [ "$(jq -r .state <<<"$PR")" != "open" ]; then
  echo "REFUSED: $UPSTREAM#$UP_NUM is $(jq -r .state <<<"$PR"), not open — nothing to relay" >&2
  exit 3
fi
HEAD_SHA=$(jq -r '.head.sha // ""' <<<"$PR")

# Every comment, oldest first, must be read: a page that failed would hide
# a newer comment. Only findings-shaped comments are judged, and only their
# authors looked up.
ROWS=$(gh api --paginate "repos/$FORK/issues/$N/comments?per_page=100" --jq '.[] | @json' 2>/dev/null) ||
  fail_read "could not read the comments on $FORK#$N"
TRUSTED=""
NEWEST=""
NEWEST_OK=""
IGNORED=""
while IFS= read -r row; do
  [ -n "$row" ] || continue
  jq -e --arg re "$FINDINGS_RE" '(.body // "") as $b
    | ($b | test("^\\s*Relayed upstream as") | not) and ($b | test($re; "i"))' <<<"$row" >/dev/null || continue
  login=$(jq -r '.user.login // ""' <<<"$row")
  NEWEST=$row
  if trusted_login "$FORK" "$login"; then
    TRUSTED=$row
    NEWEST_OK=1
  else
    NEWEST_OK=""
    IGNORED="$IGNORED  $login $(jq -r '.html_url // "?"' <<<"$row")"$'\n'
  fi
done <<<"$ROWS"

describe() { jq -r '"\(.html_url // "?") by \(.user.login // "?") at \(.created_at // "?")"' <<<"$1"; }
if [ -n "$NEWEST" ] && [ -z "$NEWEST_OK" ]; then
  echo "STOP: the newest findings-shaped comment on proxy #$N is $(describe "$NEWEST"), who is not in TRUSTED_LOGINS ($TRUSTED_LOGINS) and has no write access on $FORK. It is not relayed." >&2
  if [ -n "$TRUSTED" ]; then
    echo "The newest trusted findings comment is $(describe "$TRUSTED"). Ask the maintainer which to relay, if any." >&2
  else
    echo "There is no trusted findings comment. Ask the maintainer." >&2
  fi
  exit 4
fi
if [ -z "$TRUSTED" ]; then
  echo "REFUSED: proxy #$N has no findings comment by the machine account or a write-access collaborator — nothing to relay" >&2
  exit 3
fi
[ -z "$IGNORED" ] || { echo "note: ignored findings-shaped comments by untrusted authors (older than the one chosen):" >&2; printf '%s' "$IGNORED" >&2; }
F_URL=$(jq -r .html_url <<<"$TRUSTED")
F_AT=$(jq -r .created_at <<<"$TRUSTED")

# Double relay: a review of yours on the upstream PR newer than the chosen
# comment means it was relayed already.
ME=$(gh api user --jq .login 2>/dev/null) || fail_read "could not read your own login (gh api user)"
REVIEWS=$(gh api --paginate "repos/$UPSTREAM/pulls/$UP_NUM/reviews?per_page=100" \
    --jq '.[] | [(.user.login // ""), (.submitted_at // ""), (.html_url // "")] | @tsv' 2>/dev/null) ||
  fail_read "could not read the reviews on $UPSTREAM#$UP_NUM"
me_lc=$(tr 'A-Z' 'a-z' <<<"$ME")
while IFS=$'\t' read -r r_login r_at r_url; do
  [ "$(tr 'A-Z' 'a-z' <<<"$r_login")" = "$me_lc" ] || continue
  if [[ "$r_at" > "$F_AT" ]]; then
    echo "STOP: your review $r_url ($r_at) on $UPSTREAM#$UP_NUM is newer than the findings comment $F_URL ($F_AT) — it looks relayed already. Ask the maintainer before posting again." >&2
    exit 5
  fi
done <<<"$REVIEWS"

jq -j '.body // ""' <<<"$TRUSTED" >"$OUT/findings.md"
jq -n --argjson proxy "$N" --arg fork "$FORK" --arg upstream "$UPSTREAM" --argjson pr "$UP_NUM" \
  --arg head "$HEAD_SHA" --argjson f "$TRUSTED" --arg me "$ME" \
  '{proxy: $proxy, fork: $fork, upstream: $upstream, pr: $pr, head_sha: $head, maintainer: $me,
    findings: {url: $f.html_url, id: $f.id, author: $f.user.login, created_at: $f.created_at}}' >"$OUT/context.json"
echo "OK proxy=#$N upstream=https://github.com/$UPSTREAM/pull/$UP_NUM head=$HEAD_SHA findings=$(describe "$TRUSTED")"
echo "findings: $OUT/findings.md"
echo "context: $OUT/context.json"
