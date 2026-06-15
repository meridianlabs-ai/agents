# Migrating the scheduled inspect tests to the fork — design (proposed)

**Status: proposed, not yet implemented.** This documents the plan to move the
scheduled slow/integration tests for inspect_ai (and their failure triage) from
`meridianlabs-ai/actions` onto the `meridian` branch of the
`meridianlabs-ai/inspect_ai` fork, and to close the triage → fix loop using the
agent infrastructure already in place.

## What exists today (in `meridianlabs-ai/actions`)

- **`inspect-ai-scheduled-tests.yml`** — runs the slow/integration test suite
  every 2 hours (also on push to all branches, and via `workflow_dispatch`).
  It checks out `UKGovernmentBEIS/inspect_ai` (tracking the last-tested SHA in a
  `last-inspect-ai-sha` artifact), builds a Docker test image, runs the slow
  tests against real providers, and posts results to Slack (recording the tested
  inspect_ai SHA and the Slack thread in a `triage-context` artifact).
- **`triage-test-failures.yml`** — fires via `workflow_run` when a *scheduled*
  test run fails. It downloads the failed logs, checks out inspect_ai at the
  tested SHA (read-only), and runs a Claude triage agent (auth:
  `ANTHROPIC_API_KEY`) that classifies the failure into one of four buckets and
  acts:
  - **A — already fixed on main**: note the fixing commit in Slack; don't file.
  - **B — provider outage / intermittent**: note in Slack; don't file.
  - **C — diagnosable bug with a clear fix**: open/update a tracking issue in
    the *actions* repo with a proposed diff, assigned to a human.
  - **D — unclear/risky**: file a "needs investigation" issue.

  Triage is **read-only on inspect_ai** — it never opens PRs. A human applies
  the fix (the issue body tells them to run `claude` locally to open the PR).

