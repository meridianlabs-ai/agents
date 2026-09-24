# tests/

The repo's unit tests, run by CI (`.github/workflows/tests.yml`) on every PR
and every push to main. They check the workflows as text and run their
lifted `run:` steps against stubs; a live run of the agent workflows is
still validated by triggering them and by the hosted canaries (AGENTS.md →
Testing a change).

- `test_validate_manifest.py` — the land job's manifest validator
  (`.github/scripts/validate_manifest.py`): one valid manifest, one failing
  case per rule — including the per-caller issue policy (`allowed-issue-labels`
  / `allowed-issue-assignees` / `max-issues`) and `refuse-pr` under the triage
  workflow's actual land inputs, against forged manifests; the PR-label
  policy (`allowed-pr-labels`: the dev gate's read, which a manifest's
  `pr.labels` may not exceed — Claude Security 4628441; the loops' land
  jobs pass `[]` and the reviewer's `refuse-pr`, wiring included, issue
  #138); the review fields
  refused on every caller but the reviewer's (`allow-review`), and the
  fix-agent fields (`pr`, `replies`, `resolve_threads`, `handoff_body_file`,
  `handback`) refused under `refuse-bundle`, each against the forged manifest
  its finding describes; and the land job's `pr-draft` / `pr-assignees`
  inputs, shape-checked as `--pr-draft` / `--pr-assignees` (a malformed
  value is a usage error) and refused as `pr` manifest keys.
- `test_import_codex_final.py` — the `import-codex-final` composite's
  script (`.github/scripts/import_codex_final.py`): a regular file owned by
  the expected user is copied byte for byte; a symlink (to a runner file, or
  dangling), a directory, a FIFO (without blocking), a file owned by someone
  else and an unknown owner are refused with no copy and a stale copy
  removed; oversize input is truncated. Its `dir` mode (the Claude agent's
  landing directory): regular single-link files of the expected owner
  copied into a fresh 0700 directory; symlinks, hard links, another owner's
  files, bad names (logged escaped), directories, FIFOs, unreadable and
  over-cap files skipped; the aggregate byte and file-count caps refusing
  the whole import; a stale or symlinked destination replaced, never
  followed; and the composite's step in both modes with its defaults. Plus the `resolve-reported-threads`
  composite's step, lifted the same way, refusing a symlinked final message
  (no ids, no stripped copy), and the structural rule over the three
  write-path workflows: the codex-owned output file is named only by
  codex-action's `output-file`, every reader takes the imported copy, and
  the import step sits right after the reclaim (finding 4628447).
- `test_land_helpers.py` — the `land` composite's `lib.sh` helpers (de-fang,
  retry, open-or-adopt PR — a draft only on create when asked, an adopted
  PR's draft state untouched — and the branch-existence probe against a stub
  `gh`, the landing-failure hint), the `pr` step lifted and run against a
  stub `gh` (`pr-draft` on create, `pr-assignees` on create and adopt, a
  failed assignment recorded rather than failing the step, and the Report
  step naming it and failing the run) and
  the git sequence its fetch/push steps rely on (bundle above the start SHA;
  unbundle into an empty bare repo; refuse a moved branch; push without
  `--force`), run against local repos — including `emit-landing`'s `write`
  step, lifted from the action and run against those repos, and the
  `workflows` guard, lifted the same way: what the push would change on
  origin, listed from the branch's live tip or the base tip, never from
  the manifest's `start_sha` (a start shifted onto a fork PR's head or an
  old base commit still names the file; Claude Security 4628444) — with a
  new branch cut from a configured non-default base (the fork's `main`
  under a `meridian` default) run through the lifted `fetch` step too.
  The same guard under the caller's tier-2 opt-in
  (`allow-build-config`): build and dependency files land, `.github/`,
  agent instructions and settings stay refused and so does a build file
  they link to or import, a link at a build file is no longer followed,
  and the refusal line and the Report hint leave out the build group; the
  two lists split lib.sh's whole list. The composer tests
  (`test_dev_agent_composer.py`, `test_ci_fix_composer.py`,
  `test_review_fix_composer.py`) run each Claude prompt step both ways:
  the build files are named as refused only without the opt-in.
