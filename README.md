# agents

Shared agent infrastructure for Meridian repos: Claude Code GitHub workflows,
rollout scripts, and (eventually) shared skills and plugins. Configuration
lives here once; caller repos carry thin stubs that reference it, so behavior
changes in one place propagate everywhere.

For the *why* behind the design — the cross-org constraint, the auth journey,
the auto-review and permission tradeoffs — see [design/architecture.md](design/architecture.md).

## The two agents

| | Dev agent | Reviewer |
|---|---|---|
| Workflow | `.github/workflows/claude.yml` | `.github/workflows/claude-review.yml` |
| Trigger | `@claude` mention, or the `claude` label | auto on PR open, or `@review` |
| GitHub token | `contents: write` (edits, pushes, opens PRs) | `contents: read` (cannot push) |
| Tools | file edits + verify loop (tests/lint) + `gh` | verify loop + `gh` + inline comments; **denies** edits/git writes |

Both authenticate the same way (Workload Identity Federation) and default to
the same model (Fable, falling back to the account default). The hard
privilege boundary between them is the GitHub token scope, not the prompt — the
reviewer physically cannot push regardless of what it's asked to do.

## Layout

- `.github/workflows/claude.yml` — reusable dev-agent workflow.
- `.github/workflows/claude-review.yml` — reusable reviewer workflow.
- `examples/claude-stub.yml` — dev-agent stub to copy into a repo.
- `examples/claude-review-stub.yml` — reviewer stub to copy into a repo.
- `scripts/enable-claude.sh` — opens a PR adding both agent stubs to a repo.
- `design/architecture.md` — design rationale and history.

## Enabling the agents in a repo

```sh
scripts/enable-claude.sh meridianlabs-ai/<repo>
```

This opens a PR adding both stubs (dev `claude.yml` and reviewer
`claude-review.yml`); stubs already present are skipped, so it's safe to re-run.
Merge the PR to activate.

Prerequisite: the Claude GitHub App must have access to the repo (org-wide
install covers this).

**Optional — let the agents run your tests.** By default the agents review and
build against a bare runner (no deps installed), so they verify with static
checks only. To give them a real environment, add a
`.github/actions/claude-setup` composite action to your repo that installs your
project — ideally by delegating to your existing CI setup
(`uses: ./.github/actions/<your-setup>`), so nothing is duplicated and the build
cache is shared. Both agents run it automatically when present (a failed setup
fails the run, so keep it green). See
[design/architecture.md](design/architecture.md) for the mechanics and the
inspect_ai-fork caveat.

## Using the dev agent

Trigger it by:

- Mentioning `@claude` in an issue or PR comment, with whatever instruction
  follows (`@claude fix the failing test in auth.py`).
- Adding the `claude` label to an issue — works directly from project-board
  views, no need to open the issue.

Notes:

- On an **issue**, the agent starts from the default branch and creates a new
  branch. On a **PR**, it pushes to the existing PR branch. Drive iterative
  work (review fixes, follow-ups) from the PR, not the issue, or you'll spawn a
  parallel branch.
