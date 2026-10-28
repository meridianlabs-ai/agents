# Shared agent rules

Status: draft. Not in force until `rules-v1` is tagged.

Scope: these files are written for Meridian Labs employees and their agents. Anyone can read them. Nobody outside Meridian is expected to follow them, but we think they are reasonable, good practice.

## Purpose

These files give all our agents one set of rules for shared work. The rules apply to every harness. Each person keeps their own harness and their own extra rules.

## Files

There are two kinds of rules. **Repo-scoped** rules reach every agent in a repo through that repo's `AGENTS.md` (Ransom's [`design/shared-instructions.md`](../design/shared-instructions.md)). **Personal-agent-scoped** rules reach each person's own agents through [`meridian-agents.md`](../meridian-agents.md). See "Two kinds of rules" in `PROPOSAL.md`.

| File | Rule scope | What it covers |
|---|---|---|
| `pr-review.md` | Repo | The review bar: what blocks a merge, how to report findings. |
| `code-changes.md` | Repo | How an agent changes code and prepares a PR. |
| `security.md` | Repo and personal | Untrusted input, CI execution, workflow files, supply chain. |
| `public-writing.md` | Repo and personal | Text that other people read, and how an agent discloses itself. |
| `ownership.md` | Personal | Who is who, and who tracks CI and canary failures in each repo. |
| `CHANGELOG.md` | n/a | One line for each change to these rules. |

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
2. Add one line to `CHANGELOG.md` that says what changed and why.
3. One human maintainer who did not write the change approves it.
4. Tag a new version when a set of changes is ready (`rules-vN`). Tell the team in the PR.

To propose a new rule, open a PR or an issue with the rule text and one example that shows the problem.

## Lessons from your work

All rules in this section: Required. Provisional, from Eric.

- When you learn a lesson from your own work, propose a rule change. Examples: your person corrects you; you and another agent do the same work; a rule is wrong, missing or not clear.
- Ask your person before you open the PR.
- Open a small PR against `meridianlabs-ai/agents` that changes the rule. Give one example of the problem.
