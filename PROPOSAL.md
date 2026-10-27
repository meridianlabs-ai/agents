# Proposal: shared rules for our agents

Status: draft for discussion. Nothing here is in force.

Scope: these files are written for Meridian Labs employees and their agents. Anyone can read them. Nobody outside Meridian is expected to follow them, but we think they are reasonable, good practice.

## The problem

We have two layers.

1. **Rules.** Each of us gives our agents different markdown files. The files set different bars, criteria, processes and flows. Two agents can review the same PR to two different standards.
2. **Harnesses.** We run agents in different places: GitHub Actions, desktop apps, cloud bots. This layer does not change layer 1.

Layer 2 can stay different for each person. Layer 1 should not, for the work we share.

## The idea

Keep a small set of shared rule files in `rules/` in this repo. Each of us points our agents at them, from any harness. Then all our agents use one bar for shared work.

The files are short and harness-neutral. They say what to do, not how a specific tool does it.

Rules marked **Provisional, from Eric** are Eric's starting points. They are not agreed. Change them or remove them.

## Topics

1. **Robust PR review.** The review bar and what blocks a merge. See `rules/pr-review.md`.
2. **Review between Meridian employees.** In general, the reviewer makes the change and pushes it. The reviewer does not use "request changes". **NEEDS TEAM AGREEMENT.** See `rules/pr-review.md`.
3. **Ready for review.** The bar an author meets before asking for a review. See `rules/code-changes.md`.
4. **What an agent may do alone, and how a bot shows authorship.** See `rules/security.md` and `rules/public-writing.md`.
5. **Supply chain and CI changes.** See `rules/security.md`.
6. **Tests, including broken and flaky tests.** See `rules/code-changes.md`.
7. **Claiming work.** Two agents must not do the same work. No rule file yet. See the open questions.
8. **Releases.** Out of scope for this proposal.

## What adopting means

- You add one line or one step to each of your agent setups that loads `rules/`. `rules/README.md` shows how for common harnesses.
- Your own files can add rules. They can be stricter. If you want a rule to be looser, propose a change to the shared rule.
- You keep your harness, your tools and your personal workflow.

## How we iterate

The files are living documents. They do not need to be perfect before we adopt them.

- To change a rule, open a PR against `rules/`. One other human maintainer approves it.
- Each change adds a line to `rules/CHANGELOG.md`.
- We tag versions (`rules-v1`, `rules-v2`, ...). A harness can track `main` or pin a tag.
- An agent can draft a rule change. A human approves it.

## How we review the first version

1. The first version is a draft PR in this repo. We discuss it in the PR.
2. The team has one week to comment. Comment inline on a rule, or answer an open question below.
3. Agreement means: each active maintainer approves, or says "no objection". Open questions are settled or deferred in the PR.
4. Then we merge, tag `rules-v1`, and each of us points our agents at it.

Before step 4, nobody points an agent at these files.

## Open questions

1. Is meridianlabs-ai/agents the right place for these files? We can move them later.
2. Layout: one `rules/` directory with four files, or one file? How does this relate to `design/shared-instructions.md`, which copies a shared block into each repo's `AGENTS.md`?
3. Who approves a rule change: one other maintainer, or all active maintainers for a Required rule?
4. Is the Required and Recommended split right? Candidates to discuss: an unrun gated test as blocking; the 7-day package age limit; a second-model review.
5. Non-blocking findings: post them one time, or post no optional feedback at all?
6. Review between employees (topic 2): push the change, or ask first? Does this apply to design changes too?
7. Which PRs need no human (see `rules/pr-review.md`)? When can an agent approve or merge?
8. Claiming work (topic 7): should CODEOWNERS and GitHub review requests route and claim work? Existing examples: inspect_swarm pr 95 - a catch-all CODEOWNERS line that requests a review from one maintainer on every PR; and the CODEOWNERS lines in most of our repos that gate release PRs.
9. Which repos do the rules cover?
10. Is "add or make stricter only" the right rule for personal files?
11. What disclosure format do bots use? Does a bot account (for example a GitHub App) also need a prefix?
12. Tags and a changelog, or track `main` only?
13. Should this repo's `AGENTS.md` and the CI reviewer prompt load `rules/pr-review.md`?
14. Is ASD-STE100 required for all public text, or recommended?

## What we ask of you

- Read `rules/` (about 10 minutes).
- Comment on each rule: agree, change, or remove.
- Answer the open questions in the PR.
- After `rules-v1`, point your agents at it. Propose changes when a rule does not work.
