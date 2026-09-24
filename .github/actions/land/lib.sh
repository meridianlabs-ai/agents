#!/usr/bin/env bash
# Helpers sourced by the `land` composite's bash steps. One copy so the
# de-fang and the retry loop cannot drift between the posting steps.

# defang SRC DST — copy an agent-authored body to DST with live triggers
# and loop markers broken, then cap it under GitHub's 65,536-char comment
# limit. The land job posts as the machine account (write access), so a bare
# trigger token in agent text would fire a workflow and a quoted marker would
# forge a verdict / suppress an owed hand-back — the @auto loops match their
# markers with plain substring contains(). Case-insensitive (GNU sed `I`)
# because the stubs gate on GitHub's contains(), which ignores case. Same
# sed the codex landing steps and model-provenance use; keep the marker list
# in step with the `<!-- … -->` comments the workflows read. The codex
# reviewer's `engine: codex` footer is deliberately NOT in this list:
# claude-review.yml lands the codex review itself through this composite,
# and that footer is the anchor pr-feedback-context keys the next codex fix
# round on — splitting it here would blind every codex review-fix round.
# Callers whose bodies must not pose as a review (the CI-fix summaries in
# claude-auto.yml) sed the footer themselves before handing the file over.
# Truncation only drops trailing bytes, so it cannot resurrect a trigger the
# sed removed.
defang() {
  local src="$1" dst="$2"
  sed -E -e 's/@(review|claude|auto)/`\1`/gI' \
         -e 's/claude-review-(summary|verdict|comment|nudge)/claude-review \1/gI' \
         -e 's/auto-(handoff|converged|review-rounds|review-head|fix-attempts)/auto \1/gI' \
         "$src" >"$dst"
  if [ "$(wc -c <"$dst")" -gt 60000 ]; then
    head -c 60000 "$dst" >"$dst.trunc"
    printf '\n\n_[truncated: the body exceeded the comment size cap]_\n' >>"$dst.trunc"
    mv "$dst.trunc" "$dst"
  fi
}

# defang_str STRING — the same, for a scalar (a title); prints the result.
defang_str() {
  printf '%s' "$1" | tr -d '\n' | sed -E -e 's/@(review|claude|auto)/`\1`/gI' \
    -e 's/claude-review-(summary|verdict|comment|nudge)/claude-review \1/gI' \
    -e 's/auto-(handoff|converged|review-rounds|review-head|fix-attempts)/auto \1/gI'
}

# retry N WHAT CMD... — run CMD up to N times with 15/30/45… s backoff (the
# post-pr-comment cadence), no sleep after the last attempt; returns the last
# exit status. Its own chatter goes to stderr so `out=$(retry … gh pr create)`
# captures only the command's stdout.
retry() {
  local n="$1" what="$2" i rc=0
  shift 2
  for ((i = 1; i <= n; i++)); do
    # `$?` after a bare `if` with no branch taken is 0, so capture in else.
    if "$@"; then return 0; else rc=$?; fi
    if [ "$i" -lt "$n" ]; then
      echo "land: $what failed (attempt $i/$n); retrying" >&2
      sleep $((i * 15))
    fi
  done
  return "$rc"
}

# retry_read N WHAT CMD... — `retry` for a command whose STDOUT is the value
# (`have=$(retry_read 3 what gh api … --jq …)`): each attempt's stdout is
# captured and only the successful attempt's is printed. A failed `gh api`
# prints its JSON error body to stdout even with `--jq`, so streaming the
# attempts through (plain `retry`) poisons a fail-then-succeed capture —
# the gates' ghr() shape. Stderr passes through.
retry_read() {
  local n="$1" what="$2" i rc=0 out
  shift 2
  for ((i = 1; i <= n; i++)); do
    if out=$("$@"); then printf '%s' "$out"; return 0; else rc=$?; fi
    if [ "$i" -lt "$n" ]; then
      echo "land: $what failed (attempt $i/$n); retrying" >&2
      sleep $((i * 15))
    fi
  done
  return "$rc"
}

