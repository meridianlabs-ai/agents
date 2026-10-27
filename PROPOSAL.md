# Proposal: shared rules for our agents

Status: draft for discussion. Nothing here is in force.

Scope: these files are written for Meridian Labs employees and their agents. Anyone can read them. Nobody outside Meridian is expected to follow them, but we think they are reasonable, good practice.

## The problem

We have two layers.

1. **Rules.** Each of us gives our agents different markdown files. The files set different bars, criteria, processes and flows. For example, two agents can review the same PR to two different standards.
2. **Harnesses.** We run agents in different places: GitHub Actions, desktop apps, cloud bots. This layer does not change layer 1.

Layer 2 can stay different for each person. Layer 1 should not, for the work we share.

## The idea

Keep a small set of shared rule files in `rules/` in this repo. Each of us points our agents at them, from any harness. Then all our agents use one bar for PR review, code changes, security and public text.

The files are short and harness-neutral. They say what to do, not how a specific tool does it.

## What adopting means

- You add one line or one step to each of your agent setups that loads `rules/`. `rules/README.md` shows how for common harnesses.
- Your own files can add rules. They can be stricter. If you want a rule to be looser, propose a change to the shared rule instead.
- You keep your harness, your tools and your personal workflow.

## How we iterate

The files are living documents. They do not need to be perfect before we adopt them.

- To change a rule, open a PR against `rules/`. One other human maintainer approves it.
- Each change adds a line to `rules/CHANGELOG.md`.
- We tag versions (`rules-v1`, `rules-v2`, ...). A harness can track `main` or pin a tag.
- An agent can draft a rule change. A human approves it.

## How we review the first version

1. The first version goes up as a draft PR in this repo.
2. The team has one week to comment. Comment inline on a rule, or add an open question.
3. Agreement means: each active maintainer approves, or says "no objection". Open questions are settled or deferred in writing.
4. Then we merge, tag `rules-v1`, and each of us points our agents at it.

Before step 4, nobody points an agent at these files.

## What we ask of you

- Read `rules/` (about 10 minutes).
- Mark each rule: agree, change, or remove.
- Answer the open questions in the PR.
- After `rules-v1`, point your agents at it, and propose changes when a rule does not work.
