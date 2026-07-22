---
name: merge-approved-prs
description: Merge approved upstream inspect_ai PRs from the Atlas board's Awaiting Merge stage, one at a time — resolve conflicts against main, guard CHANGELOG/submodule invariants, coordinate companion ts-mono PRs and submodule pointer bumps when the viewer schema changed, watch CI, merge, and clean up the board. Items whose issue carries hold:release are skipped unless holds are explicitly included (the post-release sweep). Use when Ransom says he's ready to merge approved PRs / clear the Awaiting Merge queue.
---

# Merge approved inspect_ai PRs (Awaiting Merge queue)

Merge the approved upstream PRs linked from Atlas-board issues in the
**Awaiting Merge** stage, strictly one at a time — they usually conflict with
each other, so each must land before the next is rebased.

Remotes in `~/git/viewer`: `origin` = UKGovernmentBEIS/inspect_ai (upstream,
where PRs merge), `meridianlabs-ai` = the fork (where PR branches live and are
pushed).

## 1. Find the queue

```bash
gh project item-list 1 --owner meridianlabs-ai --format json --limit 1000 \
  | jq -r '.items[] | select(.stage == "Awaiting Merge")
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
- Confirm each upstream PR: `state=OPEN`, `reviewDecision=APPROVED`, note
  `mergeable` (usually `CONFLICTING`).

## 2. Per PR, in order (repeat from here after each merge)

```bash
git fetch origin main
git checkout -B <branch> meridianlabs-ai/<branch>
git merge origin/main
```

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

### Push, CI, merge

```bash
git push meridianlabs-ai <branch>
gh pr view <n> --repo UKGovernmentBEIS/inspect_ai --json mergeable,mergeStateStatus
```

Expect `MERGEABLE` + `BLOCKED` (checks pending). Watch CI in a background
task: `gh pr checks <n> --repo UKGovernmentBEIS/inspect_ai --watch
--fail-fast`. On failure, investigate and fix; on success merge with
`gh pr merge <n> --repo UKGovernmentBEIS/inspect_ai --squash` (repo history
uses squash for these).

**"Pull request was already merged" is success** — these PRs typically have
auto-merge armed, which fires the moment checks pass. Always confirm with
`gh pr view <n> --json state,mergedAt` → `state=MERGED`.

Then `git fetch origin main` and start the next PR — it now conflicts with
what just landed.

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
  field stays at "Awaiting Merge" — clear it per item:
  ```bash
  gh project item-edit --id <ITEM_ID> --project-id PVT_kwDOC7YMCM4BU68p \
    --field-id <STAGE_FIELD_ID> --clear
  ```
  (Stage field id from `gh project field-list 1 --owner meridianlabs-ai`;
  fetch item ids one at a time — batched jq lookups have returned empty ids.)
- Close any still-open fork review PRs (meridianlabs-ai/inspect_ai) with a
  comment linking the merged upstream PR.
- Report per-PR: what conflicted, how it was resolved, merge commit oid.
