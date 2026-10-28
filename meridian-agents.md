# Meridian shared agent rules: start here

## Your duties

1. Read the rule files below. Obey every rule in them.
2. Help make these rules better. See "Living rules: better, not perfect" below.

## Living rules: better, not perfect

> [!NOTE]
> All rules in this section: Provisional.

These rules are a living system. They learn from our experience, and every agent that follows them helps make them better.

**Better, not perfect.** Today, each of our agents follows different rules, and that is a bad place to be. These rules make things much better. We do not hold them to the bar of perfect, and we do not hold a change to them to that bar. A change that makes the rules better is a good change, even when it does not fix everything.

Your responsibility:

- As you work, look for ways to make these rules better. Examples: your person corrects you; you and another agent do the same work; a rule is wrong, missing or not clear; a rule is the same in every repo's `AGENTS.md` and belongs here.
- When you find one, ask your person, then open a small PR against `meridianlabs-ai/agents` that changes the rule. Give one example of the problem. See "How to change a rule" below.
- Do not wait until a change is perfect. Propose it when it is better than what is here.

## Rule files

These are only the rules that we all agree on. Every rule here is one that each Meridian person will defend. Broader taste (code style, word choice, personal preferences) is not here. That is a conversation for another day. Until then, keep taste in your personal files or in a repo's `AGENTS.md`.

> [!NOTE]
> Provisional.

| File | Rule scope | What it covers |
|---|---|---|
| [`rules/pr-review.md`](rules/pr-review.md) | Repo | The review bar: what blocks a merge, how to report findings. |
| [`rules/code-changes.md`](rules/code-changes.md) | Repo | How an agent changes code and prepares a PR. |
| [`rules/security.md`](rules/security.md) | Repo and Meridian-wide | Untrusted input, CI execution, workflow files, supply chain. |
| [`rules/public-writing.md`](rules/public-writing.md) | Repo and Meridian-wide | Text that other people read, and how an agent discloses itself. |
| [`rules/agent-authority.md`](rules/agent-authority.md) | Repo and Meridian-wide | What an agent can do alone (approve, merge, close), and what it never does alone. |
| [`rules/ownership.md`](rules/ownership.md) | Meridian-wide | Who is who, and who tracks CI and canary failures in each repo. |

## Two kinds of rules

These rules build on Ransom's [`design/shared-instructions.md`](design/shared-instructions.md).

- **Repo-scoped rules** tell an agent how to work in one repo. They reach every agent in a repo through that repo's `AGENTS.md`.
- **Meridian-wide rules** are common to all Meridian work. They are the same for every repo and every Meridian person. Because they are not part of one repo, they reach each person's own agents through this file.

| | **Repo-scoped rules** (Ransom's design) | **Meridian-wide rules** |
|---|---|---|
| What they are | How any agent works in one repo: code changes, PR review, security | Rules common to all Meridian work: who is who, which alerts each person's agent tracks, Slack, lessons from work |
| Who gets them | Every agent that works in the repo: CI agents, all tools, outside contributors | Each Meridian person's own agents |
| How they arrive | A shared block in each repo's `AGENTS.md`, kept in sync from `templates/AGENTS.md` | One line in each person's user-level agent setup that points at this file |
| Files | [`rules/code-changes.md`](rules/code-changes.md), [`rules/pr-review.md`](rules/pr-review.md) | [`rules/ownership.md`](rules/ownership.md), "Living rules" in this file |
| Files in both | [`rules/agent-authority.md`](rules/agent-authority.md), [`rules/security.md`](rules/security.md), [`rules/public-writing.md`](rules/public-writing.md) | [`rules/agent-authority.md`](rules/agent-authority.md), [`rules/security.md`](rules/security.md), [`rules/public-writing.md`](rules/public-writing.md) |

Why two paths: the two kinds reach different agents.

- A Meridian person's own agent has a setup that the person controls. We can point it at rules in any location. That is how Meridian-wide rules arrive.
- An agent that only opens a repo has no setup that we control. Examples are a CI agent, a cloud agent, or an outside contributor's tool. It reads only the files in the repo, and most tools cannot import a file from a different location into `AGENTS.md`. So repo-scoped text must be in each repo (Ransom's point).

One source: `rules/` holds the text of both kinds. Ransom's `templates/AGENTS.md` block gets its content from the repo-scoped files. The inspect_ai fork stays a special case, as in his design.

Each file in `rules/` starts with a "Rule scope" line.

We expect some content to move from repo-scoped rules to Meridian-wide rules. Many rules that each repo's `AGENTS.md` has now (for example review bars, PR conventions and test discipline) are really common to all Meridian work. When a rule is the same in every repo, move it to `rules/` as a Meridian-wide rule, and remove the copies from each repo.

## How to read the rules

- Every rule in these files is a rule. There is no "recommended" tier. A rule can say "when" or "unless" to allow judgment.
- The repo's own `AGENTS.md`, `CLAUDE.md` and `CONTRIBUTING*` add to these rules. If a repo rule conflicts with a shared rule, obey the repo rule and report the conflict.
- Your personal rules can add to these rules or make them stricter. Do not use personal rules to make a shared rule looser.

## What goes in `rules/` and what goes in a repo

> [!NOTE]
> All rules in this section: Provisional.

- Put a rule in `rules/` only when the team would defend it as a team convention for how agents behave in every Meridian repo.
- Put a repo's facts and procedures in that repo's `AGENTS.md`, `CLAUDE.md` or `CONTRIBUTING*`. Examples: build and test commands, gated test lists, release steps, layout.
- Keep personal preferences in personal files.
- When the team adopts a rule in `rules/`, that file is the authority for it. Remove the personal copies of that rule.

## Adopt the rules

Point your agent at this file, `meridian-agents.md`, at the root of this repo. It is the one entry point. Paste this block into your user-level agent instructions:

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

1. Open a PR that changes this file or files in `rules/`. An agent can write the PR. A human opens or approves it.
2. One human maintainer who did not write the change approves it.
3. Tag a new version when a set of changes is ready (`rules-vN`). Tell the team in the PR.

To propose a new rule, open a PR or an issue with the rule text and one example that shows the problem.
