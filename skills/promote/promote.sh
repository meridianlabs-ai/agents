#!/usr/bin/env bash
# Fast path of /promote (see SKILL.md): fork issue -> upstream PR + full
# tracking bookkeeping, in one invocation. Every write is check-before-write
# (idempotent), so re-running heals an already-promoted issue.
#
# Trust rule (applied BEFORE any PR text — title, body, branch name — is
# consulted): a fork PR qualifies only when its head repository is the fork
# itself AND its author is in TRUSTED_LOGINS or has write access on the
# fork. Anyone can open a PR from a personal fork into the org fork and link
# it to an issue (GitHub creates the chip natively for `Fixes #N` on a PR
# based on the default branch), so a chip alone proves nothing; a branch
# inside the org fork can only be pushed by a write-access account, which
# makes the head-repository check the load-bearing one and the author check
# defence in depth. The picked PR's title, body and head branch become the
# upstream PR opened as the operator, so nothing an outsider wrote may reach
# that step. The fork ISSUE's body is the other outsider-writable input to
# that PR: its `Upstream issue:` line (which adds a bare `Fixes #<up>`
# upstream) is believed only as /import's header — the body's first line,
# with the `---` rule below it — and only from an author who passes the same
# trust rule. Bare `#M` refs in the fork PR body are qualified to the fork
# before the body is published upstream, where bare refs resolve against
# upstream's tracker, and the result is checked with GitHub's own renderer:
# any other reference to an upstream issue or PR is refused, not published,
# and so is a qualification that changed text GitHub does not read as a
# reference (code, a link destination).
# The body is printed as it will be published, in --dry-run and in the real
# run.
#
# Usage: promote.sh <issue-number> [--dry-run] [--pr <number>]
#   --dry-run      print the candidates, each verdict, the decision and every
#                  write that would happen; nothing is written.
#   --pr <number>  pin the fork PR to promote (needed when more than one
#                  qualifies); it must still pass the trust rule.
# Exit codes: 0 ok; 3 no qualifying fork PR (stderr lists every candidate,
# why it was refused and what the fallback looked for; resolve via the
# skill's slow path); 4 branch not on the fork; 5 hard failure before any
# write (the open-PR listing the fallback needs failed or was truncated; the
# resolved PR's head is a protected branch; the fork branch has moved past
# the resolved PR's head; a REVIEWER who is provably not a collaborator on
# upstream or on the ts-mono companion's repo; the upstream PR body would
# reference an upstream issue or PR other than the import's, qualifying its
# bare refs would change text that is not a reference, or it could not be
# rendered to check; a conflict merging upstream main into the branch);
# 6 ambiguous — more than one fork PR qualifies;
# re-run with --pr <number>.
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
# Upstream reviewer to assign + request (also on the ts-mono companion);
# override per run with REVIEWER=<login>. An override is validated in
# preflight below; the default is known-good and skips the upstream lookup.
# Lower-cased once: logins are case-insensitive at the API, and the
# idempotency checks compare against canonical `.login` values (also
# lower-cased there), so `REVIEWER=DragonStyle` heals instead of re-POSTing.
DEFAULT_REVIEWER=dragonstyle
REVIEWER=$(tr 'A-Z' 'a-z' <<<"${REVIEWER:-$DEFAULT_REVIEWER}")

N=""
DRY=""
PIN=""
usage() { echo "usage: promote.sh <issue-number> [--dry-run] [--pr <number>]" >&2; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=--dry-run ;;
    --pr) [ -n "${2:-}" ] || usage; PIN=$2; shift ;;
    -*) usage ;;
    *) N=$1 ;;
  esac
  shift
done
[ -n "$N" ] || usage
case "$PIN" in *[!0-9]*) echo "--pr takes a PR number, got '$PIN'" >&2; exit 1 ;; esac

write() {  # guard every mutation; --dry-run prints instead
  if [ "$DRY" = "--dry-run" ]; then echo "DRY-RUN: $*"; else "$@"; fi
}