Secrets in `actions`: `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `GROK_API_KEY`,
`ANTHROPIC_API_KEY`, `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID`.

## Why move to the fork

1. **Co-location simplifies the test workflow.** On the fork, inspect_ai *is*
   the repo. A scheduled run checks out `ref: main` (the pristine mirror, kept
   current hourly), so the cross-repo clone and the `last-inspect-ai-sha`
   artifact dance largely go away — "what we tested" is just the fork's `main`
   SHA at run time.
2. **It closes the triage → fix loop.** Today triage files an issue in `actions`
   and a human manually runs `claude` to produce a PR. On the fork, triage can
   file the issue **on the fork with the `claude` label**, which triggers the
   dev agent → it branches from `main`, implements the fix, runs the tests to
   verify, and opens a fork PR (the existing two-stage flow) → a human reviews
   and promotes it upstream. This is the loop the project set out to build.
3. **Auth consolidation.** Triage can move from a metered `ANTHROPIC_API_KEY` to
   the same Workload Identity Federation the other agents use, so its usage is
   tracked and capped in the workspace.

### Honest alternative (considered, not recommended)

Cron fires wherever the workflow lives, so the tests **don't strictly have to
move**. A lighter option: leave the tests in `actions` and only change triage to
file its issues on the **fork** (cross-repo) with the `claude` label. That
avoids replicating provider secrets onto a public repo, but keeps the
SHA-tracking machinery and splits the system across two repos. We prefer the
full migration for the co-location and single-home benefits; the secret
replication is manageable (see below).

## Design

### Branch mechanics

- Both workflows live on **`meridian`** (the default branch — schedule and
  `workflow_run` only fire from the default branch).
- The scheduled-test workflow checks out **`ref: main`** explicitly (pristine
  upstream; the meridian delta is only workflow files, which we don't want to
  test). Record the resolved `main` SHA as an artifact for triage's
  "already-fixed-on-main" diff.
- **Drop the `push: ["**"]` trigger.** In `actions` it ran tests on every branch
  during workflow development; on the fork it would run the *slow* suite on
  every `claude/*` branch push — prohibitively expensive. Keep `schedule` +
  `workflow_dispatch` only.

### Auth & secrets

- **Provider keys** (`OPENAI`/`GOOGLE`/`GROK`/`ANTHROPIC` for the *tests*) and
  **Slack** secrets must be available to the fork. Options: repo secrets on the
  fork, or org-level secrets scoped to it. The fork is **public**, but GitHub
  encrypts secrets and does **not** expose them to fork-PR-triggered runs, so
  scheduled/dispatch runs are safe. (If putting provider keys on a public repo's
  secret store is undesirable, org-level secrets are the alternative.)
- **Triage agent auth** moves to **WIF** (the `claude-code-agent` workspace),
  matching the dev/reviewer agents — drop the standalone `ANTHROPIC_API_KEY` for
  triage. (The tests still need `ANTHROPIC_API_KEY` as a *provider* key for
  Anthropic-backed test cases; that's unrelated to WIF.)

### The triage → fix loop (the key new behavior)

Map triage buckets to actions so that **only confident fixes auto-trigger the
dev agent**, and flakes/unknowns stay human-gated:

| Bucket | Today | Proposed on the fork |
|---|---|---|
| A — already fixed on main | Slack note | unchanged (Slack note, no issue) |
| B — provider outage / intermittent | Slack note | unchanged (no issue, no fix) |
| C — diagnosable, clear fix | issue in `actions`, human applies | **issue on the fork _with the `claude` label_** → dev agent opens a fix PR |
| D — unclear / risky | "needs investigation" issue | issue on the fork, **no label** (human triages first) |

This is the safety crux: **never auto-label B or D.** Slow integration tests
flake; auto-opening a fix PR for every failure would churn tokens and annoy
upstream. The bucket classifier already exists and is conservative ("when in
doubt between C and D, pick D") — we reuse it and gate the label on bucket C.

The human gate on **upstream promotion** is unchanged: the dev agent's fix lands
as a fork PR (review surface); a human reviews and promotes it to
`UKGovernmentBEIS/inspect_ai`.

### Public-repo / operational notes

- **Scheduled-workflow auto-disable**: GitHub disables scheduled workflows after
  60 days of no repo activity. The fork has hourly sync commits and regular
  agent branches, so it should stay active; if not, port the `keep-alive.yml`
  pattern from `actions`.
- **Cost**: the slow suite every 2 hours against real providers is real spend on
  the provider keys, plus WIF usage for triage and any bucket-C fix runs.
  Revisit the cadence on the fork (a full slow suite may warrant daily rather
  than 2-hourly) and set workspace spend caps.

## Migration steps (when ready)

1. Replicate provider + Slack secrets to the fork (or set org-level secrets
   scoped to it).
2. Port `inspect-ai-scheduled-tests.yml` to `meridian`: drop the push trigger,
   check out `ref: main`, record the tested `main` SHA as an artifact, keep the
   Slack notification.
3. Port `triage-test-failures.yml` to `meridian`: switch to WIF auth; file
   issues **on the fork**; apply the `claude` label only for bucket C; check out
   inspect_ai from the fork itself (no cross-repo clone).
4. Validate via `workflow_dispatch` and a synthetic failure: confirm a bucket-C
   issue gets labeled, the dev agent opens a verified fix PR, and B/D issues do
   not trigger a fix.
5. Decommission the two workflows from `meridianlabs-ai/actions` once the fork
   versions are proven. `actions` keeps any non-inspect infrastructure.

## Open decisions (resolve at implementation time)

- **Test target**: `main` (pristine upstream — recommended; tests what upstream
  ships) vs `meridian`.
- **Auto-fix scope**: bucket C only (recommended), or start with file-only
  (no `claude` label) and enable auto-fix after observing triage quality.
- **Secret location**: fork repo secrets vs org-level secrets.
- **Cadence**: keep every-2-hours, or reduce (e.g. daily) given provider cost.
- **Keep-alive**: rely on organic fork activity, or port `keep-alive.yml`.
