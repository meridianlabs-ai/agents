# AGENTS.md — working on the `agents` repo

Instructions for any agent making changes in this repository. (This is the
agents repo's *own* instructions. The cross-repo *shared* instruction system —
distributing common rules to all Meridian repos — is designed in
[design/shared-instructions.md](design/shared-instructions.md) but **not yet
implemented**; there is no shared block here yet.)

## What this repo is

Shared agent infrastructure for Meridian: reusable GitHub Actions workflows that
run Claude Code, the thin stubs that call them, a rollout script, and design
docs. Caller repos reference the reusable workflows `@main`, so changes here
take effect on every repo's next run.

- `.github/workflows/claude.yml` — reusable dev-agent workflow (`@claude`).
- `.github/workflows/claude-review.yml` — reusable reviewer workflow (`@review`).
- `.github/workflows/*-stub.yml` — this repo's OWN caller stubs (dogfood): the
  agents run here too. `-stub` suffix because the canonical stub filenames are
  taken by the reusable definitions; keep them in sync with `examples/`. No CI
  here, so the @auto stub omits the CI-fix half.
- `.github/actions/*` — composite actions holding step logic shared across the
  reusable workflows (`set-stage`, `sync-branch`, `unresolved-merge-guard`,
  `push-base-merge`). Referenced fully-qualified
  (`meridianlabs-ai/agents/.github/actions/<name>@main`) so they resolve
  regardless of what the job checked out; put step bodies that would otherwise
  be copied between `claude.yml`, `claude-auto.yml` and `claude-auto-review.yml`
  here rather than letting the copies drift.
- `examples/` — stubs copied into caller repos by `scripts/enable-claude.sh`.
- `design/` — rationale and history; read [design/architecture.md](design/architecture.md)
  before changing how the agents work.

## Conventions

- **Make changes from a throwaway worktree, not the primary clone**
  (Ransom, 2026-08-27): Ransom works in his checkout (IDE open,
  in-progress state), so never branch-switch it — `git worktree add
  <scratch>/<name> -b <branch> origin/main`, work there, push, remove
  the worktree. Applies to every change, including quick doc PRs.
- **This repo's work is tracked on the Atlas board** (org project 1,
  `PVT_kwDOC7YMCM4BU68p`). When you open an issue or a STANDALONE PR here,
  add it to the board in the same breath — and assign Ransom (`ransomr`):
  the board's working views filter on `assignee:@me`, so an unassigned
  item is invisible to him. Add with:
  `gh api graphql -f query='mutation($p:ID!,$c:ID!){addProjectV2ItemById(input:{projectId:$p,contentId:$c}){item{id}}}' -f p=PVT_kwDOC7YMCM4BU68p -f c=<node_id>`.
  Anchored agent PRs (`claude/issue-N-*` branches) get NO card — the board's
  model is track-the-issue, link-the-PR; their issue is the card. If you
  notice an untracked open item, backfill it. (Deliberately instructions,
  not a workflow: creators here are instruction-bound sessions or anchored
  loop PRs, and a board-add workflow would need org-PAT secrets on
  pull_request_target for near-zero marginal coverage — decision: Ransom,
  2026-08-26.) PRs fixing a tracked issue should say `Fixes #N`: closing
  refs are NATIVE here (PRs base on the default branch, unlike the fork),
  so GitHub creates the Development chip and closes the issue on merge —
  no chip sweep involved; `Refs #N` stays for PRs that must not close
  their issue. Stage moves come from the agent workflows; stage cleanup on
  close is manual (see design/atlas-tracking.md → Deferred — no hourly sync
  reconciliation for this repo's items).
- **Label PRs `auto` when opening them** (default policy: Ransom,
  2026-08-27) — the review-fix loop then drives review rounds to
  convergence without babysitting. Two consequences: request the review
  (top-level `@review` comment — mandatory anyway for workflow-editing
  PRs), and once labeled, DON'T race the loop — let it address review
  findings rather than pushing your own fixes to the branch mid-round
  (it is serialized per PR; a session push mid-round invalidates its
  state). Skip the label only when deliberately keeping manual control
  of a branch you are actively iterating on. Marvin-opened PRs from
  `auto` issues inherit the label automatically.

- **The `@main` contract is load-bearing.** Every caller repo's stub calls these
  workflows at `@main`, so a change merged here is live everywhere immediately.
  Change deliberately; prefer backward-compatible edits to reusable-workflow
  inputs.
- **Permissions live in the `settings` input** (inline Claude Code
  `settings.json`), not `--allowedTools`. Keep them allow-lists; the reviewer
  carries a `deny` overlay. See design/architecture.md → Permissions.
- **The WIF IDs in the workflows are identifiers, not secrets** — don't treat
  them as sensitive, and don't add API-key secrets; auth is Workload Identity
  Federation.
- **Keep the README user-facing** (how to use the agents) and put rationale /
  operator detail in `design/`.
- Match existing YAML style; GitHub-expression splices (`${{ … && … || '' }}`)
  are how optional flags are composed into `claude_args`.

## Testing a change

There is no unit-test suite — changes are validated by triggering the agents:

- Comment `@claude …` (or add the `claude` label) on an issue/PR in the
  inspect_ai fork to exercise the dev agent; `@review` on a PR for the reviewer.
- Which model actually served a run is in the run's job summary ("Model
  provenance" table, per-model token counts), and a machine-account note lands
  on the issue/PR when a non-requested model served tokens or the result reads
  like a classifier refusal. For cost, or when the summary is gone, each run
  uploads a `claude-execution-output.json` artifact: read `modelUsage` in it —
  **not** the init line, which echoes the requested model even when the model
  fallback fired.
- Auth/permission failures surface in that artifact and in the Anthropic Console
  → Workload identity → Authentication events.

## Don't

- Don't commit secrets (there are none here by design).
- Don't break the pristine-`main` / `meridian` invariants on the inspect_ai
  fork (see design/architecture.md → The inspect_ai fork).