# Trusted identities (comma-separated), in REST form: the machine account's
# User login (the PAT, today) and its GitHub App login (Phase 2; trusted by
# name — the collaborators endpoint answers `none` for an App). The User
# leaves this ONE value when the PAT is retired. Author logins are normalised
# to this form by NORM_LOGIN before comparison.
TRUSTED_LOGINS="i-am-marvin,meridian-marvin[bot]"
# NORM_LOGIN: jq filter over an author object → its login in REST form. A
# GraphQL Bot author (`__typename: Bot`) arrives bare (`meridian-marvin`) and
# gets the `[bot]` suffix; `gh pr list/view --json author` renders an App as
# `app/<slug>`. A deleted author (null) is "". A User's login is never
# rewritten, so a User who registers an App's slug is not the App.
NORM_LOGIN='if . == null then "" elif (.__typename // "") == "Bot" then "\(.login // "")[bot]" elif ((.login // "") | startswith("app/")) then "\(.login[4:])[bot]" else (.login // "") end'
# The reviewer app's own login, trusted ONLY where its review verdicts are
# read back (the ADVISORY line): the app posts on this repo only through
# this repo's own workflows. Never trusted for PR authorship.
REVIEWER_BOT="claude[bot]"
# Open fork PRs the fallback lists at most; a listing this long is treated
# as truncated (uniqueness cannot be established) and refused.
LIST_LIMIT=500

# trusted_login <login>: 0 when <login> is in TRUSTED_LOGINS or has write
# access on the fork (admin/maintain/write from the collaborator permission
# API — a public repo answers `read` for everyone else). A failed or
# unexpected lookup is untrusted (fail closed). Cached per login for the
# run; call it in the main shell, not inside $(...), or the cache is lost.
PERM_CACHE=""
trusted_login() {
  local login perm
  login=$(tr 'A-Z' 'a-z' <<<"$1")
  [ -n "$login" ] || return 1
  case ",$(tr 'A-Z' 'a-z' <<<"$TRUSTED_LOGINS")," in *",$login,"*) return 0 ;; esac
  case "$PERM_CACHE" in *"|$login=ok|"*) return 0 ;; *"|$login=no|"*) return 1 ;; esac
  perm=$(gh api "repos/$FORK/collaborators/$login/permission" --jq .permission 2>/dev/null || true)
  case "$perm" in
    admin|maintain|write) PERM_CACHE="$PERM_CACHE|$login=ok|"; return 0 ;;
    *) PERM_CACHE="$PERM_CACHE|$login=no|"; return 1 ;;
  esac
}
# check_pr <pr-json>: applies the trust rule; sets REASON to "" when the PR
# qualifies, else to why it was refused. Reads only headRepository and
# author — never the PR's title, body or branch name.
check_pr() {
  local head login
  head=$(jq -r '.headRepository.nameWithOwner // ""' <<<"$1")
  login=$(jq -r ".author | $NORM_LOGIN" <<<"$1")
  REASON=""
  if [ "$head" != "$FORK" ]; then
    REASON="head repository is '${head:-unknown}', not $FORK"
  elif ! trusted_login "$login"; then
    REASON="author '${login:-unknown}' is not in TRUSTED_LOGINS ($TRUSTED_LOGINS) and has no write access on $FORK"
  fi
}
# fmt_pr <pr-json>: one listing line for the operator.
fmt_pr() {
  jq -r '"  #\(.number) \(.state) head=\(.headRepository.nameWithOwner // "?"):\(.headRefName) author=\(.author.login // "?")"' <<<"$1"
}
# `gh pr list/view --json` → the chip shape used everywhere below (head repo
# as owner/repo; null when the head repository was deleted).
NORM='{number, state, isDraft, title, body, headRefName, headRefOid, author:{login:(.author.login // "")},
  repository:{nameWithOwner:$fork},
  headRepository:{nameWithOwner:(if (.headRepositoryOwner.login // "") != "" and (.headRepository.name // "") != ""
    then "\(.headRepositoryOwner.login)/\(.headRepository.name)" else null end)}}'

# ---- one GraphQL round trip: chips (with PR bodies) + board item + fields
JSON=$(gh api graphql -f query='query($n:Int!){repository(owner:"meridianlabs-ai",name:"inspect_ai"){issue(number:$n){id title state body author{login __typename}
  closedByPullRequestsReferences(first:10,includeClosedPrs:true){nodes{number state isDraft title body headRefName headRefOid author{login __typename} repository{nameWithOwner} headRepository{nameWithOwner}}}
  projectItems(first:5){nodes{id project{number}
    stage: fieldValueByName(name:"Stage"){... on ProjectV2ItemFieldSingleSelectValue{name}}
    up: fieldValueByName(name:"Upstream PR"){... on ProjectV2ItemFieldTextValue{text}}}}}}}' -F n="$N")

