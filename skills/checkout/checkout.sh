#!/usr/bin/env bash
# Fast path of /checkout (see SKILL.md): chip lookup -> VS Code PR-extension
# config -> checkout, in one invocation and one network call.
#
# Chip selection: an OPEN same-repo chip wins; otherwise a single OPEN
# cross-repo chip (an External proxy's upstream PR, or a promotion) is
# checked out against ITS repo. A promotion (our own fork branch, trusted
# author) goes through gh pr checkout like a same-repo chip. An External
# pick is an OUTSIDER'S tree and never enters this clone's working tree:
# its head is fetched by PR number, refused unless it is exactly the
# headRefOid the API reported, and that literal commit is checked out
# detached — hooks and submodule recursion off — in a worktree OUTSIDE the
# clone ($CHECKOUT_WORKTREES, default ~/.local/state/checkout/<owner>--<repo>/
# pr-<M>). Nothing of it is loadable from the session's project directory
# (.claude/, .mcp.json, CLAUDE.md, AGENTS.md), no local branch is created or
# moved (the contributor names the head: `meridian` or an existing branch
# would otherwise be fast-forwarded by gh pr checkout), no submodule is
# initialised from its .gitmodules and no ts-mono companion switch runs
# (findings 4629158, 4629155).
#
# Trust rule (applied BEFORE any PR text — title, body, branch name — is
# consulted): a linked PR qualifies only when its head repository is this
# org repo AND its author is in TRUSTED_LOGINS or has write access here.
# Anyone can link a PR from a personal fork to an issue (GitHub creates the
# chip natively for `Fixes #N` on a default-branch PR), so the chip alone
# proves nothing; a branch inside the org repo can only be pushed by a
# write-access account, which makes the head-repository check the
# load-bearing one and the author check defence in depth. A cross-repo chip
# qualifies as a promotion (its head is this repo) or as a genuine External
# proxy's upstream PR (the issue was written by a trusted login and carries
# the `External` label). The body's `Upstream PR:` line is honoured only for
# such a proxy and only under UPSTREAM.
#
# Usage: checkout.sh <issue-number> [--dry-run]
#   --dry-run  print the candidates, each verdict and the decision; exit
#              before any write (no branch config, no fetch, no checkout).
# Exit codes: 0 ok; 2 dirty tree (never switch over uncommitted work — the
# External worktree's tree included); 3 no qualifying open chip (stderr
# lists every candidate and why it was refused; fall back to the slow
# path); 4 repo unresolvable; 5 External head unusable (the upstream PR's
# head moved between the API read and the fetch, or no headRefOid) —
# nothing was checked out, rerun.
set -euo pipefail

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
# The only repo an External proxy's upstream PR may live in.
UPSTREAM=UKGovernmentBEIS/inspect_ai

N=""
DRY=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    -*) echo "usage: checkout.sh <issue-number> [--dry-run]" >&2; exit 1 ;;
    *) N=$arg ;;
  esac
done
[ -n "$N" ] || { echo "usage: checkout.sh <issue-number> [--dry-run]" >&2; exit 1; }

if [ -z "$DRY" ] && [ -n "$(git status --porcelain)" ]; then
  echo "DIRTY TREE — not switching:" >&2
  git status --short >&2
  exit 2
fi

# The issue lives in the meridianlabs-ai-owned repo — NOT necessarily origin
# (fork clones point origin at upstream, whose issue numbers are unrelated).
REPO=$(git remote -v | grep -om1 'meridianlabs-ai/[A-Za-z0-9._-]*' | head -1 | sed 's/\.git$//' || true)
[ -n "$REPO" ] || REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner) ||
  { echo "no meridianlabs-ai remote and no gh default repo" >&2; exit 4; }

# trusted_login <login>: 0 when <login> is in TRUSTED_LOGINS or has write
# access on $REPO (admin/maintain/write from the collaborator permission
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
  perm=$(gh api "repos/$REPO/collaborators/$login/permission" --jq .permission 2>/dev/null || true)
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
  if [ "$head" != "$REPO" ]; then
    REASON="head repository is '${head:-unknown}', not $REPO"
  elif ! trusted_login "$login"; then
    REASON="author '${login:-unknown}' is not in TRUSTED_LOGINS ($TRUSTED_LOGINS) and has no write access on $REPO"
  fi
}

