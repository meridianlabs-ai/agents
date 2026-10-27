# Shared agent rules

Status: draft. Not in force until `rules-v1` is tagged.

Scope: these files are written for Meridian Labs employees and their agents. Anyone can read them. Nobody outside Meridian is expected to follow them, but we think they are reasonable, good practice.

## Purpose

These files give all our agents one set of rules for shared work. The rules apply to every harness. Each person keeps their own harness and their own extra rules.

## Files

| File | What it covers |
|---|---|
| `pr-review.md` | The review bar: what blocks a merge, how to report findings. |
| `code-changes.md` | How an agent changes code and prepares a PR. |
| `security.md` | Untrusted input, CI execution, workflow files, supply chain. |
| `public-writing.md` | Text that other people read, and how an agent discloses itself. |
| `CHANGELOG.md` | One line for each change to these rules. |

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

## How a harness loads the rules

Use a local clone, or fetch the files at a tag. Pick one of these methods.

- **Claude Code.** Add an import to your user `~/.claude/CLAUDE.md`: `@<path to clone>/rules/README.md` and one import for each rule file.
- **Agents that read `AGENTS.md` (Codex and others).** Add this line to your user-level instructions: "Read and obey the files in `<path to clone>/rules/`."
- **A cloud bot or app.** Add a skill or setup step that reads the files from a local clone. Or fetch them from `https://raw.githubusercontent.com/meridianlabs-ai/agents/<tag>/rules/<file>`.
- **GitHub Actions.** Check out `meridianlabs-ai/agents` at a tag, with `path:` set, and add the rule files to the agent prompt.

To stay current, track `main` and pull often. To stay stable, pin a tag.

## How to change a rule

1. Open a PR that changes files in `rules/`. An agent can write the PR. A human opens or approves it.
2. Add one line to `CHANGELOG.md` that says what changed and why.
3. One human maintainer who did not write the change approves it.
4. Tag a new version when a set of changes is ready (`rules-vN`). Tell the team in the PR.

To propose a new rule, open a PR or an issue with the rule text and one example that shows the problem.
