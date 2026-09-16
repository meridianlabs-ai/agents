---
name: checkout
description: Check out the PR branch for an issue — /checkout <issue-number> finds the issue's PR (linked-PR chip fast path, then agent comments, then the claude/issue-N-* branch convention) and checks its branch out in the current repo clone.
---

# Check out an issue's PR branch

Given an issue number (`/checkout 97`), find the right PR for that issue and
check out its branch. This is a mechanical skill: the common case is ONE
command — run it without narration and report its `OK` line at the end.

## Fast path (usual case: issue has a linked-PR chip)

Run the script that lives next to this skill (substitute this skill's base
directory, which the invocation message provides):

```sh
bash <skill-base-dir>/checkout.sh <N> [--dry-run]
```

`--dry-run` prints every candidate with its verdict and the decision, then
exits before any write (no branch config, no fetch, no checkout) — use it
when the user asks to preview, or to see why a candidate was refused.

One invocation does everything: dirty-tree guard, meridian-repo resolution
(NOT necessarily `origin` — fork clones point origin at upstream, whose issue
numbers are unrelated), a single GraphQL call for the chip + head/base refs,
the VS Code PR-extension branch config (written BEFORE the checkout — the
extension reads it on the HEAD-change event), a submodule-recursion-free
fetch + `gh pr checkout`, and a `git submodule update` so submodule working
trees match the new branch (no spurious `M` in status). If an open ts-mono
companion PR exists for the same branch name (the agent convention for
viewer work — fork branches and External contributors' upstream branches
alike; only agent-authored ts-mono PRs count, so generic contributor
branch names can't false-match), the submodule is switched onto that
branch — the parent gitlink intentionally shows modified until the
merge-time pointer bump.

**Trust rule, applied before any PR text is read.** A linked PR (chip)
qualifies only when its head repository is the org repo itself AND its
author is in the script's `TRUSTED_LOGINS` (`i-am-marvin`, and its Phase 2
GitHub App login `meridian-marvin[bot]`; one variable at the top of the
script, compared after normalising GraphQL's bare Bot logins and `gh`'s
`app/` prefix to the REST form) or holds write access there (admin/maintain/write via the
collaborator permission API; a failed lookup is untrusted). GitHub creates a
chip natively for any `Fixes #N` PR into the default branch, from anyone's
personal fork, so the chip alone proves nothing; a branch inside the org
repo can only be pushed by a write-access account, which makes the
head-repository check the load-bearing one and the author check defence in
depth. Title, body and branch name are consulted only after the rule passed.

Chip selection: the highest-numbered qualifying OPEN same-repo chip wins;
otherwise a single qualifying OPEN cross-repo chip is checked out against
its own repo. A cross-repo chip qualifies as a promotion (its head is the
org repo, trusted author) or, on a genuine External proxy, as the
contributor's upstream PR under `UKGovernmentBEIS/inspect_ai`. A genuine
proxy is an issue authored by a login in `TRUSTED_LOGINS` (the sync writes
them as marvin) AND labelled `External` — the label alone is not enough,
anyone can file an issue. With no chip at all, the script falls back to the
proxy body's machine-written `Upstream PR:` line before giving up — chips
only exist after the browser sweep runs, so fresh proxies often lack one —
but only on a genuine proxy and only for a URL under
`UKGovernmentBEIS/inspect_ai`; on any other issue that line is free text
and is refused with the reason.

Exit codes:

- **0** — prints `OK branch=… pr=… issue=#N (title)`; report that line, done
  (a `[cross-repo: …]` suffix means an upstream PR — relay its caution).
- **2** — dirty tree (files listed on stderr): STOP, show the user; never
  switch over uncommitted work.
- **3** — no qualifying open chip. stderr lists every candidate it saw
  (`#M STATE repo head=<head repo>:<branch> author=<login>`) with its
  verdict: `qualifies`, `not open`, or `REFUSED: <reason>` — a head
  repository that is not the org repo, an author who is neither trusted nor
  a write-access collaborator, a body line on an issue that is not an
  External proxy, or a body URL outside `UKGovernmentBEIS/inspect_ai`.
  Relay that listing to the user, then use the slow path below to find the
  agent's own PR. **Never check out a REFUSED candidate through the slow
  path**: it is someone else's branch presented as the issue's PR. If the
  user wants it anyway, they must say so explicitly, naming the author and
  head repository the listing showed.
- **4** — repo unresolvable: no meridianlabs-ai remote and no gh default.

## Slow path (no qualifying chip, or only closed/cross-repo entries)

The same rule applies to anything found here: before writing branch config
or running `gh pr checkout`, confirm the PR's head repository is the org
repo and its author is trusted or a write-access collaborator (the script's
listing already shows both for chips; for other candidates read them with
`gh pr view <M> -R "$REPO" --json author,headRepositoryOwner,headRepository`).

b. **Agent comments** — scan ALL machine-account comments (`i-am-marvin`, or
   `meridian-marvin[bot]` under Phase 2) on the issue for
   `/pull/<M>` refs (not just the last comment; superseded PRs sit next to
   live ones). Keep the open one, newest if several.

c. **Branch convention** — match PR heads against `claude/issue-<N>-*`:

   ```sh
   gh api "repos/$REPO/pulls?state=all&per_page=100" \
     --jq '.[] | select(.head.ref|test("^claude/issue-<N>-")) | {number, state, head:.head.ref}'
   ```

If candidates disagree: open chip > open comment-ref > open branch-match >
most recently updated. Say which rule matched. Once a PR number is in hand,
finish with the script's tail by hand: write both branch configs BEFORE the
checkout (`branch.<B>.github-pr-owner-number` = `owner#repo#M`,
`branch.<B>.vscode-merge-base` = `<base-remote>/<baseRefName>`), then
`gh pr checkout <M> -R "$REPO"`, then `git submodule update --init --quiet`
so submodule working trees match the new branch.

## Fallbacks — tell the user instead of guessing

- **No PR but a branch exists** (a "Claude finished" comment names a
  `claude/issue-N-*` branch never turned into a PR): fetch + switch to it from
  the meridian remote; say there's no PR. Still set `vscode-merge-base`
  before the switch (with no PR to read the base from, use the branch's fork
  point — `meridian` on the fork, the default branch elsewhere).
- **Only a MERGED/CLOSED cross-repo chip** (the script handles open ones):
  the work was promoted and already landed upstream. Say so; offer the
  branch only if the user still wants it.
- **Nothing found**: list what was scanned so the user can point at the right
  thing.
