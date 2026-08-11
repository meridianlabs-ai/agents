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
bash <skill-base-dir>/checkout.sh <N>
```

One invocation does everything: dirty-tree guard, meridian-repo resolution
(NOT necessarily `origin` — fork clones point origin at upstream, whose issue
numbers are unrelated), a single GraphQL call for the chip + head/base refs,
the VS Code PR-extension branch config (written BEFORE the checkout — the
extension reads it on the HEAD-change event), and a submodule-recursion-free
fetch + `gh pr checkout`.

Chip selection: an OPEN same-repo chip wins; otherwise a single OPEN
cross-repo chip is checked out against its own repo — this covers External
proxies (the contributor's upstream PR) and promotions still open upstream.

Exit codes:

- **0** — prints `OK branch=… pr=… issue=#N (title)`; report that line, done
  (a `[cross-repo: …]` suffix means an upstream PR — relay its caution).
- **2** — dirty tree (files listed on stderr): STOP, show the user; never
  switch over uncommitted work.
- **3** — no usable open chip (stderr lists the chips it saw, including
  closed ones and ambiguous multiples): use the slow path below.
- **4** — repo unresolvable: no meridianlabs-ai remote and no gh default.

## Slow path (no chip, or only closed/cross-repo entries)

b. **Agent comments** — scan ALL `i-am-marvin` comments on the issue for
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
`gh pr checkout <M> -R "$REPO"`.

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