# phys_dir <dir>: sets PHYS to the physical path of the existing directory
# <dir> (symlinks resolved). Set, not printed: `$(…)` strips every trailing
# newline from a command's output, and a path may end in one — a registered
# worktree named `line<newline>` captured through `$(pwd -P)` became `line`
# and dropped out of the containment set (review round 3 of #130). The
# sentinel keeps the capture lossless.
phys_dir() {
  PHYS=$(cd -P -- "$1" 2>/dev/null && printf '%sx' "$PWD") || return 1
  PHYS=${PHYS%x}
}
# physpath <path>: sets PHYS to the physical form of <path> — made absolute
# from $PWD, every existing component resolved through symlinks (the last
# one included), the not-yet-existing tail appended verbatim. Containment
# checks compare physical paths only: a lexical prefix test is defeated by
# a relative root, a `..` or a symlinked ancestor (review of #130). The
# caller has refused a newline in <path>, so basename/dirname on the tail
# are lossless.
physpath() {
  local p=$1 tail=""
  case "$p" in /*) ;; *) p="$PWD/$p" ;; esac
  while [ ! -d "$p" ]; do
    tail="/$(basename "$p")$tail"
    p=$(dirname "$p")
  done
  phys_dir "$p" || return 1
  PHYS="$PHYS$tail"
}

# One GraphQL round trip: title, issue author + labels (the External-proxy
# test), body (the `Upstream PR:` fallback) and the linked-PR chips WITH
# head/base refs, author and head repository, so no separate `gh pr view`
# or `gh issue view` is needed.
JSON=$(gh api graphql \
  -f query='query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){issue(number:$n){title body author{login __typename} labels(first:20){nodes{name}} closedByPullRequestsReferences(first:10,includeClosedPrs:true){nodes{number state headRefName headRefOid baseRefName author{login __typename} repository{nameWithOwner} headRepository{nameWithOwner}}}}}}' \
  -F o="${REPO%%/*}" -F r="${REPO##*/}" -F n="$N")

TITLE=$(jq -r '.data.repository.issue.title' <<<"$JSON")
ISSUE_AUTHOR=$(jq -r ".data.repository.issue.author | $NORM_LOGIN" <<<"$JSON")
ISSUE_LABELS=$(jq -r '[.data.repository.issue.labels.nodes[].name] | join(",")' <<<"$JSON")
# A genuine External proxy: written by a trusted login (the sync, as marvin)
# AND labelled External. Membership in TRUSTED_LOGINS only — write access
# is not enough here, a collaborator's own issue is not a proxy.
PROXY=""
case ",$(tr 'A-Z' 'a-z' <<<"$TRUSTED_LOGINS")," in
  *",$(tr 'A-Z' 'a-z' <<<"$ISSUE_AUTHOR"),"*)
    case ",$ISSUE_LABELS," in *",External,"*) PROXY=1 ;; esac ;;
esac

# Classify every chip. SAME_OK / CROSS_OK collect qualifying OPEN chips
# (one compact JSON object per line); LISTING is the human record of every
# candidate and its verdict, printed on refusal and in --dry-run.
SAME_OK=""
CROSS_OK=""
LISTING=""
while IFS= read -r chip; do
  [ -n "$chip" ] || continue
  line=$(jq -r '"  #\(.number) \(.state) \(.repository.nameWithOwner) head=\(.headRepository.nameWithOwner // "?"):\(.headRefName) author=\(.author.login // "?")"' <<<"$chip")
  if [ "$(jq -r .state <<<"$chip")" != "OPEN" ]; then
    LISTING="$LISTING$line — not open"$'\n'
    continue
  fi
  check_pr "$chip"
  if [ "$(jq -r .repository.nameWithOwner <<<"$chip")" = "$REPO" ]; then
    if [ -z "$REASON" ]; then
      SAME_OK="$SAME_OK$chip"$'\n'
      LISTING="$LISTING$line — qualifies"$'\n'
    else
      LISTING="$LISTING$line — REFUSED: $REASON"$'\n'
    fi
  elif [ -z "$REASON" ]; then
    CROSS_OK="$CROSS_OK$chip"$'\n'
    LISTING="$LISTING$line — qualifies (promotion: head is $REPO)"$'\n'
  elif [ -n "$PROXY" ] && [ "$(jq -r .repository.nameWithOwner <<<"$chip")" = "$UPSTREAM" ]; then
    # Tagged: the pick is an outsider's tree (it failed check_pr) and takes
    # the isolated External path below, never gh pr checkout.
    CROSS_OK="$CROSS_OK$(jq -c '. + {external:true}' <<<"$chip")"$'\n'
    LISTING="$LISTING$line — qualifies (External proxy: issue by $ISSUE_AUTHOR, label External)"$'\n'
  else
    LISTING="$LISTING$line — REFUSED: $REASON; issue is not an External proxy (author=${ISSUE_AUTHOR:-?} labels=${ISSUE_LABELS:-none})"$'\n'
  fi
