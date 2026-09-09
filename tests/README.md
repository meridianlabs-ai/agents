# tests/

The repo's only unit tests. There is no other test suite here — the agent
workflows are validated by triggering them (AGENTS.md → Testing a change).

- `test_validate_manifest.py` — the land job's manifest validator
  (`.github/scripts/validate_manifest.py`): one valid manifest, one failing
  case per rule.
- `test_land_helpers.py` — the `land` composite's `lib.sh` helpers (de-fang,
  retry, open-or-adopt PR against a stub `gh`, the landing-failure hint) and
  the git sequence its fetch/push steps rely on (bundle above the start SHA;
  unbundle into an empty bare repo; refuse a moved branch; push without
  `--force`), run against local repos.

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
