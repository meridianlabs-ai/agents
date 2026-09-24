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
  taken by the reusable definitions; keep them in sync with `examples/`. The
  @auto stub has both halves: its CI-fix half watches `tests` (below).
- `.github/workflows/tests.yml` — this repo's CI (workflow `name: tests`): the
  full `python3 -m pytest` suite on every PR and push to main, with a
  read-only token and no secrets (see Testing a change).
- `.github/actions/*` — composite actions holding step logic shared across the
  reusable workflows (`set-stage`, `sync-branch`, `assert-no-persisted-credential`,
  `reset-origin-url`, `create-codex-user`, `assert-runner-only-path`,
  `reclaim-codex-workspace`, `import-codex-final`, `claude-agent-launcher`,
  `unresolved-merge-guard`, `provision-fallback`,
  `reset-auto-counters`, `disarm-auto-loop`, `verify-auto-labeler`,
  `drop-runner-root`,
  `bind-ci-run`, `post-pr-comment`, `resolve-reported-threads`,
  `pr-feedback-context`, `emit-landing`, `land`).
  Referenced fully-qualified
  (`meridianlabs-ai/agents/.github/actions/<name>@main`) so they resolve
  regardless of what the job checked out; put step bodies that would otherwise
  be copied between `claude.yml`, `claude-auto.yml` and `claude-auto-review.yml`
  here rather than letting the copies drift.
  `emit-landing` (end of the untrusted agent job: bundle + manifest →
  artifact) and `land` (the trusted land job's body: validate, push, post,
  stage) are the landing plumbing from #79 — design/architecture.md → Landing
  job; the manifest schema is in `.github/actions/emit-landing/README.md`.
- `.github/scripts/validate_manifest.py` — the land job's manifest validator
  (stdlib only; fails closed). `tests/` holds its pytest suite and the
  `land` helpers' tests — the repo's only unit tests: `python3 -m pytest`
  from the root.
- `examples/` — stubs copied into caller repos by `scripts/enable-claude.sh`.
- `design/` — rationale and history; read [design/architecture.md](design/architecture.md)
  before changing how the agents work.

## Conventions

- **All changes land via PR; main rejects direct pushes** (repo ruleset;
  policy: Ransom, 2026-08-26). The ruleset requires no GitHub approvals,
  but the agent review requirement below still applies.
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
- **Do NOT label PRs `auto`, and do not post `@auto`** (policy: Ransom,
  2026-09-11 — reverses the 2026-08-27 default of labeling every PR
  `auto` so the review-fix loop drove rounds to convergence). Agent work
  and review happen outside the GitHub-hosted autonomous loop, which is
  not engaged on new PRs. The `@auto` workflows themselves stay in place
  for now — this is a policy change, not a removal. Every PR requires
  agent review before merge; include a short review summary in its PR
  description and address findings on the branch. If a PR does carry the
  label (a Marvin-opened PR from an `auto` issue inherits it), don't
  race the loop — it is serialized per PR, and a session push mid-round
  invalidates its state.

- **The `@main` contract is load-bearing.** Every caller repo's stub calls these
  workflows at `@main`, so a change merged here is live everywhere immediately.
  Change deliberately; prefer backward-compatible edits to reusable-workflow
  inputs.
- **Permissions live in the `settings` input** (inline Claude Code
  `settings.json`), not `--allowedTools`. Keep them allow-lists; the reviewer
  carries a `deny` overlay. See design/architecture.md → Permissions.
- **Every reusable agent workflow declares top-level `cache-mode: read`**
  (Claude Security 4629157; design/agent-cache-scope.md): agent jobs restore
  the caller's caches and never save one. Never add a write-capable
  `cache-mode` anywhere in `.github/workflows/` or `examples/`, and keep the
  stubs' calling jobs capped at `read` (tests/test_cache_mode.py; the
  canary's control job is its one listed exception).