done <<<"$(jq -c '.data.repository.issue.closedByPullRequestsReferences.nodes[]' <<<"$JSON")"

# max_by(.number), not [0]: with two OPEN same-repo chips (an old
# generation not yet closed plus its replacement) the API's list order is
# not defined as newest — highest PR number is.
PICK=$(jq -sc 'max_by(.number) // empty' <<<"$SAME_OK")
CROSS=""
HOW="open same-repo chip"
if [ -z "$PICK" ]; then
  # No same-repo chip: accept a SINGLE qualifying open cross-repo chip
  # (ambiguity goes to the slow path's judgment).
  if [ "$(jq -sc length <<<"$CROSS_OK")" = "1" ]; then
    PICK=$(jq -sc '.[0]' <<<"$CROSS_OK")
    CROSS=1
    HOW="open cross-repo chip"
  fi
fi
if [ -z "$PICK" ]; then
  # External-proxy fallback: the sync writes a canonical "Upstream PR:" line
  # into every proxy body, and chips only exist after the (browser) sweep
  # runs — so resolve the upstream PR from the body instead of requiring a
  # chip. Honoured ONLY for a genuine proxy (trusted author + External
  # label) and ONLY for a URL under $UPSTREAM: the line is free text on any
  # other issue. Same shape as a cross-repo chip pick.
  UP_LINE=$(jq -r '.data.repository.issue.body // ""' <<<"$JSON" \
    | grep -ioE 'Upstream PR:[[:space:]]*https://github\.com/[^[:space:]]+/pull/[0-9]+' | head -1 || true)
  if [ -n "$UP_LINE" ]; then
    UP_URL=$(grep -oE 'https://[^[:space:]]+' <<<"$UP_LINE")
    if [ -z "$PROXY" ]; then
      LISTING="$LISTING  body line '$UP_LINE' — REFUSED: issue is not an External proxy (author=${ISSUE_AUTHOR:-?} labels=${ISSUE_LABELS:-none})"$'\n'
    elif ! grep -qE "^https://github\.com/$UPSTREAM/pull/[0-9]+$" <<<"$UP_URL"; then
      LISTING="$LISTING  body line '$UP_LINE' — REFUSED: not under $UPSTREAM"$'\n'
    else
      UP_NUM=$(grep -oE '[0-9]+$' <<<"$UP_URL")
      # Always External: a proxy's Upstream PR is the contributor's PR, and
      # this read carries no author/head repository to prove otherwise.
      PICK=$(gh pr view "$UP_NUM" --repo "$UPSTREAM" \
          --json number,state,headRefName,headRefOid,baseRefName 2>/dev/null \
        | jq -c --arg r "$UPSTREAM" 'select(.state=="OPEN")
          | {number, state, headRefName, headRefOid, baseRefName, repository:{nameWithOwner:$r}, external:true}' || true)
      if [ -n "$PICK" ]; then
        CROSS=1
        HOW="proxy body's Upstream PR line"
        LISTING="$LISTING  body line '$UP_LINE' — qualifies (External proxy: issue by $ISSUE_AUTHOR, label External)"$'\n'
        echo "note: no chip — resolved via the proxy body's Upstream PR line" >&2
      else
        LISTING="$LISTING  body line '$UP_LINE' — upstream PR is not open (or not readable)"$'\n'
      fi
    fi
  fi
fi
if [ -z "$PICK" ]; then
  echo "NO QUALIFYING OPEN CHIP for issue #$N — use the slow path. Candidates seen (TRUSTED_LOGINS=$TRUSTED_LOGINS):" >&2
  printf '%s' "${LISTING:-  (none)
}" >&2
  exit 3
