# tests/

The repo's only unit tests. There is no other test suite here — the agent
workflows are validated by triggering them (AGENTS.md → Testing a change).

- `test_validate_manifest.py` — the land job's manifest validator
  (`.github/scripts/validate_manifest.py`): one valid manifest, one failing
  case per rule — including the per-caller issue policy (`allowed-issue-labels`
  / `allowed-issue-assignees` / `max-issues`) and `refuse-pr` under the triage
  workflow's actual land inputs, against forged manifests.
- `test_land_helpers.py` — the `land` composite's `lib.sh` helpers (de-fang,
  retry, open-or-adopt PR and the branch-existence probe against a stub
  `gh`, the landing-failure hint) and
  the git sequence its fetch/push steps rely on (bundle above the start SHA;
  unbundle into an empty bare repo; refuse a moved branch; push without
  `--force`), run against local repos — including `emit-landing`'s `write`
  step, lifted from the action and run against those repos.
- `test_ci_fix_composer.py` — `claude-auto.yml`'s `Compose landing manifest`
  step, lifted the same way: a landed fix or merge-only attempt owes the
  re-review request and sets no stage (a hand-back is mid-flight; Review is
  the gate's, on escalation), the no-change relay, and the errored attempt.
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
  the validator under the land job's `refuse-bundle`.
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
  logins — `author_association`, the cached write-permission lookup,
  fail-closed), the stale hand-back revival honouring only trusted authors
  (an outsider's `@review` or forged verdict is never re-issued as the
  machine account and ends the search), the `Companion PR:` issue-body
  line counting only from a trusted author and only for a ts-mono URL, and
  the companion merge gate clearing only on a write-access reviewer's
  APPROVED review naming the companion's current head (never on
  `reviewDecision` alone) — the machine account's App login included, with
  GraphQL's bare Bot login restored to its REST form before the check.
- `test_ci_fix_gate.py` — `claude-auto.yml`'s gate (`Resolve PR and check
  the auto label`, `Gate and count`), the escalation's reset (the
  `reset-auto-counters` composite's step, given the gate's `cid`) and the
  land job's `Refund infra-crashed attempt` step, lifted
  the same way and run against a stub
  `gh`: the PR is resolved from `pr_number` and refused when closed, a fork
  head or on another branch (never listed by branch name); the attempt
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
- `test_dev_agent_trig.py` — `claude.yml`'s `Check trigger` step, lifted the
  same way: neither of the machine account's logins kicks the dev agent off,
  from text or from an `auto`/`claude` label (Claude Security finding
  4628345 — its label was the triage agent's decision, only written by
  marvin), other Apps' labels and every bot's text trigger are refused, a
  human is authorized by the permission lookup and refused when it fails;
  the workflow's agent steps carry no bot allow-list; and the dev stubs
  exclude both machine logins on the label path like everywhere else.

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
