# Shared helpers for the local maintainer skills. Sourced by the skills'
# scripts; defines constants and functions and runs nothing. The rules they
# implement are in skills/THREAT_MODEL.md: trust by verified author (rule
# b), an outsider's tree is data (rule c), republished text is guarded
# (rule d). Every function fails closed: what cannot be checked is refused.
#
# Source it through the script's real path, so a skill linked into
# ~/.claude/skills or ~/.agents/skills still finds it:
#
#   . "$(dirname "$(realpath "$0")")/../lib/common.sh"
#
# shellcheck disable=SC2034  # REASON, UP_LINE and UP_NUM are set for the caller

SKILLS_LIB=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# Trusted identities (comma-separated), in REST form: the machine account's
# User login (the PAT, today) and its GitHub App login (Phase 2; trusted by
# name — the collaborators endpoint answers `none` for an App). The User
# leaves this ONE value when the PAT is retired. Author logins are normalised
# to this form by NORM_LOGIN before comparison.
TRUSTED_LOGINS="i-am-marvin,meridian-marvin[bot]"
# NORM_LOGIN: jq filter over an author object → its login in REST form. A
# GraphQL Bot author (`__typename: Bot`) arrives bare (`meridian-marvin`) and
# gets the `[bot]` suffix; `gh pr list/view --json author` renders an App as
# `app/<slug>`; a REST user object carries `type: Bot` and the suffix
# already. A deleted author (null) is "". A User's login is never
# rewritten, so a User who registers an App's slug is not the App.
NORM_LOGIN='if . == null then "" elif (.__typename // "") == "Bot" then "\(.login // "")[bot]" elif ((.login // "") | startswith("app/")) then "\(.login[4:])[bot]" else (.login // "") end'

# named_trusted <login>: 0 when <login> is in TRUSTED_LOGINS (by name, case
# insensitive). No lookup.
named_trusted() {
  local login
  login=$(tr 'A-Z' 'a-z' <<<"$1")
  [ -n "$login" ] || return 1
  case ",$(tr 'A-Z' 'a-z' <<<"$TRUSTED_LOGINS")," in *",$login,"*) return 0 ;; esac
  return 1
}

# trusted_login <repo> <login>: 0 when <login> is in TRUSTED_LOGINS or has
# write access on <repo> (admin/maintain/write from the collaborator
# permission API — a public repo answers `read` for everyone else). Only a
# User login is looked up: any other App (`github-actions[bot]`,
# `claude[bot]`) is never trusted, and a value not shaped like a login never
# reaches the request path. A failed or unexpected lookup is untrusted
# (fail closed). Cached per repo and login for the run; call it in the main
# shell, not inside $(...), or the cache is lost.
PERM_CACHE=""
trusted_login() {
  local repo=$1 login perm
  login=$(tr 'A-Z' 'a-z' <<<"$2")
  named_trusted "$login" && return 0
  case "$login" in ""|-*|*[!a-z0-9-]*) return 1 ;; esac
  case "$PERM_CACHE" in *"|$repo:$login=ok|"*) return 0 ;; *"|$repo:$login=no|"*) return 1 ;; esac
  perm=$(gh api "repos/$repo/collaborators/$login/permission" --jq .permission 2>/dev/null || true)
  case "$perm" in
    admin|maintain|write) PERM_CACHE="$PERM_CACHE|$repo:$login=ok|"; return 0 ;;
    *) PERM_CACHE="$PERM_CACHE|$repo:$login=no|"; return 1 ;;
  esac
}

# check_pr <repo> <pr-json>: the trust rule for a PR whose head the skill
# will act on. Sets REASON to "" when the PR qualifies (its head repository
# is <repo> AND its author passes trusted_login on <repo>), else to why it
# was refused. Reads only headRepository and author — never the PR's
# title, body or branch name. A branch inside <repo> can only be pushed by
# a write-access account, which makes the head-repository check the
# load-bearing one and the author check defence in depth.
check_pr() {
  local repo=$1 head login
  head=$(jq -r '.headRepository.nameWithOwner // ""' <<<"$2")
  login=$(jq -r ".author | $NORM_LOGIN" <<<"$2")
  REASON=""
  if [ "$head" != "$repo" ]; then
    REASON="head repository is '${head:-unknown}', not $repo"
  elif ! trusted_login "$repo" "$login"; then
    REASON="author '${login:-unknown}' is not in TRUSTED_LOGINS ($TRUSTED_LOGINS) and has no write access on $repo"
  fi
}

# genuine_proxy <issue-author> <labels-csv>: 0 when the issue is a genuine
# External proxy — written by a login in TRUSTED_LOGINS (the sync writes
# them as the machine account) AND labelled External. Membership by name
# only: write access is not enough, a collaborator's own issue is not a
# proxy, and the label alone proves nothing (anyone can file an issue).
genuine_proxy() {
  named_trusted "$1" || return 1
  case ",$2," in *",External,"*) return 0 ;; esac
  return 1
}