fi
M=$(jq -r .number <<<"$PICK")
BRANCH=$(jq -r .headRefName <<<"$PICK")
BASE_REF=$(jq -r .baseRefName <<<"$PICK")
PR_REPO=$(jq -r .repository.nameWithOwner <<<"$PICK")
EXTERNAL=$(jq -r '.external // empty' <<<"$PICK")

# Merge base = the PR's base branch on the BASE repo's remote (origin is
# upstream in fork clones — wrong diff), and baseRefName, not the default
# branch (fork PRs base on meridian).
PR_REMOTE=$(git remote -v | grep -im1 "github.com[:/]${PR_REPO}" | cut -f1 || true)
BASE_REMOTE=${PR_REMOTE:-origin}

if [ -n "$EXTERNAL" ]; then
  # The External path's inputs, resolved before anything is written so a
  # refusal (and --dry-run) can name them. SHA is the head the API reported
  # at read time; the checkout is bound to it, never to "the PR's current
  # tip". It is spliced into git commands, so it must be a full hex SHA.
  SHA=$(jq -r '.headRefOid // ""' <<<"$PICK")
  grep -qE '^[0-9a-f]{40}$' <<<"$SHA" ||
    { echo "REFUSED: $PR_REPO#$M has no usable headRefOid ('${SHA:-none}') — rerun" >&2; exit 5; }
  # The PR head is fetched from the remote that serves $PR_REPO, else its
  # https URL — never from `origin` by default, which is the fork in some
  # clone layouts and would answer refs/pull/$M/head with an unrelated PR.
  FETCH_FROM=${PR_REMOTE:-https://github.com/$PR_REPO.git}
  # Outside the clone — and outside every other worktree of it (an Orca
  # workspace is another agent session's project directory) and its git
  # dir — so nothing in the tree is under a project directory.
  # CHECKOUT_WORKTREES overrides the root. The destination is compared in
  # physical form; `.`/`..` components are refused outright (a `..` in a
  # not-yet-existing tail would only resolve once mkdir -p created it).
  WT_ROOT=${CHECKOUT_WORKTREES:-${XDG_STATE_HOME:-$HOME/.local/state}/checkout}
  WT="$WT_ROOT/${PR_REPO%%/*}--${PR_REPO##*/}/pr-$M"
  case "/$WT/" in */../*|*/./*)
    echo "REFUSED: External worktree path $WT has a . or .. component — set CHECKOUT_WORKTREES to a plain absolute path" >&2; exit 1 ;;
  esac
  if [ -L "$WT" ]; then
    echo "REFUSED: $WT is a symlink — an External worktree is a plain directory; move it aside" >&2; exit 1
  fi
  case "$WT" in *$'\n'*)
    echo "REFUSED: External worktree path contains a newline — set CHECKOUT_WORKTREES to a plain absolute path" >&2; exit 1 ;;
  esac
  physpath "$WT" || { echo "REFUSED: cannot resolve External worktree path $WT" >&2; exit 1; }
  WT=$PHYS
  # A symlinked ancestor may have brought a newline back in.
  case "$WT" in *$'\n'*)
    echo "REFUSED: External worktree path resolves to a path containing a newline — set CHECKOUT_WORKTREES elsewhere" >&2; exit 1 ;;
  esac
  # Every registered worktree root of this clone (the clone itself
  # included), physical, in an array: a path may contain any byte but NUL,
  # a newline included, so the list is read NUL-delimited (`-z`) and a
  # root that cannot be resolved is a refusal, never silently dropped
  # (review round 2 of #130). Plus the common git dir.
  # --show-cdup (a relative `../` chain, never newline-terminated) rather
  # than --show-toplevel, whose `$(…)` capture would lose a trailing
  # newline in this clone's own path; the common dir ends in `.git`.
  phys_dir "$(git rev-parse --git-common-dir)"; COMMON=$PHYS
  phys_dir "./$(git rev-parse --show-cdup)"; TOP=$PHYS
  ROOTS=()
  while IFS= read -r -d '' rec; do
    case "$rec" in "worktree "*)
      phys_dir "${rec#worktree }" ||
        { echo "REFUSED: cannot resolve registered worktree '${rec#worktree }' (run git worktree prune if it is gone)" >&2; exit 1; }
      ROOTS+=("$PHYS") ;;
    esac
  done < <(git worktree list --porcelain -z)
  case "$WT/" in "$TOP"/|"$COMMON"/|"$COMMON"/*)
    echo "REFUSED: External worktree path resolves to this clone or its git dir ($WT)" >&2; exit 1 ;;
  esac
  for root in "${ROOTS[@]}"; do
    # Equal to a registered root is the reuse case, judged below.
    [ "$root" != "$WT" ] || continue
    case "$WT/" in "$root"/*)
      echo "REFUSED: External worktree $WT would be inside $root (this clone or another worktree of it) — set CHECKOUT_WORKTREES outside them" >&2; exit 1 ;;
    esac
  done
fi

if [ -n "$DRY" ]; then
  echo "DRY-RUN: issue #$N in $REPO ($TITLE) author=${ISSUE_AUTHOR:-?} labels=${ISSUE_LABELS:-none} proxy=${PROXY:-no}; TRUSTED_LOGINS=$TRUSTED_LOGINS"
  printf '%s' "$LISTING"
  if [ -n "$EXTERNAL" ]; then
    echo "DRY-RUN: DECISION — check out $PR_REPO#$M via $HOW: UNTRUSTED external head '$BRANCH' at $SHA, detached in worktree $WT [external]"
    echo "DRY-RUN: would run: git fetch $FETCH_FROM refs/pull/$M/head (refused unless FETCH_HEAD = $SHA); git worktree add --detach --no-checkout $WT $SHA; git -C $WT checkout --detach $SHA (hooks, fsmonitor, filters and submodule recursion off). No branch, no branch config, no submodule init and no ts-mono switch in this clone."
  else
    echo "DRY-RUN: DECISION — check out $PR_REPO#$M via $HOW: branch=$BRANCH base=$BASE_REMOTE/$BASE_REF${CROSS:+ [cross-repo]}"
    echo "DRY-RUN: would run: git config branch.$BRANCH.github-pr-owner-number ${PR_REPO%%/*}#${PR_REPO##*/}#$M; git config branch.$BRANCH.vscode-merge-base $BASE_REMOTE/$BASE_REF; git fetch $BASE_REMOTE $BASE_REF; gh pr checkout $M -R $PR_REPO"
  fi
  exit 0
