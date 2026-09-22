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
| Trigger | `@claude` mention, or the `claude` label | `@review` comment (auto-review on PR open is off, 2026-09-16) |
| GitHub token | agent job: read-only; a separate land job pushes the commits and opens the PR as the machine account | `contents: read` (cannot push) |
| Tools | file edits + verify loop (tests/lint) + `gh` | verify loop + `gh` + inline comments; **denies** edits/git writes |

Both authenticate the same way (Workload Identity Federation) and default to
the same model (Fable, falling back to the account default). The hard
privilege boundary between them is the GitHub token scope, not the prompt —
neither job that runs an agent holds a write token: the reviewer physically
cannot push regardless of what it's asked to do, and the dev agent's commits
are pushed by a separate job after its run ends.

## Layout

- `.github/workflows/claude.yml` — reusable dev-agent workflow.
- `.github/workflows/claude-review.yml` — reusable reviewer workflow.
- `examples/claude-stub.yml` — dev-agent stub to copy into a repo.
- `examples/claude-review-stub.yml` — reviewer stub to copy into a repo.
- `scripts/enable-claude.sh` — opens a PR adding both agent stubs to a repo.
- `design/architecture.md` — design rationale and history.

## Local skills

`skills/` holds the six shared skills. Both `.agents/skills` and `.claude/skills` link to it, so Codex and Claude Code discover the same files when working in this repo. Use `$skill-name` in Codex or `/skill-name` in Claude Code.

For use from other repos, link each desired skill into both user skill directories. For example, from this checkout:

```sh
mkdir -p ~/.agents/skills ~/.claude/skills
ln -s "$PWD/skills/checkout" ~/.agents/skills/checkout
ln -s "$PWD/skills/checkout" ~/.claude/skills/checkout
```

Run each `ln` command only when that destination name is absent. Preserve existing links and directories from other sources. Both links must point at the same stable checkout.

## Enabling the agents in a repo

```sh
scripts/enable-claude.sh meridianlabs-ai/<repo>
```

This opens a PR adding both stubs (dev `claude.yml` and reviewer
`claude-review.yml`); stubs already present are skipped, so it's safe to re-run.
Merge the PR to activate.

Prerequisite: the Claude GitHub App must have access to the repo (org-wide
install covers this).

**The machine account's secrets.** The stubs pass two org secrets by name to
the reusable workflows: `MARVIN_APP_CLIENT_ID` and `MARVIN_APP_PRIVATE_KEY`,
the GitHub App `meridian-marvin` through which the workflows' trusted `gate`
and `land` jobs act (pushes that run CI, the `@review` hand-back, comments,
labels, the Atlas board). Grant both secrets to the repo in the org's
**Secrets and variables → Actions** page and add the repo to the app's
installation; each job then mints a one-hour token scoped to that repo and
to what the job writes. The two app secrets are the whole contract: the
machine account's older personal access token was retired on 2026-09-18 —
the workflows no longer accept it, and the org admin deletes that org
secret once the retirement has merged. Never `secrets: inherit`. A repo
without the app secrets still runs the dev agent, with the documented
degradation: pushes and PRs come from `github-actions[bot]` and trigger
nothing; the reviewer and the `@auto` loops fail at their gates' mint
step.

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
  branch; the workflow pushes that branch and opens the PR when the run ends.
  On a **PR**, its commits land on the existing PR branch. Drive iterative
  work (review fixes, follow-ups) from the PR, not the issue, or you'll spawn a
  parallel branch.
- **One push per run, at the end.** The agent itself never pushes, opens PRs
  or comments: it commits, and the workflow's land job pushes the commits as
  the machine account (so CI runs), opens the PR on an issue run, and posts
  any comment the agent left for the thread. The machine account is the
  GitHub App login `meridian-marvin[bot]` (Phase 2 of the credential
  separation; its personal access token was retired 2026-09-18); the
  workflows also still trust the User login `i-am-marvin` until that
  account is retired separately. Commits do not appear mid-run;
  to iterate on CI failures use `@auto`, whose loop re-runs the agent on each
  red CI run.
- PR follow-ups first merge the base branch into the PR branch on the runner,
  and that merge lands with the run's push even if nothing else changes — so
  a behind branch may gain a merge commit from the machine account after any
  `@claude` request (pull before pushing local work). That merge is what lets
  GitHub compute a merge ref again, so CI can run.
