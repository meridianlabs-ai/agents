# Code changes

Scope: these files are written for Meridian Labs employees and their agents. Anyone can read them. Nobody outside Meridian is expected to follow them, but we think they are reasonable, good practice.

These rules apply when an agent changes code and prepares a PR.

## Required

### Before you start

- Read the repo's `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING*`, and each nested `AGENTS.md` under the paths that you change.
- Check that the issue is still open and that no other PR fixes it.
- Make a new branch from the current base branch. Do not commit on a branch that you got from another task.
- Work in a separate worktree or clone. Do not change the branch of a checkout that a person uses.

### The change

- Make the smallest change that solves the problem. Use the idiom that the repo already uses.
- Do not add changes that are not related to the problem.
- Do not disable a lint or type-check warning without a human's approval.

### Git

- Make all changes through a PR. Do not push to a default branch or a protected branch.
- Do not force-push. Do not rebase a branch that you pushed. Merge the base branch into it.
- Merge the base branch only when the repo requires an up-to-date branch, or when the PR has a conflict.

### Tests

- Run the repo's tests and lint before you open the PR, or before you mark it ready.
- Write tests that run the real path: the real endpoint, the real container, the real page.
- Gated tests (for example slow, API or sandbox tests that the repo's `AGENTS.md` names) that cover the change must run on the current head before the PR is ready.
- Coverage is per subsystem and per provider. A pass for one provider does not show a pass for another.
- If nobody can run a gated test, keep the PR as a draft. Say which test did not run and who can run it.

### Claims

- Report what you saw, not what you started. Give the commit, the run URL or the check result.
- If you started something and did not check the result, say "started, not verified".
- After a push, fetch the remote and confirm that the commit is there.
- Verify a dependency version from the repo's lockfile, not from your local environment.

## Recommended

- Open a PR as a draft. Mark it ready after the review rounds in `pr-review.md` and the gated tests are done.
- Put the review rounds in an `### Agent review` section and the gated test counts in a `### Slow tests` section of the PR body.
- Prefer a functional style when it stays readable.
- Use `assert` for programming errors when the repo does so.
- Before you add a new mechanism, check whether an existing one already does the job.
- When a fix adds a second mechanism next to an existing one, propose to remove the old one.
