# Agent authority

> [!NOTE]
> Rule scope: **repo and Meridian-wide**. They reach agents through each repo's `AGENTS.md` and through `meridian-agents.md`.
>
> Scope: these files are written for Meridian Labs employees and their agents. Anyone can read them. Nobody outside Meridian is expected to follow them, but we think they are reasonable, good practice.

This file says what an agent can do alone, with no human, and what it never does alone. "Your person" is the human that the agent works for.

> [!NOTE]
> All rules in this file: Provisional.

## Approve and merge a PR alone

An agent can approve and merge a PR alone only when **all** of these are true:

1. Nothing is left for a human to decide. After you understand the change, no question remains where different answers lead to different actions (approve or hold, keep or cut scope, merge now or wait for a release). If each answer leads to approval, it is not a question.
2. The PR does not change security, privilege or sandbox boundaries, path or ownership checks, or `pull_request_target` jobs.
3. The PR does not change files under `.github/workflows/`.
4. The PR does not change build or dependency files (`pyproject.toml`, `uv.lock`, `package.json`, lockfiles) without a human's review of that change.
5. Its effect is not much wider than its stated scope: public contracts, stored data, downstream users, package contents, images, releases or deployments.
6. It does not change a public API, make a type wider, or break compatibility.
7. The review found no design finding.
8. No two models disagree on a blocking finding that a run did not settle.
9. The change did not need a plan before coding.
10. Every gated test ran on the current head (see `code-changes.md`).
11. The agent review is complete on the current head, to the bar in `pr-review.md`.
12. You are sure about every item above. If you are not sure, a human reviews it.

Size alone does not make a PR need a human. A long diff of mechanical or generated changes does not.

If one item is false, the PR needs a human review. The human review can be your person's review, or an approval on the current head from a maintainer who is not the author.

Operational work never needs a human: a branch that is behind, a conflict that you can fix, a check to run again. Do it, and report it in one line.

### Who can approve

- Approve alone only when your person agreed that you can.
- Do not approve a PR that your person wrote, or that you wrote for your person.
- Your approval is in your person's name. Your person is accountable for it.

## Land a PR that you approve

These rules apply to a PR that a Meridian person opens, marks ready for review, and requests a review on. "Meridian person" means a person in "Meridian People" in [`ownership.md`](ownership.md). They apply to a human reviewer and to an agent reviewer.

The reviewer:

- When you approve the PR, also merge it. Do not wait for the author.
- Merge only when all required checks pass on the current head.
- Use the merge method that the repo uses.
- Obey all other merge rules in this file. Do not use `gh pr merge --admin`. Do not bypass a branch rule.
- If you are an agent, merge only when "Approve and merge a PR alone" lets you, or when your person approved the current head.
- If the PR body tells you not to merge, approve only. The author merges. Examples: "Do not merge", "Hold: <reason or date>".
- Do not merge a draft PR.

The author:

- If the PR must not merge after approval, write a hold in the PR body. Give the reason or a date.
- When the PR can merge, remove the hold from the PR body.

## Other things an agent can do alone

On a PR by a Meridian employee or a trusted contributor:

- You can accept the author's documented agent review instead of your own review runs. It must show scope, models, a result for each finding, and coverage of the current head.
- Do not close the PR.
- Before you post a close or a "request changes", show the exact text to your person. Post it only after your person agrees.

On a PR by an external contributor:

- The author's agent review is context only. It does not replace your review.
- You can close the PR without a human when it is not eligible (no issue that describes a real problem) or not worth the work in that area. Give the reason first.
- You can request changes without a human, for the items in "A change that the reviewer makes" in `pr-review.md`.

## Never alone

- Do not bypass a branch rule or a ruleset.
- Do not use `gh pr merge --admin`, or merge past a failed or missing required check.
- Do not change branch protection, rulesets, repo settings, collaborators or org membership.
- Do not publish a package, upload a release artifact or start a release workflow.
- Do not approve or merge a PR that changes `.github/workflows/`.

A human can approve one of these actions for one specific case. That approval does not apply to other cases.
