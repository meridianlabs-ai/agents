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
  stays fast: branch, PR, ask for a review, merge. Mechanics: nothing
  reviews a PR on its own — auto-review-on-open is off in every Meridian
  repo (decision: Ransom, 2026-09-16; this repo's own stub never had it),
  so post a top-level `@review` comment when a review is wanted (it
  resolves the workflow from main, which is also why it works on
  workflow-editing PRs that a `pull_request` trigger would skip via the
  token exchange's workflow validation). Don't label the PR `auto` or post `@auto` — as of 2026-09-11
  (decision: Ransom) fix rounds are driven from Orca workspaces, not the
  GitHub-hosted loop (see AGENTS.md → Conventions); address the review's
  findings yourself. For
  low-blast-radius changes (skills/, design/, docs — nothing at
  `@main`), merging right after the PR opens is fine — an in-flight
  review just lands harmlessly on the merged PR; workflow PRs should
  wait for the review.