- **No git credential is ever written to the workspace** (issue #61,
  2026-09-09): every checkout runs `persist-credentials: false`, and every
  runner-side `git fetch`/`push` authenticates through a step-scoped
  credential helper defined in that step's `GIT_CONFIG_*` env. Copy an
  existing block verbatim: every copy reads the token from the ONE variable
  name `GIT_TOKEN` (composites set `GIT_TOKEN: ${{ inputs.<token> }}`; the
  helper runs under `sh` without `set -u`, so a block that names a variable
  the step never set fails silently with an empty password), and the helper
  key is `credential.${{ github.server_url }}.helper`, not the generic
  `credential.helper`, so no other host is ever answered with the token. The
  token is the job token for reads, the machine account's minted app token
  for pushes that must trigger CI. When adding a git network call to a
  workflow or composite, give its step that env — never rely on
  `.git/config`, and never put a token in a URL or an `http.*.extraheader`.
  A URL credential WINS over a
  helper (git never consults one when the URL carries auth), which is why
  the `reset-origin-url` step runs right after every claude-code-action
  step: the action rewrites `remote.origin.url` to carry its token, and
  until the reset every later fetch/push would use that instead of its
  helper — a revoked token on marvin-less callers. Keep that step directly
  after the action step, `always()`-gated, and put no git network call
  between the two. On the codex path, the
  `reclaim-codex-workspace` step runs unconditionally right after codex
  (`if: always() && steps.codexuser.outcome == 'success'`): it kills any
  process still running as codex, refuses a redirected git dir
  (`.git/commondir`, symlinked `.git`) and any embedded repository codex
  added (a nested `.git/config` is executed by the landing step's `git
  status`/`git add` and no restore or pin reaches it — compared against the
  list `Create codex user` snapshots, so a caller's provisioning may leave
  one), takes `.git` back and revokes the codex group's write grant on it
  and on the workspace root, restores the pre-codex `.git/config`, moves
  `.git/hooks` aside and appends `core.hooksPath`/`core.fsmonitor=false`
  to the restored config (hooks and the index are files a restore cannot
  cover, and `git fetch`/`git status` run hooks too, not just `git
  commit`) — so keep every later git-running step gated on its success
  (guard, commit/landing, the loops' manifest composer and emit-landing;
  the Surface steps set their
  error on `!= success`, not `= failure`, so a reclaim cancelled mid-run
  skips their git calls too) and put nothing that runs git between codex
  and it. Right after it, `import-codex-final` copies codex's final
  message out of the codex-owned `$RUNNER_TEMP/codex` (opened once with
  `O_NOFOLLOW`, refused unless a regular codex-owned file) to
  `$RUNNER_TEMP/codex-final.md`; every later reader — the commit subject,
  `resolve-reported-threads`, the summary body — takes that copy, and no
  runner-side step opens a path under the codex-owned dir (finding
  4628447: a planted symlink would have had the runner publish its
  target). The landing steps additionally pin
  `GIT_DIR`/`GIT_COMMON_DIR`/`GIT_WORK_TREE`, `GIT_CONFIG_GLOBAL=/dev/null`
  and the same two `core.*` keys by env as belt and braces; the guard pins
  the git dir, `GIT_CONFIG_GLOBAL` and `core.fsmonitor=false` the same way
  (`ls-files` runs no hooks); and every post-codex `git status` passes
  `--ignore-submodules=dirty`; keep all of that when touching them. **The
  job PATH is part of the same boundary** (finding 4628448, 2026-09-22):
  the runner prepends every `GITHUB_PATH` entry to every later step's PATH
  and resolves each step's shell interpreter through it, so a directory
  the codex user can write there — a workspace venv, after the grant —
  would hand codex the `sudo`, `bash` or `git` the first post-codex step
  runs as `runner`. Never put a path under `$GITHUB_WORKSPACE` on
  `GITHUB_PATH` in a job that runs codex (the codex jobs provision with
  `provision-fallback` `user: codex`, whose recipe runs under `env -i` and
  cannot reach `GITHUB_PATH`; the codex prompts get the tools by absolute
  path from the composite's `bin` output); `create-codex-user` walks every
  hop of every PATH entry (symlink targets too) before the grant, refuses a
  workspace hop, makes a codex-writable hop outside the workspace
  runner-only (the image ships `/opt` and `/usr/local/bin` mode 777) and
  refuses to start codex if one stays writable; its `reset-home` mode and
  the reclaim repeat the check without the repair
  (`assert-runner-only-path`); and every post-codex composite — and the
  `reset-home` step, which runs after provisioning as codex — pins `PATH`
  to the root-owned system directories (`system-path`, no `/usr/local`)
  before its first command; a new post-codex step should too.
  `tests/codex_path_smoke.sh` runs all of it on a hosted runner. See
  design/architecture.md → No persisted git credentials,
  design/codex-engine.md → Hook-safe landing and → Runner-side search
  path.
- **One untrusted job per engine** (Claude Security findings 4628446 and
  4629153, 2026-09-22): each reusable workflow has a Claude job (`agent`,
  `review`, `fix`) and a codex job (`agent-codex`, `review-codex`,
  `fix-codex`), the gate's `engine` output selecting one at the job level,
  and the land job `needs` both. `OPENAI_API_KEY` is referenced in the codex
  job's codex-action step and nowhere else — never add a reference to a
  job that runs the Claude agent: a referenced secret reaches the runner
  whatever the step's `if:` says. In a codex job nothing from the checkout
  runs as the runner: no `uses: ./...`, and provisioning is
  `provision-fallback` with `user: codex` (plus the caller's `provision`,
  else `codex_provision`, as `recipe`), placed after `Create codex user`,
  and followed by `Reset codex home` (`create-codex-user` with `mode:
  reset-home`: codex processes killed, `~codex/.codex` re-created) before
  the codex-action step. Once the codex user exists the runner writes
  nothing into the workspace until the reclaim: prompt files go to
  `$RUNNER_TEMP`, and `.git/info/exclude` is appended by the prep step
  before the user is created. A step that must exist on both engines
  is copied into both jobs (the checkout, assert, base and sync steps
  already are); the composers and Surface steps are per-engine.
  `tests/test_engine_job_isolation.py` enforces all of this.
- **The WIF IDs in the workflows are identifiers, not secrets** — don't treat
  them as sensitive, and don't add API-key secrets; auth is Workload Identity
  Federation.
- **Keep the README user-facing** (how to use the agents) and put rationale /
  operator detail in `design/`.
- Match existing YAML style; GitHub-expression splices (`${{ … && … || '' }}`)
  are how optional flags are composed into `claude_args`.

## Testing a change

The unit tests are in `tests/` (tests/README.md lists what each file
covers): `python3 -m pytest` from the root. Run it before pushing any change
to a workflow, composite, stub, example or script. CI runs the same command
on every PR and every push to main (`.github/workflows/tests.yml`, check
`tests / pytest`; no secrets, read-only token), so drift between the
reusable workflows and the tests that lift their steps shows up on the PR.

What that check covers is the workflows as text and their lifted `run:`
steps against stubs: the validator, the composites' scripts, the gates' trust
decisions, the structural rules (engine-job isolation, token minting, the
codex PATH boundary). What it does not cover is a live run — GitHub's
expression evaluation, the real actions, auth, the models. Those are still
validated by triggering the agents and by the hosted canaries and smoke
runs (`codex-path-smoke.yml`, `root-boundary-smoke.yml`,
`engine-isolation-canary.yml`, `cache-mode-canary.yml`):

- Comment `@claude …` (or add the `claude` label) on an issue/PR in the
  inspect_ai fork to exercise the dev agent; `@review` on a PR for the reviewer.
- Which model actually served a run is in the run's job summary ("Model
  provenance" table, per-model token counts), and a machine-account note lands
  on the issue/PR when an unexplained non-requested model served tokens (a
  subagent the agent launched on an explicit model is attributed, not flagged)
  or the result reads like a classifier refusal. Cost, duration and turn count
  are on the line under that table. The summary's served-by column is
  `modelUsage` — **not** the init line, which echoes the requested model even
  when the model fallback fired. The transcript is NOT uploaded as an artifact
  (it carries every command output and file the agent read — see
  design/architecture.md → No transcript artifacts); the job log and the
  `Surface agent errors` comment are the after-the-fact record.
- Auth/permission failures surface in that comment, the job log, and the
  Anthropic Console → Workload identity → Authentication events.

## Don't

- Don't commit secrets (there are none here by design).
- Don't break the pristine-`main` / `meridian` invariants on the inspect_ai
  fork (see design/architecture.md → The inspect_ai fork).
