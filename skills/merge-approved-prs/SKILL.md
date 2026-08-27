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
  off instead: `git fetch --no-recurse-submodules`, and for `gh pr checkout`
  set `GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=fetch.recurseSubmodules
  GIT_CONFIG_VALUE_0=false GIT_CONFIG_KEY_1=submodule.recurse
  GIT_CONFIG_VALUE_1=false`. The queue never needs submodule contents
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
  PRs" section below.
- Confirm each upstream PR: `state=OPEN`, `reviewDecision=APPROVED`, note
  `mergeable` (usually `CONFLICTING`).

## 2. Per PR, in order (repeat from here after each merge)

```bash
git fetch origin main
git fetch meridian <branch>
git checkout -B <branch> meridian/<branch>
git merge origin/main
```

Take `<branch>` from the PR JSON already in hand (`headRefName`) — NEVER
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

Commit the merge (Co-Authored-By trailer). If a code conflict was involved,
sanity-check locally before pushing: `ruff check` + `ruff format --check` on
touched files, `mypy <touched files>`, and any targeted tests that cover the
conflicted area. Pure CHANGELOG/docs conflicts can go straight to CI.

- **Run pytest with `PYTHONPATH=$PWD/src`** (from the worktree root). The
  venv's editable install points at the PRIMARY clone's `src/`, so without
  it pytest imports the main checkout's code and silently tests the wrong
  tree (observed: a green run that hadn't exercised the merge at all —
  caught only when a branch-side import didn't exist in the main clone).
  Verify once per session:
  `PYTHONPATH=$PWD/src python -c 'import inspect_ai; print(inspect_ai.__file__)'`
  must print the worktree path. (`ruff`/`mypy` take file paths, so they
  check the worktree files regardless.)
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
git push meridian <branch>   # externals: plain `git push` (contributor fork)
gh pr merge <n> --repo UKGovernmentBEIS/inspect_ai --auto --squash
```

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

- **Checkout/push**: instead of `git checkout -B <branch> meridian/<branch>`,
  use `gh pr checkout <n> --repo UKGovernmentBEIS/inspect_ai` in the worktree
  (with the submodule-recursion-off GIT_CONFIG env from the worktree rules)
  — with `maintainerCanModify` it wires the branch's push remote to the
  contributor's fork, so after `git merge origin/main` a plain `git push`
  lands on their branch (verify with `git push --dry-run` the first time).
  Never rebase or force-push a contributor branch — merge commits only;
  their local clone must stay fast-forwardable.
- **Approval can be dismissed by your push** (repo setting–dependent):
  re-check `reviewDecision` after pushing. You can re-approve — pushing to
  someone else's PR doesn't make you its author — but if branch protection
  requires approval of the most recent push by someone else, surface that in
  the report instead of looping.
- **Invariants are unchanged** (CHANGELOG entries under `## Unreleased`, no
  net submodule change) — but they were *reviewed*, not authored, by us, so
  check them even more mechanically. A violation that needs real rework goes
  back to the contributor: comment on the upstream PR, move the proxy to
  Contributor, and skip — don't rewrite their PR beyond conflict resolution.
- **ts-mono companions**: an external contributor can't author one in
  meridianlabs-ai/ts-mono — if the PR needs a schema/pointer bump, you author
  the companion yourself and follow the same sequence below.
- **Cleanup**: there is no fork review PR to close; the hourly sync closes
  the proxy on merge as usual (external proxies always carry the
  `Upstream PR` field, its join key).

## PRs that need a ts-mono change

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
2. **Update the companion ts-mono PR**: `schema.py` also regenerated
   `generated.ts` in the submodule working tree — **copy it aside before any
   git operations in the submodule**. Then in the submodule: check out the
   companion branch, `git merge origin/main`, restore the regenerated
   `generated.ts`, commit, push, wait for its CI.
3. **Merge the companion**:
   `gh pr merge <n> --repo meridianlabs-ai/ts-mono --squash` — ts-mono main is
   squash-only, and regenerate-style companions merge without human review
   (precedent: #427, #439).
   **Then immediately re-check the tracking issue** — companions are
   Development-panel-linked to it (that link IS the board's PR pill), and
   GitHub treats every panel-linked PR as a closer, so the companion's merge
   auto-closes the issue days before the inspect_ai PR lands (no closing
   keywords involved; issue #251, 2026-08-26). If
   `gh issue view <n> --repo meridianlabs-ai/inspect_ai --json state` says
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
neighbors (`LogUpdate`, `ProvenanceData`).

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