# proxy_upstream_pr <body> <upstream-repo>: reads the sync's canonical
# `Upstream PR: <url>` line from a proxy body. Sets UP_LINE to the first
# such line ("" when there is none) and UP_NUM to its PR number when the URL
# is exactly a PR of <upstream-repo> ("" otherwise). Believe it only on a
# genuine_proxy: the line is free text on any other issue.
proxy_upstream_pr() {
  local url
  UP_LINE=$(grep -ioE 'Upstream PR:[[:space:]]*https://github\.com/[^[:space:]]+/pull/[0-9]+' <<<"$1" | head -1 || true)
  UP_NUM=""
  [ -n "$UP_LINE" ] || return 0
  url=$(grep -oE 'https://[^[:space:]]+' <<<"$UP_LINE")
  if grep -qE "^https://github\.com/${2//./\\.}/pull/[0-9]+$" <<<"$url"; then
    UP_NUM=$(grep -oE '[0-9]+$' <<<"$url")
  fi
}

# pin_git_config <hooks-dir> [<worktree>]: export, for every later git call
# of the calling script, pins that neutralise the clone's inherited
# command-running configuration on an outsider's tree — because a relative
# command resolves INSIDE the worktree, i.e. to the contributor's files:
# hooks pointed at <hooks-dir> (an empty directory the caller owns;
# core.hooksPath=.githooks would run their post-checkout), core.fsmonitor
# off (a relative monitor runs on the next status), and every filter driver
# git would read — system, global, this clone, this worktree — emptied and
# made optional (their .gitattributes selects the driver; the smudge runs at
# checkout, the clean at status or add, the process one at either).
# Submodule recursion off, so nothing is cloned from their .gitmodules.
# Built with GIT_CONFIG_* (highest precedence, single-valued keys) rather
# than by editing any config file. Filter drivers are enumerated from the
# current directory's config AND from inside <worktree>: an includeIf
# gitdir:… condition in the operator's config can define a driver that is
# active only in a linked worktree, so call this again after `worktree add
# --no-checkout` and before the first checkout. GIT_CONFIG_* entries are
# cleared first; a config key cannot contain a newline, so a line per pin
# is safe.
pin_git_config() {
  local hooks=$1 wt=${2:-} pins drivers key name pin i
  pins="fetch.recurseSubmodules=false"$'\n'"submodule.recurse=false"$'\n'"core.hooksPath=$hooks"$'\n'"core.fsmonitor=false"$'\n'
  drivers=$'\n'
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    name=${key#filter.}; name=${name%.*}
    case "$drivers" in *$'\n'"$name"$'\n'*) continue ;; esac
    drivers="$drivers$name"$'\n'
    pins="${pins}filter.$name.smudge="$'\n'"filter.$name.clean="$'\n'"filter.$name.process="$'\n'"filter.$name.required=false"$'\n'
  done <<<"$( { env -u GIT_CONFIG_COUNT git config --name-only --get-regexp '^filter\..*\.(smudge|clean|process|required)$' 2>/dev/null;
                [ -n "$wt" ] && [ -d "$wt" ] && env -u GIT_CONFIG_COUNT git -C "$wt" config --name-only --get-regexp '^filter\..*\.(smudge|clean|process|required)$' 2>/dev/null; } || true)"
  i=0
  while IFS= read -r pin; do
    [ -n "$pin" ] || continue
    export "GIT_CONFIG_KEY_$i=${pin%%=*}" "GIT_CONFIG_VALUE_$i=${pin#*=}"
    i=$((i + 1))
  done <<<"$pins"
  export GIT_CONFIG_COUNT=$i
}

# defang: stdin to stdout with the agents' trigger phrases backticked and
# the loops' `<!-- … -->` markers split (outbound.py defang).
defang() {
  python3 "$SKILLS_LIB/outbound.py" defang
}

# render_markdown <markdown> <repo>: print GitHub's rendering of <markdown>
# with references resolved in <repo>'s context. Non-zero when the render
# failed. Deciding which `#M` GitHub reads as a reference is its parser's
# job; re-implementing it did not converge (PR #127, rounds 1-4).
render_markdown() {
  jq -n --arg t "$1" --arg c "$2" '{text: $t, mode: "gfm", context: $c}' \
    | gh api markdown --input - 2>/dev/null
}

# rendered_refs <html> <repo> [<allowed-number>...]: print the issues and
# PRs of <repo> that the rendered <html> links (a reference GitHub resolved,
# or a link to one), other than the allowed numbers, as `#N #M` — nothing
# when there are none.
rendered_refs() {
  local html=$1 repo=$2 n
  shift 2
  local allow=" $* "
  { grep -oiE "(data-url|href)=\"https://github\.com/${repo//./\\.}/(issues|pull)/[0-9]+" <<<"$html" || true; } \
    | { grep -oE '[0-9]+$' || true; } | sort -un | while IFS= read -r n; do
      case "$allow" in *" $n "*) ;; *) printf '#%s ' "$n" ;; esac
    done
}
