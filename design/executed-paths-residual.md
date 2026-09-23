# Closing the executed-paths residual (finding 4628446, criterion 2)

Status: proposed, 2026-09-23. Issue: none (task from Ransom, 2026-09-23).
Author: agent (Claude), reviewed by Codex; see the PR. Builds on PR #149
(`security/agent-land-executed-paths`, open when this was written), which
this design assumes merges as it stands.

## Why

Claude Security finding 4628446 has two fix criteria. Criterion 1 (no code
from the checkout runs as the runner in a job that later holds a credential)
was met for the codex jobs by #136: they provision as the unprivileged
`codex` user after that user exists. Criterion 2 (agent bundles that touch
paths later automation executes are refused, or land but hold automation
until a human approves) is PR #149. #149 refuses bundles that change the
*entry points*: `.github/`, agent instructions and settings, and build and
dependency configuration at any depth, plus symlink targets and `@path`
imports reached from them.

The residual, which #149 accepts and documents in SECURITY.md → Guarantees
(decision: Ransom, 2026-09-23): the list names entry points, not the code
they run. An agent's edit to an ordinary file that UNCHANGED configuration
executes still lands. Examples are a script a `claude-setup` step runs, a
command a `.claude/settings.json` hook or an `.mcp.json` server names, a
build backend on `backend-path`, and a module `setup.py` imports. The next
automated run on that branch executes it: the reviewer the `@review`
hand-back starts, a loop round, or a `@claude` follow-up. On codex jobs it
runs as the `codex` user. On Claude jobs it runs as `runner`, which has
passwordless sudo, the OIDC request token and, during the action step, the
Claude App's installation token. The Claude agent in those jobs also runs
the tree's own code (tests, `conftest.py`) as `runner`, which SECURITY.md
concedes for same-repo heads. After #149 that concession now covers heads
the pipeline wrote itself from an outsider's issue text.

#149 also costs something. It refuses every dependency bump, every lockfile
merge conflict the agent resolves, and all of this repository's `.github/`
code. It has no opt-out, so it refuses every run of two direct `land@main`
callers whose whole job is editing dependency files (Compatibility, below).

## Goals and non-goals

Goals:

- No file from an agent-produced head executes as `runner` in any later
  automated job of the reusable workflows, on either engine. That covers
  provisioning, agent start (hooks, MCP servers) and the Claude agent's
  own tool calls (tests).
- The Claude App's installation token, the OIDC request token
  (`ACTIONS_ID_TOKEN_REQUEST_*`), `ACTIONS_RUNTIME_TOKEN`, sudo and the
  Docker socket are out of reach of everything that runs as the agent.
- Claude and codex jobs share as much boundary machinery as possible
  (Ransom leans this way).
- Once that holds, let the build and dependency group of #149's list land
  again, for land callers that opt in.
- A hosted canary proves the boundary against a hostile checkout, as
  `engine-isolation-canary.yml` does for codex.

Non-goals:

- Keeping the model credential from the agent. The Claude CLI performs the
  Workload Identity exchange itself, so the agent user can read the
  audience-bound OIDC JWT and the Anthropic access token it yields. That is
  the declared exception (SECURITY.md → Adding or changing a workflow) and
  what the agent holds today. A broker is a follow-up (Not this design).
- Callers' own CI on agent PRs. A same-repo PR's CI runs the branch's code
  with CI's secrets, whoever pushed it. That is not specific to agents.
- The direct callers' own agent jobs (Compatibility lists what each needs).
- Relaxing #149's other two groups (`.github/` and agent instructions).

## Current behaviour

Line numbers are from `main` at 6a3aa91 unless another tree is named.

### What #149 refuses (its branch, 2aeddeba)

The land composite's `workflows` step diffs the bundle tip against the
branch's live tip, or the base tip for a new branch, over
`PROTECTED_PATHSPECS` in `.github/actions/land/lib.sh`. It also walks the
paths that protected entries reach (`protected_reach`: symlinks resolved
component by component, `@path` imports followed). It refuses on any
agent-changed path, and a base-merge result equal to a trusted reference
passes. The composite has no input to narrow the list (inputs:
`land/action.yml`, `token` … `pushed`).

### Where the next run executes the tree as `runner`

Every Claude job provisions from the checked-out tree before the agent,
as `runner`:

| Workflow | caller `claude-setup` (`uses: ./…`) | fallback dev-install | agent step |
|---|---|---|---|
| `claude.yml` job `agent` | 1028-1031 | 1048-1054 | 1227 |
| `claude-review.yml` job `review` (same-repo heads) | 952-958 | 975-987 | 1331 |
| `claude-auto.yml` job `fix` | 1060-1063 | 1074-1080 | 1190 |
| `claude-auto-review.yml` job `fix` | 1221-1224 | 1234-1240 | 1375 |

The step comments justify this: the Claude agent itself runs unsandboxed
as the runner and executes the tree's tests, so provisioning "gains
hostile code nothing there" (claude.yml:1018-1027). That argument is true.
It also means that fixing provisioning alone leaves the residual open: the
agent runs `pytest` over the agent-written `conftest.py` minutes later, as
the same user.

The reviewer's fork-head path (`fork_head == 'true'`, or external mode)
is different. It strips project configuration (810), copies the checkout to
a scratch tree (882), installs Claude Code's Bash sandbox (907) and does no
runner-side provisioning. `claude.yml` refuses fork heads outright (623),
and the loops skip cross-repository PRs.

### The codex jobs (the boundary D would share)

In `claude.yml` `agent-codex` the order is: `create-codex-user` (2082:
snapshot `.git/config` and the nested `.git` list, create the user, PATH
check, group-grant the workspace), then `provision-fallback` with `user:
codex` (2120), then `create-codex-user` `mode: reset-home` (2144: kill
every codex process, re-create the codex home), then codex-action (2301,
which runs `sudo -u codex -- codex exec`). After codex come
`reclaim-codex-workspace` (2332: kill, refuse a redirected git dir or a
new embedded repository, chown `.git` back, revoke the group grant on
`.git` and the root, restore the config snapshot, move hooks aside, pin
`core.hooksPath`/`core.fsmonitor`) and `import-codex-final` (2352:
`O_NOFOLLOW` copy of the one output file). The other three workflows repeat
this shape. Callers can supply a recipe through the `codex_provision`
input. No caller sets it yet (checked on the default branches of
inspect_flow, inspect_harbor, ts-mono, actions, inspect_swe,
inspect_sandboxes and inspect_scout, and on the inspect_ai fork's
`meridian`). The canary's `caller-recipes` job runs stand-in recipes for
four callers (`tests/fixtures/callers/README.md`).

