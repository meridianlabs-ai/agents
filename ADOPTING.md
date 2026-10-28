# Adopt the Meridian agent rules

> [!NOTE]
> This file is for Meridian developers who set up their agents. Agents do not read this file. They read [`meridian-agents.md`](meridian-agents.md).

## Point your agent at the rules

Point your agent at [`meridian-agents.md`](meridian-agents.md), at the root of this repo. It is the one entry point for agents. Paste this block into your user-level agent instructions:

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
