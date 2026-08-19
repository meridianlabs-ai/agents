---
name: promote
description: Promote a reviewed inspect_ai fork branch upstream — open the UKGovernmentBEIS PR with the fully-qualified Fixes ref, then do the tracking bookkeeping (Atlas Sign-off stage, Upstream PR field, issue comment, supersede the fork PR). Idempotent — also use it to heal the bookkeeping of an already-promoted issue.
---

# Promote fork work upstream

Promote work from `meridianlabs-ai/inspect_ai` (the fork) to
`UKGovernmentBEIS/inspect_ai` (upstream), per the tracking contract in
`meridianlabs-ai/agents` design/atlas-tracking.md → "The fork: promotion and
the terminal sync".

## Fast path (issue number in hand)

Run the script that lives next to this skill (substitute this skill's base
directory). Add `--dry-run` first if the user asked to preview:

```sh
bash <skill-base-dir>/promote.sh <N> [--dry-run]
```

One invocation does everything, each write check-before-write (idempotent —
rerunning heals an already-promoted issue): resolves the fork PR + branch
from the issue's chips; prints preflight ADVISORY lines (review verdict, fork
CI) — **relay these to the user, and pause for confirmation if the verdict
isn't `clean` or CI shows failures**; adopts the existing upstream PR via the
issue's cross-repo chip (the REST `pulls?head=` filter silently fails for
this org-fork pair — never use it) or creates it with the fully-qualified
`Fixes meridianlabs-ai/inspect_ai#N` (bare `#N` refs are rewritten — they
would rebind to upstream's tracker); assigns + requests review from
`dragonstyle` on open PRs; sets the board's `Upstream PR` field (the sync's
join key — the #90 lesson), stage → Sign-off + Status → In progress (never
on a CLOSED issue, never downgrading Sign-off/Merge); comments the upstream
link on the fork issue; supersedes and closes the open fork PR.

Exit codes: **0** ok (report the `OK …` line plus which steps were created
vs already present); **3** no fork-PR chip — resolve inputs via the slow
path below, then run the script anyway if a branch emerges (it only needs
the chip for resolution); **4** branch not on the fork; **5** preflight
hard failure.

## Slow path (no chip)

From an issue with no linked-PR chip: scan machine-account comments for
`/pull/` refs (take the still-open one). From a PR or branch: the issue
number comes from the branch name (`claude/issue-N-*`), else a same-repo
`Fixes` ref in the PR body. Human-named branches: get the branch from the
fork PR's head. Once resolved, prefer fixing the chip (run the
link-upstream-chips sweep) and re-running the script over hand-executing
its steps.

NEVER wait for GitHub to materialize a missing chip: on the fork, closing
refs (`Fixes #N`) are inert — GitHub only processes them for PRs based on
the default branch, and fork PRs base on `meridian` — so no amount of
editing the PR body or polling produces one (observed: a session polled
5 minutes for a chip that can never appear). Chips on the fork come ONLY
from the link-upstream-chips sweep (`scripts/link-upstream-chips` in the
agents checkout, or `/resolve-board`); run it, or resolve via the slow
path above and proceed.

The script itself runs that sweep as its last step (best-effort: an
expired browser login is reported, never fatal), so a normal promotion
leaves no chips pending — including the new upstream PR's own chip.

## Report

Upstream PR link, issue link, stage set, and which bookkeeping steps were
created vs already present (healed vs no-op). From here the hourly Atlas
sync owns the tail: approval → Merge, merge → Done (it closes the fork
issue), changes-requested → Review, re-request → Sign-off.

## Design decision: org-fork heads are deliberate

The fork's AGENTS.md prefers personal-fork promotion because GitHub grants
no maintainer-edits on org-fork PR heads. That concern doesn't apply to our
promotions: every upstream maintainer has write access to the meridianlabs
fork itself, so they can update or fix the PR branch directly (decision:
Ransom, 2026-08-12). Keep promoting from the org fork; the script's REST
`head_repo` creation is the required mechanism (GraphQL/`gh pr create`
cannot resolve org-fork heads at all).

## Cautions

- Never push to `main`/`meridian`; promotion only opens a PR from the
  existing branch.
- Upstream is not ours: no labels and no Meridian-internal markers on the
  upstream PR beyond the `Fixes` ref. The one exception is the `dragonstyle`
  assignee + review request (explicitly requested by Ransom).
- Do not merge anything — upstream merges are upstream's call; the fork
  issue closes via the sync when that happens.