# Comment on an issue or PR (the issues endpoint serves both) from a file.
post_comment_file() {
  local repo="$1" number="$2" file="$3"
  gh api "repos/$repo/issues/$number/comments" -F body=@"$file" --silent
}

# post_review_comment_file REPO PR COMMIT PATH LINE SIDE FILE — one inline
# (line-level) review comment on PR, anchored to COMMIT at PATH:LINE on SIDE
# (LEFT|RIGHT), body from FILE (already de-fanged by the caller). Everything
# travels as gh form data, never as a shell-interpolated command, so PATH is
# data whatever it holds. Its own retry, not `retry`: a 422 means the line is
# not in the PR's diff (the reviewer read a tip the PR page does not show, or
# mis-numbered a line) and no retry will change that, so it is final at once;
# any other failure gets three attempts. Returns 0 posted, 1 not — the
# caller folds a comment that did not post into its follow-up comment.
post_review_comment_file() {
  local repo="$1" pr="$2" commit="$3" path="$4" line="$5" side="$6" file="$7" i err
  for ((i = 1; i <= 3; i++)); do
    if err=$(gh api "repos/$repo/pulls/$pr/comments" -F body=@"$file" -f commit_id="$commit" \
               -f path="$path" -F line="$line" -f side="$side" --silent 2>&1); then
      return 0
    fi
    printf '%s\n' "$err" >&2
    case "$err" in *"(HTTP 422)"*) return 1 ;; esac
    if [ "$i" -lt 3 ]; then sleep $((i * 15)); fi
  done
  return 1
}

# remote_branch_exists REPO BRANCH — prints `yes` or `no` and returns 0 when
# the API gave a definite answer (200, or 404 = no such branch); returns 1 on
# anything else (5xx, rate limit, network) so that, under `retry`, a
# transient blip is re-asked rather than reported as "branch is not on
# origin". The 404 is recognised by gh's own `(HTTP 404)` suffix.
remote_branch_exists() {
  local repo="$1" branch="$2" err
  if err=$(gh api "repos/$repo/branches/$branch" --silent 2>&1); then
    echo yes
    return 0
  fi
  case "$err" in
    *"(HTTP 404)"*) echo no; return 0 ;;
  esac
  printf '%s\n' "$err" >&2
  return 1
}

