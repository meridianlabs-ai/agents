# tests/

The repo's only unit tests. There is no other test suite here — the agent
workflows are validated by triggering them (AGENTS.md → Testing a change).

- `test_validate_manifest.py` — the land job's manifest validator
  (`.github/scripts/validate_manifest.py`): one valid manifest, one failing
  case per rule — including the per-caller issue policy (`allowed-issue-labels`
  / `allowed-issue-assignees` / `max-issues`) and `refuse-pr` under the triage
  workflow's actual land inputs, against forged manifests; the PR-label
  policy (`allowed-pr-labels`: the dev gate's read, which a manifest's
  `pr.labels` may not exceed — Claude Security 4628441); the review fields
  refused on every caller but the reviewer's (`allow-review`), and the
  fix-agent fields (`pr`, `replies`, `resolve_threads`, `handoff_body_file`,
  `handback`) refused under `refuse-bundle`, each against the forged manifest
  its finding describes.
- `test_import_codex_final.py` — the `import-codex-final` composite's
  script (`.github/scripts/import_codex_final.py`): a regular file owned by
  the expected user is copied byte for byte; a symlink (to a runner file, or
  dangling), a directory, a FIFO (without blocking), a file owned by someone
  else and an unknown owner are refused with no copy and a stale copy
  removed; oversize input is truncated. Plus the `resolve-reported-threads`
  composite's step, lifted the same way, refusing a symlinked final message
  (no ids, no stripped copy), and the structural rule over the three
  write-path workflows: the codex-owned output file is named only by
  codex-action's `output-file`, every reader takes the imported copy, and
  the import step sits right after the reclaim (finding 4628447).
- `test_land_helpers.py` — the `land` composite's `lib.sh` helpers (de-fang,
  retry, open-or-adopt PR and the branch-existence probe against a stub
  `gh`, the landing-failure hint) and
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
- `test_ci_fix_composer.py` — `claude-auto.yml`'s `Compose landing manifest`
  step, lifted the same way: a landed fix or merge-only attempt owes the
  re-review request and sets no stage (a hand-back is mid-flight; Review is
  the gate's, on escalation), the no-change relay, and the two failure
  paths that land nothing — a failed Claude step, whatever the execution
  file says (the base merge and a commit of the run included; 4628657) and a
  codex round whose guard did not succeed.
- `test_review_fix_composer.py` — `claude-auto-review.yml`'s `Compose
  landing manifest` step, lifted from the workflow the same way: the
  review loop's ending contract (exactly one hand-back), the agent-field
  normalization, and the codex path's hand-back / hand-off decision — with
  the codex no-thread-ids case run on through `emit-landing` and the
  validator.
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
  credential-free `.git`; the post-agent check compares the whole checkout
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
  paths, the landing prep gated on the check, the 2.1.246 version floor, the
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
  closing refs / the branch convention when no chip exists. Acceptance paths
  run through `--dry-run` only; the test clone's remote is non-routable.
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
  string resets nothing).
- `test_review_trig.py` — `claude-review.yml`'s `Check trigger` step on
  comment-triggered reviews: every `@review` needs a trusted commenter
  whatever the head repo — write access, `TRUSTED_LOGINS` (the machine
  account's User and GitHub App logins, admitted without a lookup), or a bot
  the caller's `allowed_bots` names (same-repo heads only) — with the fork
  head admitted sandboxed and a failed lookup refused (Claude Security
  4085111). Also that the review step's `allowed_bots` is the caller's list
  plus `TRUSTED_LOGINS`.
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
  list and mode bits); the reclaim, `codex-usage`, the
  unresolved-merge guard and `emit-landing`'s `write` step run against a
  PLANTED `.venv/bin` of `sudo`, `git`, `find`, `jq` and friends first on
  the job PATH and touch none of it; `provision-fallback` writes nothing to
  GITHUB_PATH under `add-to-path: false`; and the wiring — the four
  workflows pass `add-to-path` from the gate's engine, the compose steps
  discover the tools from `.venv/bin` first, the commit steps pin PATH, the
  user is created, checked, then granted. `codex_path_smoke.sh` is the
  hosted-runner counterpart (`.github/workflows/codex-path-smoke.yml`): the
  same lifted bodies with the real codex user, image PATH and sudo, plus a
  control job showing the runner pick a planted interpreter when the venv IS
  on GITHUB_PATH.
- `test_dev_agent_engine.py` — `claude.yml`'s `Detect engine` step, lifted
  the same way against a stub `gh`: an issue's `auto` label is the run's
  opt-in only when the account that applied it most recently is a human
  with write access (Claude Security 4628438) — a triage account's, a
  bot's or the machine account's own label, a timeline that names no
  labeler and a failed permission lookup all leave the run one-shot with
  no `auto` in `pr_labels`, the issue's `engine:*` labels still copied; an
  `@auto` comment opts in without a label read; a PR's label is left to the
  loop gates. Also that the land job pins the PR labels to the gate's read.
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
- `test_import.py` — `skills/import/import.sh` against a stub `gh` that
  answers the upstream issue from a fixture and records the created title
  and body: every trigger phrase and loop marker in the copied title and
  snapshot is de-fanged before `gh issue create` (Claude Security 4629154 —
  the fork issue is posted under the importing maintainer's login, whom the
  gate authorizes), the import's own shape (`Upstream issue:` first line,
  `---` rule, qualified `#N` refs) survives, `--dry-run` previews the
  de-fanged title and creates nothing, and ordinary text is copied
  unchanged.
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

CI: `workflow-tests.yml` in this directory is the job that runs the same
command on pushes and PRs touching the validator, the composites or the
tests. It lives here rather than under `.github/workflows/` only because the
agent that opened the plumbing PR cannot write there (GitHub App permission);
move it to `.github/workflows/tests.yml` in a follow-up PR (a human-opened
one, or one with a top-level `@review` — see CLAUDE.md on workflow-editing
PRs).
