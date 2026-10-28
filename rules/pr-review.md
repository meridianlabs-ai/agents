# PR review

> [!NOTE]
> Rule scope: **repo**. Every agent that works in a repo obeys these rules. They reach it through the repo's `AGENTS.md`.
>
> Scope: these files are written for Meridian Labs employees and their agents. Anyone can read them. Nobody outside Meridian is expected to follow them, but we think they are reasonable, good practice.

These rules apply when an agent reviews a PR. They apply to every author: a person, an agent, a teammate or an external contributor.

"The PR owner" is the human that the reviewing agent works for.

## Rules

### Scope

> [!NOTE]
> All rules in this section: Provisional.

- Review the problem that the PR states.
- Do not ask the author to fix problems outside that scope.
- Report a problem outside the scope as a non-blocking note, only if the PR makes it worse.

### What blocks a merge

> [!NOTE]
> All rules in this section: Provisional.

- Tag every finding **blocking** or **non-blocking**.
- A finding is blocking only if it is one of these:
  - a regression on a mainline path or a documented contract;
  - a weaker security boundary;
  - data loss or data corruption;
  - a claim in the PR description that the code does not deliver;
  - a required gated test that did not run on the current head (see `code-changes.md`).
- All other findings are non-blocking.
- A regression on a fringe path is non-blocking. Say why it is fringe.
- A review with no blocking findings is a pass.

### Non-blocking findings

> [!NOTE]
> All rules in this section: Provisional.

- Post each non-blocking finding one time.
- The author fixes it or dismisses it with a reason.
- Do not start another review round for non-blocking findings.
- Do not raise a dismissed finding again on the same change.

### Evidence

> [!NOTE]
> All rules in this section: Provisional.

- Read the diff. Do not review only the PR description.
- Find the sentence in the description that the PR depends on. Check that the base branch has that problem.
- Give each finding a `file:line` at the head that you reviewed.
- Verify a finding with a test or a run when you can.
- Label a finding that you cannot verify as **unverified**.
- Report every finding that you are sure of in one pass. Do not add guesses.

### Regression risk

For each regression finding, state these four items:

1. The behavior that changes.
2. Who or what depends on it.
3. The event that causes the problem.
4. Blocking or non-blocking, and why.

### Live state

> [!NOTE]
> All rules in this section: Provisional.

- Record the head SHA that you reviewed.
- Before you post, read the head SHA again.
- If the head changed, say that your findings apply to the old head.

### Later passes

> [!NOTE]
> All rules in this section: Provisional.

- Start a later pass with a table of all earlier findings. Mark each one addressed, open, deferred, or no longer applies, with `file:line` on the current head.
- Then review the commits since the last pass.
- On a PR by another person, wait for the author to ask for a new review or to reply. New commits alone do not ask for a review.

## Second reviews and who wrote the PR

- For a change to security, privilege, sandbox or CI trust code, get a second review from a different model family. Report where the two reviews disagree first. (Provisional)
- When a maintainer's `### Agent review` section shows full scope, named models, a result for each finding and coverage of the current head, you can accept that review. Check a sample of the fixes. Do not repeat the full review. (Provisional)
- On a PR by a Meridian employee, do not use "request changes" to ask for a different design. Push the change to the branch. Post one comment that says what you changed and why. **NEEDS TEAM AGREEMENT.** (Provisional)
- On a PR by another author, use a "request changes" review to ask for a different design, not a comment. Recommend one design. Do not list neutral options. (Provisional)
- On a PR from an external contributor, check correctness and tests with more care. Use a welcoming tone. (Provisional)

## Shared definitions

### A change that the reviewer makes

> [!NOTE]
> All rules in this section: Provisional.

These words describe the size of a fix:

- **Trivial**: the fix needs no judgment. Any maintainer makes the same fix.
- **Minor or moderate**: the fix is contained and clear when you state the finding. It does not change the design or the behavior that the PR claims.
- **Design**: the fix changes what the PR says that it does.

For every author:

- Fix small, useful findings and clear in-scope acceptance items.
- Do not make the PR larger with work that is not related.
- Do not ask an author to merge up the base branch. Do it yourself when you have access.
- If a merge conflict needs a judgment, ask the author a question. Do not request changes for it.

On a PR by a Meridian employee or a trusted contributor:

- Merge up the base branch and fix conflicts, at any size.
- Fix minor and moderate findings with your own commits: defects, missing checks and tests, docstring and CHANGELOG corrections.
- Post one PR comment that says what you changed and why.
- Do not change the design. Ask the author, or the PR owner, a question.
- Request changes only in extreme cases, for example a change that the team does not want at all.

On a PR by an external contributor:

- Merge up and fix trivial conflicts only when you have access to the branch. If you do not, ask the author in the review.
- Fix a minor finding only when the fix is clear, in scope, and you have access. Verify the fix yourself.
- Do not change the design. This is a question for the PR owner.
- Request changes only for items that the author must do: a substantive conflict, a failed check with no known cause, missing gated tests, or a fix only the author can make.
- Do not request changes for optional items, or for items that you can fix yourself.

### When an agent can approve or merge

See [`agent-authority.md`](agent-authority.md).