ISSUE_NODE=$(jq -r '.data.repository.issue.id' <<<"$JSON")
ISSUE_TITLE=$(jq -r '.data.repository.issue.title' <<<"$JSON")
ISSUE_STATE=$(jq -r '.data.repository.issue.state' <<<"$JSON")
# Imported issues (skills/import) open with the machine-written header line
# `Upstream issue: <url>`, a `---` rule, then the upstream author's text
# verbatim; the upstream PR then gets a bare `Fixes #<up>` too, so the
# upstream issue links and auto-closes on merge (bare refs resolve there —
# upstream PRs base on upstream main). That line is free text on any other
# issue, and an issue body stays editable by its author forever, so it is
# believed ONLY when the body has /import's shape — the header is the
# literal first line and a `---` rule follows it, above the snapshot — AND
# the issue's author passes the trust rule (TRUSTED_LOGINS or write access
# on the fork; fail closed). Anything else is ignored and said so on stderr:
# the fork is public, and the `Fixes #<up>` would close whichever upstream
# issue the line names.
ISSUE_AUTHOR=$(jq -r ".data.repository.issue.author | $NORM_LOGIN" <<<"$JSON")
ISSUE_BODY=$(jq -r '.data.repository.issue.body // ""' <<<"$JSON" | tr -d '\r')
UP_ISSUE=""
if grep -qF 'Upstream issue:' <<<"$ISSUE_BODY"; then
  UP_HEADER=$(head -n1 <<<"$ISSUE_BODY")
  if ! grep -qE "^Upstream issue: https://github\.com/$UPSTREAM/issues/[0-9]+[[:space:]]*$" <<<"$UP_HEADER"; then
    echo "note: issue #$N's 'Upstream issue:' line ignored — it is not the body's first line (/import's header); no upstream Fixes ref will be added" >&2
  elif ! grep -qE '^---[[:space:]]*$' <<<"$ISSUE_BODY"; then
    # (the header line itself can never match; no `tail | grep -q` here — with
    # pipefail, grep closing early makes tail die of SIGPIPE on a long body and
    # the whole pipeline read as "no rule")
    echo "note: issue #$N's 'Upstream issue:' header ignored — no \`---\` rule follows it (not /import's body); no upstream Fixes ref will be added" >&2
  elif ! trusted_login "$ISSUE_AUTHOR"; then
    echo "note: issue #$N's 'Upstream issue:' header ignored — issue author '${ISSUE_AUTHOR:-unknown}' is not in TRUSTED_LOGINS ($TRUSTED_LOGINS) and has no write access on $FORK; no upstream Fixes ref will be added" >&2
  else
    UP_ISSUE=$(grep -oE '[0-9]+' <<<"$UP_HEADER" | tail -1)
  fi
fi

# ---- resolve the fork PR: trust rule first, then the pick. Every fork-base
# chip is judged by check_pr before its state or text is looked at. OPEN_OK /
# CLOSED_OK collect qualifying chips (one compact JSON object per line);
# LISTING is the human record of every candidate and its verdict.
LISTING=""
OPEN_OK=""
CLOSED_OK=""
CHIP_NUMS=" "
while IFS= read -r chip; do
  [ -n "$chip" ] || continue
  CHIP_NUMS="$CHIP_NUMS$(jq -r .number <<<"$chip") "
  line=$(fmt_pr "$chip")
  check_pr "$chip"
  if [ -n "$REASON" ]; then
    LISTING="$LISTING$line — REFUSED: $REASON"$'\n'
  elif [ "$(jq -r .state <<<"$chip")" = "OPEN" ]; then
    OPEN_OK="$OPEN_OK$chip"$'\n'
    LISTING="$LISTING$line — qualifies"$'\n'
  else
    CLOSED_OK="$CLOSED_OK$chip"$'\n'
    LISTING="$LISTING$line — qualifies (closed: heal path)"$'\n'
  fi
done <<<"$(jq -c --arg repo "$FORK" '.data.repository.issue.closedByPullRequestsReferences.nodes[]
  | select(.repository.nameWithOwner==$repo)' <<<"$JSON")"

# Closing reference to THIS issue (the keyword set the fork's own PR bodies
# use; the body transform below qualifies every bare ref regardless) or the
# agent branch convention. Read only after the rule passed.
REF_TEST='((.body // "") | test("(Fixes|Closes|Resolves)\\s+(meridianlabs-ai/inspect_ai)?#" + $n + "\\b"))
  or (.headRefName | test("^(claude/)?issue-" + $n + "-"))'