fi
# A refused candidate sat next to the one we took — say so, the operator
# may want to look at it.
grep -F -- '— REFUSED' <<<"$LISTING" | sed 's/^ */note: refused /' >&2 || true

if [ -n "$EXTERNAL" ]; then
  # An outsider's tree. Not `gh pr checkout`: it names the local branch after
  # the contributor's headRefName (fast-forwarding an existing `meridian` or
  # agent branch of that name, whose push tracking then publishes the
  # commits), takes no SHA, and materialises the tree — .claude/ hooks,
  # .mcp.json, CLAUDE.md, AGENTS.md, .gitmodules — where this session loads
  # project configuration. Instead: fetch the head WITHOUT checking it out,
  # refuse unless it is the commit the API reported, and check that literal
  # commit out detached in a worktree outside the clone.
  #
  # Every git call from here on runs with the clone's inherited
  # command-running configuration neutralised, because a relative command
  # resolves INSIDE the worktree, i.e. to the contributor's files: hooks
  # pointed at an empty directory (core.hooksPath=.githooks would run their
  # post-checkout), core.fsmonitor off (a relative monitor runs on the next
  # run's status), and every filter driver git would read — system, global,
  # this clone, this worktree — emptied and made optional (their
  # .gitattributes selects the driver; the smudge runs at checkout, the
  # clean at status, the process one at either). Submodule recursion off,
  # so nothing is cloned from their .gitmodules. Built with GIT_CONFIG_*
  # (highest precedence, single-valued keys) rather than by editing any
  # config file.
  NOHOOKS=$(mktemp -d)
  trap 'rm -rf "$NOHOOKS"' EXIT
  # pin_git_config: export the pins for every later git call. Filter drivers
  # are enumerated from the clone AND, once it exists, from inside the
  # worktree: an includeIf gitdir:… condition in the operator's config can
  # define a driver that is active only in the linked worktree (review
  # round 2 of #130), so this runs again after `worktree add --no-checkout`
  # and before the first checkout. GIT_CONFIG_* entries are cleared first;
  # a config key cannot contain a newline, so a line per pin is safe.
  pin_git_config() {
    local pins drivers key name pin i
    pins="fetch.recurseSubmodules=false"$'\n'"submodule.recurse=false"$'\n'"core.hooksPath=$NOHOOKS"$'\n'"core.fsmonitor=false"$'\n'
    drivers=$'\n'
    while IFS= read -r key; do
      [ -n "$key" ] || continue
      name=${key#filter.}; name=${name%.*}
      case "$drivers" in *$'\n'"$name"$'\n'*) continue ;; esac
      drivers="$drivers$name"$'\n'
      pins="${pins}filter.$name.smudge="$'\n'"filter.$name.clean="$'\n'"filter.$name.process="$'\n'"filter.$name.required=false"$'\n'
    done <<<"$( { env -u GIT_CONFIG_COUNT git config --name-only --get-regexp '^filter\..*\.(smudge|clean|process|required)$' 2>/dev/null;
                  [ -d "$WT" ] && env -u GIT_CONFIG_COUNT git -C "$WT" config --name-only --get-regexp '^filter\..*\.(smudge|clean|process|required)$' 2>/dev/null; } || true)"
    i=0
    while IFS= read -r pin; do
      [ -n "$pin" ] || continue
      export "GIT_CONFIG_KEY_$i=${pin%%=*}" "GIT_CONFIG_VALUE_$i=${pin#*=}"
      i=$((i + 1))
    done <<<"$pins"
    export GIT_CONFIG_COUNT=$i
  }
  pin_git_config
  git fetch -q --no-tags --no-recurse-submodules "$FETCH_FROM" "refs/pull/$M/head"
  GOT=$(git rev-parse FETCH_HEAD)
  if [ "$GOT" != "$SHA" ]; then
    echo "REFUSED: $PR_REPO#$M head is $GOT, not the $SHA read from the API — the contributor pushed meanwhile; nothing checked out, rerun" >&2
    exit 5
  fi
  if [ -e "$WT" ]; then
    # A previous run's worktree: reuse it only if it is exactly a registered
    # worktree root of this clone (not a stranger's directory, not a
    # directory inside another worktree, whose git calls would act on THAT
    # worktree), detached (a branch worktree such as an Orca workspace is
    # someone's project directory, never an External checkout) and clean.
    registered=""
    for root in "${ROOTS[@]}"; do [ "$root" = "$WT" ] && registered=1; done
    [ -n "$registered" ] ||
      { echo "REFUSED: $WT exists and is not a registered worktree of this clone — move it aside" >&2; exit 1; }
    if ! phys_dir "$(git -C "$WT" rev-parse --show-toplevel)" || [ "$PHYS" != "$WT" ]; then
      echo "REFUSED: $WT is not the root of its own worktree — move it aside" >&2; exit 1
    fi
    if ON_BRANCH=$(git -C "$WT" symbolic-ref -q --short HEAD); then
      echo "REFUSED: $WT is a worktree on branch $ON_BRANCH, not a detached External checkout — set CHECKOUT_WORKTREES elsewhere" >&2; exit 1
    fi
    if [ -n "$(git -C "$WT" status --porcelain --ignore-submodules=dirty)" ]; then
      echo "DIRTY TREE in External worktree $WT — not switching:" >&2
      git -C "$WT" status --short --ignore-submodules=dirty >&2
      exit 2
    fi
    git -C "$WT" checkout -q --detach "$SHA"
  else
    mkdir -p "$(dirname "$WT")"
    git worktree add -q --detach --no-checkout "$WT" "$SHA"
    pin_git_config  # now with the drivers the worktree's own config activates
    git -C "$WT" checkout -q --detach "$SHA"
  fi
  echo "OK worktree=$WT detached=$SHA pr=$PR_REPO#$M issue=#$N ($TITLE) [UNTRUSTED external tree: contributor head '$BRANCH', checked out OUTSIDE this clone (HEAD and local branches untouched); nothing from it runs here — do not start an agent session, install or run tests inside it]"
  exit 0