### How claude-code-action launches the CLI and where its credentials are

Verified against `anthropics/claude-code-action` at `v1` (46a42b4, tagged
v1.0.232, 2026-09-23) and the Agent SDK it depends on
(`@anthropic-ai/claude-agent-sdk` 0.3.280):

- **One runner-side process does everything.** The action's `Run Claude
  Code Action` step runs `bun … src/entrypoints/run.ts` as `runner`. That
  process mints the App token (`setupGitHubToken`), runs the mode's prepare
  step, installs the CLI, sets up Workload Identity, restores configuration
  and settings, and spawns the CLI through the SDK (`runClaude`,
  run.ts:294).
- **Custom executable.** With `path_to_claude_code_executable` set, the
  action skips its own install, appends the executable's directory to
  `GITHUB_PATH` and uses it (run.ts:60-77). The SDK spawns a path that does
  not end in `.js/.mjs/.ts/.tsx/.jsx` directly, with the CLI arguments and
  the environment the action built (`sdk.mjs`, the `vHe` check before
  `spawnLocalProcess`). So a wrapper script at that path receives the full
  argv and env and can launch the real CLI as another user. There is no
  `run-as-user` input. The action pins its CLI version in run.ts:80
  (`2.1.280` at this tag).
- **Environment passed to the CLI.** A copy of the action process's env
  (`parse-sdk-options.ts:279`). It includes `GH_TOKEN` and `GITHUB_TOKEN`
  set to the App token (run.ts:190-191) and `DEFAULT_WORKFLOW_TOKEN` (the
  job token, from `action.yml`). `ACTIONS_ID_TOKEN_REQUEST_URL/TOKEN` and
  `ALL_INPUTS` are deleted (parse-sdk-options.ts:289-294).
  `ACTIONS_RUNTIME_TOKEN` and the command-file paths are not deleted.
- **MCP config on the argv.** Prepare composes `--mcp-config '<json>'`
  into the CLI arguments (tag/index.ts:181, agent/index.ts:128), and the
  SDK passes it as argv. In tag mode the `github_comment` server is always
  present, with `GITHUB_TOKEN: <App token>` in its env
  (install-mcp-server.ts:114-121). In agent mode it is present only when
  comment tools are allowed. `github_inline_comment` and `github_file_ops`
  carry the App token the same way. `github_ci` carries the job token.
  `/proc/<pid>/cmdline` is world-readable, so any local user can read that
  argv while the process lives.
- **Git.** `configureGitAuth` writes the App token into `remote.origin.url`
  (git-config.ts:132). Prepare runs git as `runner` in the workspace:
  `setupBranch` fetches and checks out (branch.ts:214-216, 315-337),
  `configureGitAuth` runs `git config`, and on PR events
  `restoreConfigFromBase` runs `git checkout origin/<base> -- <paths>` and
  `git reset` (restore-config.ts:327-338). After the CLI exits, tag mode's
  `updateCommentLink` → `checkAndCommitOrDeleteBranch` runs `git status`,
  `git add -A`, `git commit` and `git push` as `runner`. That happens only
  when the issue branch exists on origin with no commits
  (branch-cleanup.ts:39-103), which our flow never produces.
- **Workload Identity.** `setupWorkloadIdentity` (run.ts:250) fetches an
  OIDC JWT for audience `https://api.anthropic.com`. It writes the JWT to
  `$RUNNER_TEMP/claude-workload-identity/identity-token` (dir 0700, file
  0600, runner-owned) and rewrites it every 4 minutes. It writes a profile
  dir beside it and exports `ANTHROPIC_IDENTITY_TOKEN_FILE`,
  `ANTHROPIC_CONFIG_DIR` and `ANTHROPIC_PROFILE`
  (workload-identity.ts:24, 98, 149-150). The CLI does the exchange itself
  and caches the access token in that config dir. `stop()` deletes the dir
  at step end.
- **Settings.** `setupClaudeCodeSettings` writes the merged `settings`
  input to `$HOME/.claude/settings.json` of the process running the action,
  which is `runner`'s home (setup-claude-code-settings.ts:9-10). A CLI
  running with another `HOME` does not read it.

codex-action differs in the way that matters here. Its API key sits behind
`codex-responses-api-proxy`, which runs as `runner`, and codex gets only
the proxy address. claude-code-action has no proxy. The CLI process holds
the model credential and receives the App token.

### A precedent: `meridianlabs-ai/actions`

`meridianlabs-ai/actions` `.github/actions/isolated-agent` (on `main` at
af94c72) already runs the Claude CLI directly, not through
claude-code-action, as an unprivileged `claude-agent` user under
`env -i`. The key sits behind a runner-side model broker.
`check_isolation.sh` runs as that user before the agent and fails the job
unless every one of these holds:

- the user has no sudo and no Docker;
- Yama `ptrace_scope` is 1 or more;
- no other user's `/proc/<pid>/environ` or `mem` is readable;
- no `.NET` diagnostic socket is connectable;
- the runner command files are unwritable;
- the runner's `.credentials_rsaparams` is unreadable.

Its setup gives the user search-only ACLs on the ancestors it needs,
because `/home/runner` is 0750.

### Direct `land@main` callers

- **inspect_flow `inspect-update.yml`.** Scheduled or dispatched. It cuts a
  fresh branch from the default branch (`Start the release branch`), runs
  `./.github/actions/claude-setup` and claude-code-action as `runner`, and
  lands. Its task is to advance
  `[tool.inspect_flow] inspect-reconciled-version` in `pyproject.toml` and
  run `uv lock --upgrade`. #149 refuses both paths, so every run is refused.
