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
  switches subsequent rounds.
- **Same reusable workflows**, engine picked by step conditionals. The
  scaffolding around the agent step — trust gates, TOCTOU head-pinning,
  board staging, round counters, hand-back backstops, the landed-work
  guard — is engine-neutral and hard-won; parallel codex workflows
  would fork all of it. Caller stubs are unchanged except for passing
  one extra secret.

## The structural difference: Codex cannot post or push

`openai/codex-action` runs the Codex CLI in a sandbox with **no network
access and no GitHub credentials** (permission profile `:workspace`).
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
embeds the PR title/body and bounded newest-last comment slices (last
12 discussion + 40 inline, the review-fix prep's bounds), so a trigger
like "address the feedback above" carries its referent — codex cannot
read the thread at runtime the way the Claude path does.

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
surfaces a visible error comment and, in the loops, refunds the review
round / CI-fix attempt (infra failures — e.g. a missing
`OPENAI_API_KEY` — must not march a PR toward spurious escalation);
the loops' hand-back backstop keys on the codex *run* step, so a push
that landed before the comment post died still gets its owed
`@review`. Since 2026-09-09 the landing is three steps, not one: the
`codexland` step is git-only (commit, push, `pushed`/`merge_only`
outputs — the only step carrying the pinned git env and the push
credential), the `resolve-reported-threads` composite consumes the final
message's `RESOLVED-THREADS:` line (below, under Limitations), and the
`codexpost` step composes and posts the de-fanged summary with no git
env at all. The downstream gates that used to read "landing succeeded"
(the dev verb's `Stage - Review (hand-back)`, the loop's self-handoff
detector) key on `codexpost`, which implies the two before it; the
Surface steps name a landed-but-unposted run separately from a failed
push.

### Marker/author contract

Codex output is posted **by the machine account** (MARVIN_TOKEN) so the
marker comments still trigger downstream stubs (github-actions[bot]
comments trigger nothing — recursion guard). The review-fix loop's gate
therefore accepts verdict-marked comments from `i-am-marvin` in
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
and it is exposed ONLY to the `openai/codex-action` step, which uses it
server-side to mint a scoped proxy credential; it never appears in
prompts, sandboxed command environments, or other steps. Repos without
the secret: codex-labeled runs fail at the codex step with a clear
error rather than silently falling back (a silent Claude fallback would
misattribute output).

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
`GITHUB_ENV`/`GITHUB_OUTPUT` files to a sandbox-escaped codex), and the
checkout added to the codex user's git `safe.directory` (the repo stays
runner-owned, so git run as codex otherwise refuses with "dubious
ownership", and no profile sandbox lets the agent add the exemption
itself). One deliberate divergence from a four-way copy: on the write
paths the `reclaim-codex-workspace` step (Hook-safe landing, below) runs
`chown -R runner` on `.git` right after codex (object fan-out dirs codex
creates are codex-owned, and the runner's codex-group membership never
takes effect within the job, so runner-side object writes would otherwise
fail intermittently). One more
unprivileged-user consequence, reviewer-only: the action's inline
`output-schema` input is broken under this strategy at `@v1`
(openai/codex-action#103 — the schema temp dir is mktemp'd as codex, mode
700, then written as the runner, EACCES; fix #147 unmerged), so the setup
step writes the review schema to a runner-owned file and the codex step
passes `output-schema-file` instead — the explicit-path branch creates no
temp dir. Revisit when #160's fixes land upstream (and drop the schema
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
credential, but also the loops' `Ensure hand-back after push` and `Surface
agent errors` fetches — runs as `runner` (sudo), so any of those was a route
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
  `claude.yml`'s `Open or adopt PR` run `git fetch` (which runs
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

## v1 limitations (deliberate)

- **External proxy reviews stay on Claude** — their contributor-code
  sandbox overlay is Claude-settings-specific.
- **Codex reviews run tests since 2026-09-01** (they were static in the
  first cut): the review step uses the `:workspace` profile with
  claude-setup provisioning, so codex can verify findings with
  pytest/ruff/mypy like the Claude reviewer. Read-only-ness of the
  review is enforced by instruction plus structure — the review path
  has no landing step, no push credentials, and no network, so stray
  writes die with the runner (decided after inspect_ai#392's review
  produced four static "blocking" findings of uncertain reality). No
  network still means no installs by the AGENT — but the reviewer
  workflow provisions a fallback venv (uv dev-install, as the runner,
  which has network) when claude-setup is absent and a pyproject.toml
  exists, so PR heads on the inspect_ai fork (cut from pristine main;
  cross-repository fork heads are deliberately never provisioned, issue
  #59) — and Python caller repos that never added claude-setup — get
  test-verified reviews too.
  Only repos that are not Python projects (or whose dev-install fails,
  loudly) degrade to static review.
- **Loop fix rounds run tests since 2026-09-09**: `claude-auto.yml` and
  `claude-auto-review.yml` had no provisioning step at all — neither
  claude-setup nor the reviewer's uv fallback. On Claude that was
  invisible (the agent installs what it needs; it has network); on codex
  it was total: inspect_flow#824's two review-fix rounds and two CI-fix
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
  verification line is composed from the provisioning outcomes (venv on
  PATH / provisioning failed / nothing to provision) rather than asserting
  a venv unconditionally.
- **Fix-round context is filtered and stateful since 2026-09-09**: the
  codex fix prompts (review-fix loop, dev verb on a PR) embedded the last
  12 top-level comments and the last 40 inline comments as a flat REST
  list. On inspect_ai#428 that was bare triggers, verdict markers, the
  sticky counter and stale fix summaries around the one review that
  mattered, and 36 of the 40 inline entries were threads resolved days
  earlier with nothing marking them settled. Now: machine comments are
  dropped, the slice is anchored on the newest review summary (kept in
  full with everything after it, plus the last five human comments before
  it), and review threads come from GraphQL `reviewThreads` by state —
  OPEN in full with the thread id (the handle a later change uses to
  resolve them), RESOLVED as a one-line index, resolved-and-outdated
  dropped. The reviewer is also told not to restate regression-accepted
  notes an earlier round already posted.
- **CI-trigger parity depends on MARVIN_TOKEN**: codex-path pushes fall
  back to `github.token` where the secret is absent, and those pushes
  do not trigger CI (the Claude path pushes via the app token, which
  does). Repos without the machine account can't run the loops anyway,
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
  `.github/actions/resolve-reported-threads` composite, run between the
  landing push and the summary post in both `claude.yml` and
  `claude-auto-review.yml`, resolves exactly those via the
  `resolveReviewThread` mutation — only after a push of real changes,
  only ids that are currently-open threads of this PR (shape-checked and
  intersected with a fresh query, so a hallucinated id is skipped),
  best-effort so a failed resolve never reddens a landed round (a failed
  query or mutation logs a `::warning::` carrying the API's reason rather
  than reading as "codex hallucinated N ids"). The tag is matched
  tolerantly (indent, markdown bold/backticks) because the fail-safe
  miss would otherwise be silent. The composite also writes the final
  message with the line stripped, and the post step publishes that copy
  with a header stating how many threads were resolved. Same rule as the
  Claude path's REVIEW_ETIQUETTE: never resolve what was declined or only
  answered with rationale.
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

## Testing

Create the label per repo (`gh label create engine:codex -c 8250DF -d
"route agent runs to Codex"`), apply it to a scratch issue/PR, then
exercise verbs exactly like the Claude paths (AGENTS.md → Testing a
change): @claude on a labeled issue, @review on a labeled PR, the auto
loop on a labeled PR. The codex-action step log replaces the Claude path's
model-provenance job summary as the run forensics on codex runs, and the
job summary's "Codex usage" table has the model and token split.
