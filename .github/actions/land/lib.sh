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

# open_or_adopt_pr REPO BRANCH BASE TITLE BODY_FILE — adopt the open PR
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
open_or_adopt_pr() {
  local repo="$1" branch="$2" base="$3" title="$4" body_file="$5" owner="${1%%/*}" list found skipped url
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
  url=$(gh pr create --repo "$repo" --head "$branch" --base "$base" \
          --title "$title" --body-file "$body_file") || return 1
  echo "opened ${url##*/} $url"
}

# landing_failure_hint FAILED PUSHED [WITHHELD [WORKFLOW_FILES]] — the
# one-line consequence
# the Report step adds under "Landing failed at step(s): FAILED" (a
# comma-separated list of step names, `post (…)` included): whether the
# agent's commits reached the branch, and which hand-back / verdict /
# hand-off / stage move a human now owes. WITHHELD is a second list, in the
# same format, of the planned `handback` / `verdict` / `handoff` / `stage`
# steps that never ran (skipped, not failed, because the PR step failed after
# the push — or, for the verdict, because the Post step lost a comment) — each owed just
# like a failed one, and named once even when it appears in both lists.
# WORKFLOW_FILES is the `workflows` step's list of the files under
# .github/workflows/ the bundle changes — agent-chosen paths, so they are
# de-fanged here; everything else is our own text, so the line names no live
# trigger token.
landing_failure_hint() {
  local failed="$1" pushed="$2" withheld="${3:-}" files="${4:-}" hint="" list owed
  # Report joins with ", "; match on step names with the spaces removed.
  list=",${failed// /},"
  owed="$list${withheld// /},"
  case "$list" in
    *,download,*|*,validate,*|*,plan,*)
      hint="The landing was refused before any write: the agent's commits were **not** pushed and nothing was posted." ;;
    *,workflows,*)
      hint="The agent's commits change workflow files ($(defang_str "$files")), which the machine account may not push (it has no Workflows permission); changes under \`.github/workflows/\` are made from a maintainer's machine. The commits were **not** pushed and are lost with the runner: there is no branch to look for." ;;
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