ambiguous() {  # $1 = what, $2 = the qualifying candidates (one per line)
  echo "AMBIGUOUS: more than one $1 qualifies for issue #$N — re-run with --pr <number>:" >&2
  while IFS= read -r c; do [ -n "$c" ] && fmt_pr "$c" >&2; done <<<"$2"
  exit 6
}
PICK=""
HOW=""
if [ -n "$PIN" ]; then
  # Pinned: a chip of any state, else the fork PR itself. The rule still
  # applies — pinning names the candidate, it does not vouch for it.
  PICK=$(jq -c --arg repo "$FORK" --argjson pin "$PIN" '[.data.repository.issue.closedByPullRequestsReferences.nodes[]
    | select(.repository.nameWithOwner==$repo and .number==$pin)][0] // empty' <<<"$JSON")
  [ -n "$PICK" ] || PICK=$(gh pr view "$PIN" --repo "$FORK" \
      --json number,state,isDraft,title,body,headRefName,headRefOid,author,headRepository,headRepositoryOwner 2>/dev/null \
    | jq -c --arg fork "$FORK" "$NORM" || true)
  [ -n "$PICK" ] || { echo "--pr $PIN is not a PR on $FORK" >&2; exit 3; }
  check_pr "$PICK"
  if [ -n "$REASON" ]; then
    echo "REFUSED --pr $PIN: $REASON" >&2
    fmt_pr "$PICK" >&2
    exit 3
  fi
  HOW="--pr $PIN"
  case "$CHIP_NUMS" in
    *" $PIN "*) ;;
    *) jq -e --arg n "$N" "$REF_TEST" <<<"$PICK" >/dev/null ||
         echo "WARN: --pr $PIN is not linked to issue #$N (no chip, no Fixes/Closes/Resolves #$N, head branch is not issue-$N-*); the upstream PR will still say Fixes meridianlabs-ai/inspect_ai#$N" >&2 ;;
  esac
else
  case "$(jq -sc length <<<"$OPEN_OK")" in
    1) PICK=$(jq -sc '.[0]' <<<"$OPEN_OK"); HOW="open fork-PR chip" ;;
    0) ;;
    *) ambiguous "open fork-PR chip" "$OPEN_OK" ;;
  esac
fi
if [ -z "$PICK" ]; then
  # No single open chip: fork closing refs are inert (non-default base), so
  # the agent's own PR usually has no chip at all. Fall back to the open
  # fork PRs — rule first, then the closing ref / branch convention.
  LOOKED="open fork PRs (gh pr list --repo $FORK --state open) passing the trust rule and carrying Fixes|Closes|Resolves #$N (or meridianlabs-ai/inspect_ai#$N) or a head branch claude/issue-$N-* / issue-$N-*"
  # The listing must be known-good and complete before it decides anything:
  # a failed lookup read as "no open PRs" would fall through to the closed
  # chip and re-promote an old branch; a full page could hide a second match.
  if ! OPEN_PRS=$(gh pr list --repo "$FORK" --state open --limit "$LIST_LIMIT" \
        --json number,state,isDraft,title,body,headRefName,headRefOid,author,headRepository,headRepositoryOwner \
      | jq -c --arg fork "$FORK" ".[] | $NORM"); then
    echo "ABORT: could not list the open fork PRs (gh pr list --repo $FORK --state open failed) — the fallback cannot decide; re-run, or pin with --pr <number>" >&2
    exit 5
  fi
  if [ "$(jq -sc length <<<"$OPEN_PRS")" -ge "$LIST_LIMIT" ]; then
    echo "ABORT: $FORK has $LIST_LIMIT or more open PRs — the listing is truncated, so a unique match cannot be established; pin with --pr <number>" >&2
    exit 5
  fi
  FB_OK=""
  while IFS= read -r pr; do
    [ -n "$pr" ] || continue
    case "$CHIP_NUMS" in *" $(jq -r .number <<<"$pr") "*) continue ;; esac  # judged above
    line=$(fmt_pr "$pr")
    check_pr "$pr"
    if [ -n "$REASON" ]; then
      LISTING="$LISTING$line — REFUSED: $REASON"$'\n'
    elif jq -e --arg n "$N" "$REF_TEST" <<<"$pr" >/dev/null; then
      FB_OK="$FB_OK$pr"$'\n'
      LISTING="$LISTING$line — qualifies (no chip; closing ref or branch names issue #$N)"$'\n'
    else
      LISTING="$LISTING$line — no reference to issue #$N"$'\n'
    fi
  done <<<"$OPEN_PRS"
  case "$(jq -sc length <<<"$FB_OK")" in
    1) PICK=$(jq -sc '.[0]' <<<"$FB_OK"); HOW="open fork PR matched by closing ref or branch convention (no chip)" ;;
    0) ;;
    *) ambiguous "open fork PR matched by closing ref or branch convention" "$FB_OK" ;;
  esac
fi
if [ -z "$PICK" ]; then
  # Heal path: a promoted issue's fork PR is already superseded and closed.
  case "$(jq -sc length <<<"$CLOSED_OK")" in
    1) PICK=$(jq -sc '.[0]' <<<"$CLOSED_OK"); HOW="closed fork-PR chip (heal path)" ;;
    0) ;;
    *) ambiguous "closed fork-PR chip (heal path)" "$CLOSED_OK" ;;
  esac