- `test_trusted_start.py` — the run's trusted start (Claude Security finding
  4628444, criterion 2): the validator refuses a bundle whose `start_sha` is
  not the land job's `start-sha`, or any bundle when that input is empty,
  with the forged manifests run through the real script before any fetch
  (a fork PR's head, an old base commit — both reachable on a local origin
  and both refused); `sync-branch`'s `head-sha` pin and `claude.yml`'s
  `Record base SHA`, lifted and run against local repos (a moved head fails
  the sync before the merge; the base step fetches the live tip first —
  actions/checkout leaves `origin/<base>` at the event commit, older than
  the gate's read after a push in between — and a base that advanced keeps
  the gate's tip, a rewritten base refuses, a failed fetch fails);
  `claude.yml`'s gate `Record run start` step against a stub `gh`; and the
  wiring — every land job that pushes passes its gate's read as
  `start-sha`, every sync-branch call the same value as `head-sha`, the
  reviewer passes none, and `examples/landing-smoke.yml` carries the
  gate → checkout → land shape a direct `land@main` caller needs.
- `test_ci_fix_composer.py` — `claude-auto.yml`'s `Compose landing manifest`
  step, lifted the same way: a landed fix or merge-only attempt owes the
  re-review request and sets no stage (a hand-back is mid-flight; Review is
  the gate's, on escalation), the no-change relay, and the two failure
  paths that land nothing — a failed Claude step, whatever the execution
  file says (the base merge and a commit of the run included; 4628657) and a
  codex round whose guard did not succeed. The failed step's attempt is
  kept: `test_ci_fix_gate.py` pins the refund to a step the runner never
  entered, and runs the refund → gate sequence against the cap (4628735).
- `test_land_helpers.py` also runs the `land` composite's `plan` step: a
  `handback` on a manifest with no bundle is dropped unless the caller's
  `allow-no-change-handback` is "true" (the loops pass the agent step's
  success), a bundled one is never touched, and the drop reaches the final
  report (4628734).
- `test_review_fix_composer.py` — `claude-auto-review.yml`'s `Compose
  landing manifest` step, lifted from the workflow the same way: the
  review loop's ending contract (exactly one hand-back), the agent-field
  normalization, the codex path's hand-back / hand-off decision, and the
  rule that a hand-back without a commit needs a successful agent step
  (4628734) — with the codex no-thread-ids case run on through
  `emit-landing` and the validator.
- `test_review_composer.py` — `claude-review.yml`'s `Prepare Claude review
  for landing` and `Compose landing manifest` steps, lifted the same way
  (issue #114): the reviewer's summary / verdict / inline.json files become
  the manifest's `comments` (flagged `review`), `review_verdict` and
  `review_comments` — the lenient verdict read, the malformed-inline
  fallbacks, external mode's single comment, the codex branch untouched —
  with the pr-mode and external results run on through `emit-landing` and
  the validator under the land job's `refuse-bundle`. Also the `Compose
  settings` step: the review-dir allow and posting denies, and on the
  sandboxed paths the overlay — the checkout and the review dir on
  `denyWrite`, the scratch copy the one `allowWrite`, `claudeMdExcludes`
  and the agents-md `instructionFiles` option composed with the caller's
  entries, a caller's `allowWrite` and `tlsTerminate` dropped.
- `test_review_strip.py` — `claude-review.yml`'s untrusted-checkout
  preparation and post-agent check (Claude Security 4628445), lifted the
  same way and run against local repos: the strip renames `CLAUDE.md` /
  `CLAUDE.local.md` / `AGENTS.md` to `*.untrusted` and deletes `.claude` /
  `.mcp.json` at every depth and writes a raw snapshot tree of the stripped
  checkout; the scratch step copies the stripped tree with its
  credential-free `.git` and hands it to `claude-agent`; the post-agent check compares the whole checkout
  with that snapshot tree against tree and passes on a clean tree and on
  exactly what claude-code-action does on PR events (the base-branch restore
  of its sensitive roots when object-identical to `origin/<base>` — PR-deleted
  roots, a restored `.claude/` subtree, symlinked roots and PR content behind
  them included — and its `.claude-pr/` copy), and fails on a nested or root
  configuration entry, a changed or added file anywhere (behind a link at
  any depth, through an intermediate link followed by `..`, behind a
  glob-character target), an attribute-normalised or mode change, a
  retargeted or newline-retargeted link, a configuration root reaching
  outside the checkout or into `.git`, a root `AGENTS.md`, and a missing
  base ref or snapshot (fail-closed, a hostile `BASE_REF` included) — with
  the checkout's hooks, fsmonitor and external diff never run. Also the
  wiring: `--setting-sources user` on the agent step after the caller's args, the three steps gated on the sandboxed
  paths, the landing prep gated on the post-agent reclaim and the check, the
  check gated on the reclaim, the 2.1.246 version floor, the Claude prompt
  step identical to the codex job's but for the output directory, the
  Surface step's outcomes and note, and the prompt's scratch-copy guidance.
- `test_dev_agent_composer.py` — `claude.yml`'s `Compose landing manifest`
  step, lifted the same way: the PR open for an issue run (title, body,
  labels, base, the hand-back an `auto`-labelled PR owes and the
  `request_review_after_open` one), the `@review` hand-back on an
  autonomous PR run, the stage rule, the agent's `comments` (pinned,
  shape-checked, capped), the no-change relay, the codex summary and
  thread ids, the branch-evidence rule (nothing bundled while HEAD is off
  the run's branch) and the guard-failed codex path — with the issue-run
  case run on through `emit-landing` and the validator under the land job's
  `branch-prefix`. Also checks every `workflow_call` input declares a
  `type`.
- `test_app_token_minting.py` — Phase 2 of the credential separation, as
  structural checks on the workflow text: every `gate` and `land` job (and
  the Atlas sync) mints the machine account's token first, for exactly the
  repository and permissions the design table lists — unconditionally where
  the job cannot work without it, gated on the caller's app secrets only in
  `claude.yml` (the marvin-less degradation); every token read in those
  jobs is
  `steps.mint.outputs.token` (`|| github.token` in `claude.yml` alone); the
  agent job names none of it; the commit identity follows the gate's token;
  the retired PAT is named by no tracked file outside `design/`; this
  repo's stubs and the examples pass the two app secrets explicitly and
  nothing else.
- `test_atlas_sync.py` — the hourly Atlas sync's author checks
  (`.github/scripts/atlas_sync.py`, against a fake `gh`): `trusted_author`
  (trusted logins — the machine account under both its User and GitHub App
  logins — or the cached write-permission lookup, fail-closed; the payload's
  `author_association` decides nothing, so a MEMBER or COLLABORATOR at read
  or triage is refused), the stale hand-back revival honouring only trusted authors
  (an outsider's `@review` or forged verdict is never re-issued as the
  machine account and ends the search), the `Companion PR:` issue-body
  line counting only from a trusted author and only for a ts-mono URL, and
  the companion merge gate clearing only on a write-access reviewer's
  APPROVED review naming the companion's current head (never on
  `reviewDecision` alone) — the machine account's App login included, with
  GraphQL's bare Bot login restored to its REST form before the check.
- `test_ci_fix_binding.py` — the CI-fix loop's run-to-PR binding (Claude
  Security 4628657): the `bind-ci-run` composite's step, lifted the same
  way and run against a stub `gh` answering the run record and the
  repository's pull requests from the run's head branch. The reusable acts
  only on the workflow_run event's own run, read back from the API (this
  repo's `pull_request` run, same-repo head, on the forwarded branch, latest
  attempt the one that failed); the candidate origins are the same-repo PRs
  from that branch open when the run was created, and exactly one binds —
  both association orders, same SHA / different base, a PR closed after the
  run started, fork and same-owner-fork heads in the listing, pagination;
  the bound PR must be the caller's number (an association from another
  repository refuses), open, at the run's head SHA, with no retarget on its
  timeline at or after the run's creation (a `base_ref_changed` or
  `automatic_base_change_succeeded` event refuses); the run's actors (who
  started it, who re-ran it) must be a trusted login or hold write access,
  by a cached fail-closed lookup, other bots refused unlooked-up — not a
  PR-author rule; `revalidate` reproduces the gate's context before landing
  (a retargeted PR, a pushed branch, a re-run, a reopened PR, a missing
  context); a persistent API error fails the step. Also `sync-branch`'s
  `base` input against a local origin (the pinned base merges; a PR
  retargeted since fails before any fetch or merge; no pin keeps the live
  base; the pinned base tip merges, a moved one is refused after the fetch
  and before the merge), the base tip read from the branch and emitted (an
  unreadable one refuses), and that the fix job carries no launch signal at
  all (the action's `execution_file` output is published by its error
  handler from a pre-existing file; landing and refund key on step outcomes).
  Every
  fixture is synthetic — GitHub's event generation and association ordering
  are not reproduced. Also the workflow's wiring (the event's own run id and
  attempt and `TRUSTED_LOGINS` reach both bind steps, the gate's base reaches
  the sync, both mints carry `actions: read`, Land waits for the
  revalidation, the fix job requires the tested head and goes read-only on a
  failed Claude step) and the example stub's
  forwarding of the event's own fields.
- `test_ci_fix_gate.py` — `claude-auto.yml`'s gate (`Resolve PR and check
  the auto label`, `Gate and count`), the escalation's reset (the
  `reset-auto-counters` composite's step, given the gate's `cid`) and the
  land job's `Refund infra-crashed attempt` step, lifted
  the same way and run against a stub
  `gh`: the PR is the bind step's bound PR (an unbound run skips with the
  bind step's reason and reads nothing), viewed by number and refused when
  closed, a fork head or on another branch (never listed by branch name);
  a write-access human's label authorizes another author's PR and a
  read-only labeler disarms (the trusted-labeler policy 4628657's fix
  preserves); the attempt
  counter is read only from a trusted author's marker comment (the loop's
  own first — the machine account under either of its logins — else a
  write-access account's; permission lookups cached and fail-closed; other
  `[bot]` logins never looked up) and parsed strictly; the
  reset and the refund PATCH only that comment, and re-engagement's no-id
  reset follows the gate's rule (a maintainer's counter the gate counts
  from is reset; outsiders' and Apps' never). Also `verify-auto-labeler`'s
  `trusted-logins` input.
- `test_approval_at_head.py` — the merge queue's approval-to-head binding
  (`skills/merge-approved-prs/approval_at_head.py`): the decision on canned
  review/commit payloads (approved at head; approved then pushed; no
  approval; approval then changes requested by the same reviewer; an
  approver without write access; comments never override a verdict; the
  SHA, not commit dates, is the binding) and the command line end to end
  against a stub `gh` on PATH (GETs only, `--paginate` on the lists, the
  exit codes, a head that moves during the check). Also lifts the skill's
  approval-bound checkout blocks from `SKILL.md` and runs them against local
  repos — promotions (the approved commit is checked out and merged; a tip
  moved after the check stops it before anything is checked out) and
  External PRs (the PR head is fetched without checkout and a moved head
  carrying a `post-checkout` hook is refused without the hook running; on
  the approved head the fork wiring makes a plain push land on the
  contributor's branch) — and checks that every upstream merge request and
  re-approval in the skill names the pushed commit, that the External path
  runs `checks_at_head.py` before its checkout, and — lifting the ts-mono
  step 3 block with the helper and `gh` stubbed — that the companion merge
  happens only after `companion_mergeable.py` passes on the head as it is
  then, pinned to the SHA it returned, and never after a failed recheck.
  Also lifts the CHANGELOG-section check and the conflicted-paths block
  (finding 4628737): entries carrying apostrophes, backticks, `$(…)`, quotes
  and regex specials are each reported with their heading and none of them
  runs (the block fails on an entry under a released heading, a dropped
  entry, or a copy under both), and a conflicted file name with spaces,
  quotes and `$(…)` is inspected without reaching the shell's parser.
- `test_checks_at_head.py` — the merge queue's deferral of External PR
  trees to upstream CI (`skills/merge-approved-prs/checks_at_head.py`,
  Claude Security 4122327 criterion 2): the decision on canned payloads
  (the base branch's required checks from its rules; a required check that
  never reported, is pending or failed; a failed or pending non-required
  check; `skipped`/`neutral` pass as on GitHub; app-id matching; a
  non-success commit status; no required checks configured fails closed;
  the head must be the approved SHA) and the command line end to end
  against a stub `gh` (GETs only, the rules `--paginate`d with a required
  check on a later page enforced, explicit check-run paging by
  `total_count`, exit codes).
- `test_companion_mergeable.py` — the merge queue's check of a ts-mono
  companion's own review state before merging it
  (`skills/merge-approved-prs/companion_mergeable.py`, Claude Security
  4121986 criterion 2): approved at head passes whatever the diff;
  regenerate-only (a trusted author's same-repo PR against `main` modifying
  only `generated.ts`) passes; a hand-written change without approval, an
  approval on an older head, a diff outside the generated set, an untrusted
  or fork author, a removed/added/renamed generated file and a closed PR all
  skip with both reasons; the generated set is pinned; the machine account
  (User and App logins, `TRUSTED_AUTHORS`) is trusted as an author by name
  but its own approval is looked up and refused like anyone's without write
  access (`approval_at_head.TRUSTED_LOGINS` stays empty: it may not approve,
  Ransom 2026-09-16); the command line against a stub `gh` (GETs only, the
  head re-read, cached lookups).
- `test_skill_resolution.py` — the trust rule in `skills/checkout/checkout.sh`
  and `skills/promote/promote.sh`, run against a stub `gh` that answers from
  fixtures and logs every call: a chip from a personal fork or an untrusted
  author is refused (naming it and the reason) before any write, the
  permission lookup admits write-access collaborators and fails closed, the
  External-proxy body line is honoured only on a genuine proxy, and promote
  refuses ambiguity without `--pr`, resolves with it, and falls back to
  closing refs / the branch convention when no chip exists. The upstream PR
  body: the `Upstream issue:` header is believed only in /import's shape from
  a trusted author, bare refs are qualified, and a stub `gh api markdown`
  (standing in for GitHub's renderer) finding any other upstream reference,
  or rendering the qualified fork PR body differently from the original in
  the fork's context (code, link destinations; the stub mints fresh math,
  diagram and footnote identifiers per render, as GitHub does, and those
  alone do not count), makes promote refuse before
  any write; a trusted header's `Fixes #<up>` is prepended even when the
  body quotes one. Acceptance paths
  run through `--dry-run` only; the test clone's remote is non-routable.
  Except checkout's External path (Claude Security 4629158, 4629155), run
  for real against local repos: an outsider's upstream PR head named
  `meridian`, carrying `.claude/settings.json`, `.mcp.json`, `CLAUDE.md`,
  `CLAUDE.local.md`, `AGENTS.md`, a `post-checkout` hook the clone's
  `core.hooksPath` would resolve, and a `.gitmodules` adding a submodule
  and an out-of-tree "ts-mono" path, lands detached in a worktree outside
  the clone at the SHA the API reported — the local `meridian`, HEAD,
  branch config and `.git/config` unchanged, nothing of the tree in the
  clone, the hook never run, the submodule never initialised, the ts-mono
  companion never looked up (the stub's `gh pr checkout` emulation shows
  the unfixed behaviour fast-forwarding `meridian` and running the hook);
  a head that moved since the read and a non-SHA `headRefOid` are refused;
  a rerun reuses a clean worktree and refuses a dirty one. Destination
  aliases are refused with the clone and every other worktree unchanged: a
  relative root, a `..` traversal, a symlinked ancestor or a symlink at the
  path resolving into the clone, a directory inside another linked worktree
  (pre-created or not), a stranger's directory and a registered worktree on
  a branch. Inherited command-running configuration is inert on the first
  run and on reuse: a relative `core.fsmonitor`, and `filter.*` smudge,
  clean and process drivers (one `required`) selected by the contributor's
  `.gitattributes`, all pointing at scripts the tip supplies (a plain
  worktree add of the same tip runs them), leave no marker and the clone's
  config untouched — including drivers an `includeIf gitdir:` condition
  defines only inside the linked worktree, discovered after `worktree add
  --no-checkout` and before the first checkout. Registered worktree paths
  are read NUL-delimited and captured losslessly (a `$(…)` capture drops a
  trailing newline), so a worktree whose path holds an embedded or one or
  two trailing newlines still bounds the destination, reached through a
  newline-free symlink alias from the clone and from inside that worktree,
  with and without a pre-created parent, and a destination path with a
  newline — lexical or after resolution — is refused. The diff and removal
  recipes SKILL.md gives the operator are lifted from its ```sh block and
  run, with a worktree root holding a space, a tab, a glob character, a
  `$VAR`, both command-substitution forms, both quote characters and
  backslashes (the path enters the recipe as heredoc data, never as
  command text), against an inherited clean filter and textconv driver:
  no marker, no substitution run, the worktree gone and pruned, unrelated
  sibling files intact; the plain in-worktree status the skill no longer
  recommends does run the clean filter. Promotions keep `gh pr checkout`.
- `test_review_fix_gate.py` — `claude-auto-review.yml`'s `Gate and count`,
  `Converged handoff` and `Refund infra-crashed round` steps, lifted the
  same way and run against a stub `gh`: the loop state each reads back from
  PR comments (verdict, round counter and head SHA, re-review requests,
  hand-off marker) counts only from `REVIEWER_LOGINS` / `TRUSTED_LOGINS` or
  a write-access author, a forged marker is never adopted or PATCHed, and a
  malformed counter counts as absent (Claude Security 4121983). Also runs
  the `reset-auto-counters` composite's step (lifted the same way) in a
  gate → reset → gate sequence: escalation resets the comment the gate
  counted from (`comment-id`), so re-adding the label really starts a fresh
  budget; the no-id lookup follows the review gate's rule (the loop's own
  marker or nothing) and is covered too, as is the composite's
  `trusted-logins` default (both machine-account logins; an explicit empty
  string resets nothing). Since Claude Security 4628734: the no-progress
  check escalates on the recorded tip whatever the count reads (a refunded
  round 1 leaves `rounds: 0` with its head marker), the refund → gate
  sequence on an unchanged tip escalates, and the refund's `if:` is pinned
  to `env.AGENT_SKIPPED == 'true'` (the engine's fix job's `agent_skipped`:
  its agent step was never entered; evaluated for both engines) plus
  nothing pushed — never the agent step's own outcome, the job's result or
  an execution file; a cancellation alone, or missing outputs, keeps the
  recorded count — evaluated over a shared case table (`REFUND_CASES`)
  that `test_ci_fix_gate.py` runs against the CI-fix refund too, with the
  Land step admitting a bundle-less hand-back only on the agent step's
  success.
- `test_review_trig.py` — `claude-review.yml`'s `Check trigger` step on
  comment-triggered reviews: every `@review` needs a trusted commenter
  whatever the head repo — write access, `TRUSTED_LOGINS` (the machine
  account's User and GitHub App logins, admitted without a lookup), or a bot
  the caller's `allowed_bots` names (same-repo heads only) — with the fork
  head admitted sandboxed and a failed lookup refused (Claude Security
  4085111). Also that the review step's `allowed_bots` is the caller's list
  plus `TRUSTED_LOGINS`, and that `issue_comment` is the only event the step
  admits: a `pull_request` / `pull_request_target` run fails it red with an
  error naming the caller stub (the path was removed 2026-09-22), and no
  expression in the workflow or the stubs reads the PR-event payload.
- `test_codex_path.py` — the codex path's runner-side search path (Claude
  Security 4628448): the `assert-runner-only-path` check `create-codex-user`
  runs before its grant and `reclaim-codex-workspace` runs after codex
  (a workspace entry by path or symlink — inward, outward, and through an
  intermediate symlink component or a writable symlink parent — a relative
  or empty entry, an entry or ancestor the user can write, a not-yet-existing
  entry the user could create, the sticky-directory exception, a symlink
  loop, a clean PATH, ownership as authority — an owned read-only
  directory, an owned sticky parent, an owned 0755 executable — the files
  inside an entry followed through their symlinks (into the workspace, to a
  writable file, to a file in a replaceable directory, a chain, a shared
  chain probed once), a sticky directory accepted for one child vouching for
  no other use (a missing sibling, the directory itself, a dangling link into
  it — each after the safe child), `protect` making a writable or owned image hop or
  file runner-only instead of refusing and never touching the workspace,
  and the check's own probes resolving through the pinned system PATH —
  `sudo` is a stub answering the ownership and writability probes from a
  list and mode bits; the system directories the check is handed are a
  small fixture image under the test's directory — `bin -> usr/bin`,
  symlinked tools into the real binaries' directories, an
  /etc/alternatives chain — never the host's real /bin or /usr/bin, and a
  check that lists a directory outside the test's own fails the test); the
  reclaim, `codex-usage`, the unresolved-merge guard and `emit-landing`'s `write` step run against a
  PLANTED `.venv/bin` of `sudo`, `git`, `find`, `jq` and friends first on
  the job PATH and touch none of it; `provision-fallback` writes nothing to
  GITHUB_PATH under `add-to-path: false`; and the wiring — no agent job
  puts the venv on the job PATH (the Claude jobs provision with `user:
  claude-agent` and hand the `bin` output to the launcher's `path-prefix`,
  the codex jobs provision with `user: codex`, neither passes
  `add-to-path`), the codex jobs' compose
  steps discover the tools from the composite's `bin` directories and never
  through `command -v`, the commit steps pin PATH, the user is created,
  checked, then granted, and `create-codex-user`'s `reset-home` mode
  re-checks and pins PATH (and is codex-only). `create-codex-user`'s create
  steps run against a logging `sudo` for its `user` and `grant` inputs: the
  codex sequence unchanged by default, `claude-agent` with its landing dir,
  system `safe.directory` and `/opt/meridian-agent` and none of the codex
  parts, `grant: none` without the workspace grant, and an unknown user or
  grant refused before any command; `reclaim.sh` (the reclaim's body) for
  either user, run twice in a row with the agent tampering in between
  (idempotent), and refusing an unknown user. `codex_path_smoke.sh` is the
  hosted-runner counterpart (`.github/workflows/codex-path-smoke.yml`): the
  same lifted bodies with the real codex user, image PATH and sudo — and,
  as a second matrix leg, the claude-agent user (`SMOKE_USER`), where it
  also checks that the launcher's root-owned `/opt/meridian-agent/bin`
  passes on the job PATH — plus a control job showing the runner pick a
  planted interpreter when the venv IS on GITHUB_PATH.
- `test_claude_agent_launcher.py` — the `claude-agent-launcher` composite
  (design/executed-paths-residual.md → The launcher). The wrapper
  (`claude`) run from a throwaway `/opt/meridian-agent` layout against a
  stub `sudo` (keeps the handoff) and a `git` that answers `ls-remote`
  with a chosen status: the five MCP servers the action composes and any
  server holding a privileged token dropped, a caller's clean server kept,
  variadic and `=` forms rewritten; a privileged token elsewhere (argv,
  built env, git helper, settings, `.git/config`) refusing the launch;
  job-token mode proceeding with the job token as `GH_TOKEN`; a
  `github_token` override other than the job token treated as privileged;
  malformed and file-path `--mcp-config`, `plugin` and a second
  `--settings` refused; `--version` and non-action calls passed through;
  the env allow-list; the origin URL reset and the snapshot retaken; and
  the new-branch precondition for an issue run, closed and merged PRs, a
  PR closed after the gate and the fixed-clock collision (only "no such
  ref" proceeds), with no lookup for an open-PR follow-up or a detached
  review. `agent_ns.py`'s pure parts: the handoff format, the bind plan per
  grant mode, the POSIX ACL encoding, the action-process check against a
  fake `/proc`, the run-file and handoff-file checks. The composite as
  text (order, root-owned installs, every value the wrapper and the
  namespace read is recorded, the pinned-version pattern against sample
  `run.ts` files), the isolation check's arguments, and `reclaim.sh`'s
  removal of the WIF ACL and the agent's config dir (real xattrs on Linux).
  The namespace itself is not run by any test here or in CI: the hosted
  canary's `claude-launcher` job stops at an intentional refusal before the
  namespace exists. Successful-launch coverage is step 5's real-model runs,
  and adversarial probing is left to Claude Security scans
  (design/executed-paths-residual.md → Testing; decision: Ransom,
  2026-09-24).
- `test_dev_agent_engine.py` — `claude.yml`'s `Detect engine` step, lifted
  the same way against a stub `gh`: an issue's `auto` label is the run's
  opt-in only when the account that applied it most recently is a human
  with write access (Claude Security 4628438) — a triage account's, a
  bot's or the machine account's own label, a timeline that names no
  labeler and a failed permission lookup all leave the run one-shot with
  no `auto` in `pr_labels`; a
  decided refusal (a known labeler that is not a write-access human) also
  removes the label and posts a note saying so, but only when a second
  timeline read still shows the judged `labeled` event as the newest
  `auto` event (a label re-applied or removed during the permission
  lookup or its retry is left alone); a failed read or a run started by
  applying `auto` writes nothing, and a failed removal is said (issue
  #141); an `@auto` comment opts in without a label read. Each of an
  issue's `engine:*` labels gets the same check (#139), one label at a time
  and sharing the `auto` check's timeline read: one that fails stays out of
  `pr_labels`, and a failed `engine:codex` leaves the Claude engine; a PR's
  engine label is not checked there. A PR's `auto` label makes a landed
  commit owe the `@review` hand-back only when verify-auto-labeler would
  pass it — a writer, or either machine-account login with no lookup — so a
  non-writer's or an unverifiable label costs no reviewer run, and nothing
  is written to the PR (agents#140). Also that the land job pins the PR
  labels to the gate's read.
- `test_dev_agent_trig.py` — `claude.yml`'s `Check trigger` step, lifted the
  same way: neither of the machine account's logins kicks the dev agent off,
  from text or from an `auto`/`claude` label (Claude Security finding
  4628345 — its label was the triage agent's decision, only written by
  marvin), other Apps' labels and every bot's text trigger are refused, a
  human is authorized by the permission lookup and refused when it fails;
  an opened issue whose first line is `/import`'s `Upstream issue:` line
  is no text trigger, body or title (Claude Security 4629154), while the
  same text in a human's own issue and a label on an import still are;
  the workflow's agent steps carry no bot allow-list; and the dev stubs
  exclude both machine logins on the label path like everywhere else.
- `test_engine_job_isolation.py` — one untrusted job per engine (Claude
  Security findings 4628446 and 4629153): in each reusable workflow the
  Claude job references no `OPENAI_API_KEY` and runs no codex, the codex
  job references it only at its codex-action step, the two are selected by
  the gate's `engine` output at the job level and gate no step on it, the
  land job waits for both; the codex job `uses:` no action from the
  checkout, provisions with `provision-fallback` `user: codex` (and the
  caller's `provision`, else `codex_provision`, as `recipe`) after
  `Create codex user`, runs `Reset codex home` (`create-codex-user`
  `mode: reset-home`) before the
  codex-action step, writes nothing into the workspace after the codex user
  exists (prompt files in RUNNER_TEMP, the exclude lines appended by the
  prep step), and its prompts take the tool paths from the composite's
  `bin` directories. The Claude job (plan step 5 of
  design/executed-paths-residual.md) runs no local action and no
  `claude-setup`, composes its settings before `Create agent user`
  (`create-codex-user`, `user: claude-agent`), provisions as that user with
  the same `recipe`, then runs the pre-agent reclaim and the launcher before
  the claude-code-action step, which names the launcher's executable and
  sets `classify_inline_comments: "false"` (every claude-code-action step
  does); the post-agent reclaim is the first step after it, and every later
  git step (the origin reset, the import — which also needs a successful
  launcher and an entered Claude step — the Surface tree check, the
  composers, the reviewer's re-plant check and landing prep, emit-landing's
  read-only) is gated on it; the Surface step names each boundary step's
  failure; the reviewer's sandboxed paths create the user and launch with
  `grant: none`, hand it the scratch copy and drop the overlay's
  `.git/config` mask. `provision` and `codex_provision` are declared with a
  type and reach only the two provisioning steps. The tier-2 opt-in
  (Land: tier-2 opt-in): `claude.yml`, `claude-auto.yml` and
  `claude-auto-review.yml` declare `allow_build_config` as a boolean,
  default false, read only by the land step (as `allow-build-config`) and
  by each agent job's prompt env; the reviewer, whose land refuses any
  bundle, declares none; the example stubs show it commented out in every
  job that calls a pushing workflow. Also the composites,
  lifted and run against stubs:
  `provision-fallback`'s dispatch (a `$RUNNER_TEMP` copy under `sudo -u
  codex -H` when `user` is set, the recipe directly otherwise, a caller
  recipe handed over as a `$RUNNER_TEMP` file and refused when it is not
  bash, a failing sudo fails the step), `provision.sh` against stub
  `curl`/`uv` (the venv and the `.[dev]` / `--group dev` install, a caller
  recipe replacing them after the uv bootstrap, the `.git/info/exclude`
  lines, the `GITHUB_PATH` appends made only when that file is there),
  `create-codex-user`'s `codex-home.sh` (a planted `config.toml` symlink
  and a stray file are replaced by the profile and the server-info file;
  the link's target is untouched; given the provisioning's `bin`
  directories it appends them, ahead of the PATH sudo gives the user, as
  the `shell_environment_policy.set` PATH of codex's commands, and refuses
  an empty or relative entry or a character a TOML literal string cannot
  hold, and puts the two bwrap pin directories first, refusing without
  them), `pin-bwrap.sh` against stub `sudo`/`apt-get`/`stat`/`install`
  (installs bubblewrap when missing, refreshing the lists and retrying on a
  failed install and giving up after three; re-creates the link pin and
  the copy pin, each holding only `bwrap`, in trees that share nothing but
  `/`; refuses a binary, copy or directory hop that is not root's alone and
  a pin the codex user could write) and its `reset-home` step (kills,
  then the pin when there are bin directories, then the home script with
  the `bin` input; refuses while `pkill` keeps finding codex processes),
  and every codex job's reset step passes the provisioning step's `bin`
  output.
  The four composer tests lift each engine's composer from its own job
  (`job_block` / `lift_run` in `test_land_helpers.py`).
- `test_secret_delivery_canary.py` — the hosted canary's pipeline probe
  (`engine-isolation-canary-pipeline.yml`; finding 4629153, fix criterion
  3) keeps the agent workflows' shape: in each of the four reusable
  workflows the gate, Claude agent, codex agent and land jobs reference
  the same secrets as the probe's job in that role and no other job
  references any, the `workflow_call` secret declarations match, the agent
  jobs are selected by the gate's `engine` output like the real ones and
  the land job needs all three under `always()`; each probe job ends with
  the memory scan its references imply, the canary calls the probe per
  engine with the two sentinels only, the canary keeps a weekly off-the-hour
  schedule beside its push and dispatch triggers, and the stand-in action
  prints lengths, never values.
- `secret_delivery_scan.py`, `fixtures/secret-input/`,
  `fixtures/hostile-checkout/` and `fixtures/callers/` are not tests but
  the hosted canary's pieces
  (`.github/workflows/engine-isolation-canary.yml`): the root-run scanner
  that counts synthetic sentinel secrets in the runner processes' memory,
  the stand-in action through which the pipeline probe's gate, land and
  codex jobs consume the sentinels as action inputs (printing lengths
  only),
  the hostile `setup.py` build backend the provisioning-boundary job
  installs as the codex user, and the four representative caller projects
  (with the `codex_provision` recipe each caller's stub would set and the
  checks that its interpreter/Node version, dependency groups and lockfile
  came out as intended, run as the codex user; ts-mono-like also carries a
  turbo `gate` the canary runs by name under `codex sandbox`, and checks
  that it fails under sudo's reset PATH alone) —
  `fixtures/callers/README.md` has the table.
- `test_import.py` — `skills/import/import.sh` against a stub `gh` that
  answers the upstream issue from a fixture and records the created title
  and body: every trigger phrase and loop marker in the copied title and
  snapshot is de-fanged before `gh issue create` (Claude Security 4629154 —
  the fork issue is posted under the importing maintainer's login, whom the
  gate authorizes), the import's own shape (`Upstream issue:` first line,
  `---` rule, qualified `#N` refs) survives, `--dry-run` previews the
  de-fanged title and creates nothing, and ordinary text is copied
  unchanged.
- `test_cache_mode.py` — agent jobs get read-only Actions cache access
  (Claude Security 4629157; design/agent-cache-scope.md), as structural
  checks on the workflow text: each of the four agent workflows (and the
  canary's called workflow) declares exactly one top-level `cache-mode:
  read` before `jobs:` and no job overrides it; no `cache-mode` in
  `.github/workflows/` or `examples/` is anything but `read` or `none`,
  except the canary's `control` job, listed by file and job; every calling
  job in the example stubs and this repo's stubs caps its call at `read`.
  The enforcement itself is checked by the dispatch-only
  `.github/workflows/cache-mode-canary.yml`: a called workflow under
  `cache-mode: read`, on a trusted trigger its caller sets no mode for,
  sees the mode, and neither a mode-aware client's save (skipped) nor a
  mode-ignoring client's save (refused by the service) leaves an entry,
  while a write-mode control job's save does. That workflow's `Check` step
  is lifted and run here against a stub `gh` and `curl`: green only when
  exactly the control entry exists and restores, each job's "Set up job"
  line shows its mode, and the probe's log shows the client's skip and the
  service's `cache write denied:` refusal; red, with the reason, otherwise.
- `test_ci_workflow.py` — the CI workflow itself (`.github/workflows/tests.yml`):
  the full suite on every PR and push to main with no path filter, a
  read-only token, no `secrets.` reference, no `pull_request_target`, every
  checkout with `persist-credentials: false`, actions pinned at their major
  tag and nothing that could start an agent; and the dogfood @auto stub
  (`claude-auto-stub.yml`) equal to `examples/claude-auto-stub.yml` from
  `name:` on, both halves included, except the `workflow_run` `workflows:`
  line, which names that workflow's `name:`.
- `test_model_defaults.py` — the four Claude workflows' `model` input
  defaults to the `opus` alias (never a dated id) with `fallback_model`
  `default`, and both still reach Claude Code as `--model` /
  `--fallback-model` (design/architecture.md → Model selection).

The lifted `run:` scripts execute under the runner's shell options — `bash
-e` for a workflow step without a `shell:` key, `bash --noprofile --norc
-eo pipefail` for a composite's `shell: bash` step — so a bare non-zero
status fails in a test as it would on the runner.

Run from the repo root:

```sh
pip install pytest
python3 -m pytest
```

CI: `.github/workflows/tests.yml` (workflow `tests`, job `pytest`) runs the
same command on Python 3.12 on every PR and every push to main, with no path
filter, a read-only job token and no secrets. It is the CI workflow this
repo's @auto stub watches (`test_ci_workflow.py`).