- The agent runs the project's tests/lint to verify its work before opening a
  PR (per each repo's CLAUDE.md conventions).

## Using the reviewer

The reviewer posts a top-level summary plus inline comments on a PR. It runs:

- **Automatically** when a PR is opened, reopened, or marked ready for review.
  (This relies on the workflow being present on the PR's base branch — see the
  fork caveat below, where it isn't and auto-review is driven differently.)
- **On demand** when someone comments `@review` on a PR.

It is read-only: it can run tests to verify a finding but cannot modify code or
push. Its findings are confidence-filtered (few high-signal items over many
speculative ones).

### The review → fix loop

Acting on a review is **human-mediated by design** — there is no automatic
handoff from reviewer to dev agent (the reviewer's comments don't contain
`@claude`, and the dev agent ignores bot-authored comments anyway — its
`allowed_bots` is empty). The loop:

1. Reviewer posts findings.
2. You decide which to act on.
3. Comment `@claude address the review feedback` **on the PR**. The dev agent
   pushes fixes to the same branch.

This keeps your judgment in the loop on which findings matter. See the design
doc for why we avoid a fully automatic reviewer→fixer loop.

## The inspect_ai fork

[meridianlabs-ai/inspect_ai](https://github.com/meridianlabs-ai/inspect_ai) is
a fork of [UKGovernmentBEIS/inspect_ai](https://github.com/UKGovernmentBEIS/inspect_ai)
that lets Claude work on inspect issues even though we don't control the
upstream org (we can't install the app or add workflows there).

Branch layout:

- **`main`** — pristine mirror of upstream main. Never commit to it. Protected
  by a ruleset (no updates/deletes/force-pushes) so PRs can't be merged into it
  accidentally.
- **`meridian`** (default branch) — `main` plus meridian-only workflows (dev,
  review, sync). The Claude workflows live *only* here, which is why meridian
  must be default. These workflows are maintained directly on that branch (the
  source of truth):
  [.github/workflows on `meridian`](https://github.com/meridianlabs-ai/inspect_ai/tree/meridian/.github/workflows).

> **Trigger caveat (consequence of the branch layout).** GitHub resolves
> `issue_comment` and `issues` workflows from the **default branch** (`meridian`,
> which has the workflows) but `pull_request`-family events
> (`pull_request`, `pull_request_review`, `pull_request_review_comment`) from
> the **PR's own branches** (`main` / a `main`-cut feature branch), which carry
> no Claude workflows. So on the fork, **only top-level `@claude`/`@review`
> comments and the `claude` label trigger the agents.** Inline review-comment
> replies, review submissions, and auto-review-on-PR-open do **not** fire. The
> fork's dev stub therefore drops the two PR-review triggers, and auto-review is
> restored another way: when the dev agent opens a PR it posts a top-level
> `@review` comment, and the reviewer stub sets `allowed_bots: "claude[bot]"` so
> that bot-authored comment is honored. Human-opened PRs still need a manual
> `@review`.

Both branches are kept current by `sync-upstream.yml` (hourly): it
fast-forwards `main` from upstream and merges upstream into `meridian`. It
pushes via the `SYNC_TOKEN` secret (an admin-owned fine-grained PAT) because
the ruleset's admin-role bypass lets that identity through; the default
workflow token cannot bypass rulesets.

### The two-stage PR flow

1. File an issue on the fork (e.g. from the project board) and add the `claude`
   label, or `@claude` it.
2. Claude branches from pristine `main` and opens a draft PR **within the fork**
   (`claude/xyz` → `main`), then posts a top-level `@review` comment to kick off
   the reviewer (auto-review-on-open can't fire here — see the trigger caveat).
   This PR is the review surface — it is never merged here. Because `main`
   mirrors upstream, its diff is exactly what upstream will see.
3. Iterate on the fork PR (`@claude` to fix, `@review` to re-review) — always as
   **top-level** PR comments; inline review-comment replies don't trigger here.
4. When ready, **a human** opens the upstream PR from the same branch:
   `gh pr create --repo UKGovernmentBEIS/inspect_ai --head meridianlabs-ai:claude/xyz`.
   Close the fork PR with a link. (Promotion is a deliberate human step — the
   agent's fork token can't push cross-repo, and this is the gate before
   publishing into an org we don't control.)
5. To address upstream review feedback, comment on the fork PR
   (`@claude address the feedback on UKGovernmentBEIS/inspect_ai#NNNN`) — pushes
   to the shared branch update the upstream PR automatically.

## Updating behavior across all repos

Edit the reusable workflow here and merge to `main`. All caller repos pick up
the change on their next run (stubs reference `@main`). For the inspect_ai fork,
also redeploy the changed stub to its `meridian` branch if the stub itself
changed (the reusable workflow it calls updates automatically).

## Maintainers

You don't need any of this to *use* the agents — there are no secrets or config
to set up per repo. For working on the infra:

- [design/architecture.md](design/architecture.md) — auth (WIF), the permission
  model, model selection, branch protection, one-time org setup, and the
  rationale/history behind the design.
- [design/shared-instructions.md](design/shared-instructions.md) — proposed (not
  yet built) plan for sharing `CLAUDE.md`/`AGENTS.md` across Meridian repos.
- [design/scheduled-tests-on-fork.md](design/scheduled-tests-on-fork.md) —
  proposed (not yet built) plan to move the scheduled inspect tests + triage to
  the fork and close the triage → fix loop.
- [AGENTS.md](AGENTS.md) / [CLAUDE.md](CLAUDE.md) — instructions for agents
  making changes in this repo.