fi
if [ -z "$PICK" ]; then
  echo "NO QUALIFYING FORK PR for issue #$N — resolve via the slow path, then re-run with --pr <number>." >&2
  echo "Looked for: (a) open fork-PR chips passing the trust rule; (b) $LOOKED; (c) closed fork-PR chips (heal path)." >&2
  echo "Candidates seen (TRUSTED_LOGINS=$TRUSTED_LOGINS):" >&2
  [ -n "$LISTING" ] || LISTING="  (none)"$'\n'
  printf '%s' "$LISTING" >&2
  exit 3
fi
FPR_STATE=$(jq -r .state <<<"$PICK")
FPR=$(jq -r .number <<<"$PICK")
BRANCH=$(jq -r .headRefName <<<"$PICK")
FPR_TITLE=$(jq -r .title <<<"$PICK")
FPR_BODY=$(jq -r .body <<<"$PICK")
echo "RESOLVED: fork PR #$FPR ($FPR_STATE) via $HOW — branch=$BRANCH author=$(jq -r '.author.login // "?"' <<<"$PICK") head=$(jq -r '.headRepository.nameWithOwner // "?"' <<<"$PICK"); TRUSTED_LOGINS=$TRUSTED_LOGINS"
if [ "$DRY" = "--dry-run" ]; then
  printf '%s' "$LISTING"
else
  # A refused candidate sat next to the one we took — say so.
  grep -F -- '— REFUSED' <<<"$LISTING" | sed 's/^ */note: refused /' >&2 || true
fi

ITEM=$(jq -r '[.data.repository.issue.projectItems.nodes[] | select(.project.number==1)][0].id // empty' <<<"$JSON")
CUR_STAGE=$(jq -r '[.data.repository.issue.projectItems.nodes[] | select(.project.number==1)][0].stage.name // empty' <<<"$JSON")
CUR_UP=$(jq -r '[.data.repository.issue.projectItems.nodes[] | select(.project.number==1)][0].up.text // empty' <<<"$JSON")

# ---- preflight (branch pushed + reviewer valid; review verdict + CI are advisory)
# Protected branches are never promotion heads: the create path merges
# upstream main INTO $BRANCH (SKILL.md → Cautions: never push to main/meridian).
case "$BRANCH" in
  main|meridian)
    echo "REFUSED: fork PR #$FPR's head is the protected branch $BRANCH — never a promotion branch" >&2
    exit 5 ;;
esac
BRANCH_SHA=$(gh api "repos/$FORK/branches/$BRANCH" --jq .commit.sha 2>/dev/null) ||
  { echo "branch $BRANCH not on the fork" >&2; exit 4; }
# Adoption detection uses the issue's own cross-repo chip (same branch,
# upstream repo) — the REST `pulls?head=org:br` filter silently returns []
# for this org-fork pair (verified against a known merged PR), so it cannot
# be trusted. Looked up here because the head check below depends on it.
UP_PICK=$(jq -c --arg up "$UPSTREAM" --arg br "$BRANCH" \
  '[.data.repository.issue.closedByPullRequestsReferences.nodes[]
    | select(.repository.nameWithOwner==$up) | select(.headRefName==$br)][0] // empty' <<<"$JSON")
# The fork branch must be at the resolved PR's head: the create path writes
# a merge commit onto that branch and promotes it, so a branch that moved
# since the PR was read (or stale PR data) is refused. Only the adopt path
# of a CLOSED PR tolerates a moved branch — nothing touches the branch
# there, and a promoted branch legitimately moves on (upstream reviewers
# push fixes to it after the fork PR was superseded).
FPR_HEAD=$(jq -r '.headRefOid // ""' <<<"$PICK")
if [ "$FPR_HEAD" != "$BRANCH_SHA" ]; then
  if [ "$FPR_STATE" = "OPEN" ] || [ -z "$UP_PICK" ]; then
    echo "ABORT: fork PR #$FPR's head is ${FPR_HEAD:-unknown} but $FORK:$BRANCH is at $BRANCH_SHA — the branch moved since the PR was read; re-run" >&2
    exit 5
  fi
  echo "note: $BRANCH is at $BRANCH_SHA, past closed fork PR #$FPR's head ${FPR_HEAD:-unknown} — adopting the existing upstream PR, nothing is written to the branch" >&2
