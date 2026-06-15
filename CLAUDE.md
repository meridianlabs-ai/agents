# CLAUDE.md

@AGENTS.md

## Claude Code specifics

The shared, tool-agnostic instructions for this repo are in `AGENTS.md`
(imported above). This section is for Claude-Code-only guidance.

- This repo *configures Claude Code itself* (via claude-code-action). When
  editing the reusable workflows, verify input names and behavior against the
  [claude-code-action docs](https://github.com/anthropics/claude-code-action)
  and [code.claude.com/docs](https://code.claude.com/docs) rather than assuming
  — the action's surface changes over time.
- Useful skills when working here: `/code-review` before merging a workflow
  change; the claude-api skill for any Anthropic-API / model-id questions.
- Per-repo distribution of these instructions to other Meridian repos is
  planned but not built — see [design/shared-instructions.md](design/shared-instructions.md).
