# Proposal: shared rules for our agents

Status: draft for discussion. Nothing here is in force.

This file is for review of the draft PR only. We remove it before the PR merges. Every rule and every lasting explanation is in `meridian-agents.md` and the files it links to, and no file that lands links to this file.

Scope: these files are written for Meridian Labs employees and their agents. Anyone can read them. Nobody outside Meridian is expected to follow them, but we think they are reasonable, good practice.

## Two kinds of rules: repo-scoped and Meridian-wide

This proposal builds on Ransom's [`design/shared-instructions.md`](design/shared-instructions.md) (June 2026, on `main`). His design covers **repo-scoped rules**. This proposal adds **Meridian-wide rules**.

- **Repo-scoped rules** tell an agent how to work in one repo.
- **Meridian-wide rules** are common to all Meridian work. They are the same for every repo and every Meridian person.

Because Meridian-wide rules are not part of one repo, a repo file cannot carry them. So we make each Meridian person's own agents aware of them, through [`meridian-agents.md`](meridian-agents.md). Delivery through personal agents follows from the definition. It is not the definition.

| | **Repo-scoped rules** (Ransom's design) | **Meridian-wide rules** |
|---|---|---|
| What they are | How any agent works in one repo: code changes, PR review, security | Rules common to all Meridian work: who is who, which alerts each person's agent tracks, Slack, lessons from work |
| Who gets them | Every agent that works in the repo: CI agents, all tools, outside contributors | Each Meridian person's own agents |
| How they arrive | A shared block in each repo's `AGENTS.md`, kept in sync from `templates/AGENTS.md` | One line in each person's user-level agent setup that points at [`meridian-agents.md`](meridian-agents.md) |
| Files | [`rules/code-changes.md`](rules/code-changes.md), [`rules/pr-review.md`](rules/pr-review.md) | [`rules/ownership.md`](rules/ownership.md), "Living rules" in `meridian-agents.md` |
| Files in both | [`rules/agent-authority.md`](rules/agent-authority.md), [`rules/security.md`](rules/security.md), [`rules/public-writing.md`](rules/public-writing.md) | [`rules/agent-authority.md`](rules/agent-authority.md), [`rules/security.md`](rules/security.md), [`rules/public-writing.md`](rules/public-writing.md) |

Why two paths: the two kinds reach different agents.

- A Meridian person's own agent has a setup that the person controls. We can point it at rules in any location. That is how Meridian-wide rules arrive.
- An agent that only opens a repo has no setup that we control. Examples are a CI agent, a cloud agent, or an outside contributor's tool. It reads only the files in the repo, and most tools cannot import a file from a different location into `AGENTS.md`. So repo-scoped text must be in each repo (Ransom's point).

One source: `rules/` holds the text of both kinds. Ransom's `templates/AGENTS.md` block gets its content from the repo-scoped files. The inspect_ai fork stays a special case, as in his design.

Each file in `rules/` starts with a "Rule scope" line.

We expect some content to move from repo-scoped rules to Meridian-wide rules. Many rules that each repo's `AGENTS.md` has now (for example review bars, PR conventions and test discipline) are really common to all Meridian work. When a rule is the same in every repo, move it to `rules/` as a Meridian-wide rule, and remove the copies from each repo.

## The problem

We have two layers.

1. **Rules.** Each of us gives our agents different markdown files. The files set different bars, criteria, processes and flows. Two agents can review the same PR to two different standards.
2. **Harnesses.** We run agents in different places: GitHub Actions, desktop apps, cloud bots. This layer does not change layer 1.

Layer 2 can stay different for each person. Layer 1 should not, for the work we share.

In the long term, we might want one Meridian bot that does all of this work for the team. That is too large a change to do now. This proposal addresses the most immediate and urgent need: the agents that each of us already runs must follow the same rules for the work we share.

## The idea

Keep a small set of shared rule files in `rules/` in this repo. Each of us points our agents at them, from any harness. Then all our agents use one bar for shared work.

The files are short and harness-neutral. They say what to do, not how a specific tool does it.

Rules marked **Provisional** are starting points. They are not agreed. Change them or remove them.

## Topics