# open_or_adopt_pr REPO BRANCH BASE TITLE BODY_FILE [DRAFT] — adopt the open PR
# whose head is REPO's own BRANCH if there is one (the agent may have opened
# it itself — the fork's prompt mandates it), else create one; prints
# `adopted|opened <number> <url>`. Only a SAME-REPO head is adopted:
# `gh pr list --head` matches on the head branch NAME alone, so a fork PR
# whose head happens to share the name (predictable names like
# `dependabot-fix/<date>`) would otherwise be adopted and the branch just
# pushed would never get its PR (ts-mono#670, finding B4). Each candidate
# must report `isCrossRepository: false` and a `headRepositoryOwner` equal
# to REPO's owner (a missing field fails closed); anything else is named
# on stderr and ignored, and the create runs. The filter is real `jq` over
# the listed JSON, not gh's `--jq`, so the tests' stub `gh` exercises it.
# Meant to run under `retry`: the adopt check is INSIDE the unit, so a create
# whose response was lost (timeout / 5xx after the write) is found by the
# next attempt's list and adopted, not re-created into "a pull request
# already exists". Same two paths as claude.yml's "Open or adopt PR".
# DRAFT `true` (the caller's `pr-draft`) creates the PR as a draft; an
# adopted PR's draft state is never touched, either way — a maintainer may
# already have marked it ready (a create whose response was lost is adopted
# as the draft it was created as).
open_or_adopt_pr() {
  local repo="$1" branch="$2" base="$3" title="$4" body_file="$5" draft="${6:-false}" owner="${1%%/*}" list found skipped url
  list=$(gh pr list --repo "$repo" --head "$branch" --state open \
           --json number,url,isCrossRepository,headRepositoryOwner,headRefName) || return 1
  # Logins are case-insensitive on GitHub; the repo input may not carry the
  # canonical case.
  found=$(jq -r --arg owner "$owner" --arg branch "$branch" '
    [.[] | select(.isCrossRepository == false
                  and .headRefName == $branch
                  and ((.headRepositoryOwner.login // "") | ascii_downcase) == ($owner | ascii_downcase))]
    | if length > 0 then "\(.[0].number) \(.[0].url)" else empty end' <<<"$list") || return 1
  if [ -n "$found" ]; then
    echo "adopted $found"
    return 0
  fi
  skipped=$(jq -r --arg repo "$repo" --arg branch "$branch" '.[] |
    "land: not adopting PR #\(.number) (\(.url)): its head \(.headRepositoryOwner.login // "?"):\(.headRefName // "?") is not \($repo) \($branch) (cross-repository: \(.isCrossRepository | tostring)); opening a PR for the pushed branch instead."' \
            <<<"$list") || return 1
  [ -z "$skipped" ] || printf '%s\n' "$skipped" >&2
  # The arguments are all read; "$@" carries the optional flag (an empty
  # array would trip `set -u` on bash < 4.4).
  if [ "$draft" = "true" ]; then set -- --draft; else set --; fi
  url=$(gh pr create --repo "$repo" --head "$branch" --base "$base" \
          --title "$title" --body-file "$body_file" "$@") || return 1
  echo "opened ${url##*/} $url"
}

# landing_failure_hint FAILED PUSHED [WITHHELD [WORKFLOW_FILES [ALLOW_BUILD_CONFIG]]]
# — the one-line consequence
# the Report step adds under "Landing failed at step(s): FAILED" (a
# comma-separated list of step names, `post (…)` included): whether the
# agent's commits reached the branch, and which hand-back / verdict /
# hand-off / stage move a human now owes. WITHHELD is a second list, in the
# same format, of the planned `handback` / `verdict` / `handoff` / `stage`
# steps that never ran (skipped, not failed, because the PR step failed after
# the push — or, for the verdict, because the Post step lost a comment) — each owed just
# like a failed one, and named once even when it appears in both lists.
# WORKFLOW_FILES is the `workflows` step's list of the files the bundle
# changes under .github/ or another path later automated jobs execute or
# load as configuration (its `protected` list) — agent-chosen paths, so they are
# de-fanged here; empty with `workflows` failed means the step could not list
# the paths at all (the listing failed, or the push would create the branch
# and no base tip was fetched) and refused the bundle unchecked.
# ALLOW_BUILD_CONFIG non-empty (the caller's `allow-build-config`) leaves the
# build and dependency group out of the refused kinds the line names, since
# the step did not check it. Everything else is our own text, so the line
# names no live trigger token.
landing_failure_hint() {
  local failed="$1" pushed="$2" withheld="${3:-}" files="${4:-}" allow_build="${5:-}" hint="" list owed kinds
  # Report joins with ", "; match on step names with the spaces removed.
  list=",${failed// /},"
  owed="$list${withheld// /},"
  case "$list" in
    *,download,*|*,validate,*|*,plan,*)
      hint="The landing was refused before any write: the agent's commits were **not** pushed and nothing was posted." ;;
    *,workflows,*)
      if [ -n "$files" ]; then
        kinds="anything else under \`.github/\`, agent instructions and settings (\`CLAUDE.md\`, \`AGENTS.md\`, \`.claude/\`, \`.mcp.json\`, …), or build and dependency configuration (\`pyproject.toml\`, lockfiles, \`package.json\`, …)"
        [ -z "$allow_build" ] || kinds="anything else under \`.github/\`, or agent instructions and settings (\`CLAUDE.md\`, \`AGENTS.md\`, \`.claude/\`, \`.mcp.json\`, …)"
        hint="The agent's commits change files later automated runs execute or load as configuration ($(defang_str "$files")): workflows, which the machine account may not push (it has no Workflows permission), $kinds. Such changes are made from a maintainer's machine, where a human reads them before automation runs them. The commits were **not** pushed and are lost with the runner: there is no branch to look for."
      else
        hint="The landing could not check whether the agent's commits change workflow or other executed files (the listing failed, or a new branch had no base tip to list against; see the run log), so the bundle was refused unchecked. The commits were **not** pushed and are lost with the runner: there is no branch to look for."
      fi ;;
    *,fetch,*|*,push,*) hint="The agent's commits were **not** pushed." ;;
    *) [ -z "$pushed" ] || hint="The agent's commits were pushed; only what follows the push is affected." ;;
  esac
  # Each owed action independently: a failed hand-back and a withheld stage
  # can coincide, and the human must hear about both.
  case "$owed" in *,handback,*) hint="${hint:+$hint }Post the re-review request by hand." ;; esac
  case "$owed" in *,verdict,*) hint="${hint:+$hint }Post the review verdict by hand." ;; esac
  case "$owed" in *,handoff,*) hint="${hint:+$hint }Post the hand-off by hand." ;; esac
  case "$owed" in *,stage,*) hint="${hint:+$hint }Move the Atlas stage by hand." ;; esac
  printf '%s' "$hint"
}