- The agent runs the project's tests/lint to verify its work before it
  commits (per each repo's CLAUDE.md conventions).
- **It does not run on PRs whose head lives in a fork.** That code would
  execute unsandboxed in a job holding write credentials, so the run stops
  before checking anything out; collaborators get a short comment saying so
  (outsiders get silence). Use `@review` for a sandboxed review of a fork PR,
  or push the branch to the repository itself.

## Using the reviewer

The reviewer posts a top-level summary plus inline comments on a PR. It runs:

- **On demand** when someone comments `@review` on a PR (or on an
  `External`-labeled issue).
- **Never on its own.** Auto-review on PR open / reopen / ready-for-review is
  off in every Meridian repo (decision: Ransom, 2026-09-16, after inspect_ai#501
  was reviewed unasked; actions went first on 2026-09-14 after actions#110),
  and the reusable workflow's `pull_request` / `pull_request_target` path was
  removed on 2026-09-22: an `@review` comment is the only event it admits, and
  a stub that still fires a PR event gets a red run naming the fix. Reviews are
  driven from Orca workspaces instead; a repo that wants a review from CI posts
  a top-level `@review` comment as a write-access identity.

It is read-only: it can run tests to verify a finding but cannot modify code or
push. Its findings are confidence-filtered (few high-signal items over many
speculative ones).

A collaborator's `@review` comment
reviews one — treated as untrusted code: nothing from the fork's tree is
executed on the runner itself, the reviewer installs and tests inside an
OS-level sandbox, and the fork's `.claude/` / `.mcp.json` are deleted from the
checkout first while its `CLAUDE.md` is renamed so it loads as untrusted text,
not instructions (review those files from the diff). Findings still
land on the PR. Codex-engine reviews are not sandboxed, so a fork PR labeled
`engine:codex` is reviewed by Claude instead.

### The review → fix loop

Acting on a review is **human-mediated by design** — there is no automatic
handoff from reviewer to dev agent (the reviewer's comments don't contain
`@claude`, and the dev agent ignores bot-authored comments anyway — its
`allowed_bots` names only the machine account's own bot login, and the stubs
exclude every bot actor from the text triggers). The loop:

1. Reviewer posts findings.
2. You decide which to act on.
3. Comment `@claude address the review feedback` **on the PR**. The dev
   agent's fixes land on the same branch.

This keeps your judgment in the loop on which findings matter. See the design
doc for why we avoid a fully automatic reviewer→fixer loop.

## Choosing the engine (`engine:codex`)

The verbs above default to Claude Code. Labeling an issue or PR
**`engine:codex`** routes its runs — dev agent, reviewer, and both @auto
loops — to OpenAI Codex instead; removing the label switches back on the
next run. Same triggers, same markers, same board tracking. Requirements:
the repo needs the `OPENAI_API_KEY` org secret and the `engine:codex`
label created (`gh label create engine:codex -c 8250DF -d "route agent
runs to Codex"`). Codex v1 differences: review findings arrive as one
summary comment (no inline comments; codex fix rounds do resolve the
Claude reviewer's inline threads they report as addressed), and
external proxy reviews always use Claude. Codex reviews run tests to
verify findings like the Claude reviewer — via the repo's claude-setup
action, or a fallback uv dev-install when the checkout has a
pyproject.toml but no claude-setup (fork PR branches, and any Python
caller repo that never added the action); non-Python repos degrade to
static review.
Details: [design/codex-engine.md](design/codex-engine.md).

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
> replies and review submissions do **not** fire, so the fork's dev stub drops
> the two PR-review triggers. (PRs against `meridian` itself do resolve
> `pull_request` from `meridian` — that is how inspect_ai#501 got reviewed
> unasked on 2026-09-16, and why the reviewer stub no longer carries the
> trigger.) The dev agent used to post a top-level `@review` when it opened a
> PR, as the fork's substitute for auto-review-on-open; that is off too
> (`request_review_after_open: "false"`, 2026-09-16). The reviewer stub keeps
> `allowed_bots: "claude[bot]"` so a bot-authored `@review` is still honored
> when one is posted. Every PR needs a human `@review`.

Both branches are kept current by `sync-upstream.yml` (hourly): it
fast-forwards `main` from upstream and merges upstream into `meridian`. It
pushes via the `SYNC_TOKEN` secret (an admin-owned fine-grained PAT) because
the ruleset's admin-role bypass lets that identity through; the default
workflow token cannot bypass rulesets.

### The two-stage PR flow

1. File an issue on the fork (e.g. from the project board) and add the `claude`
   label, or `@claude` it.
2. Claude branches from pristine `main` and opens a draft PR **within the fork**
   (`claude/xyz` → `main`). Nothing reviews it on its own: post a top-level
   `@review` comment when a review is wanted (see the trigger caveat).
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

- [SECURITY.md](SECURITY.md) and [design/credential-separation.md](design/credential-separation.md) — how to report a vulnerability, the trust boundaries, and the gate/agent/land pattern and machine-account identity as they stand today.
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
