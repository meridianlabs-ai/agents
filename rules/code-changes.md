# Code changes

Scope: these files are written for Meridian Labs employees and their agents. Anyone can read them. Nobody outside Meridian is expected to follow them, but we think they are reasonable, good practice.

Rule scope: **repo**. Every agent that works in a repo obeys these rules. They reach it through the repo's `AGENTS.md`.

These rules apply when an agent changes code and prepares a PR.

## Required

### Before you start

- Read the repo's `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING*`, and each nested `AGENTS.md` under the paths that you change. (Provisional, from Eric)
- Check that the issue is still open and that no other PR fixes it. (Provisional, from Eric)
- Make a new branch from the current base branch. Do not commit on a branch that you got from another task. (Provisional, from Eric)
- Work in a separate worktree or clone. Do not change the branch of a checkout that a person uses.

### The change

All rules in this section: Provisional, from Eric.

- Make the smallest change that solves the problem. Use the idiom that the repo already uses.
- Do not add changes that are not related to the problem.
- Do not disable a lint or type-check warning without a human's approval.

### Git

- Make all changes through a PR. Do not push to a default branch or a protected branch.
- Do not force-push. Do not rebase a branch that you pushed. Merge the base branch into it. (Provisional, from Eric)
- Merge the base branch only when the repo requires an up-to-date branch, or when the PR has a conflict. (Provisional, from Eric)

### Tests

- Run the repo's tests and lint before you open the PR, or before you mark it ready.
- Write tests that run the real path: the real endpoint, the real container, the real page. (Provisional, from Eric)
- Gated tests (for example slow, API or sandbox tests that the repo's `AGENTS.md` names) that cover the change must run on the current head before the PR is ready. (Provisional, from Eric)
- Coverage is per subsystem and per provider. A pass for one provider does not show a pass for another. (Provisional, from Eric)
- If nobody can run a gated test, keep the PR as a draft. Say which test did not run and who can run it. (Provisional, from Eric)

### Broken and flaky checks

All rules in this section: Provisional, from Eric.

- A missing, queued, skipped, cancelled or neutral check is not a pass.
- Compare the checks that the changed paths must start with the runs that exist.
- Do not call a failure flaky or expected without evidence. Name the check and the failure text. Show the same failure on the base branch, or a known issue.
- Explain why the PR does not cause the failure or make it worse.
- If you do not know the failure, report it. Do not invent an exception.

### Claims

All rules in this section: Provisional, from Eric.

- Report what you saw, not what you started. Give the commit, the run URL or the check result.
- If you started something and did not check the result, say "started, not verified".
- After a push, fetch the remote and confirm that the commit is there.
- Verify a dependency version from the repo's lockfile, not from your local environment.

## Ready for review

All rules in this section: Provisional, from Eric.

Before you mark a PR ready, or ask a person to review it:

- Run review and fix rounds until a round finds no new blocking findings.
- Put an `### Agent review` section in the PR body. Name each review round: the model, fresh context or not, the head, the findings, and what you fixed or dismissed. Do not list a round that did not occur.
- Put the gated test counts and the head they ran on in a `### Slow tests` section.
- Make sure that all checks pass, or that each failure has evidence as "Broken and flaky checks" says.
- Read the PR body again. Make sure that it describes the code as it is now.
- Until then, keep the PR as a draft.

## Recommended

All rules in this section: Provisional, from Eric.

- Prefer a functional style when it stays readable.
- Use `assert` for programming errors when the repo does so.
- Before you add a new mechanism, check whether an existing one already does the job.
- When a fix adds a second mechanism next to an existing one, propose to remove the old one.
