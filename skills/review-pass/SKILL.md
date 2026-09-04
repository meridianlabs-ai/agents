---
name: review-pass
description: Make a review pass on a PR or branch with the maintainers' review bar — /review-pass [<pr-number> | <branch>] reviews the diff against its merge-base in fresh context, tags every finding blocking or non-blocking, and ends with one verdict line (no blocking findings = pass). Use before merging, before requesting a human review, or when asked to "review this with the maintainers' bar".
---

# Review pass with the maintainers' bar

Review the change the way the maintainers agreed to review each other's work:
carefully, but without holding it to a bar the maintainers themselves never
held. The automated `@review`/`@auto` loop applies this same bar; this skill
is the local, in-session version.

## Review bar (maintainers' agreed standard)

- Scope is the problem the PR states. Findings outside it are comments, not
  blockers; the PR does not have to fix everything nearby.
- A finding blocks only if it is a regression on a mainline path, a weakened
  security boundary, data loss or corruption, or a claim in the PR description
  the code does not deliver. Tag every finding **blocking** or **non-blocking**.
- A review with no blocking findings is a pass. Post non-blocking findings
  once; the author fixes or dismisses each with a reason. Do not spend another
  round on them.
- Fringe regressions and fringe cases left unsolved are notes, not blockers.
  The test is whether the PR leaves the code better than it found it.

## Steps

1. **Pick the target.** A PR number → `gh pr view <N> --json title,body,baseRefName,headRefName`
   and `gh pr diff <N>`. A branch name (or none: the current branch) → diff
   against the merge-base: `git diff $(git merge-base origin/<base> <branch>) <branch>`.
   Read the PR description (or the branch's commits) for the problem the
   change states — that is the scope.

2. **Review in fresh context.** Launch one subagent with the diff, the stated
   scope, and the bar above; it must not carry this session's assumptions
   about the change. Ask it for every finding it is confident about in a
   single pass — blocking and non-blocking alike — each with `file:line`, a
   one-sentence defect statement, and the tag. Regression risk counts:
   a regression on a mainline path or documented contract is blocking; a
   fringe one is a non-blocking note. It may run the repo's tests/lint to
   verify a finding. No speculative padding.

3. **Report.** List the findings, blocking first, then end with exactly one
   verdict line:

   - `Verdict: PASS — no blocking findings.` (non-blocking notes may be listed)
   - `Verdict: CHANGES NEEDED — <n> blocking finding(s).`

## Don'ts

- Do not expand scope: no "while you're here" work, no findings about code the
  PR did not touch unless the PR breaks it.
- Do not fix anything; this is a read-only pass. The author (or `@auto`) acts
  on it.
- Do not re-raise non-blocking findings on a later pass of the same change if
  the author already dismissed them with a reason.
