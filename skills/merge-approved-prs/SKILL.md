---
name: merge-approved-prs
description: Merge approved upstream inspect_ai PRs from the Atlas board's Merge stage, one at a time — resolve conflicts against main, guard CHANGELOG/submodule invariants, coordinate companion ts-mono PRs and submodule pointer bumps when the viewer schema changed, watch CI, merge, and clean up the board. Handles both promotions (our branches) and approved External contributor PRs (their fork branches; maintainerCanModify is the prereq — skip and report those without it). Items whose issue carries hold:release are skipped unless holds are explicitly included (the post-release sweep). Use when Ransom says he's ready to merge approved PRs / clear the Merge queue.
---

# Merge approved inspect_ai PRs (Merge queue)

Merge the approved upstream PRs linked from Atlas-board issues in the
**Merge** stage, strictly one at a time — they usually conflict with
each other, so each must land before the next is rebased.

**Workspace: a throwaway worktree of `~/git/inspect_ai`**, so queue work
never collides with in-progress work in the main checkout or leftovers from
a previous queue run (a stale checked-out branch once absorbed a stray
`git merge origin/main` meant for the next queue item). Remotes in that
clone: `origin` = UKGovernmentBEIS/inspect_ai (upstream, where PRs merge),
`meridian` = the fork (where PR branches live and are pushed).

```bash
cd ~/git/inspect_ai && git fetch origin main
git worktree add --detach <scratch>/merge-queue origin/main
cd <scratch>/merge-queue
```

Worktree rules (learned the hard way):
- NEVER run `git submodule update --init` inside the worktree — git's
  worktree+submodule handling writes a broken `.git` pointer file that then
  poisons every later command. Run fetch/checkout with submodule recursion
  off instead: `git fetch --no-recurse-submodules` and
  `git -c submodule.recurse=false checkout …` (the External checkout block
  below already does). The queue never needs submodule contents
  (the gitlink invariant is checked via `git diff`, not the worktree).
- A branch already checked out in another worktree (e.g. the user has it
  open in the main clone) can't be checked out again — coordinate rather
  than force.
- `git worktree remove` it in cleanup.
- EXCEPTION — "PRs that need a ts-mono change" (below) require real
  submodule contents and pnpm builds: do those in a primary clone
  (historically `~/git/viewer`, remotes `origin`/`meridianlabs-ai`), not a
  worktree.

## 1. Find the queue

```bash
gh project item-list 1 --owner meridianlabs-ai --format json --limit 1000 \
  | jq -r '.items[] | select(.stage == "Merge")
      | [(.content.number|tostring), .repository, .title,
         ((.["linked pull requests"] // []) | join(","))] | @tsv'
```

- The stage lives in the `stage` field, **not** `status`. Always use
  `--limit 1000` — the default 30 and even 200 truncate the board silently.
- **`hold:release` gate**: check each queue issue's labels
  (`gh issue view <n> --repo meridianlabs-ai/inspect_ai --json labels`) and
  SKIP any carrying `hold:release` — approved but deliberately parked until a
  stable release point (design/atlas-tracking.md → flags). List the skips in
  the report. EXCEPTION: when the user explicitly says to include holds
  ("merge the holds too", "post-release sweep"), process them like the rest —
  and remove the `hold:release` label from each issue after its PR merges.
