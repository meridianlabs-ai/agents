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
- **All changes land via PR — main rejects direct pushes** (repo
  ruleset; policy: Ransom, 2026-08-26). The `@main` contract means a
  workflow push goes live on every caller repo's next run, and the
  automated reviews caught real bugs in every round of the sync's
  evolution. The ruleset requires a PR but zero approvals, so the flow
  stays fast: branch, PR, agent review, merge. Auto-review-on-open is off
  in every Meridian repo (decision: Ransom, 2026-09-16; this repo's own
  stub never had it). Don't label the PR `auto` or post `@auto` — as of 2026-09-11
  (decision: Ransom) fix rounds run outside the GitHub-hosted loop (see
  AGENTS.md → Conventions). Address the review's findings before merge and
  summarize the review in the PR description.
