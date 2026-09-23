# The codex engine (`engine:codex`)

Status: v1, built 2026-08-31 (decision: Ransom). OpenAI Codex as an
alternative engine for the dev agent, the reviewer, and both @auto
loops — selected per issue/PR by label, defaulting to Claude.

## Why a label, and why inside the same workflows

The @ commands encode the **verb** (@claude = make changes, @review =
review, @auto = drive the loop); which model runs is **configuration**,
an orthogonal axis. New mentions would multiply tokens across both axes
(@codex, @codex-review, …), and every token is another substring agents
must never emit — the trigger-token-leakage rules already fight that
battle and scale badly with token count. So:

- **`engine:codex` label** on the anchor item (the issue for
  issue-triggered dev runs; the PR for PR runs, reviews, and loops)
  routes that item's runs to Codex. Absent label = Claude. Namespaced
  (`engine:`) because bare labels here already have *trigger* semantics
  (`claude` starts the dev agent, `auto` opts into the loop) — a bare
  `codex` would read as "run codex now" rather than "whichever verb
  fires, use codex". Read via the API at gate time, not from the event
  payload (stale on comment triggers). Relabeling mid-loop deliberately
  switches subsequent rounds. On an issue run the label counts, and is
  copied onto the PR the run opens, only when a write-access human
  applied it most recently — the same fail-closed timeline check as the
  issue's `auto` label; otherwise the run uses Claude and the PR gets no
  engine label (#139).
- **Same reusable workflows**, engine picked by step conditionals. The
  scaffolding around the agent step — trust gates, TOCTOU head-pinning,
  board staging, round counters, hand-back backstops, the landed-work
  guard — is engine-neutral and hard-won; parallel codex workflows
  would fork all of it. Caller stubs are unchanged except for passing
  one extra secret.

## The structural difference: Codex cannot post or push

`openai/codex-action` runs the Codex CLI in a sandbox with **no GitHub
credentials** (permission profile `:workspace`; network inside the sandbox
is ON since 2026-09-09 — see "Network" below — but nothing codex can reach
carries a token).
Claude Code posts its own comments and pushes its own
branches; Codex cannot. Every GitHub side effect on the codex path
therefore moves into deterministic workflow steps:

| effect            | Claude path                  | Codex path                                      |
| ----------------- | ---------------------------- | ----------------------------------------------- |
| review comment    | agent posts via gh           | workflow posts the structured output            |
| verdict marker    | agent posts (prompt-enforced)| workflow posts from schema verdict (guaranteed) |
| branch + commits  | agent pushes                 | workflow commits/pushes the workspace edits     |
| hand-back @review | agent posts (+ backstop)     | the existing backstop posts it (always owed)    |
| task context (CI logs, review findings) | agent fetches via gh | workflow pre-fetches into the prompt/workspace |

Pre-fetching covers the dev verb's PR runs too: the prompt-compose step
embeds the PR title/body, the top-level comments filtered and anchored
on the newest review round, and the review threads by state (the
review-fix prep's shape — see Limitations → Fix-round context), so a
trigger like "address the feedback above" carries its referent — codex
cannot read the thread at runtime the way the Claude path does.

This is a feature as much as a constraint: the verdict marker and the
hand-back become guaranteed instead of prompt-enforced, and the
landed-work-guard class of failures (work stranded on the runner)
cannot happen — the workflow either lands the edits or fails loudly.

### Landing semantics

The land steps commit a dirty tree, then treat **HEAD having moved past
the run's start SHA** — not just a dirty tree — as landable work: the
prompts call local commits unnecessary but they are possible, and a
status-only check would silently discard them with the runner. The push
always uses an explicit refspec (`HEAD:refs/heads/<branch>`), so the
intended branch is updated even if codex left HEAD on some other local
ref; a diverted HEAD that isn't genuinely new work fails the push
non-fast-forward — loud, not lossy. Failure parity mirrors the Claude
path throughout: a failed codex step *or* any of its prep steps
(workspace/context prep, and the dev verb's separate prompt-compose)
surfaces a visible error comment and, in the loops, a failed prep step
refunds the review round / CI-fix attempt (the codex step was never
entered), while a codex step that ran and failed keeps its round, like a
failed Claude step (Claude Security 4628734 / 4628735, 2026-09-22: a
refund keyed on the agent step's own outcome is the agent's to trigger).
A missing `OPENAI_API_KEY` fails the entered codex action step — the key
is passed straight to it — and therefore consumes a round or attempt,
where it used to be refunded; a persistently missing key now reaches the
cap and a human instead of a refund on every trigger;
in both loops (`claude-auto.yml` since #82, `claude-auto-review.yml`
since #83) the codex step only *commits*:
the `Commit codex fix` step (reclaim-gated, hooks-pinned, no credential)
commits the tree and writes the de-fanged summary into the landing
directory, `emit-landing` bundles the commits, and the `land` job — a
fresh runner holding the machine account's minted token — pushes, posts
the summary and the
`@review` the manifest carries (or, in the review loop, the hand-off
when nothing changed — the manifest composer picks one from HEAD exactly
as the old landing step did, and carries the `RESOLVED-THREADS:` ids as
`resolve_threads`); the push and the hand-back cannot come
apart there: `land` posts the hand-back only after the push landed, and
`emit-landing` drops `handback`, `stage`, `resolve_threads` and `handoff_body_file` whenever it had to drop the
bundle (HEAD moved, but a non-descendant or a `git bundle` failure left
nothing to push), so the manifest never carries a hand-back over lost
work. The HEAD-moved test is gated on the `unresolved-merge-guard` step
having *succeeded* (on the codex path as the gate's engine resolves it —
the manifest composer runs no git and `emit-landing` is `read-only`
otherwise; the guard is gated on the codex step and the reclaim, and
those on the user setup, so one condition covers a failed prep or
user-setup step, a failed run step, a failed reclaim and a failed
guard): codex's local commits are exactly what makes ancestry
alone unsafe here, since a `git commit` over staged-but-still-marked
paths completes the base merge and moves HEAD, and the old `Land codex
fix` step's guard gate is what kept that off the branch. In
`claude.yml` (the dev verb, converted by #84 on 2026-09-11) the same
split holds, in three steps: `Commit codex work` is git-only (commit,
`committed`/`merge_only` outputs — the only step carrying the pinned git
env; no credential, no push), the `resolve-reported-threads` composite
runs parse-only on the final message's `RESOLVED-THREADS:` line (below,
under Limitations) and its `ids` travel as the manifest's
`resolve_threads`, and `Write codex summary` composes the de-fanged body
the land job posts, with no git env at all; the manifest composer decides
PR-open and hand-back from HEAD, and the Surface step names a
committed-but-unsummarized run separately from a failed commit. The
review loop has the same three roles since #83 with the credentialed
halves moved out of the agent job: the composite runs parse-only
(`resolve: "false"`, `ids` output), `Commit codex fix` commits and
writes the body, and the land job pushes, posts and resolves.

### Marker/author contract

Codex output is posted **by the machine account** (its minted app token) so the
marker comments still trigger downstream stubs (github-actions[bot]
comments trigger nothing — recursion guard). The review-fix loop's gate
therefore accepts verdict-marked comments from the machine account
(`TRUSTED_LOGINS`: `i-am-marvin`, and `meridian-marvin[bot]` once Phase 2
posts as the GitHub App) in
addition to `reviewer_login`; everything downstream keys on the same
`<!-- claude-review-summary -->` / `<!-- claude-review-verdict:* -->`
markers regardless of engine. All codex text posted to GitHub is
de-fanged first, since it posts from a write-access account:
`@review`/`@claude`/`@auto` are backticked, and the loop's marker
substrings (`claude-review-summary`/`-verdict`, plus the `auto-handoff`
/ `auto-review-rounds` / `auto-review-head` / `auto-fix-attempts`
family) are broken — the gates match them with plain substring
`contains`, so codex merely *quoting* a marker (its prompts embed
verdict-bearing comments) would otherwise forge a verdict or suppress
an owed hand-back. The deterministic workflow-posted comments are the
only marker-bearing ones. Because the widened gate accepts
machine-account verdict comments on Claude-engine loops too, the Claude
fix prompts carry the same rule prompt-side: never emit the marker
substrings verbatim in a comment (the `auto-handoff` first line of the
handoff comment is the one sanctioned use) — the codex paths enforce
this with sed, the Claude path by instruction.

## Auth — the accepted policy exception

OpenAI has no Workload Identity Federation equivalent, so the codex
engine uses an **`OPENAI_API_KEY` org secret** (created by Ransom,
2026-08-31) — the one exception to this repo's no-API-key-secrets
invariant. Containment: the reusable workflows declare it as an
optional secret, stubs pass it explicitly (never `secrets: inherit`),
and it is referenced ONLY by the `openai/codex-action` step of the codex
job — the `agent-codex`, `review-codex` and `fix-codex` jobs, one per
workflow, which the gate's `engine` output selects at the job level (One
job per engine, below). The action uses it server-side to mint a scoped
proxy credential; it never appears in prompts, sandboxed command
environments, or other steps, and since 2026-09-22 never in another job's
message either: until then the codex step sat in the same job as the
Claude agent with a step-level `if:`, and a referenced secret reaches the
job's runner whatever the step's `if:` says (the runner builds its
`secrets` context from the job message before any step runs), so every
Claude-engine run's job carried the key where the unsandboxed,
sudo-capable Claude agent could read it out of Runner.Worker's memory
(Claude Security finding 4629153). Repos without the secret: codex-labeled
runs fail at the codex step with a clear error rather than silently
falling back (a silent Claude fallback would misattribute output).

## Safety strategy: unprivileged-user, not drop-sudo

codex-action's default `drop-sudo` chmods root-owned service sockets under
`/run`, which breaks D-Bus, crashes systemd-resolved, and kills DNS — the
hosted runner then dies with "lost communication" 52–65 minutes into the
job (openai/codex-action#160; hit twice at ~62 min on the first codex
runs, inspect_ai#389). Every codex step therefore creates a dedicated
`codex` system user and runs with `safety-strategy: unprivileged-user`:
containment is the user boundary plus the permission-profile sandbox, the
API key stays unreadable (codex has no sudo), and the host is never
mutated. The setup mirrors the action's `examples/unprivileged-user.yml`
plus two grants its demo never needs: a codex-owned `$RUNNER_TEMP/codex`
dir for the explicit `output-file` (`$RUNNER_TEMP` itself is 755
`runner:runner` on the hosted image and stays that way — group-writing the
temp root would expose the runner's step scripts and per-step
`GITHUB_ENV`/`GITHUB_OUTPUT` files to a sandbox-escaped codex; and since
the directory entry is codex's to replace, no runner-side step reads the
output file in place — the `import-codex-final` composite, right after the
reclaim, opens it once with `O_NOFOLLOW`, refuses anything but a regular
codex-owned file, and copies it to `$RUNNER_TEMP/codex-final.md`, the only
path the commit subject, `resolve-reported-threads` and the summary read;
finding 4628447, 2026-09-22: a symlink planted there would otherwise have
had the runner read, and the land job publish, files codex itself cannot
read), and the
checkout added to the codex user's git `safe.directory` (the repo stays
runner-owned, so git run as codex otherwise refuses with "dubious
ownership", and no profile sandbox lets the agent add the exemption
itself). The recipe is the `create-codex-user` composite — one body for
the four `Create codex user` steps (it was a four-way verbatim copy until
#87's review asked for the composite AGENTS.md prescribes). One
deliberate divergence between the paths: on the write
paths the `reclaim-codex-workspace` step (Hook-safe landing, below) runs
`chown -R runner` on `.git` right after codex (object fan-out dirs codex
creates are codex-owned, and the runner's codex-group membership never
takes effect within the job, so runner-side object writes would otherwise
fail intermittently). One more
unprivileged-user consequence, reviewer-only: the action's inline
`output-schema` input is broken under this strategy at `@v1`
(openai/codex-action#103 — the schema temp dir is mktemp'd as codex, mode
700, then written as the runner, EACCES; fix #147 unmerged), so the
reviewer's `Prepare codex review inputs` step (right after the composite;
it also discovers the absolute tool paths for the prompt) writes the
review schema to a runner-owned file and the codex step passes
`output-schema-file` instead — the explicit-path branch creates no temp
dir. Revisit when #160's fixes land upstream (and drop the schema
workaround when #103's does).

### Hook-safe landing

The group grant that lets codex edit the tree also makes `.git`
codex-writable, and git *executes* configuration: `core.hooksPath` and the
files in `.git/hooks/` (run by `git commit`, but also by `git fetch` —
`reference-transaction` fires on every ref update, the remote-tracking ref
included — and by `git status`, which runs `post-index-change` whenever it
writes the refreshed index), `core.fsmonitor` (run whenever
an index carrying the fsmonitor extension is read — `git status`, even
`ls-files`), `filter.*.clean` drivers (run by `git add`), plus
`credential.helper`, `http.*`, `url.*.insteadOf` and `remote.origin.url`,
which decide where a push goes and what authenticates it. Every runner-side
step that runs git after codex — the landing step with the machine-account
credential, but also — until #82/#83 — the loops' `Ensure hand-back after
push` and `Surface agent errors` fetches — runs as `runner` (sudo), so any of those was a route
from the sandboxed codex to code execution with the machine-account
credential — and until #61 the credential itself sat in `.git/config`,
readable outright (Claude Security findings 4121988, 4121984, 4122332). Since
#61 (2026-09-09):

- the credential never touches the workspace (design/architecture.md → No
  persisted git credentials), so the `chown`/`chmod` exposes none;
- `Create codex user` snapshots `.git/config` to
  `$RUNNER_TEMP/git-config.pre-codex` (runner-only) right before the group
  grant, and the **`Reclaim workspace from codex` step**
  (`.github/actions/reclaim-codex-workspace`, shared by the three workflows)
  is the FIRST step after codex on every path — `if: always() &&
  steps.codexuser.outcome == 'success'`, so it runs whether codex succeeded,
  failed or was cancelled, and before the guard and the landing. It kills
  every process still running as the codex user (`pkill -KILL -u codex` —
  the runner does not reap orphans between steps, so a background process a
  prompt-injected codex left behind would otherwise outlive the step and
  could rewrite the restored config before the landing step's credentialed
  push; review round 4 of #73 — the kill is repeated until a pass finds
  nothing, since one `pkill` scans `/proc` once and a forking survivor can
  slip it), refuses if `.git/commondir` exists or `.git` is no longer a
  plain directory (below), refuses if codex left an *embedded repository*
  in the tree (below — the list of nested `.git` entries is compared with
  the one `Create codex user` snapshotted, so a caller's provisioning that
  legitimately leaves one keeps working), takes `.git` back (`chown -R
  runner`), revokes the group grant on it (`chmod -R g-w` — the chown alone
  leaves `.git/` group-writable by codex, and a `chgrp` would not do since
  codex is in the `runner` group too) and on the workspace root (`chmod
  g-w`, not recursive: a same-parent rename needs write on the parent only,
  so a survivor could otherwise `mv .git .git.x && cp -a .git.x .git` and
  own a git dir the `GIT_DIR` pin then follows by path; review round 5 of
  #73 — the rest of the working tree keeps its grant), restores the
  snapshot with `cp --remove-destination`, so a config codex replaced with a
  symlink cannot redirect the write, **moves `.git/hooks` aside** (to
  `$RUNNER_TEMP`, non-sample names logged — hooks are files, so the restore
  never reached them and the chown left codex's runner-owned and executable;
  review round 6 of #73) and **appends `core.hooksPath` / `core.fsmonitor =
  false` to the restored config**, so every later git in the job is
  hook-free without each step carrying an env pin. It runs no git itself.
  Everything downstream —
  the guard, the landing, the loops' hand-back and unlanded-work fetches —
  is either gated on that step's success or (Surface) sets its error and
  skips its git calls whenever the step did *not* succeed — `!= success`,
  not `= failure`: a job cancel that lands mid-reclaim leaves the step
  `cancelled` with the kill/chown/restore only partly done, and `always()`
  still runs Surface afterwards (review round 7 of #73) — so everything
  codex could have configured is gone before the first runner-side git
  command on every path, not just the success path. The reclaim lived inside the landing step
  first (review round 1 of #73); round 3 found that a failed guard, or the
  landing's own refusal, left the loops' `Ensure hand-back after push`
  (gated on the codex step, not the landing, so a landing that pushed and
  then died posting still owes its `@review`) running `git fetch` through
  the un-restored, codex-writable config — `remote.origin.url = ssh://…` +
  `core.sshCommand = <script>` executes as `runner` there — with
  `MARVIN_TOKEN` in its env. Moving the reclaim ahead of the guard and
  gating the hand-back on it closed that, and made the loops' Surface step's
  `[ -z "$err" ]` guards structural rather than incidental;
- the guard and landing steps pin the git dir by env — `GIT_DIR` and
  `GIT_COMMON_DIR` to `$GITHUB_WORKSPACE/.git`, `GIT_WORK_TREE` to the
  workspace. Without that, a `.git/commondir` file codex wrote would make
  git read *its* config: `gitrepository-layout(5)` — "if this file exists,
  `$GIT_COMMON_DIR` will be set to the path specified in this file unless
  `GIT_COMMON_DIR` is explicitly set", and `config` "is ignored if
  `$GIT_COMMON_DIR` is set". So codex could copy `.git` somewhere it owns,
  add a `filter.*.clean` driver or repoint `remote.origin.url`, and the
  restore would land on a file git no longer reads (review finding on #73).
  The reclaim step's refusal is the primary defence — nothing runs git
  through a redirected dir — and the pins are belt and braces for the two
  steps that write. The same two steps set `GIT_CONFIG_GLOBAL=/dev/null`:
  codex is in the `runner` group, so a group-writable `~runner/.gitconfig`
  would be a config file outside the restore; the hosted image ships it
  runner-only, and the pin makes that structural (the commit identity is in
  the repo config the reclaim restored, so nothing there needs the global
  file);
- hooks and fsmonitor are neutralized for *every* post-codex step at the
  reclaim, not per step: the two things a config restore cannot cover are
  hooks (files in `.git/hooks`) and the fsmonitor extension (in the index),
  and the steps that run git after codex are not only the landing — the
  loops' `Ensure hand-back after push` and `Surface agent errors` and
  `claude.yml`'s `Open or adopt PR` (all gone with the landing-job split,
  #82–#84) ran `git fetch` (which runs
  `reference-transaction` when it updates the remote-tracking ref — and
  codex, owning `.git` during its step, can delete the local one so the
  next fetch deterministically does) and `git status` (which runs
  `post-index-change` when it writes the refreshed index — always, after
  codex touched tracked files), with `MARVIN_TOKEN` in their env. Until
  round 6 of #73 only the landing pinned `core.hooksPath` by env, so a
  codex-written hook in `.git/hooks` — runner-owned and executable after the
  chown — ran as `runner` in those steps. The reclaim now moves `.git/hooks`
  aside and appends `core.hooksPath = $RUNNER_TEMP/no-hooks` and
  `core.fsmonitor = false` to the restored config, so a git-running step
  added later cannot drift out of the pin. The landing step still runs every
  git command with the same two keys in `GIT_CONFIG_*` env, and the
  `unresolved-merge-guard` pins `core.fsmonitor=false` and the git dir the
  same way (lower exposure — `ls-files` runs no drivers — but it reads the
  index, which the restore does not touch): belt and braces on the two
  steps that write. Keep the env pins on every git command in those steps,
  not just the ones that obviously execute config;
- a *nested* repository is refused, not pinned. Codex can leave an embedded
  repo in the tree with its *own* `.git/config` and stage the directory as
  a gitlink (`git add evil` — all local git); the landing step's `git status
  --porcelain` then spawns `git status --porcelain=2` inside it
  (`is_submodule_modified`), and `git add -A` can do the same for an
  unchanged-HEAD gitlink. That child gets `GIT_DIR`/`GIT_COMMON_DIR` cleared and reads the
  nested config, which the restore never touched. Git's `sanitize_repo_env`
  (run-command.c) keeps `GIT_CONFIG_COUNT`/`GIT_CONFIG_PARAMETERS`, so the
  `core.fsmonitor=false` and `core.hooksPath` pins do carry into the child —
  but the pins cover only those two keys, and the child *refreshes its
  index*: a tracked file whose mtime changed but whose size did not is
  re-hashed through `convert_to_git` → the `filter.*.clean` driver named by
  the nested `.gitattributes` and nested config, as `runner`, with the
  machine-account token in the environment (review round 5 of #73; env
  config cannot wildcard a filter name away). So the reclaim step compares
  `find $GITHUB_WORKSPACE -mindepth 2 -name .git` (as root, so an unreadable
  directory cannot hide one; matches gitfiles and symlinks too) against the
  pre-codex snapshot and fails on any difference — git-free, and every
  downstream git user is already gated on the step. Belt and braces: every
  post-codex `git status` passes `--ignore-submodules=dirty`, which is what
  suppresses the `is_submodule_modified` spawn while (unlike `all`) still
  reporting a changed gitlink pointer; `git add`/`git commit` have no such
  flag, which is why the refusal is the primary defence. Recognizing the
  refusal when it is a false alarm: a codex run that `git init`s a fixture
  or `git worktree add`s under the checkout (rather than `/tmp`) goes red on
  `Reclaim workspace from codex` with the nested `.git` path in the log and
  the Surface comment naming the step; the fix is the prompt (create
  scratch repos outside the workspace), not a weaker check.

Verification for a change here, all cases prompted to codex (any verb):

1. write a `.git/hooks/pre-commit` that prints `HOOK-RAN` and exits non-zero
   — the landing step must still commit, and its log must not contain
   `HOOK-RAN`;
2. `cp -r .git /tmp/x`, add `filter.x.clean = echo FILTER-RAN >&2` to
   `/tmp/x/config` with `* filter=x` in `.git/info/attributes`, and write
   `/tmp/x` into `.git/commondir` — the reclaim step must fail on its
   redirected-git-dir check, the guard and landing must be skipped, and no
   later step's log (hand-back, Surface) may contain `FILTER-RAN`;
3. the guard-failure path: on a loop run, set `remote.origin.url =
   ssh://x/y` and `core.sshCommand = <script printing SSH-RAN>` in
   `.git/config` and leave a conflict unstaged (or corrupt `.git/index`) so
   the guard fails — the run must go red on the guard with the Surface
   comment naming the unresolved merge, and no step's log may contain
   `SSH-RAN` (the reclaim restored the config before the hand-back and
   Surface fetches ran);
4. the survivor path: have codex start `setsid nohup sh -c 'while :; do
   git config core.sshCommand "sh -c \"echo SURVIVOR-RAN >&2\""; sleep 0.1;
   done' &` and leave the tree otherwise clean — the reclaim step's log must
   show the kill, `.git` and the workspace root must both be `drwxr-sr-x`
   afterwards (`stat` them in a scratch step; symbolic `g-w` keeps the
   setgid bit `Create codex user` set, so `drwxr-xr-x` would be wrong, not
   better), and the landing step's push must go out with no `SURVIVOR-RAN`
   in any log;
5. the embedded-repository path: `git init evil && git -C evil config
   filter.x.clean "echo FILTER-RAN >&2"`, `echo "* filter=x"
   >evil/.gitattributes`, commit a file inside `evil`, `touch` it, then `git
   add evil` in the outer repo — the reclaim step must fail on its
   embedded-repository check (its log names the `evil/.git` path), the guard
   and landing must be skipped, and no later step's log (hand-back, Surface)
   may contain `FILTER-RAN`. Control: a run on a caller whose provisioning
   leaves a nested `.git` before codex (an editable `git+` install under
   `src/`) must NOT be refused — the snapshot covers it;
6. the unpinned-step hooks path: on a loop run, write
   `.git/hooks/reference-transaction` and `.git/hooks/post-index-change`
   that print `HOOK-RAN` to stderr, `chmod +x` them, `git update-ref -d
   refs/remotes/origin/<head branch>` so the hand-back's fetch must recreate
   the tracking ref, and `touch` a tracked file so `git status` rewrites the
   index — the reclaim step's log must list both hooks as moved aside,
   `git config --get core.hooksPath` in a scratch step must print
   `$RUNNER_TEMP/no-hooks`, and no later step's log (landing, hand-back,
   Surface) may contain `HOOK-RAN`.

### Runner-side search path

The hook-safe landing reasons about what git *executes*; the job PATH is
the same kind of boundary for every command (Claude Security finding
4628448, 2026-09-22). Two runner facts, verified against actions/runner
`src/Runner.Worker` (`FileCommandManager.cs`, `Handlers/Handler.cs`,
`Handlers/ScriptHandler.cs`): every line a step writes to `$GITHUB_PATH`
is added to the job's prepend list and never removed (`PrependPath` is
only ever appended to and deduplicated), and every later step — composite
steps included — gets `PATH = <prepend list> : <original PATH>`, with a
step's own `env: PATH` used only as the tail; and the runner picks each
`run:` step's shell interpreter with `WhichUtil.Which(shell, …,
prependPath)`, i.e. through the same list, before the script runs. So a
directory on the job PATH that the codex user can write or replace is code
execution as `runner` in the first step after codex — and the reclaim's
own script cannot pin its way out, since its `bash` was already chosen.
The `mechanism` job of `.github/workflows/codex-path-smoke.yml` reproduces
this on the hosted image: with the workspace venv on `GITHUB_PATH` and a
`bash` planted there, the next `shell: bash` step runs under it.

Until 2026-09-22 `provision-fallback` put `$PWD/.venv/bin` on
`GITHUB_PATH` on every engine, and `create-codex-user`'s `chown -R
runner:codex` + `chmod -R g+rwX` then made that directory `drwxrwsr-x
runner codex`: a prompt-injected codex could write `.venv/bin/sudo`
(wrapping `/usr/bin/sudo`), `bash`, `git`, `find`, `jq`, and the reclaim's
first command — a bare `sudo pkill -KILL -u codex` — would have run it as
`runner`, root on a hosted runner, before the kill, the chown, the config
restore or any refusal; `codex-usage`'s `sudo test`/`sudo find`/`sudo cat`
on the reviewer's path (which has no reclaim), the guard's `git`, the
commit step's `git add -A`, emit-landing's `git bundle` likewise. The
reclaim's guarantees would have been executed by attacker-chosen binaries,
and the containment argument under "Safety strategy" would have been void.
The venv was only the convenient case: the hosted Ubuntu image itself
ships `/opt` (and so `/opt/pipx_bin` and `/opt/hostedtoolcache`, both on
the job PATH ahead of `/usr/bin`), `/usr/local/bin` and
`/usr/local/.ghcup` recursively mode 777 (actions/runner-images
`images/ubuntu/scripts/build/configure-system.sh`,
`configure-environment.sh`, `install-nodejs.sh`, `install-haskell.sh` at
`e75633902841`), so on a stock runner any unprivileged user — the codex
user included — could plant `/opt/pipx_bin/sudo` with no help from
provisioning at all (review round 1 of #131). The fix is structural, in
three layers:

- **Nothing under the workspace goes on the job PATH when codex may run.**
  Since the engine split (One job per engine, below) a job runs one engine
  only. The codex jobs provision with `provision-fallback` `user: codex`,
  whose recipe runs as the codex user under `env -i` and cannot reach
  `GITHUB_PATH` at all (the venv is still created at `.venv` — the compose
  steps take the tools from the composite's `bin` directories by absolute
  path, which is how codex was always told to run them), and they never
  run a caller's `claude-setup`. `provision-fallback` also takes
  `add-to-path` (default `true`, ignored under `user`): the Claude jobs
  keep the default, and a caller's `claude-setup` may keep putting its venv
  on the job PATH there — Claude runs as `runner` with the job PATH, so a
  shadow there gives it nothing it does not already have, and no codex user
  exists in that job. (Before the split, #131 had the workflows pass
  `add-to-path: false` when the gate's engine was codex, and a caller's
  `claude-setup` that put `$GITHUB_WORKSPACE/.venv/bin` on `GITHUB_PATH` —
  inspect_flow's and inspect_harbor's did on 2026-09-22 — failed the next
  layer on codex runs; with the split those callers' codex runs never run
  it.)
- **`Create codex user` makes the job PATH runner-only before the grant, or
  refuses to start codex** (`assert-runner-only-path`, a nested step
  between the user's creation and the workspace grant, `protect: "true"`).
  For every job PATH entry it walks every HOP of the entry as written —
  the entry and each ancestor up to `/` — and, wherever a hop is a
  symlink, every hop of the link's target as written, recursively (eight
  levels; a loop refuses). A hop is a place codex could substitute: write
  on a directory renames its child away, and a symlink is replaced by
  writing its parent — so resolving the entry first and checking only the
  target would have waved through `ws/tools -> /usr/bin`, which codex
  replaces with a directory holding `bash` once the grant lands (round 1
  of #131 reproduced exactly that). Refused, whatever the mode: a relative
  or empty entry (each step's working directory is the workspace), and a
  hop whose physical path lies inside `$GITHUB_WORKSPACE` (codex-writable
  once the grant runs — checked by path, since the grant has not happened
  yet). A hop outside the workspace that codex **owns** (`find -user`, an
  lstat — ownership is authority: an owner can chmod a read-only directory
  writable again, and the owner of a sticky directory may rename or unlink
  anyone's entries in it, unlink(2); round 2 of #131) or that `sudo -u
  codex test -w` finds writable (group memberships count) is **protected**
  — `chown runner`, `chmod go-w`, sticky and other bits kept — and refused
  only if still owned or writable afterwards. Then the FILES directly
  inside each entry — a tool a later step may call, or the runner may pick
  as an interpreter — are found in one `find` pass (every symlink,
  everything codex owns, everything codex could write through its groups or
  the world; nothing else is codex's to change, the image has no ACLs) and
  settled the same way, a symlink followed as written: its target's
  directory chain is walked like an entry and the target checked in turn,
  so `bash -> $GITHUB_WORKSPACE/.venv/bin/bash` in a runner-only directory
  is refused as a workspace hop, and a link to a file codex owns or can
  write or replace is protected or refused (round 2 of #131: the first cut
  scanned regular files by mode bits and skipped symlinks — a link from a
  runner-only directory to a codex-writable target was the planted
  interpreter by another name). Hops and files found unconditionally safe
  are remembered, so a chain many links share (`/etc/alternatives`, a
  toolcache) is probed once — but a sticky directory accepted for one child
  is not: that acceptance is the child's, and round 3 of #131 showed a safe
  `T/runner-bin` vouching for a later `T/not-yet/bin`, for `T` itself and
  for a dangling `bash -> T/not-yet/bash`. The check runs as one privileged process (it re-executes itself
  under `sudo` once; probes are then plain `find`/`test` and `runuser -u
  codex -- test -w`), because a separate `sudo` per probe cost ~40 ms on
  the hosted runner and ~55 s per check in round 3. On the stock image that
  protects `/opt`, `/opt/pipx_bin`, the
  toolcache chain, `/usr/local/bin` and `/usr/local/.ghcup`, the
  world-writable files in them and the targets their links reach, and
  keeps the runner user's own writes — npm's global bin, which
  openai/codex-action's `npm install -g @openai/codex` needs; the toolcache
  setup-* actions fill — while taking the codex user's away. The one
  exception to "writable refuses" is a sticky directory codex does NOT own,
  such as `/tmp`, where a user may create entries but not rename or unlink
  another user's: accepted when the child on the way down exists and is
  not codex-owned. A not-yet-existing entry whose nearest existing ancestor
  is writable is refused the same way (codex could create it). This is the
  primary defence: it runs before
  codex, on a PATH only the runner has written, and when it refuses codex
  never runs. The check's own probes resolve through its `system-path`
  input — `/usr/sbin:/usr/bin:/sbin:/bin`, the root-owned system
  directories; NOT `/usr/local/bin`, world-writable on the image — never
  through the PATH under test. codex cannot add to `GITHUB_PATH` later —
  the per-step file lives under the runner-only `$RUNNER_TEMP` — and
  openai/codex-action installs the CLI with `npm install -g` and adds no
  path, so the PATH the pre-grant check accepted is the PATH every
  post-codex step gets.
- **Belt and braces after codex.** `reclaim-codex-workspace` runs the same
  check as its first step — without `protect`: a hop that became writable
  after the grant is refused, not fixed — so a `GITHUB_PATH` addition made
  between the grant and the reclaim by some later action would fail the
  reclaim closed (and, through its outcome, skip every git-running step),
  and its script then pins `PATH` to `system-path` before its first
  command; `codex-usage` (the reviewer's first post-codex step),
  `unresolved-merge-guard` and `emit-landing`'s `write` step pin the same
  way, and so do the three `Commit codex` steps. The pins cover command
  resolution only — the interpreter is the runner's choice — which is why
  the pre-grant check, not the pins, carries the guarantee. Every one of
  these steps takes a `system-path` input (default
  `/usr/sbin:/usr/bin:/sbin:/bin`) so a self-hosted layout can name its
  own, and so the tests can plant a stub `sudo` there.

Verification: `tests/test_codex_path.py` runs the composites' scripts
against a planted `.venv/bin` of `sudo`, `bash`, `git`, `find`, `jq` and
friends with a stub `sudo` (macOS or Linux, no codex user);
`tests/codex_path_smoke.sh`, run by `.github/workflows/codex-path-smoke.yml`
on every push touching the composites (and by hand), runs the SAME lifted
bodies of the checked-out revision on the hosted image with the real codex
user, the real image PATH and real sudo: the pre-grant check passes on the
stock PATH (naming what it protected), refuses a workspace venv and an
outward workspace symlink, refuses or protects (with real codex ownership)
a codex-owned 555 directory, a codex-owned sticky parent, a runner-only
directory linking `bash` into the workspace and one linking to a
codex-owned file — codex can chmod or write none of them afterwards — the
grant runs, a codex-user "codex" plants into
the venv and cannot plant into the protected directories, npm's global
install still works for the runner, and the reclaim, `codex-usage` and the
guard complete without running a planted file; the `mechanism` job shows
the planted interpreter being chosen when the venv IS on `GITHUB_PATH`,
and the check refusing that PATH. Because the composites reference each
other `@main`, a branch's nested `uses:` would resolve to `main`'s bodies;
the smoke lifts the blocks from the checkout so a branch is tested as
itself, and logs the revision. For a live codex run: prompt codex on a
caller whose provisioning leaves a venv at `.venv` to write
`.venv/bin/sudo` as a script that appends `HIJACKED` to a file under
`/tmp` and then `exec`s `/usr/bin/sudo "$@"`, plus the same for `bash`,
`git`, `find` and `jq`; the run's `Reclaim workspace from codex`, `Codex
usage`, guard and commit steps must complete normally, no step's log may
show `HIJACKED`, and a scratch step after the reclaim that prints `$PATH`
must show no entry under the workspace. Control: nothing in a codex job
adds to the job PATH before `Create codex user` since the engine split (the
caller's `claude-setup` runs only in the Claude job), so a live control
needs a scratch workflow step that appends `$GITHUB_WORKSPACE/.venv/bin` to
`GITHUB_PATH` before it: the run must go red on `Create codex user` with
the entry named in its log, the codex step skipped, and the Surface comment
naming the user setup. The smoke harness's `mechanism` job is the hosted
counterpart.

## v1 limitations (deliberate)

- **External proxy reviews stay on Claude** — their contributor-code
  sandbox overlay is Claude-settings-specific.
- **Codex reviews run tests since 2026-09-01** (they were static in the
  first cut): the review step uses the `:workspace` profile with a
  provisioned venv (claude-setup until 2026-09-22, the fallback recipe run
  as the codex user since — One job per engine, below), so codex can
  verify findings with pytest/ruff/mypy like the Claude reviewer. Read-only-ness of the
  review is enforced by instruction plus structure — the review path
  has no landing step and no push credentials, so stray
  writes die with the runner (decided after inspect_ai#392's review
  produced four static "blocking" findings of uncertain reality). The
  reviewer
  workflow provisions a fallback venv (uv dev-install, as the runner)
  when claude-setup is absent and a pyproject.toml
  exists, so PR heads on the inspect_ai fork (cut from pristine main;
  cross-repository fork heads are deliberately never provisioned, issue
  #59) — and Python caller repos that never added claude-setup — get
  test-verified reviews too.
  Only repos that are not Python projects (or whose dev-install fails,
  loudly) degrade to static review.
- **Loop fix rounds run tests since 2026-09-09**: `claude-auto.yml` and
  `claude-auto-review.yml` had no provisioning step at all — neither
  claude-setup nor the reviewer's uv fallback. On Claude that was
  invisible (the agent installs what it needs); on codex — sandbox network
  still off at the time — it was total: inspect_flow#824's two review-fix rounds and two CI-fix
  attempts all ran in a bare checkout (`No module named inspect_ai /
  pytest / ruff / pyright`), the rounds that pushed were verified only by
  tests that import nothing (which is how a broken test reached CI), and
  CI-fix attempt 2 correctly declined to guess. Both loops now run the
  reviewer's two provisioning steps (the uv fallback body is the shared
  `provision-fallback` composite) after the base sync and before the
  attempt/round is recorded, so a provisioning failure skips the agent
  without burning a round; the Surface step names it (and, on the Claude
  path, notes that the runner's clean base merge was still pushed by the
  `merge_sha`-gated backstop — see design/architecture.md → Provisioning).
  On a **conflicted**
  codex round the failure is tolerated instead (`continue-on-error` when
  the sync left conflicts): provisioning runs over the in-progress merge,
  so a conflicted dependency file fails it every time, and skipping the
  agent there would strand the branch — the round runs static-only, the
  prompt says so and tells codex to report static-only verification, and
  the next round provisions on the resolved branch. The codex prompt's
  verification line is composed from the provisioning outcomes (venv
  provisioned / provisioning failed / nothing to provision) rather than
  asserting a venv unconditionally.
- **Fix-round context is filtered and stateful since 2026-09-09**: the
  codex fix prompts (review-fix loop, dev verb on a PR) embedded the last
  12 top-level comments and the last 40 inline comments as a flat REST
  list. On inspect_ai#428 that was bare triggers, verdict markers, the
  sticky counter and stale fix summaries around the one review that
  mattered, and 36 of the 40 inline entries were threads resolved days
  earlier with nothing marking them settled. Now: machine comments are
  dropped (bare triggers included — but only genuinely bare ones, so a
  short human instruction opening with the token survives; verdict
  comments likewise only when they are the bare verdict line, so a
  reviewer run that folds its summary into the verdict comment is still
  embedded rather than filtered out — the prep guard's error names that
  filter as a possible cause of an empty section), the slice is
  anchored on the newest review round (its first marker-bearing comment
  from a machine author — the Bot type or the marvin account — since the
  reviewer stamps every top-level comment it posts and a round can be
  several; kept in full with everything after it up to a bound of the
  round's marker comments plus 25 others, plus the last five human
  comments before it), and review threads come from GraphQL
  `reviewThreads` by state — OPEN in full with the thread id (the handle
  the `RESOLVED-THREADS:` ending-contract line names for the
  `resolve-reported-threads` composite), RESOLVED as a one-line index (only
  when there are any), resolved-and-outdated dropped; the query's page
  bounds (newest 100 threads, first 20 comments each) print a note when
  hit rather than truncating silently. Both sections are written by the
  shared `.github/actions/pr-feedback-context` composite (a step ahead of
  each compose step; the compose cats its file), so the dev verb and the
  review-fix loop run one jq program rather than two copies; its fetches
  are retry-then-fail, and a failure is surfaced as a pre-agent error
  (and refunded, in the loop) like a failed prep. Both anchor
  patterns — the Claude marker and the codex reviewer's `engine: codex`
  footer — join the de-fanged substrings on both the prompt and the
  output side (every codex summary that posts as marvin, the CI-fix
  loop's included), so a codex summary echoing either cannot become a
  false anchor (the reviewer's own footer is appended after its sed AND
  after its comment-size cap, so a truncated review still carries the
  anchor), and the Claude-path prompts that post on a PR — the fix
  prompt's forbidden-substring list and the dev verb's review-etiquette
  rule — name them too; the author restriction covers a caller's human
  commenters, who are under neither rule. The
  reviewer is also told not to restate regression-accepted notes an
  earlier round already posted.
- **Codex reviews get the PR thread since 2026-09-09**: the Claude
  reviewer reads the PR description, prior review rounds and thread state
  with `gh` at runtime; codex cannot, and its prompt had been the generic
  `review_prompt` plus the codex adjustments — 7.5 KB, nothing PR-specific
  beyond the number. inspect_ai#428's two codex reviews (2026-09-09) never
  saw the 18 KB description's "implementation decisions that differ from
  the design" section or a human adjudication sitting on an open thread,
  and re-derived intent from the design doc. A `Compose codex review
  context` step now embeds title/body, the newest 15 top-level comments,
  and the review threads with resolution state (GraphQL `reviewThreads`:
  RESOLVED/OPEN, outdated) — the prompt tells codex a RESOLVED thread is
  settled unless the current code contradicts it — and hands codex a
  prompt file (same retry-then-fail fetches, source-bounded slices, 120 KB
  cap on the finished file, marker/trigger de-fang as the other preps).
  The cap trims from the end, so the threads go in BEFORE the top-level
  comments and every embedded comment body is cut to 8 KB at the source
  (the PR body is left whole — it is the piece #428 most needed); a
  failed compose step has its own clause in the Surface step. The de-fang
  also runs over the `review_prompt` text, which reached codex verbatim
  before — harmless, the codex adjustments restate both rules.
- **Tools are named by absolute path in every codex prompt** (reviewer
  since 2026-09-01; dev verb and loops since 2026-09-09): under the
  `unprivileged-user` strategy codex-action launches codex via `sudo -u
  codex` (no `-E`), and sudo's `env_reset`/`secure_path` replaces PATH
  before codex starts, so a venv on `GITHUB_PATH` does not resolve as bare
  names (inspect_flow#818: `command -v pytest ruff mypy` printed nothing;
  only the `drop-sudo` strategy forwards the runner PATH). The compose
  steps used to run as the runner with the provisioned PATH and discover
  the paths there; since the provisioning runs as codex (2026-09-22) they
  look the list (`pytest ruff mypy pyright python3 node pnpm npm`, in all
  four workflows) up in the directories the `provision-fallback` composite
  reports (`bin`: the venv's, `node_modules/.bin`, `~codex/.local/bin`),
  never on a PATH, and splice the paths into the verification instruction.
  The venv is never on the job PATH on a codex job (Runner-side search
  path, above): the recipe runs as codex under `env -i` and cannot reach
  `GITHUB_PATH`.
- **CI-trigger parity depends on the machine account's secrets**: codex-path
  pushes fall back to `github.token` where the app secrets are absent, and
  those pushes do not trigger CI (the Claude path pushes via the app token,
  which does). Repos without the machine account can't run the loops anyway,
  so the gap is dev-verb runs only.
- **No inline review comments** from codex reviews: one summary comment
  with file:line references in the body.
- **No fork-head PRs on codex dev runs**: the landing step pushes to
  origin, where a fork's branch doesn't exist — the prep step declines
  fork PRs loudly rather than failing mid-run. Since agents#59 the dev
  workflow's trigger gate refuses fork heads before engine detection (for
  both engines — the fork's code would otherwise run unsandboxed), so the
  prep step's check is defense in depth. **No codex reviews of fork-head
  PRs** either: the `:workspace` profile is not the bubblewrap sandbox and
  the runner-side provisioning would execute the fork's build backend, so
  an `engine:codex` label on a fork-head PR falls through to the Claude
  engine's sandboxed review path (logged, not commented).
- **Review-thread resolution in codex fix rounds — since 2026-09-09**
  (was a limitation: codex cannot run gh, so every thread the loop opened
  on a codex PR stayed OPEN and the handoff asked humans to resolve them;
  by inspect_ai#428's eighth round the fix and review prompts carried
  dozens of settled threads nobody could tell from live). The codex
  ending contract now ends with `RESOLVED-THREADS: <id> ...` naming the
  OPEN threads (ids from the embedded REVIEW THREADS section) it fully
  addressed in code, or `none`; the shared
  `.github/actions/resolve-reported-threads` composite — parse-only in the
  agent job of `claude.yml` (since #84) and `claude-auto-review.yml`
  (since #83), its `ids` output travelling as the landing manifest's
  `resolve_threads`, which the land job intersects with the PR's own
  threads and resolves after the push (before those conversions it ran
  between the landing push and the summary post) — resolves exactly those via the
  `resolveReviewThread` mutation — only after a push of real changes,
  only ids that are currently-open threads of this PR (shape-checked and
  intersected with a fresh query, so a hallucinated id is skipped),
  best-effort so a failed resolve never reddens a landed round (a failed
  query or mutation logs a `::warning::` carrying the API's reason rather
  than reading as "codex hallucinated N ids"). The tag is matched
  tolerantly (indent, list bullet, markdown bold/backticks closed on
  either side of the colon, and the ids rendered as a bulleted or
  numbered list on the lines beneath the tag rather than on its line —
  one awk pass parses and strips the same block, so the two cannot
  disagree) because the fail-safe miss would otherwise be silent. The
  composite also writes the final
  message with the block stripped, and the post step publishes that copy
  with a header stating how many threads were resolved. The stripped copy
  lives directly under `$RUNNER_TEMP`, not in the codex-owned output dir
  (2026-09-10): the composite runs as the runner, and a path inside
  `$RUNNER_TEMP/codex` made its awk die on the first write — the ids were
  lost and every one of inspect_ai#428's ten overnight rounds posted
  "(codex produced no final message)" while codex's summary sat unread; a
  codex-owned dir was also a symlink hazard for a runner-side write. The
  READ has the same hazard (2026-09-22, finding 4628447): the composite's
  awk follows a symlink as readily as `[ -f ]` does, so the callers no
  longer hand it the codex-owned output file but the runner-owned copy the
  `import-codex-final` composite verified (`O_NOFOLLOW`, regular file,
  codex-owned) and it refuses a symlink or non-regular `final-message`
  itself. If
  the stripped copy is still missing the composite warns and the post step
  falls back to the unstripped imported copy (a read needs no write
  permission) before it gives up with the placeholder — which is also what
  a refused (redirected) final message yields, along with the default
  commit subject. Same rule as the Claude path's
  REVIEW_ETIQUETTE: never resolve what was declined or only answered with
  rationale.
- **Branch sync is deterministic, not prompted** (was a limitation; fixed
  2026-09-01 after inspect_ai#392 sat 11 commits behind `main` across 20
  commits, with CI never running because GitHub cannot compute a merge ref
  for a conflicted PR). A `Sync branch with base` step merges the base on
  the runner for BOTH engines, before either agent starts. Codex still
  can't fetch — it never does the merge itself; it is handed the *result*.
  On a conflict the merge is left IN PROGRESS and the conflict list is
  spliced into the codex prompt as a first-class task; the
  `unresolved-merge-guard` step ahead of the landing step then refuses to
  land while `git ls-files --unmerged` is non-empty or a
  `<<<<<<< `/`>>>>>>> ` marker survives in one of those files. (`=======`
  is deliberately not matched — it is a legitimate rST/Markdown heading
  underline.) The Claude path in `claude.yml` cannot receive an in-progress
  merge: tag mode's `setupBranch` runs `git checkout <branch> --`, which
  aborts with "you need to resolve your current index first" on an unmerged
  index, so a conflicted merge is aborted there and handed back to the
  agent via `branch_sync_prompt`. The loop workflows run the action in
  agent mode, which does not call `setupBranch`, and mirror the split for
  one mental model. Same behavior, different mechanism.
- **Model**: `codex_model` input, default empty — codex-action then omits
  `--model` and the run uses the codex CLI's own default (gpt-6-astra as of
  2026-09). The CLI is installed unpinned (`codex-version` unset), so the
  default tracks OpenAI's current flagship without a PR here each time one
  ships; the trade is that a silent OpenAI default change moves our runs
  too — the codex-action step log names the model actually used, and
  callers can set `codex_model` to pin. (Decision: Ransom, 2026-09-08;
  pinned `gpt-5.6-sol` from 2026-08-31 until then.) Reasoning effort IS
  pinned per role via `codex_effort` — independent of model, passed as
  `model_reasoning_effort` config: reviews run at `xhigh` — correctness
  over turnaround — and implementation runs (dev agent, both loops) at
  `high` (implementation took the CLI default before 2026-09-08).
- **Token split in the job summary** (2026-09-09): the codex CLI's
  step-log footer is a single "tokens used" number — its `blended_total`,
  non-cached input + output — so the `codex-usage` composite action runs
  right after every codex step and reads the session rollouts codex
  persisted under its CODEX_HOME (`sessions/YYYY/MM/DD/rollout-*.jsonl`;
  `codex exec` persists unless `--ephemeral`). Each rollout is one
  thread; its last `token_count` event's `total_token_usage` carries that
  thread's cumulative input / cached-input / output / reasoning-output /
  total counts, and its last `turn_context` names the model and effort
  that actually served. CODEX_HOME is fresh per job, so every rollout
  belongs to the run: the action sums the split over all of them (one
  file today; subagent threads, should codex ever spawn any, get their
  own) and lists the distinct models. A "Codex usage" table lands in the
  job summary (the codex counterpart of Model provenance) and the same
  numbers go to the step log. Tokens only — pricing is not encoded; apply
  the model's published rates, or read the OpenAI usage dashboard, for
  dollars. Best-effort: the script exits 0 on every path (a missing or
  unparsable rollout logs why and moves on) and the step carries
  `continue-on-error`, because it runs before the landing step, whose
  implicit `success()` gate would otherwise skip the landing and strand
  the agent's work. Reads each file once via sudo (they are codex-owned
  under /home/codex; the runner's codex-group grant is inert within the
  job — see the `Create codex user` step comments) into a scratch copy
  deleted on exit, and copies nothing off the runner: rollouts hold the
  whole transcript, the same reason the Claude path stopped uploading its
  execution log (#65).
- The claude-* file/marker names stay — historical, and renaming them
  is churn across every consumer.

## One job per engine (2026-09-22)

Each reusable workflow runs the codex engine in a job of its own —
`agent-codex` (claude.yml), `review-codex` (claude-review.yml),
`fix-codex` (both loops) — next to the Claude job it used to share, and the
gate's `engine` output selects exactly one of the two at the job level
(`if: needs.gate.outputs.engine == 'codex'` / `!= 'codex'`). The land job
`needs` both and gates its steps on the one that ran (`AGENT_RESULT` /
`AGENT_OUTCOME` job env in claude.yml and the loops; the reviewer's land
job skips on either being cancelled and reads the Claude job's outputs,
which are empty when the codex job ran). Two Claude Security findings
(2026-09-22) drove it:

- **4629153 — the Claude job carried OPENAI_API_KEY.** A secret a step
  references is delivered to the job's runner whether or not the step
  runs: the runner builds its `secrets` expression context from the job
  message before the first step (`actions/runner`,
  `Runner.Worker/ExecutionContext.cs`, `InitializeJob`:
  `ExpressionValues["secrets"] = Global.Variables.ToSecretsContext()`) and
  evaluates a step's `if:` itself when the step is reached
  (`StepsRunner`). The codex step's `if: engine == 'codex'` therefore kept
  the key out of the codex *process* on Claude runs but not out of
  Runner.Worker's memory, where an unsandboxed agent with passwordless
  sudo (every same-repo Claude run) could read it. A job-level `if:` is
  decided by the service before dispatch and a skipped job gets no job
  message, so the Claude job now references no key at all; the codex job
  references it at its codex-action step and nowhere else
  (`tests/test_engine_job_isolation.py`). Whether the service scopes
  referenced secrets per job or per called workflow is not documented; the
  hosted canary below measures it — per job, in the agent workflows' own
  gate/agent/land shape too — and design/credential-separation.md →
  section 7 carries the residual that this is measured platform behaviour,
  not a contract.
- **4628446 — provisioning ran head code as the runner ahead of the codex
  boundary.** The caller's `./.github/actions/claude-setup` and the
  fallback's `uv pip install -e` (the checkout's build backend) executed
  as `runner` — passwordless sudo, the OIDC request token, unrestricted
  egress — before `create-codex-user` and before the codex-action step
  that holds the key, on heads the pipeline itself produces: a
  `claude/issue-N-*` branch an earlier run wrote from an outsider's issue
  text, pushed and labelled by the machine account, so "same-repo" says
  nothing about who wrote its build hooks. In the codex job the order is
  now checkout → sync → (prep) → `create-codex-user` → **provisioning as
  the codex user** → compose → codex: the shared `provision-fallback`
  composite takes `user: codex` and runs its `provision.sh` under `sudo -u
  codex -H` from a copy in `$RUNNER_TEMP`, so the uv bootstrap, the venv,
  the build backend and the tiktoken warm all run with codex's own
  boundary (no sudo, no token, no view of the runner's processes), and
  the workspace group grant is what lets them write. A composite action
  can only run as the runner, so the codex jobs never run the caller's
  `claude-setup`, even on callers that define one; the venv lands in the
  checkout as before, codex-owned, and the compose steps take the tool
  paths from the composite's `bin` output (the venv's bin,
  `node_modules/.bin`, `~codex/.local/bin`). The reclaim step's
  embedded-repository refusal covers a `.git` provisioning planted.
- **The boundary between provisioning and codex-action (review round 1 of
  the fix).** Provisioning as codex runs BEFORE codex-action's runner-side
  bootstrap, and that bootstrap reads `~codex/.codex/config.toml`
  (`writeProxyConfig`, following a symlink) and writes the merged file back
  readable by codex — so a build backend running as codex could replace
  the file with a link to `/proc/self/environ` and have the action copy its
  own process environment (the OIDC request token, the `INPUT_*` key) into
  a file codex reads, or leave a background process to do it later. The
  `Reset codex home` step (the `create-codex-user` composite in `mode:
  reset-home`) therefore runs between provisioning and codex-action: it
  kills every process running as codex (the reclaim step's loop; a
  survivor fails the step and skips codex) and deletes and re-creates
  `~codex/.codex` from the composite's `codex-home.sh` — the same script
  the create mode runs — so the only files codex-action reads before
  launching codex are the ones the composite just wrote. For the same
  reason the runner writes nothing into the workspace once the codex user
  exists: the codex prompt files live in `$RUNNER_TEMP` (755 runner:runner,
  readable by codex), and the `.git/info/exclude` lines are appended by the
  prep steps before `Create codex user` — a runner-side write into the
  group-writable tree could otherwise follow a symlink a codex-uid process
  planted (a prompt written through `.codex-prompt.md` → `$GITHUB_ENV`
  would have handed later steps an environment composed from the issue
  text). The hosted canary (below) exercises the whole sequence against a
  hostile checkout.
- **The caller's recipe (`codex_provision`).** The four reusable workflows
  take a `codex_provision` input — bash the caller's stub supplies, run as
  the codex user after the uv bootstrap in place of the generic venv +
  dev-install (the `provision-fallback` composite's `recipe` input, written
  to `$RUNNER_TEMP` and passed to `provision.sh` as its one argument). It
  is the codex-run counterpart of the caller's `claude-setup`: a Python
  pin (`uv venv --python 3.11`), a lockfile sync (`uv sync --dev`), Node
  tooling (`corepack enable --install-directory ~/.local/bin && pnpm
  install --frozen-lockfile`). It comes from the stub — a workflow file
  resolved from the caller's default branch — so it is trusted like every
  other input, and it runs with codex's boundary, never the runner's. The
  known callers' recipes, for their stubs to adopt after this lands (the
  task keeps caller stubs out of this change): the inspect_ai fork's
  `meridian` claude-setup is `uv venv --python 3.11 && uv pip install -e
  ".[dev]"`; inspect_flow's `setup` action is Python 3.11 plus `uv sync
  --dev` (its `[tool.uv] default-groups = ["dev"]`); inspect_harbor's is
  Python 3.12 plus `uv sync` (default groups `dev` and `doc`); ts-mono's is
  Node 22 with `pnpm install --frozen-lockfile` (`packageManager:
  pnpm@11.22.0` — `corepack enable --install-directory ~/.local/bin` then
  `pnpm install --frozen-lockfile`; the hosted image's `node` is on codex's
  PATH, and `~codex/.local/bin` is one of the tool directories the prompts
  search). Until a stub sets the input, that caller's codex runs get the
  generic recipe (or none, for a repository without a `pyproject.toml`).
  The provisioning step runs when the checkout has a `pyproject.toml` OR
  the stub set a recipe (review round 2: gating on the pyproject alone
  would have skipped ts-mono's recipe), and a recipe runs under `bash
  --noprofile --norc -eo pipefail`, a `shell: bash` step's options, so a
  failing command fails provisioning instead of a later successful one
  hiding it (review round 2). Each of the four recipes is exercised by the
  hosted canary's `caller-recipes` job against an agents-owned stand-in
  project (`tests/fixtures/callers/`, its README has the table): the
  interpreter or Node version the recipe selects, the dependency groups or
  extras it installs, the lockfile left unchanged by a locked sync, and
  every discovered tool run as codex under codex-action's launch shape
  (run 35798100140 on `743b495`, all four green — the results are in
  design/credential-separation.md → section 6).

Caller-visible effects of the codex-side change (the Claude jobs are
unchanged, minus the key): a codex run on a caller with a `claude-setup`
action (the inspect_ai fork's `meridian` branch for dev-agent issue runs,
inspect_flow, inspect_harbor, ts-mono) gets the generic recipe until its
stub sets `codex_provision` — the runner's default Python rather than the
action's pin, no Actions cache, `.[dev]` plus a `dev` dependency group when
one exists — and a caller whose project is not a Python project (ts-mono:
pnpm) gets no provisioning on codex runs at all until then; codex's prompt
says so and tells it that it may install what verification needs inside
its sandbox, which has network. No caller is in a hurry: `engine:codex`
exists as a label on the inspect_ai fork and inspect_flow only (one item
each on 2026-09-22). The Claude jobs still run the caller's `claude-setup`
as the runner, which
is exactly how the Claude agent itself runs there (SECURITY.md → By
design). The loops' prep steps split in two around the new order — the
identity and landing-directory part before the codex user exists (the
`.git/config` it writes is what `create-codex-user` snapshots), the
prompt composition after provisioning — so their Surface steps name a
`codexcompose` failure separately, and `agent_outcome` counts it.

The stubs are unchanged: every reusable workflow still declares
`OPENAI_API_KEY` (a stub passing an undeclared secret fails to load) and
the stubs keep passing it; only the codex job reads it. The example stubs
carry the `codex_provision` guidance as comments.

**The hosted canary** (`.github/workflows/engine-isolation-canary.yml`,
`workflow_dispatch`, pushes touching the composites or the harness, and a
weekly schedule on `main` that would catch a change in the platform's
secret delivery; a failed scheduled run is emailed to whoever last edited
the cron line, and GitHub disables the schedule after 60 days without
repository activity) is
the evidence for both findings on a real `ubuntu-latest` runner, with no
real secret and no model. Its `probe` job calls a reusable workflow shaped
like the agent workflows with two synthetic repository secrets
(`CANARY_SENTINEL_A` / `CANARY_SENTINEL_B`, values `CANARYA-<16 hex>` and
`CANARYB-<16 hex>`, set by hand) and runs `tests/secret_delivery_scan.py`
as root over the memory of the runner processes in each of three jobs: one
references sentinel A in a step whose `if:` is never true (the pre-fix
Claude job's shape; expected present — the positive control), one
references nothing (the fixed Claude job; expected neither sentinel,
although the caller passed both and sibling jobs reference each — which
also settles per-job scoping), one uses sentinel B in a step that runs
(the codex job's shape; expected present). Its `provisioning-boundary` job
runs this revision's `create-codex-user`, `provision-fallback` with `user:
codex` over `tests/fixtures/hostile-checkout` (a `setup.py` build backend
that plants the config.toml symlink, leaves a survivor process and tries
runner-only reads and writes), then `create-codex-user` in `reset-home`
mode, asserting the positive controls before the reset and the pristine
boundary after it, then a caller `recipe` as codex. Its `pipeline-probe`
job repeats the secret-delivery measurement in the agent workflows' own
shape (`engine-isolation-canary-pipeline.yml`, once per engine): the three
secrets under their real names carrying the sentinels, a gate and a land
job referencing the App secrets in job env and as action inputs, and the
two agent jobs selected by the gate's `engine` output; the Claude agent job
is expected to hold neither sentinel, the codex job the OpenAI stand-in
alone, and `tests/test_secret_delivery_canary.py` keeps the probe's
per-job references equal to the reusable workflows'. Results per run are in
the run's logs; the recorded runs are in design/credential-separation.md →
section 6.

## Network inside the codex sandbox

ON since 2026-09-09 (decision: Ransom) — effective since 2026-09-10. The
mechanism is a **named permission profile**: `create-codex-user` writes
`[permissions.workspace_net]` with `extends = ":workspace"`, the checkout
as a workspace root, and `[permissions.workspace_net.network] enabled =
true` into the codex user's `config.toml`, and the workflows pass
`permission-profile: workspace_net` to codex-action. The first attempt
(#87) set the legacy `[sandbox_workspace_write] network_access = true`
instead and changed nothing: codex-action selects the profile explicitly
(`default_permissions=":workspace"`), and codex deliberately ignores that
legacy table for an explicit builtin selection ("explicitly selecting
`:workspace` intentionally ignores those legacy settings",
`core/src/config/mod.rs`) — trio still died on `setsockopt` in every
2026-09-10 round while the detector reported the key present. Named
profiles may extend a builtin (`extensible_builtin_parent_profile`) and
their `network.enabled = true` compiles straight to
`NetworkSandboxPolicy::Enabled` (`compile_network_sandbox_policy`), which
is the condition under which the seccomp filter is not installed. The
action keeps a pre-existing config and appends its provider block, and
rejects `permissions.*` through `codex-args`, so the file is the only
route; home and `.codex` are 755 so the runner-side action can read it
back — a 700 home would make it read "" and drop the block silently.

The profile's survival rides on the action APPENDING to the existing file
(`writeProxyConfig.ts` today), and `@v1` is a moving tag. Under #87 that
was the one setting whose loss would have been silent (a dropped legacy
key just left the sandbox closed, with no symptom but trio tests failing
again), so the `codex-usage` composite carried a post-run detector that
re-read the `config.toml` codex ran with and warned if the key was gone
(review round 4 of #87). The named profile made that loss loud instead:
the action still passes `default_permissions="workspace_net"`, and codex
refuses to start when `default_permissions` names an undefined profile
(`PermissionProfileResolutionError::UndefinedProfile`,
`codex-rs/config/src/permissions_toml.rs`), so a revision that overwrote
the file would fail the run at the codex step before any test runs. The
detector was therefore dropped in #90 — it would have fired only in a
case that had already failed loudly, and its warning ("codex ran with
network off") would have been wrong for that case. The flip side of the
same mechanism, accepted: any codex start against that `CODEX_HOME`
WITHOUT `default_permissions` is now a hard error too (profiles defined
but none selected); today the only codex invocation there is
codex-action's, which always passes the flag when `permission-profile` is
set.

Pre-creating `.codex` has one side effect the step must compensate for
(caught in review round 1 of #87): codex-action's `resolve-codex-home`
returns early when `~codex/.codex` already exists ("assume it's correctly
permissioned"), and only its create path pre-touches the world-writable
`$CODEX_HOME/$GITHUB_RUN_ID.json` that `codex-responses-api-proxy` —
launched as `runner`, no sudo — writes its server info into. Without that
file the proxy gets EACCES in the codex-owned 755 dir and the action fails
at "Wait for Responses API proxy" before codex runs. So the step mirrors
the action: `sudo touch` + `chmod 666` on that path (the action's `-s`
probe treats the empty file as "not running yet" and locks it to
`444`/root once the proxy is up). Anyone adding another file under
`.codex` before the action runs should check what else the skipped
bootstrap would have done.

Why: with network off, codex's Linux sandbox installs a seccomp filter
(`linux-sandbox/src/landlock.rs`, `Restricted` mode) that allows AF_UNIX
`socket`/`socketpair` but denies `setsockopt`, `getsockopt`,
`getsockname`, `shutdown`, `bind` and `connect` unconditionally — seccomp
cannot see an fd's address family. Trio's event loop dies creating its
wakeup socketpair (it sets `SO_SNDBUF`), inspect_ai's control server
cannot bind, asyncio thread wake-ups stall, and nothing downloads a
tokenizer. inspect_ai#428's codex reviews (2026-09-09) lost every trio
test and half the asyncio suite to this and fell back to timers and
stubs. `network.enabled = true` on the `workspace_net` profile (which
extends the builtin `:workspace`) skips the filter entirely; the legacy
`network_access = true` key on the builtin itself does not when the
profile is selected explicitly, as above.

Trust argument: every codex run here is a same-repo tree — the reviewer
routes fork heads to Claude, the dev agent refuses them, the loops gate on
`isCrossRepository` — and codex holds NO credentials: the API key sits
behind the action's proxy, no GitHub token reaches it, and its curated env
carries no OIDC request token. Network therefore buys installs and test
fixtures, not a push path or an exfiltration channel beyond what the
Claude engine already has (it runs pytest unsandboxed with full network
AND a token). Read-only-ness of reviews and the deterministic landing of
fix rounds never rested on the network being off. The prompts now say
network is available for installs and fixtures and that nothing codex runs
can push to or post on GitHub. One thing network does newly expose
(review round 2 of #87, accepted): codex-action's
`codex-responses-api-proxy` listens on loopback, so anything codex runs —
tests, installed packages — can `connect()` to it and `POST /v1/responses`
(billed to the org key, which never leaves the proxy) or, because the
action starts it with `--http-shutdown`, hit an unauthenticated
`GET /shutdown` that kills the proxy and fails the codex run mid-way.
Same-repo trust covers it as it covers the Claude engine's unsandboxed
test runs; the shutdown case fails loudly rather than silently.

What DID shift is the "codex cannot fetch" rationale scattered through the
sync comments: checkout runs `persist-credentials: false`, so origin is a
plain tokenless URL and an anonymous `git fetch` from a public caller (the
inspect_ai fork) would now succeed. The prompts therefore say "must NOT"
rather than "cannot" — a "cannot" invites the agent to test it and trust
its own finding — and the in-progress merge hand-off means codex never
needs to fetch. The guarantee is instruction, not structure; the
unresolved-merge-guard and the landing step never depended on it.

Two install caveats the prompts carry, both regressions accepted in #87's
review: `reclaim-codex-workspace` refuses any nested `.git` codex added
(compared against the pre-codex snapshot), and a `pip install -e git+…`
into a workspace venv creates exactly that (pip's default `--src` is
`<venv>/src`) — so the prompts forbid installing from a `git+` URL (a
local `pip install -e '.[dev]'`, which the provisioning-failed text
suggests, clones nothing and is fine), and the guard fails the run loudly
if one slips through. And on the landing paths a
`uv add`-style install rewrites `pyproject.toml`/`uv.lock`, which the
landing step would commit — so the prompts also say not to edit dependency
files the task does not call for. Where provisioning FAILED on a
conflicted round, the prompt now tells codex it may provision the venv
itself after resolving the dependency file (`.venv/` and `*.egg-info/`
are already in `.git/info/exclude` from the prep step), instead of the
former "cannot install (no network)".

The alternatives considered — a runner-side test sidecar with an
allow-list, or an upstream AF_UNIX-complete restricted mode — remain
options if the posture ever needs to tighten.

## Testing

Create the label per repo (`gh label create engine:codex -c 8250DF -d
"route agent runs to Codex"`), apply it to a scratch issue/PR, then
exercise verbs exactly like the Claude paths (AGENTS.md → Testing a
change): @claude on a labeled issue, @review on a labeled PR, the auto
loop on a labeled PR. Applying `engine:codex` is never itself a trigger:
an issue runs only from an `opened` mention or the `claude`/`auto` label
(until #97, a phrase in the issue body let ANY later `labeled` event,
this label included, start a real codex run — trig alone gates the codex
step, so no action-side check caught it). The codex-action step log
replaces the Claude path's model-provenance job summary as the run
forensics on codex runs, and the job summary's "Codex usage" table has
the model and token split.
