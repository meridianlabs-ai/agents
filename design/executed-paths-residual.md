# Closing the executed-paths residual (finding 4628446, criterion 2)

Status: proposed, 2026-09-23. Issue: none (task from Ransom, 2026-09-23).
Author: agent (Claude), reviewed by Codex; see the PR. Builds on PR #149
(`security/agent-land-executed-paths`, open when this was written), which
this design assumes merges as it stands. Ransom decided the four
questions the reviewed draft left open on 2026-09-23. They are recorded
where they apply: the model-credential exception (What D leaves), the loss
of sudo and Docker (Compatibility), `runner`-group parity (The agent user)
and the revised acceptance of criterion 2 for build files (Land: tier-2
opt-in).

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
  what the agent holds today. Accepted as such for now (decision: Ransom,
  2026-09-23). A broker is a separate change (Not this design).
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
every codex process, re-create the codex home; since 2026-09-23 also with
the provisioning's `bin` directories as codex's command PATH, see
Provisioning), then codex-action (2301,
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
  carry the App token the same way. `github`, added when `claude_args`
  allow `mcp__github` tools, runs `docker run … github-mcp-server` with the
  App token as `env.GITHUB_PERSONAL_ACCESS_TOKEN`
  (install-mcp-server.ts:213-229). `github_ci` carries the job token
  and runs `bun` by name (install-mcp-server.ts:197-208).
  `/proc/<pid>/cmdline` is world-readable, so any local user can read that
  argv while the process lives.
- **Completion.** The action breaks out of the SDK iterator on the first
  `result` message (run-claude-sdk.ts:198). The SDK's cleanup then waits at
  most about 2 seconds for the CLI process to exit before the action goes
  on to its tracking-comment update, branch cleanup and step outputs. So
  the action does not wait for the spawned process to finish. The reviewer
  measured this with SDK 0.3.280 and a synthetic CLI: `query()` returned
  after 2,237 ms while simulated post-exit work was still running. The
  same happens without any `result`. A malformed protocol line (for
  example `{"type":"control_response"}`) makes the SDK's reader throw, and
  an SDK abort ends the iterator. In both cases the SDK kills the process
  it spawned after its grace period, while the action process lives on.
  The action catches the error (run-claude-sdk.ts:212) and runs its
  `finally` block (run.ts:325-372). The reviewer measured 2,713 ms and
  3,003 ms with a relay still reclaiming. So an executable at
  `path_to_claude_code_executable` cannot hold back the action's post-CLI
  code on every path. Whatever the CLI's stdout carries, the process
  controls neither when the action resumes nor whether it itself survives.
- **What the action does after the CLI** (run.ts:325-372 and the
  composite's later steps, for our inputs):
  - `workloadIdentity.stop()` deletes the runner-owned token dir;
  - in tag mode with a tracking comment (`claude.yml` only),
    `updateCommentLink` makes API calls with the App token. Its
    `checkAndCommitOrDeleteBranch` runs `git status`/`add`/`commit`/`push`
    in the workspace only if the branch the prepare created (`claudeBranch`:
    set for an issue, and for a closed or merged PR, branch.ts:165-175)
    exists on origin with no commits (branch-cleanup.ts:39-103). `setupBranch` does not
    guarantee that the name is free. It regenerates the name once if
    `git ls-remote` finds it on origin, and does not check the fallback. It
    treats any `ls-remote` error as "absent" (branch.ts:280-299). The
    default name has minute resolution (branch-template.ts:102-129), and
    the reviewer showed the fallback can equal the first name. So a
    pre-existing ref can survive preparation;
  - `writeStepSummary` runs only when `display_report` is not `'false'`
    (its default is `'false'`, and we do not set it);
  - step outputs, including `github_token`, go to the runner-only
    `GITHUB_OUTPUT` file;
  - `Post buffered inline comments` runs unless `classify_inline_comments`
    is `'false'` (next bullet);
  - `Revoke app token` runs `curl -H "Authorization: Bearer <App token>"`
    (action.yml:448-459). The token is in `curl`'s argv, which every local
    user can read through `/proc/<pid>/cmdline`, and in the step script the
    runner writes into `$RUNNER_TEMP` with expressions substituted. The
    step tolerates a failed revocation (`|| true`);
  - `Cleanup SSH signing key` and `Re-prepend system bin dirs` run only
    with inputs we do not set.
- **Post-steps after the CLI.** The composite runs `Post buffered inline
  comments` (action.yml:431, unless `classify_inline_comments` is
  `'false'`) with the App token. It reads the fixed path
  `/tmp/inline-comments-buffer.jsonl` with no owner check, and without an
  `ANTHROPIC_API_KEY` (our WIF jobs) posts every record whose `confirmed`
  is not false (post-buffered-inline-comments.ts:1-16). Any local user can
  create that file. `Revoke app token` follows (`curl`, found by `PATH`).
- **Git.** `configureGitAuth` writes the App token into `remote.origin.url`
  (git-config.ts:132). Prepare runs git as `runner` in the workspace:
  `setupBranch` fetches and checks out (branch.ts:214-216, 315-337),
  `configureGitAuth` runs `git config`, and on PR events
  `restoreConfigFromBase` runs `git checkout origin/<base> -- <paths>` and
  `git reset` (restore-config.ts:327-338). After the CLI exits, tag mode's
  `updateCommentLink` → `checkAndCommitOrDeleteBranch` runs `git status`,
  `git add -A`, `git commit` and `git push` as `runner`. That happens only
  when the branch the prepare created exists on origin with no commits
  (branch-cleanup.ts:39-103). The next bullet shows that absence is not
  guaranteed.
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
  branch` from the default branch). #149 refuses the runs whose fix touches
  `pyproject.toml` or `uv.lock` (a version pin, a re-lock). A source-only
  fix still lands.
- **ts-mono `dependabot-fix.yml`** (`origin/main` 53250ae). It edits
  `package.json` overrides and `pnpm-lock.yaml`, so #149 refuses every run.
  When `continuing`, it checks out its own earlier agent branch and merges
  the default branch (195-230). It skips its runner-side `pnpm install` on
  that path (260). The claude-code-action step (267, as `runner`, with the
  job token passed as `github_token`, so no App token exists in that job)
  then runs `pnpm install`, build and test itself. That is this residual
  inside a direct caller, in the agent step itself, and a fix in the
  reusable workflows does not reach it. The batch branch's PR is also open
  to the reusable workflows (`@claude` follow-ups, reviews), which land on
  the same branch the next continuation executes.
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
under Alternatives considered. Ransom accepted the costs this carries
(decision: Ransom, 2026-09-23):
- the model credential stays in the agent's reach;
- Claude agents lose sudo and Docker;
- `claude-agent` keeps `runner`-group parity with codex;
- for build files, criterion 2 is met by the tier-2 opt-in rather than by
  refusal.

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
| 6 | claude-code-action with `path_to_claude_code_executable` and `classify_inline_comments: "false"` | runner (action); CLI as claude-agent | two inputs added |
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
- makes the group grant with setgid, unless the new `grant: "none"` input
  is set (the reviewer's fork and external heads; Per-workflow notes);
- sets `safe.directory`.

For `codex` the `safe.directory` entry stays in the user's global git
config, as now. For `claude-agent` it goes in the root-owned system config
(`git config --system --add safe.directory <workspace>`). The reviewer's
sandbox denies reads of the user's `~/.gitconfig` (claude-review.yml:1295),
and git as `claude-agent` over the runner-owned checkout would otherwise
refuse it as dubious ownership. The system file is not the agent's to
change, and the entry names only the checkout.

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
how the user traverses `/home/runner` (0750) (decision: Ransom,
2026-09-23). The in-namespace isolation check (The agent namespace, setup step 6) is the proof that it holds on the
hosted image. It fails the job if the agent can read another user's
process memory, a runner command file or the runner's credentials.
Moving both engines to ACL traversal is a separate change (Not this
design).

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
Nothing goes on `GITHUB_PATH`, as on the codex path. The codex jobs already
do the same for codex (since 2026-09-23, option B of ts-mono#694's step-2
finding; decision: Ransom): `create-codex-user`'s `reset-home` mode writes
the `bin` output into codex's `config.toml` as the PATH of every command
codex runs, ahead of sudo's PATH. Before that codex named tools by absolute
path, and ts-mono's turbo-based `pnpm check` failed because turbo looks
`pnpm` up on PATH (design/codex-engine.md → Tools in the codex sandbox).
Because those directories are agent-writable and codex's sandbox runs the
first `bwrap` on that PATH, two root-owned directories go first, one
holding only a link to the apt-installed `/usr/bin/bwrap` and one only a
copy of it under `/var/lib` (decision: Ransom, 2026-09-24; two so that one
survives codex's working-directory exclusion).
The caller recipes in the table under Compatibility are unchanged by it.

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

The kill here also means that nothing runs as `claude-agent` outside the
agent namespace (launch step 7) from this point on. The wrapper starts the
CLI inside that namespace, and nothing can join it from outside.

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
   and `agent-ns-init` next to it. The action appends this directory to
   `GITHUB_PATH` (run.ts:60-77). It is root-owned, so
   `assert-runner-only-path` passes it. The reviewer's version check
   (claude-review.yml:1379) finds `claude` there.
3. **Records the grant mode.** The launcher has an input `grant`,
   `workspace` (the default) or `none`, passed with the same value as
   `create-codex-user`'s. It writes the mode to the root-owned
   `/opt/meridian-agent/run/grant`, which the wrapper reads. The mode is
   never taken from the environment the SDK hands the wrapper. (The
   agent's settings file cannot be written here: the action merges the
   settings inside its own step, so the wrapper copies them at launch,
   below.)
4. **Runs the pre-action user check** as the user, with plain `sudo -n -u
   claude-agent` and no namespace. It checks only what exists before the
   action and does not depend on its process or files. It fails the job
   before the action step if the user can sudo or reach Docker, or if Yama
   `ptrace_scope` is below 1. Everything that depends on the namespace,
   the action's process or the WIF files is checked later, inside the
   prepared namespace, immediately before the CLI starts (The agent
   namespace, setup step 6). Only then do all three exist.

The claude-code-action step gains `path_to_claude_code_executable:
/opt/meridian-agent/bin/claude`.

**The wrapper** (bash, runner-side, spawned by the SDK with the action's
env and the CLI argv) has two paths:

- **Version passthrough.** For `--version`, or when not called from the
  action step (`CLAUDE_CODE_ACTION != 1`), it execs the real root-owned
  binary as `runner`. That serves the version check and nothing else, since
  the binary is trusted.
- **Launch.** Otherwise it builds the agent's launch, writes it to a
  runner-only file, and execs into the agent namespace with a clean argv and a minimal
  env. The steps are below.

The launch path, in order:

1. **Rewrite the argv.** Parse the arguments as data and never `eval`.
   Rewrite the one `--mcp-config` value with `jq`. Drop the five servers
   the action composes (`github_comment`, `github_inline_comment`,
   `github_file_ops`, `github`, `github_ci`) by name. Also drop any other
   server whose JSON contains a privileged token value (defined below) as a
   substring anywhere: env, args or command. Dropping `github_ci` as well,
   although it carries only the job token, avoids trusting a `bun` path for
   the agent. The agent reads CI through `gh` with the job token (`actions:
   read`) as it already can. A parse failure aborts the launch. Refuse an
   argv that starts with `plugin` (we pass no plugins; installing one would
   run as the agent without this path's preparation). Append `--settings
   /opt/meridian-agent/run/settings.json`.

   **Then check, and refuse to launch** (the wrapper exits non-zero and the
   agent never starts) if a privileged token value occurs anywhere in the
   rewritten argv or the built env. The check is by value, whatever key or
   server it sits under, so a server or variable a future `@v1` adds cannot
   carry it through unnoticed.

   **The privileged token values** are the non-empty values of the
   incoming `GH_TOKEN`, `GITHUB_TOKEN` and `OVERRIDE_GITHUB_TOKEN`, minus
   the value of `DEFAULT_WORKFLOW_TOKEN` (the job token, from `${{
   github.token }}`). There are two modes:
   - **App-token mode** (no `github_token` input: the reusable workflows).
     `GH_TOKEN` is the App token, it differs from the job token, and it is
     refused wherever it appears.
   - **Job-token mode** (`github_token: ${{ github.token }}`, as ts-mono's
     `dependabot-fix.yml:274` and inspect_flow's `inspect-update.yml:267` and
     `inspect-ai-main-failure.yml:322` pass). The action returns the
     override unchanged (token.ts:158-165) and puts it in `GH_TOKEN`
     (run.ts:190-191), so it equals `DEFAULT_WORKFLOW_TOKEN`. The set is
     then empty, and the job token the agent is meant to hold passes.

   A caller passing any other `github_token` (a PAT, a minted App token)
   has that value in the set, and it is refused like the App token. The
   job token itself is never treated as privileged. It is read-only by the
   job's `permissions:` block, which the stubs cap.
2. **Build the agent env as an allow-list**, as `env -i` input:
   - `HOME=/home/claude-agent`;
   - `PATH`: the provisioning `bin` dirs, the real CLI's dir and the
     system dirs (no `bun`: no action MCP server is started);
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
   snapshot lacks them. Then, in `workspace` mode only, re-grant
   `claude-agent` write on `.git` and the workspace root (the pre-agent
   reclaim revoked it), so the agent can commit. In `none` mode (the
   reviewer's fork and external heads) there is no re-grant. The checkout,
   its root and `.git` stay runner-owned and read-only to the agent's uid,
   so its unsandboxed Write/Edit tools cannot change them either.
6. **Refuse a pre-existing new branch** (every launch, every workflow).
   The launcher (step 5, before the action step) records what the
   workspace has checked out in the root-owned run directory: the symbolic
   ref (`git symbolic-ref -q HEAD`), or `detached:<sha>`. That is the
   default branch for an issue run, the PR head after `sync-branch` for a
   PR run, and the pinned SHA for a review. At launch the wrapper reads
   `HEAD` again. If it names a different branch, the action's prepare made
   a new branch: `setupBranch`'s `checkout -b` for an issue, and also for a
   closed or merged PR (branch.ts:165-175), including a PR that closed
   between the gate and the prepare. That new branch is the `claudeBranch`
   the post-CLI cleanup looks for. The wrapper then runs `git ls-remote
   --exit-code origin refs/heads/<branch>` as `runner` over the reclaimed
   `.git`, with the job-token helper:
   - exit status 2 (no such ref) is the only one that lets the launch
     proceed;
   - status 0 (the ref exists: a collision the fallback did not avoid)
     refuses the launch, and so does any other status (a lookup error).

   The run then fails before the agent starts. An open-PR follow-up checks
   out the PR's existing head, which equals the recorded ref, so it is
   unaffected and leaves `claudeBranch` unset. So does a detached review
   checkout. This replaces the assumption, which does not hold (Current
   behaviour → What the action does after the CLI), that `setupBranch`
   leaves the name free.
7. **Exec into the agent namespace.** Write the rewritten argv and env
   NUL-separated to a 0600 file under a runner-only 0700 dir, and `cd /`.
   Then run `exec sudo -n /opt/meridian-agent/bin/agent-ns-launch <file>
   <action pid>`. `<action pid>` is the wrapper's parent, the action's
   `bun` process. After the exec, the SDK-owned PID is `sudo`, and its
   `/proc/<pid>/cmdline` and `environ` show only that, with no App token.

**The agent namespace** gives the agent its own PID namespace and its own
mount namespace. Root sets it up; it is not an unprivileged user
namespace. Two root-owned scripts do it:

- `agent-ns-launch` runs on the host side as `sudo`'s child. It checks that
  `<action pid>` is the `bun` process of this job (its `/proc/<pid>/exe`
  and its parent chain up to `Runner.Worker`), and opens a pidfd on it
  (`pidfd_open`). It then `exec`s `setpriv --pdeathsig KILL -- unshare
  --pid --fork --kill-child --mount --propagation private --mount-proc --
  agent-ns-init <file>`, with the pidfd kept open as fd 3.
- `agent-ns-init` is a Python 3 script (stdlib only, `-I`). It runs as the
  namespace's PID 1 and stays root. It builds the agent's view, then starts
  the CLI as its only child with privileges dropped.

It builds the view in this order:

1. It first `chdir`s to `/`, so it holds no reference to the old tree
   through its working directory. The wrapper did the same before the
   exec, so no process in the chain inherits a cwd inside `/home/runner`.
2. It bind-mounts each directory the agent needs to a staging point under
   a root-only `/run/agent-ns/`:
   - the workspace;
   - `$RUNNER_TEMP/claude-agent`;
   - `$RUNNER_TEMP/scratch` (review `none` mode);
   - `$RUNNER_TEMP/claude-workload-identity`.
3. It mounts tmpfs over `/home/runner` and over `/tmp`. Then it
   bind-mounts each staged directory back at its own path: the workspace
   read-write in `workspace` mode and read-only in `none` mode, the
   landing dir and scratch read-write, the WIF dir read-only (with the ACL
   of launch step 3).
4. It detaches every staging mount (`umount -l`) and removes
   `/run/agent-ns/`, so no path reaches the original tree.
5. It closes every descriptor except 0-2 and the pidfd (`close_range`), so
   the CLI inherits no directory or setup descriptor.
6. **It runs the isolation check, inside the finished namespace**, as the
   user and exactly as the CLI will run: the same `setpriv` drop, the same
   cwd and the same environment. That is `check_isolation.sh` from
   `meridianlabs-ai/actions`, adapted with no broker probes. PID 1 runs it
   as its first child. If it exits non-zero, PID 1 exits non-zero without
   starting the CLI, the launch fails, and the action step fails before
   any agent code runs. At this point the action's process exists (its
   pidfd is held) and the WIF dir exists (the action created it before
   spawning the executable, run.ts:250), so none of the checks is vacuous.

   It fails if the user can:
   - sudo, or reach Docker;
   - see any process outside its namespace in `/proc`, or read another
     process's `environ` or `mem`;
   - connect to any `.NET` diagnostic socket (its `/tmp` is private);
   - open the runner command files, `.credentials_rsaparams`, `_actions`
     or anything else under `/home/runner` beyond the four binds;
   - reach the original tree through a relative path, a held descriptor
     or `/proc/self/cwd`.

   It also fails when Yama `ptrace_scope` is below 1. The launcher passes
   the absolute paths it expects to be unreachable (the command files, the
   runner root) in the handoff file, so the probes test real paths.

   The grant mode's expectations are checked too. In `workspace` mode the
   checkout is writable. In `none` mode the user cannot create or rename a
   root entry, write a tracked file, or write or rename `.git`, and can
   write scratch. A missing WIF dir, workspace or landing dir fails the
   setup closed rather than skipping its bind.
7. It opens the final workspace bind by its absolute path, freshly
   resolved, and starts the child with that as its cwd. That is `setpriv
   --reuid claude-agent --regid claude-agent --init-groups
   --inh-caps=-all --bounding-set=-all -- env -i <env…> <real claude>
   <args…>`, with the pidfd closed in the child. The child inherits the
   SDK's stdio.

From inside, the agent sees:

- `/proc` is the namespace's own mount, from `--mount-proc`. The agent
  sees only its own processes, so no host process's `cmdline`, `environ`
  or `cwd` is visible to it: not the `sudo`/`unshare` chain, the action's
  `bun`, `Runner.Worker` or the revoke step's `curl`. Its own
  `/proc/self/cwd` and `/proc/self/fd/*` point only into the final binds.
  So does PID 1's, since PID 1 also changed directory before the mounts.
- `/tmp` is private, so no `.NET` diagnostic socket and no
  `/tmp/inline-comments-buffer.jsonl` is shared with the host.
- Under `/home/runner` only the four binds exist. `$RUNNER_TEMP`'s step
  scripts and command files, `_actions` and the runner's install directory
  are unreachable by absolute path, and no relative path or inherited
  descriptor leads back to them.
- `/opt/meridian-agent` and the agent's own home are outside `/home/runner`
  and stay as they are.

The agent has no capabilities in that namespace, so it cannot mount,
unmount or re-mount anything, nor `setns` into the host's namespaces.
Nothing outside can join the namespace, because only root could, and the
pre-agent kill left no `claude-agent` process outside it.

**The namespace's lifetime is enforced from the SDK-owned PID.** The
namespace ends, and the kernel kills every process in it however it was
started (`setsid`, `nohup`, double fork, a stopped process), in any of
these events:

- **The CLI exits.** PID 1 exits when its child does.
- **The action process exits.** PID 1 polls the pidfd and, when the
  action's `bun` is gone, `SIGKILL`s its child and exits.
- **The SDK-owned PID (`sudo`) dies, whatever the signal.** `setpriv
  --pdeathsig KILL` gave `unshare` a parent-death signal from `sudo`. The
  pdeath signal survives the `exec` of the non-setuid `unshare`.
  `unshare`'s death then fires `--kill-child`'s `SIGKILL` at PID 1.
- **`unshare` or PID 1 is killed directly** (job cancellation kills the
  step's processes). `--kill-child` covers `unshare`. PID 1's death ends
  the namespace by definition.

None of these relies on the CLI cooperating. A CLI that ignores EOF and
`TERM`, or is stopped, still dies with the namespace. The SDK sends
`TERM` and then, 5 seconds later, `KILL` to the PID it spawned
(`sdk.mjs`, the process-exit handler). `unshare --fork` ignores `TERM`,
so it is the `KILL` of `sudo` that ends the namespace, at most about 5
seconds after the SDK gives up on the CLI. If the SDK never kills it (its
timers are `unref`'d), the action process's exit does, through the
pidfd. An earlier revision claimed that killing `sudo` closes the CLI's
stdio. It does not: descendants keep their inherited pipe ends, which is
why the chain above ties teardown to process death, not to EOF.

**No supervisor process.** Nothing on the host side runs agent-related
code after the CLI. Cleanup after the agent is step 7's alone: it kills
any leftover (normally none), reclaims, removes the WIF ACL entries and
deletes the agent's config dir (in its home).

**Why this is enough while the action continues.** The action's error and
abort paths resume the action before the spawned process is gone (Current
behaviour → Completion), so no executable at the hook can gate them. The
design instead makes every piece of action code after the CLI harmless
for as long as agent processes might live. Taking the list under Current
behaviour → What the action does after the CLI in turn:

- **Deleting the token dir, step outputs, the step summary (off) and the
  inputs we do not set** read or execute nothing the agent can write.
- **The tracking-comment update** (tag mode only) makes API calls from the
  runner-owned execution file the SDK wrote. Its text is the agent's
  output, as today.
- **The branch cleanup's git calls** are the only action code that runs git
  in the workspace, and they need `claudeBranch` (a branch the prepare
  created: an issue run, or a closed or merged PR) to exist on origin.
  Launch step 6 has proven any such branch absent, failing closed on a
  lookup error. The agent cannot create it: it holds only the read-only job
  token, the launch refuses any privileged token value in its argv or
  env, the URL is credential-free, and every App-token MCP server is
  dropped. So the ref can appear during the run only through a
  write-access human or the machine account, and neither acts for the
  agent. Agent-mode workflows (the reviewer and both loops) and open-PR
  runs of `claude.yml` create no new branch. For them `claudeBranch` is
  unset and this path does not exist.
- **`Post buffered inline comments`** is disabled by input (below). The
  agent's `/tmp` is private in any case.
- **`Revoke app token`** puts the App token in `curl`'s argv and in a step
  script under `$RUNNER_TEMP`. A surviving agent process cannot see
  either. Host `/proc` entries are outside its PID namespace, and
  `$RUNNER_TEMP` apart from the three bind-mounted directories is under
  its tmpfs. `curl` itself resolves through runner- and root-owned PATH
  directories. The ones the action step adds (setup-bun's under
  `~runner`, the launcher's) are created by runner or root in places the
  agent cannot create or replace entries. The restriction holds for the
  whole interval, whether or not revocation is slow or fails.

Live agent processes during that window can still rewrite the tree and
`.git` through their bind mounts. Nothing runner-side reads either until
step 7, which kills first and then reclaims.

**Reassessment.** The previous revision said a parent-side gate would
close no remaining exposure. Round 3 of the review showed otherwise: the
revoke step's argv and the unproven branch absence were both open while
survivors lived. The namespace closes the first for the whole interval,
survivors or not. Launch step 6 closes the second. A parent-side gate
would still need a fork of the action or D2. If a future `@v1` adds
post-CLI code that runs git in the workspace or reads a path the agent can
write through its bind mounts, this argument breaks. The canary asserts
the list above (Testing), and pinning the action (Not this design) would
freeze it.

**The buffered inline-comment post-step is disabled.** All four workflows
pass `classify_inline_comments: "false"` to the action. That input skips
the action's `Post buffered inline comments` step, which would otherwise
post, with the App token, any records a local process left in
`/tmp/inline-comments-buffer.jsonl`. The reviewer's inline findings reach
the PR only through `inline.json` and the landing manifest, as today. The
agent's private `/tmp` means the step would not see an agent-written
buffer anyway. That is belt and braces.
`test_engine_job_isolation.py` requires the input on every
claude-code-action step.

### After the agent

- **Step 7** runs `reclaim-codex-workspace` with `if: always() &&
  steps.agentuser.outcome == 'success'`. It is the only cleanup after the
  agent, and it runs whether the agent step succeeded, failed or was
  cancelled. It kills first (normally nothing is left: the namespace ended
  with the CLI), then removes the WIF ACL entries and the agent's config
  dir, then refuses or reclaims exactly as the codex reclaim does. If it
  refuses, every git-running step below is skipped.
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
  "the reclaim first, then this". The wrapper already removed the token
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
- **`claude-review.yml`, same-repo heads.** Steps 2-10 as in the table.
  The workspace grant is needed because provisioning writes `.venv` at the
  root. The reviewer lands no bundle, so what the agent edits in the tree
  goes nowhere.
- **`claude-review.yml`, fork heads and external mode.** These keep their
  existing protections: the strip, the read-only checkout for sandboxed
  commands, the writable scratch copy, `OUT_DIR` on `denyWrite`,
  `--setting-sources user`, and the re-planted-configuration check. The
  agent's uid changes, so the setup changes too. In order:
  1. checkout, `Record checkout SHA` and the strip, as now (runner);
  2. `create-codex-user` with `user: claude-agent` and `grant: "none"`.
     This takes the snapshots, creates the user, runs the PATH check and
     writes the system `safe.directory`. The checkout gets no group grant:
     it stays runner-owned and read-only to the agent's uid. So the agent's
     own Write/Edit tools, which the sandbox does not govern, can no longer
     edit it either, which is a tightening;
  3. the scratch copy, as now (`cp -a` into `$RUNNER_TEMP/scratch/src`),
     then `sudo chown -R claude-agent:claude-agent "$RUNNER_TEMP/scratch"`.
     The copy is untrusted content that no runner-side step reads again
     (the re-planted check compares the checkout, not the copy). So the
     agent's uid, and the sandboxed commands it runs, can install and test
     there. The copy's `.git` is agent-owned, so git there needs no
     `safe.directory`;
  4. `Install review sandbox`, as now (root; it lifts the AppArmor userns
     restriction bubblewrap needs for an unprivileged user);
  5. the launcher with `grant: "none"` (so the wrapper does not re-grant
     and the isolation check expects a read-only checkout), then the action
     step and steps 7-10. Step 4 of the table (the pre-agent reclaim) is
     skipped, since no provisioning ran and nothing was granted. The kill
     is still needed and moves into the launcher's start.

  **The sandbox overlay's `.git/config` mask is removed on these paths.**
  It uses `extract` and `onExtractNoMatch: "deny"`, so a config with no
  token becomes unreadable to sandboxed commands, and git in the checkout
  (the reviewer's `git diff`) would break. Its job moves into the wrapper,
  which refuses to launch unless `.git/config` is free of the App token
  (launch step 5 resets the URL first, and the by-value check covers it).
  The overlay keeps denying `~/.gitconfig`. `safe.directory` now comes
  from the system config, which the sandbox does not deny. A hosted test
  (Testing) runs `git diff` in the checkout and an install and test in
  scratch as `claude-agent` under the real sandbox with this overlay.
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

**This is how criterion 2 is met for tier 2** (decision: Ransom,
2026-09-23: the revised acceptance is accepted). As written, finding
4628446's criterion 2 asks that bundles touching build configuration be
refused, or land and hold automation until a human approves. With D and the
opt-in, such files land. Every automated job that executes them does so
only as an unprivileged user holding nothing but the read-only job token.
That is criterion 1's remedy, and it is accepted as meeting criterion 2
for tier 2. Tier 1 still meets criterion 2 literally, by refusal. The
implementation PR (plan step 6) records this in SECURITY.md → Guarantees
with the finding number, and on the finding's Security board item.

A land job may pass the opt-in only when **every automated consumer of the
branches it pushes** runs both its provisioning and its agent (the CLI and
every tool call) as a non-runner user. Provisioning alone is D-lite, which
this design rejects. A consumer is any job that later checks out and runs
code from such a branch. The rule is per repository, not per workflow.
Several writers push to the same branches: a `@claude` follow-up or a
review-fix round on a Dependabot batch PR lands on the branch the next
`dependabot-fix` continuation checks out. So:

- **The reusable workflows gain an input `allow_build_config`**, default
  `false`, which their land jobs pass through as `allow-build-config`. It
  is honoured only from implementation step 6 on, after step 5 has moved
  the Claude jobs to `claude-agent`. It is a trusted per-caller decision:
  the stub is a workflow file on the default branch. A caller's stubs set
  it to `true` only when no other automation in that repository executes
  agent-landed branches as `runner`. Callers' CI is out of scope here, as
  under Goals.
- **Direct callers pass `allow-build-config` under the same rule.**
  inspect_flow's `inspect-update.yml` and `inspect-ai-main-failure.yml`
  start from the default branch every run. inspect_flow's only other
  consumers of its agent branches are the reusable workflows. So
  inspect_flow can set it in both direct workflows and in its stubs once
  step 6 is in.
- **ts-mono sets neither until `dependabot-fix.yml` is migrated whole.**
  Its continuation must provision the batch branch as an agent user
  (`create-codex-user`, then `provision-fallback` with `user` and its pnpm
  recipe, then the pre-agent reclaim). It must also launch its agent
  through `claude-agent-launcher` with the post-agent reclaim. Only then do
  its `dependabot-fix` land job and its stubs opt in. Until then its stubs
  keep `allow_build_config: false`. So neither a follow-up on a batch PR
  nor the scheduled run can land build files that a runner-uid
  continuation would execute. This is a companion change in ts-mono and a
  prerequisite for ts-mono's opt-in, not for anyone else's.
- The refusal message and the Report hint name the tier-2 files only when
  the input is off.

### What D leaves

- **The model credential.** The agent user can read the WIF JWT file while
  the step runs, and the Anthropic access token the CLI caches. A hostile
  agent-user process can spend on Meridian's workspace until the job ends.
  This is what the agent holds today, and it is the declared exception,
  accepted for now (decision: Ransom, 2026-09-23). A runner-side broker that does the exchange (the actions repository's
  broker is API-key based) would reduce it to a loopback token. See Not
  this design.
- **The read-only job token** is in the agent's env, as today.
- **Anything the agent user can reach, a hostile file it executes can
  reach.** That includes the tree it will commit, its landing files (still
  validated) and the network. Codex has the same boundary.
- **Direct callers' own agent jobs** keep their current shape until each
  adopts the launcher (Compatibility).
- **Drift of `claude-code-action@v1`.** The wrapper depends on the action's
  launch contract: the custom-executable path, the argv, the MCP server
  set, and the post-steps after the CLI. `@v1` moves without a push here.
  The by-value token check fails closed on a new token-bearing channel in
  the argv or env. A new post-step, or a change to completion handling,
  would show up only at the next weekly canary. That is an operational
  risk Ransom should keep in view. Pinning the action is the remedy, under
  Not this design.

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
- **A completion barrier in a supervisor process** (an earlier revision). It
  would hold the CLI's `result` until the kill and reclaim were done. The
  SDK ends the iterator on a malformed protocol line or an abort without
  any result, then kills the spawned process and resumes the action, so
  the barrier holds only on the happy path. Rejected in favour of showing
  the action's post-CLI code harmless in an unreclaimed state
  (Launcher → Why this is enough). A real parent-side gate needs D2 or a
  fork of the action.
- **Hide other users' processes with `mount -o remount,hidepid=invisible
  /proc` on the host, instead of the agent namespace.** It would close the
  argv and environ channels too. But it changes `/proc` for every process
  on the VM, the runner's behaviour under it is unmeasured, and it hides
  neither the step scripts under `$RUNNER_TEMP` nor kills survivors. The
  namespace does all three for the agent alone.
- **Pass `github_token: ${{ github.token }}` so no App token exists in the
  job.** ts-mono's `dependabot-fix.yml` already runs agent mode this way
  (267), so it works there. Tag mode's `createInitialComment` needs issues
  write, which the job token lacks, so `claude.yml` cannot follow. Agent
  mode's actor-permission lookup with the job token is unverified for the
  reviewer's and loops' bot actors. This is a worthwhile simplification for
  the three agent-mode workflows, but not needed once the wrapper keeps
  the App token runner-side (Not this design).
- **A separate `create-claude-user` composite.** Parity with codex is the
  goal, and the snapshot, PATH check, grant, reclaim and import are
  identical. Parametrising keeps one body.
- **Remove the `runner`-group membership and grant ACL traversal instead**
  (isolated-agent's way). It would be tighter for both engines. Parity says
  change both at once or neither. Ransom kept parity now (decision:
  2026-09-23), and both engines' move is a separate change.

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
| actions | nothing provisioned | none | nothing |
| agents (dogfood) | nothing provisioned | `uv venv --python 3.12 && uv pip install pytest` (added 2026-09-24, after the step-5 real-model runs found the dev agent installing pytest itself; decision: Ransom) | nothing |

A caller that has not added its recipe when step 5 lands gets the generic
recipe on Python repositories, or nothing (ts-mono). The run still works,
but its tools are missing, as on codex today.

**Behaviour changes for every Claude run.**

- The agent cannot `sudo` (no `apt-get` of a missing tool) and cannot
  reach Docker.
- `gh` writes fail, because the agent holds the read-only job token and not
  the App token. The same goes for every other channel to a `claude[bot]`
  write that the agent can drive: MCP servers dropped, buffered inline
  comments disabled, the URL reset, and the branch cleanup's git gated on
  a branch the agent cannot create. So the `claude[bot]` write channel SECURITY.md → By design
  describes ends, and that entry shrinks to "the action's token, used by
  the action's own runner-side code only".
- In `claude.yml` the tracking comment is not updated mid-run, and thread
  resolution goes through the manifest.
- Its files are `claude-agent`-owned, and its home, caches and git identity
  are the agent user's.

Tests needing root or Docker cannot run in the agent. That is inherent to
D, since Docker group membership is root-equivalent, and it is accepted
(decision: Ransom, 2026-09-23). Docker-dependent verification (for
example inspect_ai's docker sandbox tests, inspect_sandboxes' local
provider tests) happens elsewhere: in CI or on a maintainer's machine.
Rootless Podman for the agent user is the route if a caller later needs it
in the agent (Not this design).

**Direct `land@main` callers.**

- inspect_flow's `inspect-update.yml` is refused on every run by #149.
  `inspect-ai-main-failure.yml` is refused whenever its fix touches
  `pyproject.toml` or `uv.lock`. Both work again with `allow-build-config:
  "true"` (a companion PR after plan step 6, together with
  `allow_build_config: true` in inspect_flow's stubs). Their own agent jobs
  start from the default branch and are unchanged.
- ts-mono's `dependabot-fix.yml` is refused on every run by #149. Before
  ts-mono opts in anywhere, its continuation must run both provisioning and
  the agent as an agent user, with both reclaims (Land: tier-2 opt-in).
  That is a ts-mono companion. Until it lands, ts-mono's stubs and
  `dependabot-fix` keep the default, so build files stay refused on every
  ts-mono branch.
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
  in-namespace isolation check, which runs right before the CLI, proves
  the user cannot read other users' process memory or environ, or reach
  the runner's diagnostic socket or files.
- **The wrapper's input** (argv and env from the SDK). The argv carries
  caller-supplied `claude_args` and the prompt text, which holds issue text.
  It is handled as an array, never evaluated. `--mcp-config` is parsed as
  JSON with `jq`, and a parse failure aborts the launch rather than passing
  the value through. The env is rebuilt from a fixed allow-list of names.
  No privileged token value (anything but the job token) may then occur in
  the result. The handoff file is runner-only.
- **The CLI's stdout** (agent-controlled JSON lines) goes straight to the
  SDK, as today. Nothing runner-side in this design parses it. A malformed
  or early message can end the action's iterator early, which is why
  nothing depends on when that happens.
- **The action's code after the CLI** may run while agent processes are
  alive and the workspace is unreclaimed. Every piece of it is listed
  under Launcher → Why this is enough, with why each is harmless in that
  state:
  - the only workspace git is gated on a remote branch the agent cannot
    create;
  - the only reader of an agent-writable file with the App token,
    `Post buffered inline comments`, is disabled by input;
  - `curl` resolves through runner- and root-owned PATH directories.

  Step 7, the first workflow step after the action, kills and reclaims
  before any workflow git.
- **The namespace setup** runs as root from root-owned files before the
  drop to `claude-agent`. It takes no input from the agent: the bind list
  is fixed by the grant mode, and the handoff file is runner-only. The
  agent gets no capabilities (`--inh-caps=-all`) and so cannot change its
  mounts or enter the host's namespaces.
- **The job PATH.** The launcher's directory, the one the action appends to
  `GITHUB_PATH`, is root-owned. `assert-runner-only-path` runs for
  `claude-agent` before the grant and in the reclaim.
- **Credentials:**
  - The App token stays in runner-side processes and files: the action
    process, the revoke step's `curl` argv and its step script under
    `$RUNNER_TEMP`. The agent runs in its own PID and mount namespace, so
    for its whole lifetime, and any survivor's, it sees no host process
    and nothing under `/home/runner` except its bind mounts. The token also
    never enters the agent's env or argv (refused by value), the URL is
    reset before the agent starts, and no MCP server carries it.
  - The OIDC request token and `ACTIONS_RUNTIME_TOKEN` stay in runner-owned
    processes, behind both the uid boundary and the namespace.
  - The WIF JWT is readable by the agent through a named ACL and a
    read-only bind mount, both removed or gone at exit. That is the
    accepted model-credential exception (decision: Ransom, 2026-09-23).
- **Survivors.** Agent processes end with the namespace. The namespace
  ends when the CLI exits, when the SDK-owned `sudo` dies (by any signal,
  through a parent-death signal on `unshare` and `--kill-child`), or when
  the action process exits (a pidfd PID 1 polls). No path depends on the
  CLI cooperating. Until then, the action's post-CLI code is harmless to
  them by construction (Launcher → Why this is enough). The only workspace
  git in it requires a prepare-created branch that launch step 6 proved
  absent, failing closed on a lookup error.
- **Path escapes out of the namespace.** Every process in the chain
  `chdir`s to `/` before the tmpfs covers `/home/runner`, the staging
  binds are detached, descriptors other than stdio are closed, and the CLI
  starts with a freshly resolved cwd inside the final bind. So no cwd,
  descriptor or `/proc/*/cwd` link reaches the original tree.
- **What the agent can do.** A compromised agent user can write a hostile
  landing manifest, commits and body files. The land job's validator, and
  #149's tier-1 refusal, handle those as today.

## Testing

- **Unit (pytest, lifted `run:` blocks and scripts, as the existing
  suites do).**
  - The wrapper, over synthetic instances of all five servers the action
    composes (`github_comment`, `github_inline_comment`, `github_file_ops`,
    `github` with `GITHUB_PERSONAL_ACCESS_TOKEN`, `github_ci`), plus a
    caller-style server carrying the App token in an arg:
    - every one of them is dropped;
    - an unrelated caller server with no token survives;
    - an App-token value planted in any other argv position or env
      variable refuses the launch;
    - in job-token mode (`OVERRIDE_GITHUB_TOKEN` = `GH_TOKEN` =
      `DEFAULT_WORKFLOW_TOKEN`), the launch proceeds and the agent's
      `GH_TOKEN` is that token;
    - a `github_token` override distinct from the job token is refused
      wherever it appears;
    - a malformed `--mcp-config` aborts;
    - `plugin` argv is refused;
    - `--version` passes through;
    - the env allow-list has no `ACTIONS_*`, command-file or App-token
      value, and `GH_TOKEN` is the job token.
  - The new-branch precondition, against a stub `git`, with the recorded
    pre-action ref and the post-prepare `HEAD` set for each case:
    - an issue run (default branch recorded, a new `claude/issue-…`
      checked out): `ls-remote` exit 2 proceeds; exit 0 (the ref exists)
      and exit 128 (lookup error) refuse;
    - the reviewer's fixed-clock case, where `setupBranch`'s fallback name
      equals its first name and both exist, refuses;
    - a closed PR and a merged PR (a new `claude/pr-…` checked out) are
      checked the same way;
    - a PR that was open at the gate and closed before the prepare, so a
      new branch appears, is checked;
    - an open-PR follow-up (the recorded head equals `HEAD`) and a detached
      review checkout do no lookup and proceed.
  - `reclaim.sh` run twice (idempotence).
  - `import-codex-final` `dir` mode: symlinks, hardlinks, foreign owner,
    bad names, unreadable files, the per-file cap and aggregate exhaustion
    (the whole import refuses rather than truncating). The existing
    single-file mode keeps its defaults.
  - `test_land_helpers.py`: tier 2 refused by default and landed with
    `allow-build-config`, while tier 1 and its reach stay refused with it.
  - `test_engine_job_isolation.py` extended to the Claude jobs:
    - no `./` action;
    - every claude-code-action step sets `classify_inline_comments:
      "false"`;
    - provisioning with `user: claude-agent` between `create-codex-user`
      and the action step;
    - the pre-agent reclaim between them;
    - `path_to_claude_code_executable` set;
    - the post-agent reclaim first after the action;
    - every git-running step gated on it;
    - the land jobs pass `allow-build-config` only from the
      `allow_build_config` input, whose default is `false`.
  - `test_dev_agent_composer.py` covers `resolve_threads` from the Claude
    manifest-extra.
- **No probe canary; launch coverage moves to step 5 (decision: Ransom,
  2026-09-24).** The SDK-window harness and the three probe-driven canary
  jobs below were the plan until step 4. None of them is built, and no CLI
  stand-in is written:
  - Adversarial probing of the boundary (the `/proc` and escape probes,
    the fsmonitor survivors, the revoke window) is covered by Claude
    Security scans instead ("in reality we probably won't rely on the
    adversarial probes and will use Claude Security scans instead"; "We
    rely on Claude Security scans for adversarial probes").
  - Successful-launch coverage moves to step 5's real-model runs, which
    launch the real CLI through the wrapper in both grant modes (the
    fork-head review is the `none` mode). That covers SDK completion and
    teardown, the WIF/config grant and the reclaim after it.

  Step 4 ships the unit tests, `codex_path_smoke.sh`'s claude-agent cases,
  and the `claude-launcher` canary job. That job runs the composite up to
  an intentional refusal before the namespace exists. The launch chain and
  its teardown (the CLI's exit, a KILL of `sudo`, the action process's
  exit) were also exercised by hand in a privileged container while step 4
  was written. The next two bullets record the original plan.
- **The action's post-CLI window, against the real SDK** (not built; see
  above).
  `tests/sdk_barrier/` is a small harness pinned to the SDK version the
  action's lockfile resolves (0.3.280 today). It runs **under Bun**, at
  the version the action pins (`bun-version` in its `action.yml`,
  installed with the same `oven-sh/setup-bun`). It runs from a canary
  workflow step, so its ancestry (Runner.Worker, the step's shell, then
  `bun`) is exactly what `agent-ns-launch` checks for the action's
  process. The production check is not relaxed for tests, and the scripts
  carry no test switch.

  It first writes a `$RUNNER_TEMP/claude-workload-identity` dir (0700,
  with a dummy token file) as the action's `setupWorkloadIdentity` would.
  It then drives `query()` as `run-claude-sdk.ts` does, breaking on the
  result and catching errors, through the real wrapper, `agent-ns-launch`
  and `agent-ns-init`. Every variant therefore goes setup → in-namespace
  isolation check → CLI, in both grant modes. Two control cases:
  - a harness run under Node instead of Bun is refused by
    `agent-ns-launch`'s process check;
  - a setup with the WIF dir missing is refused by `agent-ns-init`.

  The synthetic CLI answers the initialize handshake and then does one of
  these:
  - emits a result;
  - emits a malformed `control_response`;
  - is aborted by the harness's `AbortController`;
  - dies.

  Every variant leaves a background process in the agent namespace that
  keeps rewriting `.git/config` (a `core.fsmonitor` that writes a marker
  with `id -un`), and a `reclaim.sh` stand-in that takes 10 seconds. The
  harness needs a runner's process tree and sudo, so it runs only in the
  canary. Locally, pytest covers the scripts' pure parts: argument and
  handoff parsing, and the bind list per grant mode.

  Asserted:
  - the harness regains control within the SDK's grace in every variant
    (the documented early continuation);
  - once the synthetic CLI exits, however it ends, no process of the
    namespace remains (checked from the host as root), including one the
    CLI started with `setsid nohup`;
  - with a CLI that ignores EOF and `TERM`, and separately with a CLI
    stopped by `SIGSTOP`:
    - `TERM` then `KILL` to the SDK-owned PID (`sudo`), as the SDK sends
      them, ends the namespace within 1 second of the `KILL`;
    - a `KILL` alone does the same;
    - exiting the harness process (standing in for the action's `bun`)
      while leaving `sudo` alive ends the namespace through the pidfd;
    - in each case the harness process itself terminates within a bound
      (no wait on inherited pipes), and no namespace process remains;
  - the harness then runs the action's own post-CLI git sequence
    (`checkAndCommitOrDeleteBranch` with a stub octokit answering 404 for
    the branch, as origin does for a fresh name) and no marker appears;
  - running step 7's reclaim afterwards kills the survivor, restores the
    config, and leaves no marker from any later git.
  - A control run with a stub octokit answering 200 and zero commits shows
    that the marker does appear with `runner`. So the test detects the
    exposure it relies on being unreachable.

  The round-1 and round-2 reviewer probes (2,237 ms, 2,713 ms, 3,003 ms)
  become these regression cases.
- **Hosted canary** (the probe-driven jobs are not built; see above).
  `engine-isolation-canary.yml` gains a `claude-boundary`
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
  - the probe's report (written as `claude-agent`) contains the raw strings
    from its env and argv, every readable `/proc/*/cmdline` and `environ`,
    `.git/config` and its `--mcp-config`. A later runner-side step compares
    them by value against the App token (the action's `github_token` step
    output) and the job token (`github.token`). Both are `ghs_` installation
    tokens, so a prefix test cannot tell them apart. The App token must
    appear nowhere, and the job token must appear as `GH_TOKEN`;
  - it also shows no `ACTIONS_ID_TOKEN_REQUEST_TOKEN` or
    `ACTIONS_RUNTIME_TOKEN`, no sudo, no Docker, no readable runner
    process memory and no connectable `.NET` socket;
  - positive controls: the probe can read the identity-token file (and
    still can after a forced refresh), and it sees the job token and its
    rewritten argv;
  - the probe, before exiting, writes a valid-looking record to
    `/tmp/inline-comments-buffer.jsonl`: the host's `/tmp` never has that
    file (the agent's `/tmp` is private). The disabled post-step itself is
    checked structurally (unit tests) and live in the same-repo review run
    below;
  - the probe emits a malformed protocol line and leaves a background
    process that plants a `core.fsmonitor` marker hook. No marker written
    as `runner` appears during the rest of the action step, and step 7's
    log shows the survivor killed before any later step's git;
  - the probe runs from the cwd the CLI is given, and it is started, as in
    production, by an SDK whose own cwd is the real workspace. It tries to
    reach `$RUNNER_TEMP` and the runner's tree by relative paths
    (`../../_temp/…`), by `openat` on every descriptor it holds, and
    through `/proc/self/cwd/..` and `/proc/1/cwd`. It finds no step script,
    command file or sentinel file that the job planted under
    `$RUNNER_TEMP`, while absolute-path scans see only the four binds;
  - a second run of the job with `grant: "none"` (the fork-review shape):
    after the wrapper's preparation, the probe, as `claude-agent` and
    outside any sandbox, fails to overwrite a tracked file, create a root
    entry, or rename or write `.git`, and succeeds in writing
    `$RUNNER_TEMP/scratch`;
  - both token modes: the job runs once with no `github_token` (App-token
    mode, the probe must not see the App token) and once with
    `github_token: ${{ github.token }}` (job-token mode, the launch must
    proceed and the probe sees that token as `GH_TOKEN`);
  - after the step, no `claude-agent` process remains, and `.git` and the
    root are back to `drwxr-sr-x` runner-owned.

  A second job, `claude-sandbox-review`, sets up a fork-head-shaped review
  with this revision's steps: the strip, `grant: "none"`, the chowned
  scratch copy, the sandbox install and the overlay as composed without the
  `.git/config` mask. Then, as `claude-agent` inside the agent namespace
  (so bubblewrap nests in it, as it will in production) and under `srt`
  with that overlay (no model), it runs `git diff origin/main` in the checkout, `uv venv && uv
  pip install -e .` and `pytest` in scratch, and a write into the checkout
  and into `OUT_DIR`. Asserted: the first three succeed and the two writes
  are refused.

  A third job, `claude-revoke-window`, tests the interval after the CLI,
  when the action still holds the App token and a survivor might be alive.
  It starts the namespace launch through the wrapper, as the action would,
  with a probe that keeps scanning for 60 seconds. The probe scans every
  `/proc/*/cmdline` and `environ` it can read, and every file it can read
  outside its bind mounts. While it runs, the job:
  - runs a runner-side `curl -H "Authorization: Bearer <sentinel>"` against
    a loopback responder that holds the request for 30 seconds (a slow
    revocation), and a second one against a refusing port (a failed
    revocation);
  - runs a `run:` step whose script embeds the sentinel through an
    expression, as the revoke step embeds the App token, so the runner
    writes it into `$RUNNER_TEMP`.

  Asserted:
  - the probe never saw the sentinel;
  - a control probe run as `claude-agent` without the namespace (plain
    `sudo -u`) did see it, both in `curl`'s argv and in the step script.
    So the channel is real, and the namespace is what closes it;
  - the real action's revoke step runs in `claude-boundary` with an
    in-namespace survivor forced to outlive the SDK's grace (the probe
    ignores `SIGTERM`), and the same by-value App-token scan over that
    survivor's report is empty.

  The jobs need no model call: the probe exits after its report, and the
  action's failure is expected and asserted. The `caller-recipes` matrix
  gains a Claude leg (the same fixtures with `user: claude-agent`, tools run
  through the agent env's PATH). `@v1` moves without a push here, so the
  canary gains a weekly `schedule` next to its push and dispatch triggers.
- **`codex_path_smoke.sh`** runs its PATH-boundary cases for `claude-agent`
  too.
- **One real-model run per workflow**, on scratch issues and PRs made for
  it in this repository, never on live PRs:
  - an issue run of `claude.yml` that commits and lands, with a thread
    resolution via the manifest;
  - a same-repo and a fork-head review (the fork-head one runs `git diff`
    and a scratch test inside the sandbox). The same-repo review's scratch
    PR carries a test that writes a valid inline-comment record to
    `/tmp/inline-comments-buffer.jsonl` when run; afterwards no inline
    comment by `claude[bot]` may appear on the PR;
  - a review-fix and a CI-fix round on a scratch `auto` PR.

  Check model provenance and that settings denies are in effect (a denied
  `git push` attempt is refused). These runs are also the launcher's
  successful-launch coverage (decision: Ransom, 2026-09-24): the issue run
  and the same-repo review launch in `workspace` mode and the fork-head
  review in `none` mode. Each run checks that the CLI completes and its
  namespace ends with it, and that the post-agent reclaim leaves no WIF ACL
  and no `~claude-agent/.anthropic-config`.
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
   `claude` wrapper, `agent-ns-launch`, `agent-ns-init`, the adapted
   isolation check), the post-agent reclaim's cleanup of the WIF ACL and
   the agent's config dir, `codex_path_smoke.sh`'s claude-agent cases, the
   wrapper and namespace unit tests, and a canary job (`claude-launcher`)
   that runs the composite on a hosted runner up to an intentional refusal
   before the namespace. Run the canary green before step 5. No CLI
   stand-in, SDK harness or probe-driven canary job is part of this step
   (decision: Ransom, 2026-09-24; Testing). Adversarial probing is covered
   by Claude Security scans instead. Successful-launch coverage (SDK
   completion and teardown, the WIF/config grant and its reclaim, both
   grant modes) moves to step 5's real-model runs.
5. **Switch the Claude jobs.** In the four workflows, remove
   `claude-setup` and the runner fallback. Add steps 2-4, the launcher, the
   executable input, the post-agent reclaim, the moved `reset-origin-url`,
   the landing import, the gating and env pins,
   `classify_inline_comments: "false"`, the reviewer's fork and external
   setup order (`grant: "none"`, the scratch chown, the overlay without the
   mask), and `claude.yml`'s `resolve_threads` and prompt sentence. The same PR updates SECURITY.md,
   credential-separation.md, codex-engine.md, architecture.md, README.md
   and AGENTS.md, and extends `test_engine_job_isolation.py`. Then do the
   real-model runs.

   As implemented (2026-09-24), three points the steps above leave open:
   - **No `drop-runner-root` in the Claude jobs** (decision: Ransom,
     2026-09-24, option a). #151 added that step while the agent was
     `runner`; it replaces the sudoers policy with a root-only one, and the
     post-agent reclaim needs the runner's sudo (kill, chown, the WIF ACL).
     The agent's uid never had sudo, and the launcher checks that before the
     action step and inside the namespace, as the codex jobs rely on their
     user. The composite stays for callers that run an agent as the runner.
   - **The real-model runs follow the merge** (decision: Ransom,
     2026-09-24): every trigger in this repository resolves to the default
     branch's stubs, which call the workflows and their composites `@main`,
     so no run can exercise this change before it merges.
   - Found while wiring it: the settings composers (which read a `settings`
     file from the checkout) run before `Create agent user`; the launcher
     re-creates the landing directory empty after its kill, so nothing
     provisioning wrote there passes for the agent's output (a reviewer's
     `verdict.txt`); and the reviewer imports its review files into
     `$RUNNER_TEMP/review`, the directory its landing prep already read,
     rather than into `$RUNNER_TEMP/landing`.
6. **Tier-2 opt-in** (after #149 and step 5 have merged). Add `land`'s
   `allow-build-config` and the `lib.sh` split, and the reusable workflows'
   `allow_build_config` input (default `false`) that their land jobs pass
   through. Update tests and examples/. Update SECURITY.md, recording
   there, and on the finding's Security board item, that criterion 2 is met
   for tier 2 by the opt-in (decision: Ransom, 2026-09-23). Then open the companion
   PRs that opt in: inspect_flow (its stubs and its two direct workflows),
   the inspect_ai fork, inspect_harbor, inspect_swe, inspect_sandboxes,
   inspect_scout, actions, and this repository's dogfood stubs. Each PR
   first confirms that no other automation in that repository runs
   agent-landed branches as `runner`.
7. **ts-mono** (companion, any time after step 5): `dependabot-fix.yml`'s
   continuation provisions and runs its agent as an agent user with both
   reclaims. Only then does a ts-mono PR opt in, in its stubs and in
   `dependabot-fix`.

## Open questions

None. Ransom answered the four open questions of the reviewed draft on
2026-09-23 ("yes to 1, 2 and 3; accept the revised criterion for 4"). They
are recorded as decisions in the sections they affect.

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
- Passing `github_token: ${{ github.token }}` in the three agent-mode
  workflows (as ts-mono's `dependabot-fix` already does), so no App token
  is minted there at all.
- Callers' CI runs agent-authored same-repo branches with CI's secrets.
  This is pre-existing and not agent-specific.
- The direct callers' own agent jobs (inspect_flow's two, ts-mono's
  `dependabot-fix`) still run claude-code-action as `runner`. They can
  adopt the launcher.
- `claude-code-action` puts the App token on the CLI's argv
  (`--mcp-config`), where every local user can read it. That is worth
  reporting upstream alongside anthropics/claude-code-action#1818.
