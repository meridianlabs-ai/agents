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
# in step with the `<!-- … -->` comments the workflows read. Truncation only
# drops trailing bytes, so it cannot resurrect a trigger the sed removed.
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

# Comment on an issue or PR (the issues endpoint serves both) from a file.
post_comment_file() {
  local repo="$1" number="$2" file="$3"
  gh api "repos/$repo/issues/$number/comments" -F body=@"$file" --silent
}
