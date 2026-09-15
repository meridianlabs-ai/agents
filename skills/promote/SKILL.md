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
directory). Add `--dry-run` if the user asked to preview; add `--pr <number>`
when the script reports more than one qualifying fork PR (exit 6) or when
the user names the PR:

```sh
bash <skill-base-dir>/promote.sh <N> [--dry-run] [--pr <number>]
```

`--dry-run` prints every candidate with its verdict, the decision
(`RESOLVED: fork PR #M (STATE) via <how>`) and each write that would happen;
nothing is written.

**Trust rule, applied before any PR text is read.** A fork PR qualifies only
when its head repository is `meridianlabs-ai/inspect_ai` itself AND its
author is in the script's `TRUSTED_LOGINS` (`i-am-marvin`; one variable at
the top of the script — Phase 2's GitHub App identity changes that one
value) or holds write access on the fork (admin/maintain/write via the
collaborator permission API; a failed lookup is untrusted). Anyone can open
a `Fixes #N` PR from a personal fork into the fork's default branch and
GitHub links it to the issue natively, so a chip alone proves nothing; a
branch inside the org fork can only be pushed by a write-access account,
which makes the head-repository check the load-bearing one and the author
check defence in depth. The picked PR's title, body and head branch become
the upstream PR opened as the user, which is why nothing is read from a
candidate until it has passed.

Resolution order, every candidate judged by the rule first: (1) the
qualifying OPEN fork-PR chip — exactly one, or exit 6; (2) with none, the
open fork PRs (`gh pr list --repo meridianlabs-ai/inspect_ai --state open`)
that pass the rule and either carry a closing reference to the issue in
their body (`Fixes|Closes|Resolves #N`, or fully qualified
`meridianlabs-ai/inspect_ai#N` — fork closing refs are inert, so the agent's
own PR usually has no chip) or whose head branch matches
`claude/issue-N-*` / `issue-N-*` — exactly one, or exit 6 (the listing must
succeed and be complete — under the script's `LIST_LIMIT` of 500 open PRs —
or the step aborts with exit 5 rather than falling through); (3) with none,
the qualifying CLOSED fork-PR chip (the heal path) — exactly one, or exit 6.
`--pr <number>` skips the search and names the fork PR (chip or not, open or
closed); it must still pass the rule, and the script warns when the pinned
PR carries no link to the issue.