1. **Robust PR review.** The review bar and what blocks a merge. See `rules/pr-review.md`.
2. **Review between Meridian employees.** In general, the reviewer makes the change and pushes it. The reviewer does not use "request changes". **NEEDS TEAM AGREEMENT.** See `rules/pr-review.md`.
3. **Ready for review.** The bar an author meets before asking for a review. See `rules/code-changes.md`.
4. **Agent authority: what an agent may do alone.** When an agent can approve and merge with no human, and what it never does alone. See `rules/agent-authority.md`. How a bot shows authorship: see `rules/public-writing.md`.
5. **Supply chain and CI changes.** See `rules/security.md`.
6. **Tests, including broken and flaky tests.** See `rules/code-changes.md`.
7. **Claiming work.** Two agents must not do the same work. See `rules/ownership.md`: one tracker for each repo's CI and canary failures. Slack bug reports: see open question 14.
8. **Releases.** Out of scope for this proposal.

## What adopting means

- You point each of your agent setups at one file, [`meridian-agents.md`](meridian-agents.md). It is the one entry point for agents and links to all the rules. [`ADOPTING.md`](ADOPTING.md) has the block to paste and where it goes for each harness.
- Your own files can add rules. They can be stricter. If you want a rule to be looser, propose a change to the shared rule.
- You keep your harness, your tools and your personal workflow.

## How we iterate

The files are living documents. The bar is better, not perfect: today we are in a bad place, and these rules make things much better. They do not need to be perfect before we adopt them, and neither does a change to them.

- To change a rule, open a PR against `rules/`. One other human maintainer approves it.
- We tag versions (`rules-v1`, `rules-v2`, ...). A harness can track `main` or pin a tag.
- An agent can draft a rule change. A human approves it.
- When an agent learns a lesson from its own work, it asks its person, then opens a small PR that changes the rule, with one example of the problem. See "Living rules: better, not perfect" in `meridian-agents.md`. (Provisional)

## How we review the first version

1. The first version is a draft PR in this repo. We discuss it in the PR.
2. The team has one week to comment. Comment inline on a rule, or answer an open question below.
3. Agreement means: each active maintainer approves, or says "no objection". Open questions are settled or deferred in the PR.
4. Then we merge, tag `rules-v1`, and each of us points our agents at it.

Before step 4, nobody points an agent at these files.

## Open questions

1. **Which repos are in scope? (top-level discussion)** Some Meridian repos may not need agent alignment: these rules and a tracker. Which repos are in scope? Suggestions only: morpheus, veevals, condorai. Other candidates are demo, private or stale repos: flow_steps_demo, inspect-viz-example-logs, inspect_ai_dev, inspect_petri_dev, meridian-docs, meridian-fundraising, release-testing, remote-worker-dashboard, scout-hal-import, sentinel-presentation, situational-awareness, ts-mono-test. How does `rules/ownership.md` mark a repo that is out of scope? For example, a separate "Out of scope" list.
2. Is meridianlabs-ai/agents the right place for these files? We can move them later.
3. Layout and paths: one `rules/` directory with several files, or one file? Is the split right between repo-scoped rules (through Ransom's sync) and Meridian-wide rules (through `meridian-agents.md`)? See "Two kinds of rules". Ransom: does this fit your design?
4. Who approves a rule change: one other maintainer, or all active maintainers for a new rule?
5. Non-blocking findings: post them one time, or post no optional feedback at all?
6. Review between employees (topic 2): push the change, or ask first? Does this apply to design changes too?
7. Agent authority (see `rules/agent-authority.md`): is the checklist right? Who can approve: must the person opt in, and is the author's own agent excluded?
8. Claiming work (topic 7): should CODEOWNERS and GitHub review requests route and claim work? Existing examples: inspect_swarm pr 95 - a catch-all CODEOWNERS line that requests a review from one maintainer on every PR; and the CODEOWNERS lines in most of our repos that gate release PRs.
9. Is "add or make stricter only" the right rule for personal files?
10. What disclosure format do bots use? Does a bot account (for example a GitHub App) also need a prefix?
11. Tags, or track `main` only?
12. Should this repo's `AGENTS.md` and the CI reviewer prompt load `rules/pr-review.md`?
13. Is ASD-STE100 a rule we all agree on for public text, or taste to leave out for now?
14. Slack bug reports. Many of our agents can scan Slack for bug reports and feature requests. Do we want this? If yes: who tracks which channel? How do we prevent duplicate work, for example one tracker for each channel, as in `rules/ownership.md`? What does an agent do with a report: open an issue, tell its person, or reply in the thread?

## What we ask of you

- Read `rules/` (about 10 minutes).
- Comment on each rule: agree, change, or remove.
- Answer the open questions in the PR.
- After `rules-v1`, point your agents at it. Propose changes when a rule does not work.
