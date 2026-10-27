# PR review

Scope: these files are written for Meridian Labs employees and their agents. Anyone can read them. Nobody outside Meridian is expected to follow them, but we think they are reasonable, good practice.

These rules apply when an agent reviews a PR. They apply to every author: a person, an agent, a teammate or an external contributor.

## Required

### Scope

- Review the problem that the PR states.
- Do not ask the author to fix problems outside that scope.
- Report a problem outside the scope as a non-blocking note, only if the PR makes it worse.

### What blocks a merge

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

- Post each non-blocking finding one time.
- The author fixes it or dismisses it with a reason.
- Do not start another review round for non-blocking findings.
- Do not raise a dismissed finding again on the same change.

### Evidence

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

- Record the head SHA that you reviewed.
- Before you post, read the head SHA again.
- If the head changed, say that your findings apply to the old head.

### Later passes

- Start a later pass with a table of all earlier findings. Mark each one addressed, open, deferred, or no longer applies, with `file:line` on the current head.
- Then review the commits since the last pass.
- On a PR by another person, wait for the author to ask for a new review or to reply. New commits alone do not ask for a review.

## Recommended

- For a change to security, privilege, sandbox or CI trust code, get a second review from a different model family. Report where the two reviews disagree first.
- Before you ask a person to review a PR, run review and fix rounds until a round finds no new blocking findings.
- A PR that an agent helped write has an `### Agent review` section in its body. The section names each review round: the model, fresh context or not, the head, the findings, and what was fixed or dismissed.
- When a maintainer's `### Agent review` section shows full scope, named models, a result for each finding and coverage of the current head, you can accept that review. Check a sample of the fixes. Do not repeat the full review.
- To ask for a different design, use a "request changes" review, not a comment. Recommend one design. Do not list neutral options.
- On a PR from an external contributor, check correctness and tests with more care. Use a welcoming tone.
