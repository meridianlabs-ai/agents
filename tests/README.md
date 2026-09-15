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
- `test_review_fix_gate.py` — `claude-auto-review.yml`'s `Gate and count`,
  `Converged handoff` and `Refund infra-crashed round` steps, lifted the
  same way and run against a stub `gh`: the loop state each reads back from
  PR comments (verdict, round counter and head SHA, re-review requests,
  hand-off marker) counts only from `REVIEWER_LOGINS` / `TRUSTED_LOGINS` or
  a write-access author, a forged marker is never adopted or PATCHed, and a
  malformed counter counts as absent (Claude Security 4121983). Also runs
  the `reset-auto-counters` composite's step (lifted the same way) in a
  gate → reset → gate sequence: escalation resets the counter the gate
  selected, so re-adding the label really starts a fresh budget.
- `test_review_trig.py` — `claude-review.yml`'s `Check trigger` step on
  comment-triggered reviews: every `@review` needs a trusted commenter
  whatever the head repo — write access, `TRUSTED_LOGINS`, or a bot the
  caller's `allowed_bots` names (same-repo heads only) — with the fork head
  admitted sandboxed and a failed lookup refused (Claude Security 4085111).

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