fi
# A typo'd or non-collaborator REVIEWER would 422 the review request AFTER the
# upstream PR exists (set -e then exits mid-bookkeeping); fail before any write.
# The collaborator lookup is permission-gated (push access on the repo; a
# lesser token gets 403, not 404), so only a 404 proves the login wrong —
# anything else is "could not verify" and must not break the default path.
check_reviewer() {  # $1 = owner/repo; 204 ok, 404 exit 5, other → warn
  local rc
  rc=$(gh api -i "repos/$1/collaborators/$REVIEWER" 2>/dev/null | head -1 | awk '{print $2}' || true)
  case "$rc" in
    204) ;;
    404) echo "REVIEWER=$REVIEWER is not a collaborator on $1" >&2; exit 5 ;;
    *)   echo "WARN: could not verify REVIEWER=$REVIEWER on $1 (HTTP ${rc:-none}); continuing" >&2 ;;
  esac
}
# Only an OVERRIDE needs validating upstream: the default was never checked
# before and is known-good, and a promoting token without push access there
# would otherwise print the WARN line on every default run.
[ "$REVIEWER" = "$DEFAULT_REVIEWER" ] || check_reviewer "$UPSTREAM"
# The ts-mono companion (same branch name, open) gets the SAME reviewer, so an
# override valid upstream but unknown on ts-mono would 422 there instead —
# look the companion up here and check it too, before any write. Unconditional
# (default included): the promoting token has push on ts-mono, so this never
# warns spuriously and catches a dropped collaborator for free.
COMPANION=$(gh pr list --repo "$TSMONO" --head "$BRANCH" --state open --json number --jq '.[0].number // empty' 2>/dev/null || true)
[ -n "$COMPANION" ] && check_reviewer "$TSMONO"
# Idempotency probe for the assign/request steps: $1 = key (a|r) in the
# state JSON $2. Case-insensitive on both sides (see REVIEWER above).
has_login() {
  jq -e --arg u "$REVIEWER" --arg k "$1" '(.[$k] // []) | map(ascii_downcase) | index($u)' <<<"$2" >/dev/null 2>&1
}
# --paginate: busy @auto issues/PRs exceed 100 comments, and the API returns
# oldest-first — a single page never sees recent comments. Only a verdict
# posted by the reviewer app or a trusted login counts (the PR is public:
# anyone can post a comment carrying the marker); the latest such verdict
# wins, and ignored ones are counted on the ADVISORY line. The lookup must
# complete: a page that fails after earlier pages returned rows would leave
# an OLDER verdict standing, so partial rows are discarded and the verdict
# is reported unavailable (not clean — the SKILL.md pause applies).
VERDICT="verdict:none"
VERDICT_IGNORED=0
if VERDICT_ROWS=$(gh api --paginate "repos/$FORK/issues/$FPR/comments?per_page=100" \
    --jq '.[] | select(.body | contains("claude-review-verdict")) | [.user.login, (.body | gsub("[\\t\\r\\n]"; " "))] | @tsv' 2>/dev/null); then
  while IFS=$'\t' read -r v_login v_body; do
    [ -n "$v_login" ] || continue
    if [ "$v_login" = "$REVIEWER_BOT" ] || trusted_login "$v_login"; then
      VERDICT=$(grep -o 'verdict:[a-z]*' <<<"$v_body" | tail -1 || true)
      VERDICT=${VERDICT:-verdict:none}
    else
      VERDICT_IGNORED=$((VERDICT_IGNORED + 1))
    fi
  done <<<"$VERDICT_ROWS"
  [ "$VERDICT_IGNORED" -eq 0 ] || VERDICT="$VERDICT ($VERDICT_IGNORED verdict comment(s) by untrusted authors ignored)"
else
  echo "WARN: could not read fork PR #$FPR's comments (gh api failed) — review verdict unavailable; treat as not clean" >&2
  VERDICT="verdict:unavailable (comment lookup failed)"
fi
CI=$(gh pr checks "$FPR" -R "$FORK" 2>&1 | awk -F'\t' '{print $2}' | sort | uniq -c | tr '\n' ' ' || true)
echo "ADVISORY: fork PR #$FPR review $VERDICT; CI: ${CI:-unknown}; reviewer: $REVIEWER; upstream issue: ${UP_ISSUE:+#}${UP_ISSUE:-none}"

# ---- upstream PR: adopt (UP_PICK, looked up in preflight) or create.
M=$(jq -r '.number // empty' <<<"$UP_PICK")
if [ -n "$M" ]; then
  UP_URL="https://github.com/$UPSTREAM/pull/$M"
  echo "ADOPTED existing upstream PR #$M ($(jq -r .state <<<"$UP_PICK"))"
