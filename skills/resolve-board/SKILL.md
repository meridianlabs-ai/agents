---
name: resolve-board
description: Bring the Atlas board current in one shot — dispatch the fork's hourly Atlas sync workflow now (external-PR discovery + upstream state sync) and run the local link-upstream-chips sweep (clickable issue↔PR chips). Use when the board looks stale or after a batch of agent/upstream activity.
---

# Resolve the Atlas board

Runs the two reconciliation mechanisms on demand instead of waiting for their
natural cadence (the sync runs hourly at :17; chips only land when the local
sweep runs). Both are idempotent — running this at any time is safe.

Locate the agents checkout first (this skill is a symlink into it):

```sh
AGENTS=$(dirname "$(dirname "$(realpath ~/.claude/skills/resolve-board)")")
```

## Step 1 — dispatch the Atlas sync and wait

```sh
gh workflow run atlas-sync.yml --repo meridianlabs-ai/inspect_ai
sleep 10
RID=$(gh run list --repo meridianlabs-ai/inspect_ai --workflow atlas-sync.yml \
        --limit 1 --json databaseId --jq '.[0].databaseId')
# poll to completion (typically < 2 min; timeout-minutes is 15)
gh run watch "$RID" --repo meridianlabs-ai/inspect_ai --exit-status || true
```

Pull its summary — the actions it took (stage moves, proxies created/healed,
skips) live in the run output:

```sh
gh run view "$RID" --repo meridianlabs-ai/inspect_ai --log 2>/dev/null \
  | sed -n '/=== Atlas sync summary ===/,/^$/p'
```

If the run FAILED at preflight, MARVIN_TOKEN has a permissions problem
(project scope) — surface that to the user rather than continuing.

## Step 2 — run the chip sweep

```sh
cd "$AGENTS/scripts/link-upstream-chips" && node index.mjs
```

- Links every pending chip: External proxies ↔ upstream PRs, and fork agent
  PRs (`claude/issue-N-*`) ↔ their issues (whose `Fixes` refs are inert on the
  fork — non-default base branch).
- If it exits with "Not signed in", the ~2-week browser session expired: run
  `node index.mjs --login`, let the user complete the GitHub sign-in in the
  window that opens, then rerun the sweep.
- `FAILED`/`WRONG LINK` lines need eyes — the script never clicks a
  non-matching result, so failures mean the Development-panel markup changed
  (`--headed` to debug) or a genuinely missing search result.

## Step 3 — report

One short summary combining both: sync actions taken (or "no changes"), chips
linked (or "none pending"), plus anything needing the user (expired login,
preflight failure, WRONG LINK).

## Notes

- The sync only manages issues assigned to `ransomr` (pilot scoping) and only
  those with an `Upstream PR` field for the promotion tail; promotion
  bookkeeping itself is the `promote` skill's job.
- Neither mechanism touches Agent Working / Human Review transitions driven by
  the agent workflows — this skill reconciles the upstream tail and the chips,
  not the agent lifecycle.
