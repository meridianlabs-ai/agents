# emit-landing / land — the landing manifest

Phase 1 of the credential-separation work (issue #79; design:
[design/architecture.md → Landing job](../../../design/architecture.md#landing-job)).
Two composites and a validator move every privileged write out of the job that
runs the agent:

```
gate job   (trusted: trigger check + pre-agent marvin writes; job token + MARVIN_TOKEN; no checkout of PR code)
  -> agent job  (untrusted: checkout, provision, agent; job token ONLY; commits locally; emit-landing)
  -> land job   (trusted: fresh runner; land: download, validate, push, post, resolve, stage as marvin)
```

- **`emit-landing`** runs last in the agent job (`if: always()`). It bundles the
  commits above the run's start SHA, writes `manifest.json`, and uploads the
  landing directory as one artifact. It holds no token. The upload sets
  `overwrite: true`: artifacts belong to the run, not the attempt, so on a
  re-run from the Actions UI the new attempt's landing replaces the previous
  one instead of the land job re-landing the first attempt's manifest.
- **`.github/scripts/validate_manifest.py`** runs first in the land job. Stdlib
  only; one line per violation; any violation refuses the whole manifest.
  Tests: `tests/test_validate_manifest.py`.
- **`land`** is the land job's body: download → validate → push → PR → posts →
  hand-back / hand-off → stage → provenance → error report. It never checks
  out code; the manifest is data until the validator has passed it.

The manifest is the **trust boundary**. The agent job — where the agent, its
subagents and anything it left running had a shell and the job token — can
write anything into the artifact; the land job acts only on what the validator
accepts, and every body it posts passes through the de-fang sed (triggers lose
their `@`, loop markers are split) except two fixed bodies no agent text
reaches: the hand-back, posted verbatim as exactly `@review`, and the
reviewer's verdict comment, chosen by `review_verdict`.

## Manifest schema (version 1)

`landing/manifest.json`. Every `*_file` value is a path **relative to the
landing directory**; the workflow writes those files there before
`emit-landing` runs. Unknown keys at any level are a violation (fail closed on
schema drift): a new field must be added to the validator's `KNOWN_*` sets and
to this document together.

```json
{
  "schema": 1,
  "repo": "owner/name",
  "run_id": 123,
  "branch": "claude/issue-123-fix-thing",
  "start_sha": "<40 hex>",
  "head_sha": "<40 hex>",
  "has_bundle": true,
  "pr_number": 456,
  "issue_number": 123,
  "pr": { "open": true, "title": "…", "body_file": "pr-body.md", "base": "main", "labels": ["auto"], "issue": 123 },
  "comments": [ { "number": 456, "body_file": "c1.md" } ],
  "replies":  [ { "review_comment_id": 789, "body_file": "r1.md" } ],
  "resolve_threads": [ "PRRT_…" ],
  "issues":   [ { "repo": "owner/name", "title": "…", "body_file": "i1.md", "labels": ["auto"], "comment_on": null } ],
  "stage": "Review",
  "handback": true,
  "handoff_body_file": "handoff.md",
  "error": { "message": "…", "fail_run": true },
  "provenance_comment_file": "prov.md",
  "review_verdict": "suggestions"
}
```

| field | set by | rule |
| --- | --- | --- |
| `schema` | emit-landing | must be `1` |
| `repo` | emit-landing (`$GITHUB_REPOSITORY`) | must equal the repo the land job operates on (case-insensitive) |
| `run_id` | emit-landing (`$GITHUB_RUN_ID`) | must equal the land job's `$GITHUB_RUN_ID` — an artifact from another run cannot be replayed |
| `branch` | emit-landing (`branch` input) | `^[A-Za-z0-9._/-]{1,200}$`, no `..`, not `refs/…`, not the default branch (the land job's `default-branch` input, looked up with the read token when the event carries none; an empty default branch refuses the manifest rather than skip the rule), not on the land job's `refused-branches` list (`main` by default — so the inspect_ai fork's pristine `main`, which is not its default branch, is refused by the validator and not only by its ruleset), not `pr.base`; when `pr_number` is set, must equal that PR's live `headRefName`; when the run names no PR and the land job's `branch-prefix` is set, must start with it (an issue run's `claude/issue-N-`) — with no PR and no prefix the branch is agent-chosen within the bounds above. Under the land job's `refuse-bundle` (the reviewer) nothing is ever pushed to `branch`, so the shape, default-branch and refused-list rules do not apply — a fork-head PR's `main`, or any head ref git accepts, passes; only the head-ref pin / `branch-prefix` and a no-control-characters, ≤ 1000 chars check remain |
| `start_sha`, `head_sha` | emit-landing | 40 lowercase hex; equal iff `has_bundle` is false |
| `has_bundle` | emit-landing | boolean; when true `commits.bundle` must exist and its tip must be `head_sha` and descend from `start_sha` (checked by `land`, in an empty repo) |
| `pr_number` | emit-landing (`pr-number` input) | positive integer or null; **must equal the land job's `pr-number` input** (null when that is empty) — the PR the run's trusted context names, so on a PR run an agent job cannot steer the push, replies, thread resolutions and hand-back at a PR of its choosing, nor drop the number to skip the head-ref rule and let `pr.open` adopt another PR (on a run that names no PR, `branch-prefix` is what keeps the push off the branches of PRs opened for other issues; a still-open PR from an earlier run on the same issue carries the prefix and is adopted). Required by `replies` and `resolve_threads` |
| `issue_number` | emit-landing (`issue-number` input) | positive integer or null; **must equal the land job's `issue-number` input** the same way; where `land` posts the error report / hand-off / provenance when there is no PR |
| `pr` | workflow | `open` (bool), `title` (≤ 256 chars), `body_file`; optional `base` (branch name; absent or empty means the land job's default branch, as `gh pr create` would default), `labels` (strings, applied whether `land` opened the PR or adopted an agent-opened one — the `auto` and `engine:*` labels must reach both) and `issue` (the originating issue, gets a "✅ Opened a pull request" comment only when `land` opened the PR). Skipped when `pr_number` is already set; an existing open PR for `branch` is adopted, and the adopt check runs inside the create retry so a create whose response was lost is adopted on the next attempt, not duplicated |
| `comments[]` | workflow | `number` (positive integer — an issue or a PR; the issues endpoint serves both), `body_file` |
| `replies[]` | workflow | `review_comment_id` positive integer, `body_file`; posted on `pr_number` |
| `resolve_threads[]` | workflow | `^PRRT_[A-Za-z0-9_-]+$`; `land` resolves only IDs that belong to `pr_number` |
| `issues[]` | workflow | `repo` in the land job's `allowed-issue-repos`, `title` (≤ 256), `body_file`, optional `labels`, optional `comment_on` (positive integer: comment on that issue instead of creating one). Created issues are added to Atlas by node ID |
| `stage` | workflow | one of `Contributor`, `Agent`, `Review`, `Sign-off`, `Merge`, or absent. Dropped by `emit-landing`, together with `handback`, when HEAD moved past `start_sha` but the bundle could not be written (non-descendant HEAD, or a `git bundle` failure): nothing lands, so the stage stays where the gate put it; the drop is recorded in `error` |
| `handback` | workflow | boolean; posts exactly `@review` on the PR (needs `pr_number` or `pr.open`). A hand-back is owed by a landed commit, so `emit-landing` drops it with the bundle (see `stage`) — the workflow composes it from its own "HEAD moved" test, and this is what keeps the push and the hand-back from coming apart |
| `handoff_body_file` | workflow | posted on the PR (or issue) with `<!-- auto-handoff -->` as its first line |
| `error` | workflow (or emit-landing on a packaging failure) | `message` (string), `fail_run` (bool); posted de-fanged on the PR/issue, and the land job exits non-zero after every other step when `fail_run` is true. The same final report names any landing step that failed after the push (a lost comment, reply or follow-up issue is recorded rather than allowed to block the hand-back, hand-off and stage move, and a failed hand-back does not withhold the hand-off or the stage move either) and every planned hand-back, hand-off or stage move that never ran because the PR step failed after the push (a failed fetch or push owes nothing — the work never landed), and posts that on the PR/issue too. When the manifest never validated, the report's target is the land job's `pr-number` / `issue-number` inputs (from the event payload — see below), so a refusal reaches the requester |
| `provenance_comment_file` | workflow | posted with `<!-- model-provenance -->` as its first line |
| `review_verdict` | workflow (claude-review.yml's codex path) | `clean` or `suggestions`; needs `pr_number`. `land` posts one of two FIXED bodies — the reviewer's `🔎 Review complete …` marker comment with the `claude-review-summary` / `claude-review-verdict:<value>` markers live — after `comments[]` (which carries the de-fanged review body) and only when the Post step lost nothing, so the @auto loop never sees a verdict over a review that did not land. The second body posted verbatim besides the hand-back; no agent text reaches it |

`emit-landing`'s `read-only` input and `land`'s `refuse-bundle` input are the
pair for a caller whose agent never commits (claude-review.yml): the former
runs no git at all and writes `head_sha` = `start_sha`, `has_bundle` false;
the latter makes the validator refuse any manifest that carries commits,
claims HEAD moved or ships a `commits.bundle` — whatever the agent job
uploaded — so the reviewer's land job cannot become the push channel its own
`contents: read` token denies it. With no push, `branch` is only the pin that
ties the manifest to the run's PR (its live head ref) or, in external mode,
to the `branch-prefix`: the push-side branch rules are off, so a review of a
fork-head PR whose branch is `main` lands (see the `branch` row above).

File references: relative, `^[A-Za-z0-9._/-]{1,200}$`, no `..`, no path
component starting with `.` (emit-landing uploads with
`include-hidden-files: false`, so a dot-named file or directory would be
silently dropped from the artifact — the validator refuses the name instead
of reporting a missing file), no symlink anywhere in the path, a regular file
inside the landing directory, under 64 KiB. `manifest.json` itself must be a
regular file under 1 MiB.

Core fields (everything emit-landing sets) **win** over the workflow's
`manifest-extra`: the extra supplies only what the core does not define.

## Using the pair

`pr-number` / `issue-number` go to **both** composites as the **same
expression**, evaluated in the caller's trusted context (the event payload,
or a gate-job output computed before any untrusted code ran): `emit-landing`
copies them into the manifest, and `land`'s validator refuses a manifest
whose numbers differ from its own inputs. That is what ties the landing to
the PR/issue the run is actually for.

Mind the PR-comment shape: on `issue_comment` events for a PR (the
`@claude`-on-a-PR and auto-loop runs) `github.event.pull_request.number` is
**empty** and the PR number is `github.event.issue.number`, so a snippet that
reads only the former passes an empty `pr-number` there, the validator forces
`pr_number: null`, and every manifest with `replies`, `resolve_threads` or
`handback` is refused ("need pr_number"). `github.event.issue.pull_request`
is present in the payload exactly when the issue is a PR — a trusted, API-free
discriminator — so one expression covers `pull_request*`,
`pull_request_review*` and PR-comment events, and its complement yields the
issue number only on a real issue:

```yaml
pr-number: ${{ github.event.pull_request.number || (github.event.issue.pull_request && github.event.issue.number) || '' }}
issue-number: ${{ !github.event.issue.pull_request && github.event.issue.number || '' }}
```

A `workflow_run` event (claude-auto.yml) names no PR in a usable shape
(`workflow_run.pull_requests` is empty for many runs), so there the trusted
value is the **gate job's** API lookup — `gh pr list --head <branch>` run
before any PR code was checked out — passed as `needs.gate.outputs.pr` to
both composites; `pr-head-ref` is the event's head branch, which is what the
gate resolved the PR by.

Agent job, last step. It runs git in the workspace, so on the codex path it
is gated on the reclaim step having **succeeded** — `== 'success'`, never
`!= 'failure'`: a reclaim cancelled mid-run must skip every later git call
(AGENTS.md → the codex path). The Claude path has no reclaim step, hence the
path-aware condition (`codexuser` / `codexreclaim` are claude.yml's step ids):

```yaml
      - name: Emit landing manifest
        if: always() && (steps.codexuser.outcome != 'success' || steps.codexreclaim.outcome == 'success')
        uses: meridianlabs-ai/agents/.github/actions/emit-landing@main
        with:
          start-sha: ${{ steps.sync.outputs.start_sha || steps.base.outputs.sha }}
          branch: ${{ steps.sync.outputs.branch || steps.claude.outputs.branch_name }}
          pr-number: ${{ github.event.pull_request.number || (github.event.issue.pull_request && github.event.issue.number) || '' }}
          issue-number: ${{ !github.event.issue.pull_request && github.event.issue.number || '' }}
          manifest-extra: ${{ runner.temp }}/landing-extra.json   # composed by an earlier step
```

Land job (`needs: agent`, `if: always()`, a fresh runner, **no checkout**):

```yaml
      - uses: meridianlabs-ai/agents/.github/actions/land@main
        with:
          token: <MARVIN_TOKEN — the land job is the only job that names it>
          allowed-issue-repos: ${{ github.repository }}
          # The same expressions as above: the validator pins the manifest's
          # pr_number / issue_number to them, and they are the trusted
          # fallback target for the final report when the manifest never
          # validated (missing artifact, tampered manifest) — from the EVENT
          # payload, never from the manifest.
          pr-number: ${{ github.event.pull_request.number || (github.event.issue.pull_request && github.event.issue.number) || '' }}
          issue-number: ${{ !github.event.issue.pull_request && github.event.issue.number || '' }}
          # Issue runs have no PR head ref to pin the branch to; the prefix
          # claude-code-action gives issue branches, composed from the
          # trusted issue number, keeps the push off the branches of PRs
          # opened for other issues. Ignored on PR runs.
          branch-prefix: ${{ !github.event.issue.pull_request && github.event.issue.number && format('claude/issue-{0}-', github.event.issue.number) || '' }}
```

(The token expression is spelled out in `examples/landing-smoke.yml`; it is
kept out of this directory so `grep -rn 'secrets\.' .github/actions/emit-landing`
stays empty — issue #79's third verification.)

`examples/landing-smoke.yml` is a complete three-job workflow that exercises
the pair on a throwaway branch.
