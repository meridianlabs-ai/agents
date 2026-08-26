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
- **Workflow changes go through PR + review** (policy change, Ransom,
  2026-08-26): anything under `.github/workflows/` lands via a PR — the
  `@main` contract means a direct push goes live on every caller repo's
  next run, and the automated reviews have caught real bugs in every
  round of the sync's evolution. Two mechanics to know: auto-review-on-
  open SKIPS for workflow-editing PRs (claude-code-action's workflow-
  validation security check — the PR's copy of the file differs from
  main's), so request the review with a top-level `@review` comment,
  which resolves the workflow from main; and the loop can drive fixes
  if you add the `auto` label.
- Everything else (skills/, design/, scripts/, README) may still be
  committed directly to `main` — no feature branch needed. (This
  overrides the default "branch before committing on the default
  branch" behavior.) scripts/ run locally, not at `@main`, so their
  blast radius is a failed local invocation, not every caller repo.