fi

# Config BEFORE the checkout: the PR extension re-reads branch config on the
# HEAD-change event; written after, it is only noticed on the NEXT switch.
git config --replace-all "branch.$BRANCH.github-pr-owner-number" "${PR_REPO%%/*}#${PR_REPO##*/}#$M"
git config --replace-all "branch.$BRANCH.vscode-merge-base" "$BASE_REMOTE/$BASE_REF"

# No submodule recursion, for fetch OR checkout (clones set
# submodule.recurse=true): switching branches only moves the gitlink
# pointer; a later `git submodule update` fetches/updates on demand if the
# submodule contents are ever needed.
git fetch -q --no-recurse-submodules "$BASE_REMOTE" "$BASE_REF"
GIT_CONFIG_COUNT=2 \
  GIT_CONFIG_KEY_0=fetch.recurseSubmodules GIT_CONFIG_VALUE_0=false \
  GIT_CONFIG_KEY_1=submodule.recurse GIT_CONFIG_VALUE_1=false \
  gh pr checkout "$M" -R "$PR_REPO"

# The recursion-free switch moves gitlink pointers without touching submodule
# working trees, leaving them at the previous branch's commits (status shows
# them modified). Sync them to the recorded pointers; this only hits the
# network if a recorded commit is missing locally. --init is safe ONLY in the
# primary clone: in a linked worktree it writes a broken submodule .git
# pointer file that poisons every later git command, so skip the sync there
# (worktree submodules were never initialized; there is nothing to sync).
if [ "$(git rev-parse --git-dir)" = "$(git rev-parse --git-common-dir)" ]; then
  git submodule update --init --quiet

  # ts-mono companion: work that spans the viewer lives on a ts-mono branch
  # with the SAME name as this one (dev-agent convention; the parent gitlink
  # only bumps at merge time, so the recorded pointer is NOT the companion).
  # If an open companion PR exists, put the submodule on its branch so both
  # halves of the issue are editable together.
  TSMONO=$(git config --file .gitmodules --get-regexp '^submodule\..*\.path$' 2>/dev/null              | awk '$2 ~ /ts-mono/ {print $2; exit}' || true)
  # Only trusted picks reach here (an External pick exited above with its
  # worktree: its .gitmodules and branch name are the contributor's, so no
  # submodule init and no companion switch for it), so no cross-repo gate:
  # a promotion's companion is named after the fork branch. Guard on
  # authorship anyway — an unrelated ts-mono PR sharing a generic name must
  # not be treated as a companion.
  if [ -n "$TSMONO" ]; then
    COMP=$(gh pr list --repo meridianlabs-ai/ts-mono --head "$BRANCH" --state open              --json number,author,headRefName --jq '[.[] | select((.author.login == "i-am-marvin") or (.author.login == "app/meridian-marvin") or (.author.login == "app/claude") or (.headRefName | startswith("claude/issue-")))][0].number // empty' 2>/dev/null || true)
    if [ -n "$COMP" ]; then
      if [ -z "$(git -C "$TSMONO" status --porcelain 2>/dev/null)" ]; then
        git -C "$TSMONO" fetch -q origin "$BRANCH"           && git -C "$TSMONO" checkout -q -B "$BRANCH" "origin/$BRANCH"           && NOTE_TSMONO=" [ts-mono companion: PR meridianlabs-ai/ts-mono#$COMP — submodule on branch $BRANCH; parent gitlink intentionally differs until the merge-time bump]"           || NOTE_TSMONO=" [ts-mono companion PR #$COMP exists but submodule checkout failed — handle manually]"
      else
        NOTE_TSMONO=" [ts-mono companion PR #$COMP exists but the submodule tree is dirty — not switching it]"
      fi
    fi
  fi
fi

NOTE="${NOTE_TSMONO:-}"
[ -n "$CROSS" ] && NOTE="$NOTE [cross-repo: $PR_REPO — upstream PR, don't push without cause]"
echo "OK branch=$(git branch --show-current) pr=$PR_REPO#$M issue=#$N ($TITLE)$NOTE"
