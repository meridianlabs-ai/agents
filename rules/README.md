# Shared agent rules

Status: draft. Not in force until `rules-v1` is tagged.

Scope: these files are written for Meridian Labs employees and their agents. Anyone can read them. Nobody outside Meridian is expected to follow them, but we think they are reasonable, good practice.

## Purpose

These files give all our agents one set of rules for shared work. The rules apply to every harness. Each person keeps their own harness and their own extra rules.

## Files

Each file has a rule scope. See "Two kinds of rules" below.

| File | Rule scope | What it covers |
|---|---|---|
| `pr-review.md` | Repo | The review bar: what blocks a merge, how to report findings. |
| `code-changes.md` | Repo | How an agent changes code and prepares a PR. |
| `security.md` | Repo and Meridian-wide | Untrusted input, CI execution, workflow files, supply chain. |
| `public-writing.md` | Repo and Meridian-wide | Text that other people read, and how an agent discloses itself. |
| `ownership.md` | Meridian-wide | Who is who, and who tracks CI and canary failures in each repo. |

## Two kinds of rules

These rules build on Ransom's [`design/shared-instructions.md`](../design/shared-instructions.md).

- **Repo-scoped rules** tell an agent how to work in one repo. They reach every agent in a repo through that repo's `AGENTS.md`.
- **Meridian-wide rules** are common to all Meridian work. They are the same for every repo and every Meridian person. Because they are not part of one repo, they reach each person's own agents through [`meridian-agents.md`](../meridian-agents.md).

| | **Repo-scoped rules** (Ransom's design) | **Meridian-wide rules** |
|---|---|---|
| What they are | How any agent works in one repo: code changes, PR review, security | Rules common to all Meridian work: who is who, which alerts each person's agent tracks, Slack, lessons from work |
| Who gets them | Every agent that works in the repo: CI agents, all tools, outside contributors | Each Meridian person's own agents |
| How they arrive | A shared block in each repo's `AGENTS.md`, kept in sync from `templates/AGENTS.md` | One line in each person's user-level agent setup that points at [`meridian-agents.md`](../meridian-agents.md) |
| Files | `code-changes.md`, `pr-review.md` | `ownership.md`, "Lessons from your work" in this file |
| Files in both | `security.md`, `public-writing.md` | `security.md`, `public-writing.md` |

Why two paths: the two kinds reach different agents.

- A Meridian person's own agent has a setup that the person controls. We can point it at rules in any location. That is how Meridian-wide rules arrive.
- An agent that only opens a repo has no setup that we control. Examples are a CI agent, a cloud agent, or an outside contributor's tool. It reads only the files in the repo, and most tools cannot import a file from a different location into `AGENTS.md`. So repo-scoped text must be in each repo (Ransom's point).

One source: This directory holds the text of both kinds. Ransom's `templates/AGENTS.md` block gets its content from the repo-scoped files. The inspect_ai fork stays a special case, as in his design.

Each file here starts with a "Rule scope" line.

We expect some content to move from repo-scoped rules to Meridian-wide rules. Many rules that each repo's `AGENTS.md` has now (for example review bars, PR conventions and test discipline) are really common to all Meridian work. When a rule is the same in every repo, move it here as a Meridian-wide rule, and remove the copies from each repo.

## How to read the rules

- Each file has a **Required** section and a **Recommended** section.
- Obey every Required rule.
- Obey a Recommended rule unless you have a reason. If you skip one, say why in your output.
- The repo's own `AGENTS.md`, `CLAUDE.md` and `CONTRIBUTING*` add to these rules. If a repo rule conflicts with a shared rule, obey the repo rule and report the conflict.
- Your personal rules can add to these rules or make them stricter. Do not use personal rules to make a Required rule looser.

## What goes here and what goes in a repo

All rules in this section: Provisional, from Eric.

- Put a rule here only when the team would defend it as a team convention for how agents behave in every Meridian repo.
- Put a repo's facts and procedures in that repo's `AGENTS.md`, `CLAUDE.md` or `CONTRIBUTING*`. Examples: build and test commands, gated test lists, release steps, layout.
- Keep personal preferences in personal files.
- When the team adopts a rule here, this file is the authority for it. Remove the personal copies of that rule.

## Adopt the rules

Point your agent at one file: [`meridian-agents.md`](../meridian-agents.md) at the root of this repo. It links to everything else. Paste this block into your user-level agent instructions:

```markdown
## Meridian shared agent rules
At the start of each session, run: `git -C ~/src/meridian-agents pull -q`
(First time: `git clone https://github.com/meridianlabs-ai/agents ~/src/meridian-agents`.)
If you cannot run git, read https://github.com/meridianlabs-ai/agents/blob/main/meridian-agents.md
Then read ~/src/meridian-agents/meridian-agents.md and follow it.
```

Where to paste it:

- **Claude Code.** User-level `~/.claude/CLAUDE.md`. It then applies in every repo.
- **Codex.** `~/.codex/AGENTS.md`.
- **Cursor.** Settings > Rules > User Rules.
- **Grok Bot or another bot.** The bot's profile, or a skill that loads at session start.
- **GitHub Actions.** Check out `meridianlabs-ai/agents` at a tag, with `path:` set, and add `meridian-agents.md` and the files it links to the agent prompt.

To stay current, track `main` and pull often. To stay stable, check out a tag.

## How to change a rule

1. Open a PR that changes files in `rules/`. An agent can write the PR. A human opens or approves it.
2. One human maintainer who did not write the change approves it.
3. Tag a new version when a set of changes is ready (`rules-vN`). Tell the team in the PR.

To propose a new rule, open a PR or an issue with the rule text and one example that shows the problem.

## Lessons from your work

All rules in this section: Required. Provisional, from Eric.

- When you learn a lesson from your own work, propose a rule change. Examples: your person corrects you; you and another agent do the same work; a rule is wrong, missing or not clear.
- Ask your person before you open the PR.
- Open a small PR against `meridianlabs-ai/agents` that changes the rule. Give one example of the problem.