# TIER1_PATHSPECS, TIER2_PATHSPECS — the entry points and configuration
# files a later automated job on an agent's branch executes or loads, as git
# pathspecs (not everything those run: see below); the `workflows` step
# refuses a bundle in which the agent changed any of them (Claude Security
# 4628446, criterion 2, 2026-09-23): the machine account's push would
# otherwise move agent-written files into the same-repo tree the next run —
# the reviewer the `@review` hand-back starts, a loop round, a `@claude`
# follow-up — provisions from or reads as instructions, with no human having
# read them. Tier 1 is always refused. Tier 2 is refused unless the caller
# passes `allow-build-config: "true"` (design/executed-paths-residual.md →
# Land: tier-2 opt-in), which a caller may do only when every automated
# consumer of the branches it pushes provisions and runs its agent as an
# unprivileged user, as the reusable workflows do since that design's plan
# step 5; the files then land, and what executes them holds only the
# read-only job token (decision: Ransom, 2026-09-23: that meets criterion 2
# for tier 2). Grouped by the consumer that reads them from the checkout:
#   Tier 1:
#   - .github/ whole (root only: GitHub reads no nested one): the workflows
#     (the machine account has no Workflows permission), the composite
#     actions a workflow runs with `uses: ./…` — the callers' claude-setup,
#     as `runner` before any sandbox — and the scripts and configuration
#     workflows and GitHub read from there.
#   - Agent instructions and settings Claude Code and codex load from the
#     working tree, at every depth (Claude Code reads a nested CLAUDE.md
#     beside the first file it opens there): CLAUDE.md, CLAUDE.local.md,
#     AGENTS.md (codex's instructions, and Claude Code's where there is no
#     CLAUDE.md), AGENTS.override.md, .claude (settings, hooks, skills,
#     agents, rules; a file or link of that name too), .mcp.json (spawned
#     MCP servers), .codex/ and .agents/ (codex's project configuration and
#     skills), and the rest of what claude-code-action's
#     restoreConfigFromBase treats as executable configuration:
#     .claude.json, .gitmodules, .ripgreprc, .husky/.
#   Tier 2:
#   - Build and dependency configuration provisioning executes, at every
#     depth (workspace members are built and installed too):
#     provision-fallback's `uv pip install -e .[dev]` runs the build backend
#     pyproject.toml / setup.py / setup.cfg name and installs what they, and
#     uv.toml's indexes and .python-version, select; callers' recipes run
#     `uv sync` (uv.lock), pip requirements files, or `pnpm install`, which
#     runs every package.json lifecycle script, .pnpmfile.cjs hooks and
#     yarn's .yarn/ plugins, from the registries .npmrc / .yarnrc name.
# Accepted gap (decision: Ransom, 2026-09-23), so this does not close the
# class: ordinary files UNCHANGED configuration executes — a script a
# claude-setup step, a settings hook or an MCP server command runs, a build
# backend on `backend-path`, a module a setup.py or build hook imports, any
# file a caller's recipe reads — are not listed, and an agent's change to
# one still lands; the reusable workflows' next run executes it during
# provisioning, at agent start or in the agent's own tool calls only as the
# unprivileged agent user (`codex`, or `claude-agent` on Claude jobs);
# SECURITY.md → Guarantees states it. Links and CLAUDE.md imports out of
# the checked paths are followed by protected_reach below.
# shellcheck disable=SC2034  # read by the land composite's `workflows` step
TIER1_PATHSPECS=(
  .github
  ':(glob)**/CLAUDE.md' ':(glob)**/CLAUDE.local.md' ':(glob)**/AGENTS.md' ':(glob)**/AGENTS.override.md'
  ':(glob)**/.claude' ':(glob)**/.claude/**' ':(glob)**/.mcp.json' ':(glob)**/.claude.json'
  ':(glob)**/.codex' ':(glob)**/.codex/**' ':(glob)**/.agents' ':(glob)**/.agents/**'
  ':(glob)**/.gitmodules' ':(glob)**/.ripgreprc' ':(glob)**/.husky' ':(glob)**/.husky/**'
)
# shellcheck disable=SC2034  # read by the land composite's `workflows` step
TIER2_PATHSPECS=(
  ':(glob)**/pyproject.toml' ':(glob)**/setup.py' ':(glob)**/setup.cfg'
  ':(glob)**/uv.lock' ':(glob)**/uv.toml' ':(glob)**/.python-version' ':(glob)**/requirements*.txt'
  ':(glob)**/package.json' ':(glob)**/package-lock.json' ':(glob)**/npm-shrinkwrap.json'
  ':(glob)**/pnpm-lock.yaml' ':(glob)**/pnpm-workspace.yaml' ':(glob)**/.pnpmfile.cjs'
  ':(glob)**/yarn.lock' ':(glob)**/.yarnrc' ':(glob)**/.yarnrc.yml' ':(glob)**/.yarn' ':(glob)**/.yarn/**'
  ':(glob)**/.npmrc'
)