One invocation does everything, each write check-before-write (idempotent —
rerunning heals an already-promoted issue): resolves the fork PR + branch
as above; preflight refuses a head branch named `main`/`meridian` and a
fork branch that has moved past the resolved PR's head (exit 5, before any
write — except when a CLOSED fork PR's upstream PR is adopted, where a
moved branch is normal and only noted); prints preflight ADVISORY lines
(review verdict, fork CI) — **relay these to the user, and pause for
confirmation if the verdict isn't `clean` or CI shows failures**. Only
verdict comments posted by the reviewer app (`claude[bot]`, the script's
`REVIEWER_BOT`) or by a trusted login / write-access collaborator count;
the PR is public, so others are ignored and the ADVISORY line says how
many were. If the comment lookup fails part-way the verdict is reported
`verdict:unavailable` (partial pages are discarded — an older `clean` must
not stand in for a newer verdict): treat it as not clean and pause. It
adopts the existing upstream PR via the
issue's cross-repo chip (the REST `pulls?head=` filter silently returns
[] for org-owned heads — observed on the org-fork pair AND same-repo on
the fork itself — never use it anywhere) or, on the create path, first syncs
the branch with upstream main (server-side merge via the fork-network merges
API — org-fork heads take no maintainer edits and these branches trail the
fork's main mirror, so a fresh promotion usually opens behind; a conflict,
commonly CHANGELOG, aborts with exit 5 BEFORE any upstream PR is opened, for
a human to resolve on the branch) and creates it with the fully-qualified
`Fixes meridianlabs-ai/inspect_ai#N` (bare `#N` refs are rewritten — they
would rebind to upstream's tracker), plus a bare `Fixes #<up>` when the
fork issue was imported from upstream (its `Upstream issue:` body line —
see the import skill; creation-time only, adopted PRs aren't edited); assigns +
requests review from `dragonstyle` on open PRs (the default — `REVIEWER=<login>`
overrides it, see Cautions); sets the board's `Upstream PR` field (the sync's
join key — the #90 lesson), stage → Sign-off + Status → In progress (never
on a CLOSED issue, never downgrading Sign-off/Merge); comments the upstream
link on the fork issue; supersedes and closes the open fork PR.

Exit codes: **0** ok (report the `OK …` line plus which steps were created
vs already present); **3** no qualifying fork PR — stderr says what it
looked for and lists every candidate
(`#M STATE head=<head repo>:<branch> author=<login>`) with its verdict,
`qualifies` or `REFUSED: <reason>` (head repository not the fork, or an
author neither trusted nor a write-access collaborator), plus the open PRs
that carried no reference to the issue. Relay the listing; resolve via the
slow path below and re-run with `--pr <number>`. **Never promote a REFUSED
candidate**, by `--pr` or by hand-executing the steps: its text would be
published upstream as the user. **4** branch not on the fork; **5** hard
failure before any write: the open-PR listing the fallback needs failed or
hit the limit (re-run, or pin with `--pr`); the resolved PR's head is
`main`/`meridian` (never promote it); the fork branch moved past the
resolved PR's head (re-run — the PR data was stale, or someone pushed); a
`REVIEWER` who is provably not a collaborator on upstream or on the ts-mono
companion's repo; or a conflict merging upstream main into the branch
(resolve on the branch and re-run). No upstream PR was opened in any of
these; **6** ambiguous — more
than one fork PR qualifies at the same step (stderr lists them): ask the
user which one and re-run with `--pr <number>`. Never guess — the
2026-08-27 incident (agents #32) promoted the wrong PR and closed a live one
as superseded.

## Slow path (exit 3: nothing qualified)

The script already tried the open fork PRs' closing refs and branch names,
so this is for the rest: scan machine-account comments on the issue for
`/pull/` refs (take the still-open one); for a human-named branch, find the
fork PR whose head is that branch. Once a fork PR number is in hand, re-run
the script with `--pr <number>` — it applies the trust rule to the pinned
PR too, so a candidate the listing REFUSED is refused again, by design.
Prefer that over hand-executing the script's steps.

Multiple chips are normal: an issue can carry its fork PR, a ts-mono
companion, and (after promotion) the upstream PR. Resolution filters by
repo, so extra chips never confuse it. A ts-mono companion (same branch
name, open) additionally gets the SAME reviewer assigned and
review-requested at promotion time — ts-mono has no promotion step of
its own, so this is where the viewer half enters human sign-off.

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
If the tail reports the sweep failed on an expired login, recover the
same way resolve-board does — it's the same interactive session: run
`node index.mjs --login` in `scripts/link-upstream-chips`, let the user
complete the GitHub sign-in in the window that opens (good for ~2
weeks), then re-run the sweep. The script deliberately never does this
itself: a promotion must not block on a browser sign-in.

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

- Never push to `main`/`meridian`. Promotion opens a PR from the existing
  branch, and on the create path writes a single merge commit onto that PR
  branch to sync it with upstream main (server-side, never a local checkout);
  it touches no other ref.
- Upstream is not ours: no labels and no Meridian-internal markers on the
  upstream PR beyond the `Fixes` ref. The one exception is the `dragonstyle`
  assignee + review request (explicitly requested by Ransom). Override the
  reviewer for one run with `REVIEWER=<login>` in the environment
  (`REVIEWER=<login> bash <skill-base-dir>/promote.sh <N>`); the default
  stays `dragonstyle`. The override covers the ts-mono companion too (both
  halves deliberately get the same reviewer). Preflight checks an OVERRIDDEN
  login against upstream (the default is known-good and skipped there) and
  any reviewer against ts-mono when a companion exists (exit 5 on a 404,
  before any write). The upstream collaborator lookup needs push access, so
  a token that cannot see it gets a `WARN: could not verify` line and the
  run continues as before — only a 404 is treated as a bad login. The login
  is case-insensitive (lower-cased once; the idempotency checks compare
  case-insensitively too). The script prints the effective reviewer on its
  `ADVISORY:` line.
- Do not merge anything — upstream merges are upstream's call; the fork
  issue closes via the sync when that happens.