- **inspect_flow `inspect-ai-main-failure.yml`.** Same shape (`Start the fix
  branch` from the default branch). Its fixes often touch `pyproject.toml`
  and `uv.lock`, which #149 refuses.
- **ts-mono `dependabot-fix.yml`.** It edits `package.json` overrides and
  `pnpm-lock.yaml`, so #149 refuses every run. When `continuing`, it checks
  out its own earlier agent branch and runs `pnpm install
  --frozen-lockfile` and claude-code-action as `runner`. That is this
  residual inside a direct caller, and a fix in the reusable workflows does
  not reach it.
- **actions `triage-test-failures.yml`.** `refuse-bundle: "true"`, so no
  path refusal applies. It already runs its agent isolated.

## Options compared

| | (B) approval gating | (C) dependency following at landing | (D) Claude as an unprivileged user |
|---|---|---|---|
| Closes the residual | Yes, whatever language or recipe executes it | Partly, by construction | Yes: nothing from the head runs as `runner` |
| Also closes the Claude agent running tests as `runner` | Only where the agent runs sandboxed (a new sandboxed-writer mode) | No | Yes |
| Autonomous loops | Stop for a human approval every round | Unchanged | Unchanged |
| Dependency bumps | Could land again (a human approves before automation) | Stay refused | Can land again (tier-2 opt-in, below) |
| New machinery | Push attribution, approval-at-head lookup in four gates, fork-head mode for three writing workflows | Parsers for composites, hooks, MCP commands, build backends, fail-closed | Agent-user launcher for the Claude CLI, reuse of the codex composites |
| Caller cost | Human approval latency on every agent PR | False refusals | Provisioning recipes replace `claude-setup`; no sudo or Docker for the Claude agent |

**(B) Approval gating.** A head that contains a commit the machine account
pushed is treated like a fork head in every later automated job until a
write-access human approves at that exact head. That means no provisioning,
config stripped and the sandbox. It is general: it does not need to know
what executes. The costs:

- **Push attribution.** The gates must know which commits the machine
  account pushed. Commit metadata is the agent's to write, and GitHub
  records no pusher per commit. The land job would have to mark each SHA it
  pushes (a commit status or check run, which may need an App permission
  it does not hold today). The gates would then look for the mark on every
  commit in base..head, so a human pushing on top does not launder the
  agent's commits.
- **No fork-head mode for writers.** `claude.yml` and both loops refuse
  fork heads today. B needs either a sandboxed-writer mode (a writable,
  sandboxed checkout that still stops re-planted configuration) or a refusal
  until approval. A refusal stops `@auto` every round: each loop push
  creates a fresh unapproved head.
- **Four gates change.** The approval-at-head check (a review with
  `state == APPROVED` and `commit_id == head`, by a write-access account;
  `skills/merge-approved-prs/approval_at_head.py` has the logic) enters
  four trust decisions that are already dense.

B fits a product that wants a human between every automated step. The
`@auto` loop exists not to be that.

**(C) Heuristic dependency following.** C would parse composite `run:`
lines, hook and MCP commands and `backend-path`, and fail closed on anything
unrecognised. It is partial by construction:

- inspect_flow's `claude-setup` delegates to a nested composite, and
  harbor's and ts-mono's use marketplace actions.
- Python build steps import modules. For example,
  `[tool.setuptools.dynamic] version = {attr = …}` imports the package.
- `npm`/`pnpm` scripts name arbitrary files, which then `require` more.
- Any recipe can read any file.

The fail-closed version would refuse most real changes, and the permissive
version would miss the rest. C also leaves the Claude agent running tests
as `runner`. Rejected.