# resolve_tree_path REV PATH — follow PATH through REV's tree as the kernel
# would on a checkout of it: component by component, a symlink's target
# (relative to the link's directory) substituted in place, `.` dropped, `..`
# taking the resolved prefix back a level. Prints, NUL-terminated, every
# symlink location the walk passes through, then the path it ends at (which
# need not exist) — `.` when that is the repository root, whose pathspec
# covers the whole tree (review round 1: `.claude/rules -> ..` exposes every
# file as a rule) — and sets RTP_END to that end. Stops printing, with
# RTP_END empty, when the walk leaves the tree — an absolute target, `..`
# past the root — or follows more than 40 links (a loop): no bundle can
# change what lies outside the tree. Returns 1 on a failed read.
resolve_tree_path() {
  local rev="$1" rest="$2" at="" comp cand out oid target hops=0
  RTP_END=""
  while [ -n "$rest" ]; do
    comp="${rest%%/*}"
    if [ "$comp" = "$rest" ]; then rest=""; else rest="${rest#*/}"; fi
    case "$comp" in
      "" | .) continue ;;
      ..)
        [ -n "$at" ] || return 0
        case "$at" in */*) at="${at%/*}" ;; *) at="" ;; esac
        continue ;;
    esac
    cand="${at:+$at/}$comp"
    out=$(git ls-tree "$rev" -- ":(literal)$cand") || return 1
    if [ "${out%% *}" = "120000" ]; then
      printf '%s\0' "$cand"
      hops=$((hops + 1))
      [ "$hops" -le 40 ] || return 0
      out="${out%%$'\t'*}"
      oid="${out##* }"
      target=$(git cat-file blob "$oid") || return 1
      case "$target" in /*) return 0 ;; esac
      rest="$target${rest:+/$rest}"
    else
      at="$cand"
    fi
  done
  RTP_END="${at:-.}"
  printf '%s\0' "$RTP_END"
}

# logical_names PATH — sets LN_NAMES to PATH and the names it is also
# reachable under through the directory links protected_reach has resolved
# so far (LINK_ENDS[i] reached from LINK_AT[i]; `.` is the root): a file
# under `.agents/` read through `.claude -> .agents` is `.claude/...` to
# Claude Code, and is classified by that name (review round 1). Every name
# is derived, never a truncated set (review round 2: a cap of 20 let sibling
# aliases crowd out the `.claude/rules/...` name, so a rule's import went
# unprotected). The names are finite unless a link sits inside its own
# target (`a/b/up -> ..`): past 256 names this returns 1, and protected_reach
# fails closed. A link to the root never gets here — protected_reach stops
# walking once the whole tree is protected.
logical_names() {
  local i=0 k n r l c y seen
  LN_NAMES=("$1")
  while [ "$i" -lt "${#LN_NAMES[@]}" ]; do
    if [ "${#LN_NAMES[@]}" -gt 256 ]; then
      echo "::error::land: $(printf '%q' "$1") is reachable under more than 256 names through directory links (a link inside its own target?)." >&2
      return 1
    fi
    n="${LN_NAMES[$i]}"
    k=0
    while [ "$k" -lt "${#LINK_ENDS[@]}" ]; do
      r="${LINK_ENDS[$k]}" l="${LINK_AT[$k]}" c=""
      if [ "$r" = "." ]; then c="$l/$n"
      elif [ "$n" = "$r" ]; then c="$l"
      else case "$n" in "$r"/*) c="$l/${n#"$r"/}" ;; esac
      fi
      k=$((k + 1))
      [ -n "$c" ] || continue
      seen=""
      for y in "${LN_NAMES[@]}"; do [ "$y" != "$c" ] || { seen=1; break; }; done
      [ -n "$seen" ] || LN_NAMES+=("$c")
    done
    i=$((i + 1))
  done
}

# protected_reach REV PATHSPEC... — the in-tree paths REV's entries under
# PATHSPEC reach beyond the pathspecs themselves, NUL-terminated: every
# symlink's resolution (resolve_tree_path: the links passed through and the
# end), and the `@path` imports Claude Code expands in instruction files —
# CLAUDE.md, CLAUDE.local.md, AGENTS.md, anything under .claude/rules/, and
# every file reached by an import — resolved relative to the importing
# file. A file is classified by every name it is reachable under
# (logical_names: `.agents/rules/x.md` behind `.claude -> .agents` is a
# rule), and an import is resolved against the directory of each of those
# names as well as its physical one; `~/` and absolute imports are outside
# the tree. To a fixed point: what a reached path contains is walked too, so
# a link inside a linked directory or an import of an import is followed;
# more than 20 rounds fails, and so does a file with too many names
# (logical_names). Reaching the root (`.`) ends the walk: the whole tree is
# then protected. Over-matching is the safe direction: an `@` word
# that is not an import (a mention) names a path that is normally absent,
# and the imports of code spans are taken too. Returns 1 on a failed read.
protected_reach() {
  local rev="$1" empty meta path mode oid content round=0 added imp seen f g x y d n rooted="" _
  shift
  local -a specs=("$@") found=() imported=() cands=() dirs=()
  LINK_ENDS=() LINK_AT=()
  empty=$(git hash-object -t tree /dev/null) || return 1
  f=$(mktemp) && g=$(mktemp) || return 1
  while :; do
    round=$((round + 1))
    if [ "$round" -gt 20 ]; then
      echo "::error::land: the protected paths' links and imports did not settle in 20 rounds." >&2
      rm -f "$f" "$g"; return 1
    fi
    git diff --raw -z --no-renames --no-abbrev "$empty" "$rev" -- "${specs[@]}" ${found[@]+"${found[@]/#/:(literal)}"} >"$f" \
      || { rm -f "$f" "$g"; return 1; }
    added=""
    # shellcheck disable=SC2094  # the loop removes its input only on the way out (return 1)
    while IFS= read -r -d '' meta && IFS= read -r -d '' path; do
      read -r _ mode _ oid _ <<<"$meta"
      logical_names "$path" || { rm -f "$f" "$g"; return 1; }
      imp=""
      for n in "${LN_NAMES[@]}"; do
        case "/$n" in */CLAUDE.md | */CLAUDE.local.md | */AGENTS.md | */.claude/rules/*) imp=1; break ;; esac
      done
      [ -n "$imp" ] || for x in ${imported[@]+"${imported[@]}"}; do [ "$x" != "$path" ] || { imp=1; break; }; done
      cands=()
      if [ "$mode" = "120000" ]; then
        # A link's resolution; an instruction file's link target is read
        # for imports like the file it stands in for, and what lies under a
        # directory link is known by the link's name too.
        resolve_tree_path "$rev" "$path" >"$g" || { rm -f "$f" "$g"; return 1; }
        if [ -n "$RTP_END" ]; then
          seen=""
          for x in ${LINK_AT[@]+"${LINK_AT[@]}"}; do [ "$x" != "$path" ] || { seen=1; break; }; done
          # A new name for a directory: walk again, so files listed before
          # the link in this round are classified by it too.
          [ -n "$seen" ] || { LINK_ENDS+=("$RTP_END"); LINK_AT+=("$path"); added=1; }
        fi
        while IFS= read -r -d '' x; do
          cands+=("$x")
          [ -z "$imp" ] || imported+=("$x")
        done <"$g"
      elif [ -n "$imp" ]; then
        content=$(git cat-file blob "$oid") || { rm -f "$f" "$g"; return 1; }
        dirs=()
        for n in "${LN_NAMES[@]}"; do
          case "$n" in */*) d="${n%/*}/" ;; *) d="" ;; esac
          seen=""
          for x in ${dirs[@]+"${dirs[@]}"}; do [ "$x" != "$d" ] || { seen=1; break; }; done
          [ -n "$seen" ] || dirs+=("$d")
        done
        while IFS= read -r y; do
          y="${y#"${y%%[![:space:]]*}"}"
          y="${y#@}"
          case "$y" in "" | /* | "~"*) continue ;; esac
          for d in "${dirs[@]}"; do
            resolve_tree_path "$rev" "$d$y" >"$g" || { rm -f "$f" "$g"; return 1; }
            while IFS= read -r -d '' x; do cands+=("$x"); imported+=("$x"); done <"$g"
          done
        done < <(printf '%s\n' "$content" | grep -oE '(^|[[:space:]])@[^[:space:]]+' || true)
      fi
      for x in ${cands[@]+"${cands[@]}"}; do
        seen=""
        for y in ${found[@]+"${found[@]}"}; do [ "$y" != "$x" ] || { seen=1; break; }; done
        [ -n "$seen" ] || { found+=("$x"); added=1; }
        [ "$x" != "." ] || rooted=1
      done
      # The root is reached: `.` already protects every path in the tree,
      # so no further link or import can add one.
      [ -z "$rooted" ] || break
    done <"$f"
    [ -z "$rooted" ] || break
    [ -n "$added" ] || break
  done
  rm -f "$f" "$g"
  for x in ${found[@]+"${found[@]}"}; do printf '%s\0' "$x"; done
}

# atlas_todo REPO NUMBER — put an issue the land job created, reopened or
# assigned on the Atlas board (org project 1) by node ID and set its Status
# to Todo when it has none or is Done. Explicit, not via the board's built-in
# "item added -> Todo" flow, which left inspect_ai#444 at Done on add; and
# only when unset or Done — never over a status a human moved. Adding is
# idempotent (an item already on the board comes back with its id). Returns
# 1 when the item could not be added (a token without the `project` scope);
# a failed status write is a warning, the add stood. The Status is written
# only on a SUCCESSFUL read (retried) that came back unset or Done: a failed
# read is not "unset", and treating it so would write Todo over a status a
# human chose — the read failing is a warning and the write is skipped.
atlas_todo() {
  local repo="$1" number="$2" node item cur
  local project=PVT_kwDOC7YMCM4BU68p status_field=PVTSSF_lADOC7YMCM4BU68pzhKizZM todo=f75ad846
  node=$(gh api "repos/$repo/issues/$number" --jq '.node_id') || return 1
  [ -n "$node" ] || return 1
  item=$(gh api graphql \
    -f query='mutation($p:ID!,$c:ID!){addProjectV2ItemById(input:{projectId:$p,contentId:$c}){item{id}}}' \
    -f p="$project" -f c="$node" --jq '.data.addProjectV2ItemById.item.id') || return 1
  [ -n "$item" ] || return 1
  if ! cur=$(retry_read 3 "Atlas Status read for $repo#$number" gh api graphql \
    -f query='query($i:ID!){node(id:$i){... on ProjectV2Item{s: fieldValueByName(name:"Status"){... on ProjectV2ItemFieldSingleSelectValue{name}}}}}' \
    -f i="$item" --jq '.data.node.s.name // ""'); then
    echo "::warning::land: $repo#$number is on Atlas but its Status could not be read; not set to Todo."
    return 0
  fi
  if [ -z "$cur" ] || [ "$cur" = "Done" ]; then
    if gh api graphql \
         -f query='mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){projectV2Item{id}}}' \
         -f p="$project" -f i="$item" -f f="$status_field" -f o="$todo" --silent; then
      echo "land: $repo#$number on Atlas, Status=Todo (was '${cur:-unset}')."
    else
      echo "::warning::land: $repo#$number is on Atlas but its Status could not be set to Todo."
    fi
  else
    echo "land: $repo#$number on Atlas, Status '$cur' left as is."
  fi
  return 0
}

# slack_post_file CHANNEL THREAD_TS FILE [TAIL_FILE] — post FILE's text with
# chat.postMessage, as a thread reply when THREAD_TS is non-empty. The token
# is read from $SLACK_TOKEN, never an argument (arguments show in `ps`). The
# body is built by jq from the files, so the text is data whatever it holds;
# it is capped below Slack's 40,000-character limit. TAIL_FILE (the land
# job's own `Tracking issue:` lines) is appended AFTER the cap is applied to
# FILE, so the agent's text is what gets cut and the tail always arrives.
# Slack answers 200 with `{"ok": false, "error": …}` on a refused post, so
# `ok` is what decides; returns 1 with the error on stderr. Meant to run
# under `retry`.
slack_post_file() {
  local channel="$1" thread_ts="$2" file="$3" tail_file="${4:-/dev/null}" body resp
  [ -n "${SLACK_TOKEN:-}" ] || { echo "slack_post_file: SLACK_TOKEN is empty" >&2; return 1; }
  body=$(jq -n --arg c "$channel" --arg t "$thread_ts" --rawfile text "$file" --rawfile tail "$tail_file" \
           '{channel: $c, text: (($text | .[0:(39000 - ($tail | length))]) + $tail)}
            + (if $t != "" then {thread_ts: $t} else {} end)') || return 1
  resp=$(curl -sS -X POST https://slack.com/api/chat.postMessage \
           -H "Authorization: Bearer $SLACK_TOKEN" \
           -H "Content-Type: application/json; charset=utf-8" \
           --data @- <<<"$body") || return 1
  if [ "$(jq -r '.ok // false' <<<"$resp" 2>/dev/null)" = "true" ]; then
    return 0
  fi
  echo "slack_post_file: Slack refused the post: $(jq -r '.error // "unparsable response"' <<<"$resp" 2>/dev/null || echo "unparsable response")" >&2
  return 1
}