- **External items**: the same labels call reveals the `External` label —
  these are contributor PRs the sync queued on approval. Their linked-PR chip
  is usually absent (it can't be scripted for cross-org PRs), so take the
  upstream PR URL from the proxy issue body's `Upstream PR:` line instead.
  Then the prereq: `gh pr view <n> --repo UKGovernmentBEIS/inspect_ai --json
  maintainerCanModify` must be `true` — if not, SKIP it (leave it queued),
  and report it with the remedy: ask the contributor to enable "Allow edits
  by maintainers", or merge manually. Process externals per the "External
  PRs" section below — nothing from an External tree runs in this session.
- Confirm each upstream PR: `state=OPEN`, `reviewDecision=APPROVED`, note
  `mergeable` (usually `CONFLICTING`).
- **Bind the approval to the head commit** — every PR, promotions and
  externals alike, BEFORE anything of it is checked out or has `main`
  merged into it (the script lives next to this skill; substitute its base
  directory):
  ```bash
  OUT=$(python3 <skill-base-dir>/approval_at_head.py https://github.com/UKGovernmentBEIS/inspect_ai/pull/<n>); RC=$?; echo "$OUT"
  APPROVED=$(printf '%s\n' "$OUT" | awk '$1 == "approved" { print $2 }')   # the approved SHA; empty unless RC is 0
  ```
  Exit 0 prints `approved <sha> by <login> at <time>`: a reviewer with write
  access approved the PR's CURRENT `headRefOid` and has not since requested
  changes or been dismissed. Any other exit prints the reason (`approval is
  for <sha>, head is <sha2>; N commits pushed after <time>`, `no approval
  for head <sha>`, `head moved during the check: was <sha>, now <sha2>`, or
  a `gh` error) and means **SKIP**: leave the item
  queued, report that line verbatim, and do not check the branch out, merge
  `origin/main` into it, run anything from its tree, or arm auto-merge.
  NEVER re-approve to get past it — the head moved after the review, so it
  is unreviewed code, and `reviewDecision` alone cannot tell you that (it is
  PR-level and survives a push unless upstream dismisses stale approvals,
  which this skill does not assume). The check is read-only; the one push
  this skill makes to a PR branch, the `origin/main` merge commit, happens
  later and only after this has passed (see "Your own push" under External
  PRs). Keep `APPROVED`: section 2 checks out that literal commit, and the
  merge request is pinned to the commit you push.

## 2. Per PR, in order (repeat from here after each merge)

Re-run the approval-at-head check (section 1) for THIS PR now, immediately
before its checkout, and set `APPROVED` from its line: a queue run takes
hours, and an approval that bound when you listed the queue may not bind by
the time you reach the item. Skip and report on a non-zero exit exactly as
above. Then check out the approved commit ITSELF, never the branch tip: the
branch can move between the check and the checkout, and `git checkout
meridian/<branch>` (or `gh pr checkout`) would silently follow it — so the
sequence verifies the fetched tip against `APPROVED` and checks out that
literal SHA. `BRANCH` is `headRefName` from the PR JSON.

```bash
git fetch origin main
git fetch meridian "$BRANCH"
test "$(git rev-parse "meridian/$BRANCH")" = "$APPROVED"   # non-zero: the head moved since the check — SKIP, report both SHAs
git checkout -B "$BRANCH" "$APPROVED"                        # the literal approved commit, never the branch tip
git merge origin/main
```

(tests/test_approval_at_head.py lifts this block and runs it against local
repos: a moved tip must stop it before anything is checked out.)

Take `BRANCH` from the PR JSON already in hand (`headRefName`) — NEVER
type it from memory: a guessed branch name once failed the checkout and the
follow-on `git merge origin/main` landed on whatever branch was current.
Same rule for chained commands: don't pipe state-changing git commands
through `| tail`/`| head` inside `&&` chains — the pipe's exit status masks
the failure (run them bare; inspect output separately).

### Conflict resolution invariants

- **CHANGELOG.md** (conflicts almost every time): keep origin/main's released
  sections intact; the PR's entries belong under `## Unreleased` at the top
  (create the section if missing — upstream releases frequently, so it's often
  gone). Then verify **every** branch entry mechanically — entries relocate
  under released headings *silently*, including via clean auto-merges:
  ```bash
  git diff "$(git merge-base origin/main HEAD)" HEAD -- CHANGELOG.md | grep '^+- '
  ```
  For each added line, confirm its section is `## Unreleased` (awk trick:
  `awk '/^## /{sec=$0} /<entry text>/{print sec}' CHANGELOG.md`). Check this
  even when CHANGELOG didn't conflict.
- **Submodule gitlink**: after the merge,
  `git diff --cached origin/main -- src/inspect_ai/_view/ts-mono` must be
  empty (branch carries no net submodule change). If not, restore:
  `git checkout origin/main -- src/inspect_ai/_view/ts-mono`.
  **Exception**: a PR that changes the viewer type schema needs a deliberate
  pointer bump — see "PRs that need a ts-mono change" below.
- **Code conflicts** (common once earlier queue PRs land in main): before
  resolving, inspect what main changed since divergence —
  `git log/diff "$(git merge-base HEAD origin/main)"..origin/main -- <file>` —
  and make sure refactors main applied to code this PR deletes are already
  present in the surviving replacement (e.g. main refactored `run_multiple`
  and its successor identically; deleting `run_multiple` was safe). Then grep
  the whole tree for stale references to anything deleted (docstrings too).

Commit the merge (Co-Authored-By trailer). **Promotions only**: if a code
conflict was involved, sanity-check locally before pushing: `ruff check` +
`ruff format --check` on touched files, `mypy <touched files>`, and any
targeted tests that cover the conflicted area. Pure CHANGELOG/docs conflicts
can go straight to CI. For an External PR run NOTHING from the tree here,
conflict or not: its checks ran in upstream CI on the approved commit
(verified before the checkout) and run again on the merge commit you push —
see "External PRs".

- **Give the worktree its own venv and test from it** (promotions): `uv sync --frozen`
  in the worktree (about three seconds from a warm uv cache; `.venv` is
  gitignored), then `.venv/bin/pytest`, `.venv/bin/ruff`, `.venv/bin/mypy`.
  Rerun the sync after the merge when it touched `uv.lock`. Never borrow the
  primary clone's venv, with or without `PYTHONPATH=$PWD/src`: its editable
  install points at the PRIMARY clone's `src/`, so pytest silently tests the
  wrong tree (observed: a green run that hadn't exercised the merge at all,
  caught only when a branch-side import didn't exist in the main clone), and
  its dependencies are main's, not the branch's. Verify once per session:
  `.venv/bin/python -c 'import inspect_ai; print(inspect_ai.__file__)'`
  must print the worktree path. (decision: Ransom, 2026-09-15)
- **A merged branch may owe more than textual resolution**: when main has
  established a new cross-cutting contract (e.g. mutation verbs carry
  `--terse` with piped-output default; human output goes through the `_echo`
  sanitizer wrappers, enforced by a meta-test), a branch that ADDS a new
  command or path in that family must honor the contract even where git
  reports no conflict — thread the new flags/wrappers through the branch's
  additions and pin pre-contract tests (`--no-terse`) the way main's own
  tests were adapted.

### Push, arm auto-merge, watch

```bash
git push meridian "$BRANCH"   # externals: plain `git push` (contributor fork)
gh pr merge <n> --repo UKGovernmentBEIS/inspect_ai --auto --squash --match-head-commit "$(git rev-parse HEAD)"
```

`--match-head-commit` pins the merge request to the commit you just pushed
(gh sends it as `expectedHeadOid` on the auto-merge mutation as well as on
a direct merge): if the PR head is anything else when the request lands,
GitHub refuses it instead of arming whatever is there. A refusal means
someone moved the head in the window between your push and the arming —
skip and report; never retry without the flag or with a SHA you did not
push. A push rejected as non-fast-forward is the same event one step
earlier: the head moved, so never pull the new commits in and retry.

Arm auto-merge (squash — repo history uses it) **per PR as you reach it,
never on the whole queue up front**: the later PRs' green CI is against
stale main, and arming them all can land semantically-conflicting merges
concurrently.

Then watch in a background monitor (poll ~60s) for FOUR terminal
conditions — the last two are silent stalls that a merged-or-failed watch
never fires on (observed: checks green, auto-merge armed, PR sat `BEHIND`
until someone happened to look):

- **MERGED** → confirm, fetch main, next PR.
- **any check failed** → investigate and fix.
- **checks green but `mergeStateStatus: BEHIND`** → main moved during CI and
  branch protection wants branches current, so auto-merge waits forever:
  merge `origin/main` again, RE-VERIFY the CHANGELOG/submodule invariants
  (every merge re-rolls the relocation dice — clean auto-merges relocated an
  entry into a released section twice in one day), push. Each loop costs one
  more CI round; expect several on a busy release day.
- **checks green but `mergeStateStatus: DIRTY`** → main now genuinely
  conflicts; resolve per the invariants above.
- **checks green but `autoMergeRequest` is null** → auto-merge was disarmed:
  GitHub drops it when someone without write access pushes to the PR (an
  external contributor, after you armed it). Do NOT re-arm — the head is no
  longer the commit you pushed; re-run `approval_at_head.py` (it fails for
  it), skip and report.
- **runs stuck in `action_required`** → the fork-PR workflow-approval gate:
  first-time contributors need a maintainer "Approve and run" on EVERY push,
  including ours (repeat externals don't hit this). Detect via
  `gh api "repos/<upstream>/actions/runs?head_sha=<sha>"` and approve with
  `gh api -X POST .../actions/runs/<id>/approve` — but only for the exact
  sha WE pushed; never blind-approve a head someone else moved.
- Watch-script pitfalls (both produced silent hour-long stalls): `gh pr
  checks` prints "no checks reported" on STDERR — capture `2>&1` or the
  gated state looks like an empty happy loop; and never hand-extend an
  abbreviated push sha into a query — `head_sha=` with a fabricated tail
  matches nothing and the poll spins forever. Resolve with `git rev-parse`.

**"Pull request was already merged" is success** — auto-merge fired the
moment checks passed. Always confirm with
`gh pr view <n> --json state,mergedAt` → `state=MERGED`.

Then `git fetch origin main` and start the next PR — it now conflicts with
what just landed.

## External PRs (contributor-owned branches)

Same flow as above with these substitutions — the branch lives on the
*contributor's* fork, not meridianlabs-ai:

- **Nothing from the tree runs in this session.** Every step of the flow
  above that executes code from the checked-out tree is skipped for an
  External PR: `uv sync --frozen` in the worktree (an editable install runs
  the tree's build backend) and the `.venv/bin/python -c 'import inspect_ai…'`
  probe, `ruff check` / `ruff format --check`, `mypy` (it imports whatever
  plugins the tree's `pyproject.toml` names), the targeted `pytest`, and for
  viewer-schema PRs `python src/inspect_ai/_view/schema.py`
  (which also runs `types:generate` in the submodule) and `python
  .github/scripts/check_openapi_drift.py`. The tree is a contributor's: the
  review approved its content, not its execution next to your `gh` login and
  home directory (finding 4122327; the CI external-review path runs the same
  class of code only inside a sandbox). Upstream CI is the substitute.
  Immediately after the approval-at-head re-check of section 2 and BEFORE
  the checkout below, require it to have run and passed on the approved
  commit:
  ```bash
  OUT=$(python3 <skill-base-dir>/checks_at_head.py https://github.com/UKGovernmentBEIS/inspect_ai/pull/<n> --sha "$APPROVED"); RC=$?; echo "$OUT"
  ```
  Exit 0 prints `checks passed at <sha>: N check runs, M statuses; required
  (K): <names>`: every check run on `$APPROVED` completed with `success`,
  `skipped` or `neutral` (GitHub's own passing conclusions for a required
  check), every commit status is `success`, and each status check upstream's
  `main` ruleset requires has reported. Any other exit prints the reason
  (`checks at <sha> not green: missing required: …; pending: …; failed: …`,
  `head is <sha>, not the approved <sha2>`, or a `gh` error) and means
  **SKIP**: leave the item queued and report that line verbatim. One remedy
  is yours: when the missing or pending checks are workflow runs awaiting
  "Approve and run" for `$APPROVED` (`gh api
  "repos/UKGovernmentBEIS/inspect_ai/actions/runs?head_sha=$APPROVED"`,
  `status: action_required` — the first-time-contributor gate), approve
  them — that commit is the reviewed one and CI is upstream's sandbox — wait,
  and re-run the check; never approve a run for any other SHA. After the
  push of your `origin/main` merge commit, watch CI on the pushed head as
  usual — the only content it adds is main's. A red check there is fixed by
  editing what your conflict resolution broke and letting CI verify, or it
  goes back to the contributor (below); never reproduce it locally.
- **Checkout/push**: instead of the fetch/checkout lines of section 2,
  fetch the PR head WITHOUT checking it out, refuse it unless it is the
  approved commit, and only then check that commit out — wiring the branch
  to the contributor's fork the way `gh pr checkout` would have:
  ```bash
  git fetch --no-tags --no-recurse-submodules origin "refs/pull/<n>/head"   # the PR head, fetched but NOT checked out: nothing from its tree runs
  test "$(git rev-parse FETCH_HEAD)" = "$APPROVED"                  # non-zero: the contributor pushed since the check — SKIP, report both SHAs
  git -c submodule.recurse=false checkout -B "$BRANCH" "$APPROVED"  # the literal approved commit
  git config "branch.$BRANCH.remote" "$FORK_URL"                    # `git push` goes to the contributor's fork, as after `gh pr checkout`
  git config "branch.$BRANCH.pushRemote" "$FORK_URL"
  git config "branch.$BRANCH.merge" "refs/heads/$BRANCH"
  ```
  `BRANCH` is `headRefName` and `FORK_URL` is
  `https://github.com/<headRepositoryOwner.login>/<headRepository.name>.git`,
  both from `gh pr view <n> --repo UKGovernmentBEIS/inspect_ai --json
  headRefName,headRepositoryOwner,headRepository,maintainerCanModify`. Not
  `gh pr checkout`: it checks out whatever `refs/pull/<n>/head` points at
  right now and takes no SHA, and a checkout is not inert — in a clone whose
  `core.hooksPath` points into the tree, a contributor's `post-checkout`
  hook runs during the checkout, before any comparison could refuse it.
  Fetching materializes nothing; the only checkout is of the approved
  commit. (tests/test_approval_at_head.py lifts this block too: a moved head
  carrying such a hook is refused without the hook ever running.) With
  `maintainerCanModify` that wiring makes a plain `git push` land on their
  branch after `git merge origin/main` (verify with `git push --dry-run` the
  first time). Never rebase or force-push a contributor branch — merge
  commits only; their local clone must stay fast-forwardable.
- **Your own push can dismiss the approval** (repo setting–dependent). This
  is the `origin/main` merge commit you push AFTER the approval-at-head
  check passed — the only content it adds is main's — so it is the one push
  you may re-approve: re-check `reviewDecision` after pushing, and if it
  dropped, approve only when the PR's `headRefOid` is exactly the commit you
  pushed — and name that commit in the review, so the approval binds to it
  rather than to whatever the head is when the request lands:
  ```bash
  test "$(gh pr view <n> --repo UKGovernmentBEIS/inspect_ai --json headRefOid --jq .headRefOid)" = "$(git rev-parse HEAD)"
  gh api -X POST repos/UKGovernmentBEIS/inspect_ai/pulls/<n>/reviews -f event=APPROVE -f commit_id="$(git rev-parse HEAD)" \
    -f body="Re-approving the merge queue's origin/main merge commit; the reviewed content is $APPROVED."
  ```
  (Pushing to someone else's PR doesn't make you its author.) A head that is
  anything else moved under you: the contributor pushed — re-run
  `approval_at_head.py` (it will fail), skip and report. The same applies
  when your plain `git push` is rejected as non-fast-forward: that rejection
  IS the contributor's push, so never pull their new commits in and retry.
  If branch protection requires approval of the most recent push by someone
  else, surface that in the report instead of looping.
- **Invariants are unchanged** (CHANGELOG entries under `## Unreleased`, no
  net submodule change) — but they were *reviewed*, not authored, by us, so
  check them even more mechanically. A violation that needs real rework goes
  back to the contributor: comment on the upstream PR, move the proxy to
  Contributor, and skip — don't rewrite their PR beyond conflict resolution.
- **Viewer schema / ts-mono**: an External PR that needs the "PRs that need
  a ts-mono change" sequence — `checks_at_head.py` names
  `check-schema-and-types` (or `dist-validation`, `submodule-on-main`), or
  the check goes red after your push — is **skip-and-report**, not done
  here: step 1 of that sequence is `schema.py`, which imports and runs the
  contributor's Python. Report `<PR> needs a schema regeneration and ts-mono
  companion; do it by hand or in a sandbox, not in the queue session`. (An
  in-session container was considered and rejected: it needs the Python
  deps, pnpm, the submodule and a copy-back step — not a short reviewable
  block; decision recorded in the PR that added this bullet.) An external
  contributor can't author a companion in meridianlabs-ai/ts-mono anyway,
  so that hand-done regeneration is also where you author it.
- **Cleanup**: there is no fork review PR to close; the hourly sync closes
  the proxy on merge as usual (external proxies always carry the
  `Upstream PR` field, its join key).

## PRs that need a ts-mono change

**Promotions only** — for an External PR this whole section is skip-and-report
(see "External PRs" → Viewer schema / ts-mono).

**Recognize it**: the PR touches `src/inspect_ai/_view/inspect-openapi.json`
(or the Pydantic models feeding it) and its `check-schema-and-types` check is
failing. A companion PR usually already exists in **meridianlabs-ai/ts-mono**
(the inspect_ai PR/issue or the failing check's diff will reference it; also
`gh pr list --repo meridianlabs-ai/ts-mono` and search branch names).

**Why the ordering is forced** — three jobs in the "Validate Embedded Viewer"
workflow (`.github/workflows/log_viewer.yml`):

- `check-schema-and-types`: the submodule's
  `packages/inspect-common/src/types/generated.ts` (at the pinned commit) must
  byte-match `pnpm --filter @tsmono/inspect-common types:generate` run against
  the committed `inspect-openapi.json`; and the schema must match the Python
  source modulo docstring-only drift (`python .github/scripts/check_openapi_drift.py`).
- `submodule-on-main`: the pointer must be an **ancestor of ts-mono main** —
  you cannot point at a branch commit, so the ts-mono PR merges *first*.
- `dist-validation`: checked-in `src/inspect_ai/_view/dist` must match
  `pnpm --filter @meridianlabs/log-viewer build` at the pinned commit. Bumping
  the pointer picks up every viewer change on ts-mono main since the last
  bump, so the bump commit almost always needs a rebuilt `dist/` too.

**Sequence** (submodule remote: `origin` = meridianlabs-ai/ts-mono):

1. **Sync the inspect_ai branch with origin/main first** — the final schema
   depends on the merged Python. Then regenerate and check:
   `python src/inspect_ai/_view/schema.py` +
   `python .github/scripts/check_openapi_drift.py`. Commit
   `inspect-openapi.json` if drift is structural. (Seen in practice: the PR's
   committed schema was stale against *its own* Python — a PR-authored
   `Literal` had been extended — so don't assume the branch's schema is
   current just because its CI once passed the drift step.) Push; other CI
   starts churning while the viewer checks stay red — expected.
2. **Verify the companion, then update it.** The companion's own review
   state, not the board stage that queued the item, is what lets it merge
   (finding 4121986) — check it BEFORE any git operation in the submodule
   (the script lives next to this skill):
   ```bash
   OUT=$(python3 <skill-base-dir>/companion_mergeable.py https://github.com/meridianlabs-ai/ts-mono/pull/<n>); RC=$?; echo "$OUT"
   COMPANION_HEAD=$(printf '%s\n' "$OUT" | awk '$1 == "approved" || $1 == "regenerate-only" { sub(":$", "", $2); print $2 }')   # the verified SHA; empty unless RC is 0
   ```
   Exit 0 prints `approved <sha> by <login> at <time>` (a reviewer with write
   access on ts-mono approved the companion's CURRENT head and has not since
   changed their verdict — approval_at_head.py's rule) or `regenerate-only
   <sha>: <files> by <author>` (a trusted author's same-repo PR against
   `main` whose diff modifies nothing but
   `packages/inspect-common/src/types/generated.ts` — exactly what
   `types:generate` writes; precedent #427). Any other exit prints why both
   rules failed and means **SKIP the whole item**: the inspect_ai PR cannot
   land without its companion, and the skill never approves a companion to
   get past this. A hand-written companion — the barrel re-exports in
   `index.ts` count as hand-written — needs a ts-mono approval at its head
   first: ask for one, report, move on.
   Then: `schema.py` also regenerated `generated.ts` in the submodule
   working tree — **copy it aside before any git operations in the
   submodule**. In the submodule: `git fetch origin`, refuse a moved head
   (`test "$(git rev-parse origin/<branch>)" = "$COMPANION_HEAD"` — SKIP on
   mismatch), `git checkout -B <branch> "$COMPANION_HEAD"`, `git merge
   origin/main`, restore the regenerated `generated.ts`, commit and push
   ONLY if there is something to commit (ts-mono's required checks are not
   strict, so an unchanged companion need not have main merged in), and wait
   for its CI. A push moves the head: a regenerate-only companion passes
   step 3 again on its own, but a hand-written one now needs a ts-mono
   approval of the pushed head — the skill never supplies it. Ask for one,
   report, and skip the item if it does not come.
3. **Merge the companion**, re-verified on the head as it is NOW — a push
   in step 2, or an approval withdrawn while CI ran, must be seen here, so
   the helper runs again and the merge is pinned to the SHA it returns.
   ts-mono main is squash-only:
   ```bash
   OUT=$(python3 <skill-base-dir>/companion_mergeable.py https://github.com/meridianlabs-ai/ts-mono/pull/<n>); RC=$?; echo "$OUT"
   COMPANION_HEAD=$(printf '%s\n' "$OUT" | awk '$1 == "approved" || $1 == "regenerate-only" { sub(":$", "", $2); print $2 }')   # empty unless RC is 0
   [ "$RC" -eq 0 ] && [ -n "$COMPANION_HEAD" ] && gh pr merge <n> --repo meridianlabs-ai/ts-mono --squash --match-head-commit "$COMPANION_HEAD"   # a non-zero RC: SKIP the whole item, report the line
   ```
   `--match-head-commit` makes GitHub refuse the merge if the head is no
   longer the commit the helper verified. (The old rule "regenerate-style
   companions merge without human review,
   precedent #427, #439" is now the helper's regenerate-only rule, checked
   against the diff; #439's `index.ts` edit would need an approval today.)
   **Then immediately re-check the tracking issue** — companions are
   Development-panel-linked to it (that link IS the board's PR pill), and
   GitHub treats every panel-linked PR as a closer, so the companion's merge
   auto-closes the issue days before the inspect_ai PR lands (no closing
   keywords involved; issue #251, 2026-08-26). If
   `gh issue view <issue> --repo meridianlabs-ai/inspect_ai --json state` says
   CLOSED, reopen it with a comment saying the companion merge closed it
   early, and restore the board `Status` field to "In progress" (Stage is
   untouched):
   ```bash
   gh project item-edit --id <ITEM_ID> --project-id PVT_kwDOC7YMCM4BU68p \
     --field-id PVTSSF_lADOC7YMCM4BU68pzhKizZM --single-select-option-id 47fc9ee4
   ```
   (Status field and option ids as in atlas_sync.py; fetch the item id one
   at a time as in the cleanup section below.) The Atlas
   sync also self-heals this on its next run, but fixing it inline keeps
   the board honest while the queue is still working the PR.
4. **Bump pointer + rebuild dist in one commit**: in the submodule,
   `git fetch origin main && git checkout <squash-sha>`; verify
   `types:generate` is now a no-op; `pnpm install --frozen-lockfile` and
   `pnpm --filter @meridianlabs/log-viewer build`. In the parent, commit the
   gitlink and the modified `dist/` files together, push.
5. Resume the normal push/CI/merge flow above. Upstream main often moves
   during all this (`mergeStateStatus: BEHIND`) — merge it in again and
   re-verify the CHANGELOG invariant before merging.

If ts-mono review comments come in (an automated reviewer runs there), apply
actionable ones in the companion PR before merging it — barrel re-exports in
`packages/inspect-common/src/types/index.ts` are the recurring one: new
public types plucked from `generated.ts` should be re-exported like their
neighbors (`LogUpdate`, `ProvenanceData`). Such an edit makes the companion
hand-written: re-run `companion_mergeable.py`, which now needs a ts-mono
approval at the new head before step 3.

## 3. Clean up

- The hourly Atlas sync closes each fork issue, sets `Status: Done`, and
  clears `Stage` — for items whose **`Upstream PR` field** is set (its join
  key; promotions missing the field are invisible to it — the #90 lesson).
  So the fast path is: verify the field is set, then run `/resolve-board` (or
  just wait for :17). Manual cleanup below is the immediate path or the
  missing-field fallback.
- Issues auto-close and board `Status` auto-moves to Done, but the `Stage`
  field stays at "Merge" — clear it per item:
  ```bash
  gh project item-edit --id <ITEM_ID> --project-id PVT_kwDOC7YMCM4BU68p \
    --field-id <STAGE_FIELD_ID> --clear
  ```
  (Stage field id from `gh project field-list 1 --owner meridianlabs-ai`;
  fetch item ids one at a time — batched jq lookups have returned empty ids.)
- Close any still-open fork review PRs (meridianlabs-ai/inspect_ai) with a
  comment linking the merged upstream PR.
- **Then check for newly-ready work**: approvals often land while a set is
  merging, and the board only reflects them after a sync. Dispatch the Atlas
  sync (or `/resolve-board`) and re-run step 1 — if new items entered Merge,
  process them as the next set. Repeat until the queue comes back empty; only
  then write the final report.
- Report per-PR: what conflicted, how it was resolved, merge commit oid.