**(D) Run the Claude engine as an unprivileged user**, provisioning as that
user, as codex does. This is the only option that closes the residual
without a human in every loop round. It also closes the larger concession
(the Claude agent's own test runs as `runner`), and it removes the reason
the build and dependency group of #149's list exists (Design → Land). The
source reading above shows it is feasible with the action as it is: the
wrapper hook exists, and every credential channel is either in the wrapper's
control or on the runner's side of a uid boundary. What it costs is
covered under Compatibility (no sudo or Docker for the agent; recipes
replace `claude-setup`) and Security (the model credential stays readable).

**Combinations.**

- **D alone** (keep #149's list whole) closes the residual but keeps every
  #149 cost, including the two direct callers refused on every run.
- **D + tier-2 opt-in** (recommended) relaxes only the build and dependency
  group, only for land callers that pass a new input. The reusable
  workflows pass it once their Claude jobs run as the agent user; the codex
  jobs already do. It restores dependency bumps and lockfile conflict
  resolution.
- **D + relaxing `.github/` or agent instructions** is not recommended.
  Agent instructions carry instruction authority, which a uid does not
  bound: an outsider's steering text in `CLAUDE.md` would steer every later
  run, the reviewer's verdict included. `.github/` composites are run by
  callers' other workflows, which can be privileged (`on: push` to any
  branch). Both refusals cost little.
- **B + D** adds human latency for little over D. Not recommended.

**Recommendation: D, plus the tier-2 opt-in.** Rejected variants of D are
under Alternatives considered.

## Design

### Shape of a Claude job

Each of the four Claude jobs changes from checkout → sync → provision
(runner) → action → reset URL → … to:

| # | Step | Runs as | New or changed |
|---|---|---|---|
| 1 | checkout, assert, base SHA / sync-branch | runner | unchanged |
| 2 | `create-codex-user` with `user: claude-agent` | runner (sudo) | composite gains `user` |
| 3 | `provision-fallback` with `user: claude-agent`, `recipe: inputs.provision` | claude-agent | replaces `claude-setup` and the runner fallback |
| 4 | `reclaim-codex-workspace` with `user: claude-agent` (pre-agent) | runner | new position: before the action |
| 5 | `claude-agent-launcher` | runner (sudo) | new composite |
| 6 | claude-code-action with `path_to_claude_code_executable` | runner (action); CLI as claude-agent | input added |
| 7 | `reclaim-codex-workspace` with `user: claude-agent` (post-agent) | runner | new; first step after the action |
| 8 | `reset-origin-url` | runner | now after 7 |
| 9 | `import-codex-final` in directory mode (landing and review files) | runner | composite gains a directory mode |
| 10 | model provenance, Surface, composers, emit-landing | runner | git-running steps gated on step 7 |

Steps 2-4 and 7 are the codex composites with a `user` input, so both
engines run one body. The composite names stay (renaming is churn; see Not
this design).

### The agent user

`create-codex-user` gains `user` (default `codex`) and runs its create mode
unchanged for any user. It does the following:

- snapshots `.git/config` and the embedded-`.git` list;
- adds the system user, with the same cross-enrolment of the user and
  `runner` groups;
- runs `assert-runner-only-path` with `protect: "true"` for that user;
- makes the group grant with setgid;
- sets `safe.directory` in the user's global git config.

The codex-only parts are guarded on `user == 'codex'`: the codex home
bootstrap `codex-home.sh`, the `$RUNNER_TEMP/codex` output dir and the
server-info file. For `claude-agent` it creates instead:

- `$RUNNER_TEMP/claude-agent`, owned `runner:claude-agent`, mode 2775.
  It is the landing directory the Claude agent writes to, and it replaces
  `$RUNNER_TEMP/landing` on the Claude path. It is also the reviewer's
  `$OUT_DIR`. The composers read it only through step 9's copy.
- A root-owned 0755 `/opt/meridian-agent/` that will hold the CLI and the
  launcher.

The user name `claude-agent` matches the actions repository's precedent and
avoids `agent` (harden-runner hardcodes `/home/agent`).

The `runner`-group membership is kept for parity with codex, since it is
how the user traverses `/home/runner` (0750). The boundary check below
decides whether that holds up (Open questions 3).

### Provisioning

`provision-fallback` already runs its recipe as `user` under `env -i`,
with uv bootstrapped into the user's home. The Claude jobs pass `user:
claude-agent` and the caller's recipe. The reusable workflows gain one
input, `provision`, with the same semantics as `codex_provision`, used by
both engines. `codex_provision` stays as a fallback when `provision` is
empty, so no stub breaks. The `if:` becomes the codex jobs' one: run when a
recipe is set or `pyproject.toml` exists. Caller `claude-setup` composites
are never run by the reusable workflows again. A composite can only run as
the runner.

The composite's `bin` output (venv `bin`, `node_modules/.bin`,
`~claude-agent/.local/bin`) becomes the CLI's `PATH` prefix (step 5).
Nothing goes on `GITHUB_PATH`, as on the codex path.

### Pre-agent reclaim (step 4)

The action's prepare runs git as `runner` in a workspace that provisioning
could write: fetch, checkout, `git config`, and the base-config restore on
PR events. A build backend running as `claude-agent` could have planted a
hook, repointed `core.fsmonitor`, or renamed `.git` and built its own,
because the workspace root is group-writable. So the full reclaim runs
before the action step:

- kill every `claude-agent` process until a pass finds none;
- refuse `.git/commondir`, a non-directory `.git`, or a new embedded
  repository;
- chown `.git` to runner and revoke the group's write on `.git` and on the
  root;
- restore the snapshot, move hooks aside, and pin hooksPath/fsmonitor.

This is the codex reclaim unchanged, run a second time per job. The body
moves into `reclaim.sh` next to its `action.yml`, so the launcher can run it
too. The composite's step runs the script.

The kill here is also what makes argv hiding in step 6 race-free: after it,
nothing runs as `claude-agent` until the launcher starts the CLI.

### The launcher (step 5) and the wrapper

`claude-agent-launcher` is a new composite, runner-side, run after the
pre-agent reclaim. It does four things:

1. **Installs Claude Code root-owned** under `/opt/meridian-agent/claude/`,
   at the version the action pins. It reads the version from the downloaded
   action (`$(dirname "$RUNNER_WORKSPACE")/_actions/anthropics/claude-code-action/v1/src/entrypoints/run.ts`,
   the `claudeCodeVersion` literal) and fails closed if it cannot. The
   installer is the one the action uses, run as root with `HOME` set to that
   directory.
2. **Installs the wrapper** as `/opt/meridian-agent/bin/claude` (root, 0755)
   and the supervisor next to it. The action appends this directory to
   `GITHUB_PATH` (run.ts:60-77). It is root-owned, so
   `assert-runner-only-path` passes it. The reviewer's version check
   (claude-review.yml:1379) finds `claude` there.
3. **Writes the agent's settings file.** The launcher cannot see the
   action's merged settings, because the action writes them inside its
   step. So the supervisor copies them at launch (below).
4. **Runs the isolation check as the user**: `check_isolation.sh` from
   `meridianlabs-ai/actions`, adapted with no broker probes and with the
   workspace grant expected. It fails the job before the action step if the
   user can:
   - sudo, or reach Docker;
   - read another user's `/proc/<pid>/environ` or `mem`;
   - connect to a `.NET` diagnostic socket;
   - write a runner command file;
   - read `.credentials_rsaparams`.

   It also fails when Yama `ptrace_scope` is below 1.

The claude-code-action step gains `path_to_claude_code_executable:
/opt/meridian-agent/bin/claude`.

**The wrapper** (bash, runner-side, spawned by the SDK with the action's
env and the CLI argv) has two paths:

- **Version passthrough.** For `--version`, or when not called from the
  action step (`CLAUDE_CODE_ACTION != 1`), it execs the real root-owned
  binary as `runner`. That serves the version check and nothing else, since
  the binary is trusted.
- **Launch.** Otherwise it builds the agent's launch, writes it to a
  runner-only file, and execs the supervisor with a clean argv and a minimal
  env. The steps are below.

The launch path, in order:

1. **Rewrite the argv.** Parse the arguments as data and never `eval`.
   Rewrite the one `--mcp-config` value with `jq`: drop every server whose
   `env.GITHUB_TOKEN` equals the App token (`$GH_TOKEN`).
   That removes `github_comment`, `github_inline_comment` and
   `github_file_ops` and keeps `github_ci` (job token). Refuse an argv that
   starts with `plugin` (we pass no plugins; installing one would run as the
   agent without this path's preparation). Append `--settings
   /opt/meridian-agent/run/settings.json`.
2. **Build the agent env as an allow-list**, as `env -i` input:
   - `HOME=/home/claude-agent`;
   - `PATH`: the provisioning `bin` dirs, the real CLI's dir and the
     system dirs;
   - `LANG`;
   - the `CLAUDE_CODE_*`, `DETAILED_PERMISSION_MESSAGES` and
     `GITHUB_ACTION_INPUTS` values the action set;
   - the `ANTHROPIC_*` federation IDs and model variables;
   - `ANTHROPIC_IDENTITY_TOKEN_FILE`;
   - `ANTHROPIC_CONFIG_DIR` repointed at the agent's own copy (below);
   - `GH_TOKEN` and `GITHUB_TOKEN` set to `DEFAULT_WORKFLOW_TOKEN` (the
     read-only job token);
   - `GITHUB_REPOSITORY`, `GITHUB_SERVER_URL`, `GITHUB_API_URL`,
     `GITHUB_RUN_ID`, `GITHUB_EVENT_NAME` and `MCP_*`;
   - the step's `GIT_TOKEN`/`GIT_CONFIG_*` job-token credential helper,
     as today.

   Everything else is left out: `ACTIONS_*` (runtime and cache tokens,
   OIDC request), `GITHUB_ENV/PATH/OUTPUT/STATE/STEP_SUMMARY`, `INPUT_*`,
   `OVERRIDE_GITHUB_TOKEN` and the App token. The allow-list is a fixed set
   of names in the script, not a pattern over the incoming env.
3. **Workload Identity.** Grant `claude-agent` search on
   `$RUNNER_TEMP/claude-workload-identity` and read on `identity-token` by
   named-user ACL entries. Keep the mask the way isolated-agent's
   `grant_traverse` does. Create an agent-owned 0700 copy of the profile dir
   (`configs/default.json`) as the agent's `ANTHROPIC_CONFIG_DIR`, so the
   CLI's credential cache is the agent's own. The action rewrites the token
   file in place every 4 minutes, which keeps the inode and so the ACL.
   Verify that on the canary.
4. **Settings.** Copy the action's `~runner/.claude/settings.json` (already
   merged with our `settings` input) to
   `/opt/meridian-agent/run/settings.json`, root-owned 0644, and pass it as
   `--settings` (step 1). Flag settings take precedence over the agent's
   own user settings, which it can write.
5. **Git.** Run `git remote set-url origin
   https://<host>/<owner>/<repo>.git`, which removes the App token the
   action wrote there. It runs as `runner` over the reclaimed `.git`. Then
   take a fresh `.git/config` snapshot for the post-agent restore. The
   action set `user.name`/`user.email` in prepare, and the pre-action
   snapshot lacks them. Then re-grant `claude-agent` write on `.git` and the
   workspace root (the pre-agent reclaim revoked it), so the agent can
   commit.
6. **Exec the supervisor.** Write the rewritten argv and env NUL-separated
   to a 0600 file under a runner-only 0700 dir, then `exec env -i
   PATH=/usr/bin:/bin /bin/bash /opt/meridian-agent/bin/claude-supervise
   <file>`. After the exec, `/proc/<pid>/cmdline` and `environ` show only
   that. No `claude-agent` process exists before this point, so none could
   read the original argv with the App token in its `--mcp-config`.

**The supervisor** reads the file and runs `sudo -n -u claude-agent -H --
env -i <env…> <real claude> <args…>` as its child, with stdio inherited
(the SDK's stream-json pipes). It forwards `TERM` and `INT` to the child
and waits. Then:

- it kills every `claude-agent` process until a pass finds none;
- it runs `reclaim.sh` with the fresh snapshot, so the action's own
  post-CLI code runs no git over a `.git` the agent owns;
- it removes the ACL entries and the agent's config dir;
- it exits with the CLI's status.

`claude-code-action` then finishes as today (tracking-comment update, step
outputs). The branch-cleanup git calls stay unreachable: the agent holds no
token that can create the remote issue branch. The reclaim means they would
be safe anyway.

### After the agent

- **Step 7** runs `reclaim-codex-workspace` with `if: always() &&
  steps.agentuser.outcome == 'success'`. It is idempotent after the
  supervisor's, and it covers a supervisor that was killed.
- **Every later git-running step is gated on step 7, as on the codex
  path**: `reset-origin-url`, Surface (`!= success` skips its git), the
  composers, the reviewer's re-planted-configuration check (1480) and
  emit-landing. The landing-related steps get the codex path's env pins:
  `GIT_DIR`/`GIT_COMMON_DIR`/`GIT_WORK_TREE`, `GIT_CONFIG_GLOBAL=/dev/null`,
  hooksPath and fsmonitor.
- **Step 9** generalises `import-codex-final` with a `dir` mode. It copies
  every regular, `claude-agent`-owned, single-link file directly under
  `$RUNNER_TEMP/claude-agent` whose name matches `[A-Za-z0-9._-]+` without a
  leading dot, opened `O_NOFOLLOW`, with a size cap per file and overall.
  The copies go to a runner-only `$RUNNER_TEMP/landing`. The composers and
  the reviewer's `Prepare Claude review for landing` read only that copy,
  and the land validator's body-file rules are unchanged.
- **`reset-origin-url`** stays as belt and braces, moved after step 7.
  AGENTS.md's "directly after the action step, no git between" rule becomes
  "the reclaim first, then this". The supervisor already removed the token
  from the URL before the agent started.

### Per-workflow notes

- **`claude.yml` (tag mode).** The agent loses the `github_comment` MCP
  server, and with it mid-run updates to its tracking comment. The action
  still creates the comment and marks it finished, runner-side. The
  LANDING prompt already routes answers through manifest `comments`. It
  gains a sentence: "your tracking comment is updated by the workflow; do
  not try to edit it". The agent also can no longer resolve review threads
  through `gh api graphql`, because its `gh` holds the read-only job token.
  So `manifest-extra.json` on this path accepts `resolve_threads` alongside
  `comments`, with the same `^PRRT_…` filtering `claude-auto-review.yml`
  applies (1741). `REVIEW_ETIQUETTE` says to list the threads there.
- **`claude-review.yml`.** Same-repo heads get steps 2-4. Fork heads keep
  the strip, scratch copy and sandbox install, with no provisioning, and
  run as `claude-agent` too. `srt`/bubblewrap run fine unprivileged once
  the install step (root, trusted, before the grant) has lifted the AppArmor
  userns restriction. The sandbox overlay's `.git/config` mask becomes
  redundant, because no token sits there during the agent's run. It stays.
- **`claude-auto.yml`, `claude-auto-review.yml` (agent mode).** No
  `github_*` server is composed unless the caller's `claude_args` allow
  those tools. If they do, the wrapper drops the App-token servers. The
  review-fix path already takes `replies` and `resolve_threads` from
  `manifest-extra.json`.

### Land: tier-2 opt-in

`land` gains `allow-build-config` (default `"false"`, today's #149
behaviour). When it is `"true"`, the `workflows` step splits
`PROTECTED_PATHSPECS` into two lists. Tier 1 is `.github/` and agent
instructions and settings, with the symlink and `@import` reach computed
from them. It is refused as now. Tier 2 is the build and dependency group
and is not checked. The inputs are fixed values in trusted workflow YAML.

- The four reusable land jobs pass `allow-build-config: "true"` in the same
  PR that moves their Claude jobs to `claude-agent` (implementation step 5),
  never before.
- A direct caller may pass it when every automated job that later
  provisions a branch it lands does so as a non-runner user. For
  inspect_flow's two workflows that holds today: they start from the
  default branch, and their PRs' later runs are the reusable workflows.
  ts-mono's `dependabot-fix.yml` qualifies only after its continuing run
  provisions as an agent user.
- The refusal message and the Report hint name the tier-2 files only when
  the input is off.

### What D leaves

- **The model credential.** The agent user can read the WIF JWT file while
  the step runs, and the Anthropic access token the CLI caches. A hostile
  agent-user process can spend on Meridian's workspace until the job ends.
  This is what the agent holds today, and it is the declared exception. A
  runner-side broker that does the exchange (the actions repository's
  broker is API-key based) would reduce it to a loopback token. See Not
  this design.
- **The read-only job token** is in the agent's env, as today.
- **Anything the agent user can reach, a hostile file it executes can
  reach.** That includes the tree it will commit, its landing files (still
  validated) and the network. Codex has the same boundary.
- **Direct callers' own agent jobs** keep their current shape until each
  adopts the launcher (Compatibility).

## Alternatives considered

- **D-lite: provision as the agent user, keep the Claude CLI as `runner`.**
  Simpler: no wrapper. But the agent then runs `pytest` over the same
  agent-written files as `runner`, so the residual moves minutes later
  instead of closing. Rejected.
- **D2: drop claude-code-action and run the CLI directly, as
  `actions/isolated-agent` does.** No argv or env interception is needed,
  and the action's version drift cannot break the boundary. But
  `claude.yml` depends on tag mode: the trigger check, the tracking comment,
  the issue and PR context prompt, `setupBranch` and the base-config
  restore. Rebuilding those is larger than this whole change. It is the
  fallback if the wrapper proves brittle (the canary below watches for
  that).
- **Hide other users' processes with `mount -o remount,hidepid=invisible
  /proc`, instead of exec'ing away the wrapper's argv.** It closes the argv
  and environ channels for any process. But it changes the whole VM's
  `/proc` for every tool, and the runner's behaviour under it is unmeasured.
  The exec and the empty window after the pre-agent kill are enough.
  Possible later hardening.
- **Pass `github_token: ${{ github.token }}` so no App token exists in the
  job.** It works for agent mode in principle. Tag mode's
  `createInitialComment` needs issues write, which the job token lacks,
  and the action's actor-permission lookup with the job token is
  unverified. Not needed once the wrapper keeps the App token runner-side.
- **A separate `create-claude-user` composite.** Parity with codex is the
  goal, and the snapshot, PATH check, grant, reclaim and import are
  identical. Parametrising keeps one body.
- **Remove the `runner`-group membership and grant ACL traversal instead**
  (isolated-agent's way). It would be tighter for both engines. Parity says
  change both at once or neither, so it is left to Open questions.

## Compatibility and migration

**Callers of the reusable workflows** (stubs on `@main`). Once step 5 of
the plan merges, `./.github/actions/claude-setup` stops running on Claude
jobs. That is the convention README.md and architecture.md describe.
Each caller needs a `provision:` recipe in its stubs before then (plan
step 2):

| Caller | Today on Claude jobs | Recipe (canary fixture) | Loses |
|---|---|---|---|
| inspect_ai fork (`meridian`) | `claude-setup`: uv, Python 3.11, `pip install -e ".[dev]"`, venv on `GITHUB_PATH` | `uv venv --python 3.11 && uv pip install -e ".[dev]"` (inspect-ai-like) | Docker for the agent (docker sandbox tests) |
| inspect_flow | `claude-setup` → `./.github/actions/setup` (Python 3.11, `uv sync`, uv cache) | `uv venv --python 3.11 && uv sync --dev` (inspect-flow-like) | GitHub uv cache; the "delegate to CI setup" convention |
| inspect_harbor | setup-python 3.12, setup-uv, `uv sync` | `uv venv --python 3.12 && uv sync` (inspect-harbor-like) | GitHub uv cache |
| ts-mono | pnpm/action-setup, setup-node 22 with pnpm cache, turbo cache, `pnpm install --frozen-lockfile` | `corepack enable --install-directory ~/.local/bin && pnpm install --frozen-lockfile` (ts-mono-like) | pnpm and turbo caches (slower installs) |
| inspect_swe, inspect_sandboxes, inspect_scout | runner fallback (generic dev-install) | none; the generic recipe runs as `claude-agent` | Docker (inspect_sandboxes' provider tests) |
| actions, agents (dogfood) | nothing provisioned | none | nothing |

A caller that has not added its recipe when step 5 lands gets the generic
recipe on Python repositories, or nothing (ts-mono). The run still works,
but its tools are missing, as on codex today.

**Behaviour changes for every Claude run.**

- The agent cannot `sudo` (no `apt-get` of a missing tool) and cannot
  reach Docker.
- `gh` writes fail, because the agent holds the read-only job token and not
  the App token. That ends the `claude[bot]` write channel SECURITY.md →
  By design describes, so that entry shrinks to "the action's token
  runner-side only".
- In `claude.yml` the tracking comment is not updated mid-run, and thread
  resolution goes through the manifest.
- Its files are `claude-agent`-owned, and its home, caches and git identity
  are the agent user's.

Tests needing root or Docker cannot run in the agent. That is inherent to
D, since Docker group membership is root-equivalent. Rootless Podman for
the agent user is the route if a caller needs it (Not this design).

**Direct `land@main` callers.**

- inspect_flow's `inspect-update.yml` and `inspect-ai-main-failure.yml`:
  refused on every run by #149, and working again with `allow-build-config:
  "true"` (a companion PR after plan step 6). Their own agent jobs start from
  the default branch and are unchanged.
- ts-mono's `dependabot-fix.yml`: refused by #149. To use the opt-in, it
  must first provision its continuing branch as an agent user
  (`create-codex-user`, then `provision-fallback` with `user` and its pnpm
  recipe), and ideally launch its agent through the launcher. That is a
  ts-mono companion.
- actions' `triage-test-failures.yml`: unaffected.

The default of `allow-build-config` keeps #149's behaviour, so no caller
changes by accident.

**`@main` contract.** New inputs (`provision`, the composites' `user` and
`dir`, `allow-build-config`) are additive with safe defaults.
`codex_provision` keeps working. Stored formats: the landing manifest
schema gains nothing, because `resolve_threads` already exists and only
`claude.yml`'s Claude composer starts reading it from `manifest-extra.json`.
No viewer types or eval logs are involved.

**Docs.** The implementer updates:

- SECURITY.md → Guarantees: the codex-job bullet becomes both engines;
  the #149 residual note is closed; the opt-in is described.
- SECURITY.md → By design (the App token).
- design/credential-separation.md (I4, 3.5, 7).
- design/codex-engine.md (the boundary is now shared).
- design/architecture.md (the provisioning convention).
- README.md (callers set `provision`).
- AGENTS.md (the reset-origin-url rule, and the job-isolation paragraph's
  "In a codex job nothing from the checkout runs as the runner").
- examples/ stubs.

## Security

Untrusted input reaching the new code, and how each is handled:

- **Files provisioning or the agent writes** (the tree, `.git`, the landing
  dir, the agent's home and config dir). They are never read runner-side
  before a reclaim, and the landing files only through the `O_NOFOLLOW`,
  owner-checked, size-capped copy. `.git` is taken back and its config
  restored from a runner-only snapshot before any runner-side git, both
  before the action and after the CLI. Nested repos and git-dir redirection
  are refused.
- **Processes the agent user leaves.** They are killed before the action
  step, which makes the argv window empty, and again after the CLI. The
  isolation check proves the user cannot read other users' process memory
  or environ, or reach the runner's diagnostic socket.
- **The wrapper's input** (argv and env from the SDK). The argv carries
  caller-supplied `claude_args` and the prompt text, which holds issue text.
  It is handled as an array, never evaluated. `--mcp-config` is parsed as
  JSON with `jq`, and a parse failure aborts the launch rather than passing
  the value through. The env is rebuilt from a fixed allow-list of names.
  The handoff file is runner-only.
- **The job PATH.** The launcher's directory, the one the action appends to
  `GITHUB_PATH`, is root-owned. `assert-runner-only-path` runs for
  `claude-agent` before the grant and in the reclaim.
- **Credentials:**
  - The App token stays in the runner-side action process. It never enters
    the agent's env or argv, the URL is reset before the agent starts, and
    no MCP server carries it.
  - The OIDC request token and `ACTIONS_RUNTIME_TOKEN` stay in runner-owned
    processes, behind the uid boundary.
  - The WIF JWT is readable by the agent through a named ACL removed at
    exit. That is the accepted model-credential exception.
- **What the agent can do.** A compromised agent user can write a hostile
  landing manifest, commits and body files. The land job's validator, and
  #149's tier-1 refusal, handle those as today.

## Testing

- **Unit (pytest, lifted `run:` blocks and scripts, as the existing
  suites do).**
  - The wrapper: `--mcp-config` rewriting drops exactly the App-token
    servers and keeps `github_ci`. A malformed value aborts. `plugin` argv
    is refused. `--version` passes through. The env allow-list has no
    `ACTIONS_*`, command-file or App-token value, and `GH_TOKEN` is the job
    token.
  - The supervisor's post-exit sequence, against a fake CLI and a stub
    `sudo`.
  - `reclaim.sh` run twice (idempotence).
  - `import-codex-final` `dir` mode: symlinks, hardlinks, foreign owner,
    bad names, size caps.
  - `test_land_helpers.py`: tier 2 refused by default and landed with
    `allow-build-config`, while tier 1 and its reach stay refused with it.
  - `test_engine_job_isolation.py` extended to the Claude jobs:
    - no `./` action;
    - provisioning with `user: claude-agent` between `create-codex-user`
      and the action step;
    - the pre-agent reclaim between them;
    - `path_to_claude_code_executable` set;
    - the post-agent reclaim first after the action;
    - every git-running step gated on it;
    - the land jobs pass `allow-build-config`.
  - `test_dev_agent_composer.py` covers `resolve_threads` from the Claude
    manifest-extra.
- **Hosted canary.** `engine-isolation-canary.yml` gains a `claude-boundary`
  job, which runs the real claude-code-action at `@v1` on this repository
  (the Claude App is installed here and WIF matches `meridianlabs-ai`). It
  uses this revision's composites and a probe in place of the real CLI:
  the launcher is configured to exec `tests/fixtures/claude-probe` as
  `claude-agent`. The job runs over `tests/fixtures/hostile-checkout`,
  whose backend runs as `claude-agent`, plants a `.git/hooks/post-checkout`
  and a `core.fsmonitor`, and leaves a survivor.

  Asserted:
  - the backend ran as `claude-agent`;
  - the survivor was dead before the action step;
  - no hook or fsmonitor ran as `runner` during the action's prepare
    (marker files record `id -un`);
  - the probe's report (written as `claude-agent`) shows none of the
    following: a `ghs_` value in its env; in any readable
    `/proc/*/cmdline` or `environ`; in `.git/config`; in its `--mcp-config`;
  - it also shows no `ACTIONS_ID_TOKEN_REQUEST_TOKEN` or
    `ACTIONS_RUNTIME_TOKEN`, no sudo, no Docker, no readable runner
    process memory and no connectable `.NET` socket;
  - positive controls: the probe can read the identity-token file (and
    still can after a forced refresh), and it sees the job token and its
    rewritten argv;
  - after the step, no `claude-agent` process remains, and `.git` and the
    root are back to `drwxr-sr-x` runner-owned.

  The job needs no model call: the probe exits after its report, and the
  action's failure is expected and asserted. The `caller-recipes` matrix
  gains a Claude leg (the same fixtures with `user: claude-agent`, tools run
  through the supervisor's PATH). `@v1` moves without a push here, so the
  canary gains a weekly `schedule` next to its push and dispatch triggers.
- **`codex_path_smoke.sh`** runs its PATH-boundary cases for `claude-agent`
  too.
- **One real-model run per workflow**, on scratch issues and PRs made for
  it in this repository, never on live PRs:
  - an issue run of `claude.yml` that commits and lands, with a thread
    resolution via the manifest;
  - a same-repo and a fork-head review;
  - a review-fix and a CI-fix round on a scratch `auto` PR.

  Check model provenance and that settings denies are in effect (a denied
  `git push` attempt is refused).
- **CI.** This repository has no CI. The canary and smoke workflows run on
  pushes touching the composites or harness, by dispatch, and weekly.

## Implementation plan

1. **`provision` input** (this repo). Add `provision` to the four reusable
   workflows, falling back to `codex_provision`, used by the codex jobs
   now. Update examples/, README and tests. Files: the four workflows,
   `examples/*.yml`, `tests/test_engine_job_isolation.py`.
2. **Caller stubs** (companion PRs, opened after step 1 merges): add the
   recipes in the table to the inspect_ai fork (`meridian`), inspect_flow,
   inspect_harbor and ts-mono. Verify with a dispatch on a scratch branch.
3. **Composites generalised.** `create-codex-user` gains `user` and gets
   the `claude-agent` landing dir. `reclaim-codex-workspace` gains `user`,
   with its body moved to `reclaim.sh`. `import-codex-final` gains `dir`
   mode. Unit tests. Files: `.github/actions/{create-codex-user,reclaim-codex-workspace,import-codex-final}/*`,
   `tests/test_codex_path.py`, `tests/test_import_codex_final.py`.
4. **Launcher.** `.github/actions/claude-agent-launcher/` (action.yml, the
   `claude` wrapper, `claude-supervise`, the adapted isolation check). Add
   `tests/fixtures/claude-probe`, the canary's `claude-boundary` job and
   its weekly schedule, and the wrapper unit tests. Run the canary green
   before step 5.
5. **Switch the Claude jobs.** In the four workflows, remove
   `claude-setup` and the runner fallback. Add steps 2-4, the launcher, the
   executable input, the post-agent reclaim, the moved `reset-origin-url`,
   the landing import, the gating and env pins, and `claude.yml`'s
   `resolve_threads` and prompt sentence. The same PR updates SECURITY.md,
   credential-separation.md, codex-engine.md, architecture.md, README.md
   and AGENTS.md, and extends `test_engine_job_isolation.py`. Then do the
   real-model runs.
6. **Tier-2 opt-in** (after #149 and step 5 have merged). Add `land`'s
   `allow-build-config` and the `lib.sh` split, and have the reusable land
   jobs pass it. Update tests and SECURITY.md. Then open the inspect_flow
   companion that passes it in its two workflows. ts-mono follows when its
   `dependabot-fix` provisions as an agent user.

## Open questions

1. **Accept the model credential in the agent user's reach for now?**
   Recommendation: yes. It is the declared exception and what the agent
   holds today. Build the WIF broker as its own change.
2. **Accept that Claude agents lose sudo and Docker?** Recommendation: yes.
   Docker access is root, so D cannot keep it. Rootless Podman if a caller
   asks. Does any caller rely on the Claude agent running Docker tests
   today? inspect_sandboxes is the likely one.
3. **Keep the `runner`-group cross-enrolment for `claude-agent`, as codex
   has?** Recommendation: yes for parity now, with the isolation check as
   the proof. Moving both engines to ACL traversal is a separate change.
4. **Does D plus the tier-2 opt-in meet the finding's criterion 2 as
   written?** Criterion 2 lists `pyproject.toml` among the paths to refuse.
   Under D those files execute only as the agent user, so criterion 1's
   intent holds. Recommendation: yes, and record it with the finding.

## Not this design

- A runner-side model broker for Workload Identity, so the agent user
  holds only a loopback token (the actions repository's broker is API-key
  based).
- Rootless Podman or Docker for the agent user.
- Renaming the `*-codex-*` composites to engine-neutral names once both
  engines use them.
- Moving both engines from `runner`-group traversal to ACL traversal.
- Pinning claude-code-action to a SHA, so the wrapper's contract with it
  cannot drift silently.
- Callers' CI runs agent-authored same-repo branches with CI's secrets.
  This is pre-existing and not agent-specific.
- The direct callers' own agent jobs (inspect_flow's two, ts-mono's
  `dependabot-fix`) still run claude-code-action as `runner`. They can
  adopt the launcher.
- `claude-code-action` puts the App token on the CLI's argv
  (`--mcp-config`), where every local user can read it. That is worth
  reporting upstream alongside anthropics/claude-code-action#1818.
