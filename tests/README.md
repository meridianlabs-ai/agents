# tests/

The repo's only unit tests. There is no other test suite here — the agent
workflows are validated by triggering them (AGENTS.md → Testing a change).

- `test_validate_manifest.py` — the land job's manifest validator
  (`.github/scripts/validate_manifest.py`): one valid manifest, one failing
  case per rule.
- `test_land_helpers.py` — the `land` composite's `lib.sh` helpers (de-fang,
  retry, open-or-adopt PR and the branch-existence probe against a stub
  `gh`, the landing-failure hint) and
  the git sequence its fetch/push steps rely on (bundle above the start SHA;
  unbundle into an empty bare repo; refuse a moved branch; push without
  `--force`), run against local repos — including `emit-landing`'s `write`
  step, lifted from the action and run against those repos.
- `test_review_fix_composer.py` — `claude-auto-review.yml`'s `Compose
  landing manifest` step, lifted from the workflow the same way: the
  review loop's ending contract (exactly one hand-back), the agent-field
  normalization, and the codex path's hand-back / hand-off decision — with
  the codex no-thread-ids case run on through `emit-landing` and the
  validator.
- `test_dev_agent_composer.py` — `claude.yml`'s `Compose landing manifest`
  step, lifted the same way: the PR open for an issue run (title, body,
  labels, base, the fork's review request), the `@review` hand-back on an
  autonomous PR run, the stage rule, the agent's `comments` (pinned,
  shape-checked, capped), the no-change relay, the codex summary and
  thread ids, the branch-evidence rule (nothing bundled while HEAD is off
  the run's branch) and the guard-failed codex path — with the issue-run
  case run on through `emit-landing` and the validator under the land job's
  `branch-prefix`. Also checks every `workflow_call` input declares a
  `type`.
- `test_atlas_sync.py` — the hourly Atlas sync's author checks
  (`.github/scripts/atlas_sync.py`, against a fake `gh`): `trusted_author`
  (trusted logins, `author_association`, the cached write-permission lookup,
  fail-closed), the stale hand-back revival honouring only trusted authors
  (an outsider's `@review` or forged verdict is never re-issued as the
  machine account and ends the search), and the `Companion PR:` issue-body
  line counting only from a trusted author and only for a ts-mono URL.
- `test_ci_fix_gate.py` — `claude-auto.yml`'s gate (`Resolve PR and check
  the auto label`, `Gate and count`), the escalation's `Reset the attempt
  counter` and the land job's `Refund infra-crashed attempt` step, lifted
  the same way and run against a stub
  `gh`: the PR is resolved from `pr_number` and refused when closed, a fork
  head or on another branch (never listed by branch name); the attempt
  counter is read only from a trusted author's marker comment (the loop's
  own first, else a write-access account's; permission lookups cached and
  fail-closed; `[bot]` logins never looked up) and parsed strictly; the
  reset and the refund PATCH only that comment. Also `verify-auto-labeler`'s
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
  re-approval in the skill names the pushed commit.

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