else
  # The fork PR body was written for the fork's tracker; republished on a PR
  # based on upstream main, every bare `#M` rebinds to upstream issue M and a
  # closing keyword before it would close that issue on merge. Three steps:
  #  1. Best effort: qualify the common spelling — a `#M` at the start of a
  #     line or after whitespace — to the fork, as /import does in the other
  #     direction, and make sure a closing ref to THIS issue is present
  #     (prepend one otherwise). A validated import header's bare
  #     `Fixes #<up>` is always prepended: a `Fixes …#<up>` already in the
  #     body may be quoted (code, a comment, a link title) and close nothing.
  #  2. The guarantee: render the result with GitHub's own Markdown renderer
  #     in upstream's context and refuse (exit 5, before any write) if it
  #     resolves any reference to an upstream issue or PR other than <up> —
  #     `(#M)`, `GH-M`, a qualified `UKGovernmentBEIS/inspect_ai#M`, an issue
  #     URL. Deciding which `#M` GitHub treats as a reference is GitHub's
  #     parser's job; re-implementing it did not converge (PR #127, rounds
  #     1-4), so anything step 1 misses is refused for the operator to fix in
  #     the fork PR body rather than published.
  #  3. Step 1 must not change anything else: a whitespace-preceded `#M` can
  #     be code (`echo #1`) or a link destination (`[r]( #1-x )`). In the
  #     fork's context a qualified fork ref renders exactly as the bare one,
  #     so the fork PR body and its qualified text render identically there
  #     unless the rewrite touched something that is not a reference to an
  #     existing fork issue; then promote refuses rather than publish the
  #     altered body. The renderer mints fresh identifiers on every call —
  #     math's `data-run-id`, diagrams' (mermaid, geojson, topojson, stl)
  #     `data-identity`, and footnotes' `user-content-fn…-<hex>` suffix — so
  #     exactly those values are blanked before comparing; all text and every
  #     other attribute still has to match.
  QUAL=$(FPR_BODY="$FPR_BODY" python3 -c '
import os, re
print(re.sub(r"(?<!\S)#(\d+)\b", r"meridianlabs-ai/inspect_ai#\1", os.environ["FPR_BODY"]))')
  BODY=$(ISSUE_N="$N" UP_ISSUE="$UP_ISSUE" QUAL="$QUAL" python3 -c '
import os, re
n, up, body = os.environ["ISSUE_N"], os.environ["UP_ISSUE"], os.environ["QUAL"]
kw = r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+"
if not re.search(kw + r"meridianlabs-ai/inspect_ai#%s\b" % n, body, re.I):
    body = "Fixes meridianlabs-ai/inspect_ai#%s\n\n" % n + body
if up:
    body = "Fixes #%s\n" % up + body
print(body)')
  # The operator sees the body as it will be published under their name —
  # every closing reference included — before (dry-run) or as it is created.
  echo "upstream PR body (as published):"
  sed 's/^/  | /' <<<"$BODY"
  render() {  # $1 = markdown, $2 = the repository its references resolve in
    jq -n --arg t "$1" --arg c "$2" '{text: $t, mode: "gfm", context: $c}' \
      | gh api markdown --input - 2>/dev/null
  }
  stable() {  # blank the renderer's per-call identifiers (see step 3)
    sed -E -e 's/ data-run-id="[0-9a-f]+"/ data-run-id=""/g' \
      -e 's/ data-identity="[0-9a-f-]+"/ data-identity=""/g' \
      -e 's/(="#?user-content-fn(ref)?-[^"]*)-[0-9a-f]{32}"/\1-"/g'
  }
  render_failed() {
    echo "ABORT: could not render the upstream PR body with GitHub's Markdown API (gh api markdown failed) — its references cannot be checked; re-run. Nothing was written." >&2
    exit 5
  }
  RENDERED=$(render "$BODY" "$UPSTREAM") || render_failed
  STRAY=$(grep -oiE "(data-url|href)=\"https://github\.com/$UPSTREAM/(issues|pull)/[0-9]+" <<<"$RENDERED" \
    | grep -oE '[0-9]+$' | sort -un | grep -vxF "${UP_ISSUE:-none}" | sed 's/^/#/' | tr '\n' ' ' || true)
  if [ -n "$STRAY" ]; then
    echo "ABORT: the upstream PR body references $UPSTREAM issue(s)/PR(s) ${STRAY% } (GitHub resolves them there; a closing keyword before one would close it on merge)." >&2
    echo "Qualify each as meridianlabs-ai/inspect_ai#M in fork PR #$FPR's body, or drop the upstream reference, and re-run — nothing was written." >&2
    exit 5
  fi
  if [ "$QUAL" != "$FPR_BODY" ]; then
    R_ORIG=$(render "$FPR_BODY" "$FORK" | stable) || render_failed
    R_QUAL=$(render "$QUAL" "$FORK" | stable) || render_failed
    if [ "$R_ORIG" != "$R_QUAL" ]; then
      echo "ABORT: qualifying bare #M refs would change text in fork PR #$FPR's body that GitHub does not read as a reference to an existing $FORK issue (code, a link destination, a number with no issue behind it). The rendered lines that change:" >&2
      diff <(echo "$R_ORIG") <(echo "$R_QUAL") | grep '^[<>]' | head -20 >&2 || true
      echo "Reword each so no #M follows whitespace there (e.g. quote it, or qualify a real ref by hand) in fork PR #$FPR's body, and re-run — nothing was written." >&2
      exit 5
    fi
  fi
  # Sync the branch with upstream main before opening the PR. Org-fork PR
  # heads take no maintainer edits, and these branches are cut from the fork's
  # main mirror (which trails upstream), so a fresh promotion usually opens a
  # few commits behind — unmergeable until updated, and CHANGELOG entries in
  # particular almost always conflict (fork AGENTS.md → "Opening an upstream
  # PR from an org fork" calls for exactly this pre-open sync). Merge
  # server-side via the fork-network merges API: upstream commits are
  # reachable from the fork by SHA, so no local checkout is touched and only
  # the PR branch is written — never main. A conflict aborts BEFORE any
  # upstream PR is opened, surfacing it (usually CHANGELOG) for a human.
  UP_SHA=$(gh api "repos/$UPSTREAM/commits/main" --jq .sha)
  if [ "$DRY" = "--dry-run" ]; then
    echo "DRY-RUN: gh api repos/$FORK/merges -X POST -f base=$BRANCH -f head=$UP_SHA (merge upstream main into the branch)"
  elif MERGE_OUT=$(gh api "repos/$FORK/merges" -X POST \
        -f base="$BRANCH" -f head="$UP_SHA" \
        -f commit_message="Merge upstream $UPSTREAM main into $BRANCH before promotion" \
        2>/dev/null); then
    # exit 0 → 201 (merge-commit JSON on stdout) or 204 (empty: already current)
    if [ -n "$MERGE_OUT" ]; then
      echo "branch: merged upstream main into $BRANCH (branch was behind; CI will re-run)"
    else
      echo "branch: already up to date with upstream main"
    fi
  else
    # non-zero → gh prints the response body to stdout, the HTTP status to stderr
    if grep -qi "merge conflict" <<<"$MERGE_OUT"; then
      echo "ABORT: merging upstream main into $BRANCH conflicts (commonly CHANGELOG.md)." >&2
      echo "Resolve on the branch and re-run promote — no upstream PR was opened." >&2
    else
      echo "ABORT: could not merge upstream main into $BRANCH:" >&2
      echo "${MERGE_OUT:-(no response body)}" >&2
    fi
    exit 5
  fi
  # Org forks cannot be resolved as `owner:branch` heads — PR creation needs
  # an explicit head_repo, which `gh pr create` lacks (cli/cli#6462; see the
  # fork's AGENTS.md → "Opening an upstream PR from an org fork").
  if [ "$DRY" = "--dry-run" ]; then
    echo "DRY-RUN: gh api repos/$UPSTREAM/pulls -X POST -f base=main -f head=$BRANCH -f head_repo=$FORK -f title=\"$FPR_TITLE\" -f body=<the body printed above>"
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
    has_login a "$UP_STATE" ||
      write gh api "repos/$UPSTREAM/issues/$M/assignees" -X POST -f "assignees[]=$REVIEWER" --silent
    has_login r "$UP_STATE" ||
      write gh api "repos/$UPSTREAM/pulls/$M/requested_reviewers" -X POST -f "reviewers[]=$REVIEWER" --silent
  fi
fi

# ---- ts-mono companion: the viewer half needs the SAME human reviewer.
# Companions share the branch name (dev-agent convention; the sync and the
# chip sweep key on it). ts-mono has no promotion step — its PR merges in
# place — so sign-off review is requested here, at promotion time.
# (COMPANION was looked up in preflight, where the reviewer was checked on it.)
if [ -n "$COMPANION" ]; then
  C_STATE=$(gh api "repos/$TSMONO/pulls/$COMPANION" --jq '{a: [.assignees[].login], r: [.requested_reviewers[].login]}' 2>/dev/null || echo '{}')
  has_login a "$C_STATE" ||
    write gh api "repos/$TSMONO/issues/$COMPANION/assignees" -X POST -f "assignees[]=$REVIEWER" --silent
  has_login r "$C_STATE" ||
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
