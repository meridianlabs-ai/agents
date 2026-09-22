# Credential separation in the agent workflows

How the workflows in this repository keep the machine account's credential,
and every other secret they control, away from content an outsider can
shape, as they stand on `main` today. Written for someone who
maintains these workflows or adds a caller. The rules here are the ones the
tests under `tests/` check and the ones a review of a workflow change holds
it to; [SECURITY.md](../SECURITY.md) is the short public statement of the
same boundaries.

**History.** How this shape was reached, one issue and one review round at a
time, is in [architecture.md](architecture.md) ("No persisted git
credentials", "Landing job", "Untrusted checkouts") and stays there; this
document describes the result, not the path.

## 1. The problem class

An agent that works on a repository reads things an outsider wrote: the head
of a pull request (a fork's, or a same-repo branch a contributor pushed), the
text of issues and comments (the inspect_ai fork is public, so anyone can
write there), the output of tests it runs, dependency alerts, CI logs, and
the packages a `pip install` pulls. If the job that reads those also holds a
credential that can push, post or write to another repository, then whoever
shaped the content can, through the agent or through code the job executes,
use that credential. Before 2026-09 every agent-running workflow here held
the machine account's long-lived, multi-repo personal access token in the
same job as the agent, and the token was persisted into the checkout by
`actions/checkout`. The design below removes the credential from that job
entirely rather than trying to constrain what the agent does with it:
constraints enforced in code hold where prompts do not.

## 2. Invariants

The four reusable workflows in this repository (`claude.yml`,
`claude-review.yml`, `claude-auto.yml`, `claude-auto-review.yml`) satisfy
these as written, and `tests/test_app_token_minting.py` checks the ones
that can be read from the workflow text. Section 4 records, workflow by
workflow, where the Atlas sync, the `actions` repository's workflows and
the other Meridian repositories' conversions meet them and where they
differ; the exceptions are listed there, not assumed away here.

- **I1. The agent job holds no credential of the machine account.** A job
  that runs an agent, or any code from a checkout the org does not fully
  control, references no secret of the machine account and no token minted
  from it: not in `env:`, not as an action input, not through a composite.
  What it does hold is its own single-repo, read-only job token, the model
  credential, and, while the `claude-code-action` step runs, that action's
  own installation token of the Claude GitHub App, which is write-capable on
  the caller repository and is fenced by identity and a deny list, not by
  the job's `permissions:` block (section 3.5).
- **I2. Every write the agent asks the machine account for happens in a
  landing job.** Pushes,
  PR creation, comments and review replies, thread resolutions, issue
  writes, Atlas board moves and Slack posts that the agent job requests run
  in a separate job on a fresh runner. That job checks out no third-party
  code, installs nothing, and acts only on a validated manifest and a git
  bundle produced by the agent job. Two kinds of write sit outside the
  manifest and are deterministic, never agent-controlled: the trusted
  gate's own writes before the agent runs (the acknowledgement, the stage
  move to Agent, labels, loop counters), and the land job's final error
  report, posted to the PR or issue the event payload names when the
  manifest was refused.
- **I3. The privileged identity is minted per job and scoped per repository.**
  Each trusted job obtains its own GitHub App installation token, for the one
  repository it writes to and the permissions it uses, and the token is
  revoked when the job ends. The reusable workflows in this repository read
  no long-lived write token; revoking the retired PAT in the machine
  account and deleting its org secret are administrative steps that these
  files do not establish (section 3.4).
- **I4. Untrusted code never runs before authorization or outside a sandbox.**
  Who may trigger a run is decided by a deterministic step, by login or by a
  permission lookup that fails closed, before any PR head is checked out and
  before any local action runs. Code from a fork head or an external checkout
  runs only inside the reviewer's OS-level sandbox, and project configuration
  from that tree (`.claude/`, `.mcp.json`, `CLAUDE.md`) is removed before the
  agent starts.
- **I5. No credential is persisted in the workspace.** Every
  `actions/checkout` in the four reusable workflows runs
  `persist-credentials: false`. A runner-side git command that must
  authenticate does so through a credential helper defined in that one step's
  environment, keyed to `github.server_url` so no other host is ever
  answered. The one credential that does sit in the workspace for a bounded
  time is the Claude action's own token, which the action writes into
  `remote.origin.url` for the duration of its step and the `reset-origin-url`
  composite removes right after (section 3.5). The Atlas sync's checkout of
  this repository, in a trusted job that runs no agent, uses checkout's
  defaults and so persists the job token.
- **I6. No transcript is uploaded.** The enforced policy is that no agent
  transcript is uploaded, by any switch (decision: Ransom, 2026-09-08). Cost,
  model, turns and the error flag go to the job summary; the reusable
  workflows' only artifact is the landing directory, which holds a manifest,
  body files and a bundle of commits. The body files are agent-written text
  that the land job posts after the de-fang; nothing scans them for secrets,
  so this is a policy about the transcript, not a guarantee about every
  file an agent produces. The `actions` repository's ci-perf workflow
  uploads its evidence files and refuses to publish any that contains its
  key (section 4.2).

## 3. The architecture

### 3.1 Gate, agent, land

The four reusable workflows are three jobs each (the `actions`
repository's two agent workflows fold the gate's role into the agent job's
`if:` and run two; section 4.2):

```
gate   (trusted)    trigger check by login or permission lookup; the pre-agent
                    writes as the machine account (👀, stage → Agent, opt-in
                    label, loop counters); mints its own token; checks out
                    no PR code.
  -> agent/fix/review (untrusted)
                    checkout with persist-credentials: false; provisioning;
                    the agent; commits locally and never pushes; the read-only
                    job token, plus the Claude action's own token while its
                    step runs; emit-landing runs last, if: always(), with no
                    token.
  -> land  (trusted, fresh runner)
                    mints its own token; downloads the artifact; validates
                    the manifest; pushes the bundle; opens or adopts the PR;
                    posts; resolves; files issues; moves the stage; reports.
```

The gate exists because some writes must happen before the agent runs (the
acknowledgement, the stage move, the loop's attempt counter), and those are
safe only because they run before any untrusted content is checked out. The
land job exists because everything after the agent must not share a runner,
a process tree or a filesystem with anything the agent ran or left running.
The agent job has everything it needs to do the work and no credential of
the machine account; the trusted jobs have that credential and run nothing
that could take it.

The land job is not in the workflow's per-item concurrency group: ordering
inside a group is arbitrary and at most one job pends, so a landing that had
to wait behind the next run's gate could be cancelled and its bundle lost.
The residual overlap ends in the land composite's non-fast-forward refusal,
reported on the PR and never forced.

### 3.2 The manifest contract

The agent job's output is a directory, `${RUNNER_TEMP}/landing`, holding
`manifest.json`, the body files it names and, when commits exist,
`commits.bundle`. The `emit-landing` composite writes the manifest from
values the runner computed (`git rev-parse`, the run's start SHA, the
branch, the trusted PR or issue number from the event payload) merged with a
`manifest-extra` file the workflow composed, and uploads the directory as
one artifact with a one-day retention. Where an agent may write into the
manifest at all, it writes a separate `manifest-extra.json`, and a runner
step whitelists and normalizes the few keys it may set (the review loop's
`replies`, `resolve_threads`, `handback`, `handoff_body_file`; the dev
agent's `comments`, pinned to the run's own thread) before the merge. A
mistyped field is dropped with a warning rather than refused, because the
validator's refusal is all-or-nothing and would take the commits with it.

The manifest is the trust boundary for what the agent asked for. The land
job treats the artifact as data: `.github/scripts/validate_manifest.py`
(stdlib only, one line per violation, any violation refuses the whole
manifest) runs before anything in the artifact is acted on or read into a
shell variable. The composite's only earlier network calls look up trusted
values to validate against (the PR's live head ref and the repository's
default branch, by the caller's `pr-number` input, never by a number from
the manifest). A refused manifest causes none of the actions it requested;
the land job then posts one error report to the PR or issue the event
payload names, and fails the run. It requires:

- `schema` 1; `repo` and `run_id` equal to the land job's own, so an
  artifact from another run cannot be replayed; a manifest under 1 MiB.
- `branch` a bare name (`^[A-Za-z0-9._/-]{1,200}$`, no `..`, not `refs/…`),
  not the default branch, not on the land job's `refused-branches` list
  (`main` by default, so the inspect_ai fork's pristine `main` is refused
  even though it is not that repository's default branch), not `pr.base`;
  equal to the PR's live head ref when the run names a PR; starting with the
  land job's `branch-prefix` (an issue run's `claude/issue-N-`) when it does
  not.
- `pr_number` and `issue_number` equal to the numbers the caller passed from
  the event payload, so the agent job cannot steer a push, a reply or a
  hand-back at a thread of its choosing.
- `start_sha` and `head_sha` 40 hex, with `has_bundle` true exactly when they
  differ.
- Every `*_file` a plain relative name of a regular, non-symlinked file
  inside the artifact, under 64 KiB.
- `issues[].repo` on the land job's `allowed-issue-repos` list (empty for the
  three writing workflows; the caller repo for the reviewer; the inspect_ai
  fork for the triage workflow in the `actions` repo); thread ids
  `PRRT_…`; `stage` one of the Atlas options; `review_verdict` `clean` or
  `suggestions`; a `comments[]` entry flagged `review` only alongside a
  `review_verdict`.
- No unknown key at any level, so schema drift fails closed. A new field is
  added to the validator's `KNOWN_*` sets, to
  `.github/actions/emit-landing/README.md` and to
  `tests/test_validate_manifest.py` together.

Under the land job's `refuse-bundle` input (the reviewer, the triage
workflow) the validator additionally refuses any manifest that carries
commits, claims HEAD moved or ships a bundle, whatever the agent job
uploaded, and the push-side branch rules are off because nothing is pushed.
`emit-landing`'s `read-only` input is the producing side of the same pair:
no git runs, `head_sha` equals `start_sha`, no bundle.

Every agent-authored body the land job posts passes through a de-fang step
first (trigger tokens lose their `@`, loop markers are split,
case-insensitively, capped under the comment limit). Two bodies no agent
text reaches are posted verbatim: the hand-back, exactly `@review`, and the
reviewer's verdict comment, one of two fixed marker bodies chosen by
`review_verdict`. The `<!-- claude-review-comment -->` anchor is appended by
`land` itself after the de-fang.

### 3.3 Moving commits by bundle

The agent job's job token cannot push and its settings deny `git push`, so
its commits travel by bundle: `emit-landing` runs
`git bundle create` over the range above the run's start SHA when HEAD
descends from it and sits on the run's branch, and drops the bundle together
with `handback`, `stage`, `resolve_threads` and `handoff_body_file` when it
cannot (a rebased HEAD, a bundle failure), recording why in the manifest's
`error`, so a bare `@review` is never posted over lost work.

The land job materializes the bundle in an empty bare repository: fetch the
start SHA from origin by SHA with the read token, `git bundle verify`,
unbundle, assert the tip equals `head_sha` and descends from `start_sha`,
read the branch's live tip with `ls-remote` and refuse unless it is an
ancestor of `head_sha` ("moved during the run"), then push
`head_sha:refs/heads/<branch>` with the privileged token through the same
step-scoped credential helper every push uses. Never `--force`; the read and
the write are separate steps because a step has one `GIT_TOKEN`. Because the
pusher is the machine account, CI and the `@review` hand-back fire as they
would for a human's push; a `github.token` push triggers nothing, which is
the whole reason a machine identity exists.

Commits made runner-side in the agent job (the `sync-branch` base merge, the
codex commit) are authored by whatever the gate's mint step reported as the
identity (`meridian-marvin[bot]` when it minted), so author and pusher
agree.

### 3.4 The identity

The machine account is the GitHub App **`meridian-marvin`** (App ID 4969131,
bot login `meridian-marvin[bot]`), owned by `meridianlabs-ai` and installed
on the repositories whose workflows write as it. Its repository permissions
are contents, issues and pull requests write, and actions, checks,
Dependabot alerts and metadata read; its one organization permission is
projects write, for the Atlas board. Its client id and private key are the
org secrets `MARVIN_APP_CLIENT_ID` and `MARVIN_APP_PRIVATE_KEY`, granted
repository by repository. A second app installed only on the fork for the
workflows that ingest third-party content was considered and not wanted
(decision: Ransom, 2026-09-16).

Every trusted job mints its own token as its first step with
`actions/create-github-app-token@v2`: `app-id` (which takes the Client ID;
v2 has no `client-id` input), `private-key`, `owner`, `repositories` naming
the one repository the job writes to (`github.event.repository.name` in the
reusable workflows; `inspect_ai` in the Atlas sync and the `actions` repo's
workflows) and `permission-*` inputs for only what that job uses. The token
lives at most an hour and the action's post step revokes it at job end; the
land job runs for seconds. Every GitHub call in the job reads
`steps.mint.outputs.token`; composites take it as an input and never mint.
The mint step runs unconditionally wherever the job cannot work without the
machine account, so a caller without the secrets fails there, loudly, before
anything else runs (decision: Ransom, 2026-09-18). Of this repository's
workflows only `claude.yml`'s gate and land gate it on
`secrets.MARVIN_APP_CLIENT_ID != ''` and fall back to `github.token`: the
dev agent has a documented degradation (below), the reviewer and the loops
do not. The `actions` repository's two workflows and inspect_flow's gates
still mint conditionally (sections 4.2 and 4.3). What each job in this
repository mints:

| job | `repositories` | permissions |
| --- | --- | --- |
| `claude.yml` gate | caller repo | issues, pull requests, org projects: write |
| `claude.yml` land | caller repo | contents, issues, pull requests, org projects: write |
| `claude-review.yml` gate | caller repo | issues, org projects: write; pull requests: read |
| `claude-review.yml` land | caller repo | issues, pull requests, org projects: write (no contents: bundles are refused) |
| `claude-auto.yml` gate | caller repo | issues, pull requests, org projects: write; actions: read (the failed run's record, for the run-to-PR binding) |
| `claude-auto-review.yml` gate | caller repo | contents: read; issues, pull requests, org projects: write |
| `claude-auto.yml` / `claude-auto-review.yml` land | caller repo | contents, issues, pull requests, org projects: write; `claude-auto.yml` also actions: read (the binding's revalidation) |
| `atlas-sync.yml`, fork token | `inspect_ai` | issues, pull requests, org projects: write; actions: read |
| `atlas-sync.yml`, ts-mono token | `ts-mono` | metadata, pull requests: read |

No job writes to two repositories. The three writing workflows' land jobs
refuse follow-up issues outright (`allowed-issue-repos: ""`), the reviewer's
may file them in the caller repo only, and the one job that reads a second
repository, the Atlas sync's companion-PR check against ts-mono, does so
under a separate read-only token rather than a widened write token
(decision: Ransom, 2026-09-16).

**Two trusted logins.** Every trust decision in the four reusable workflows
reads one workflow-level `env` value, `TRUSTED_LOGINS:
i-am-marvin,meridian-marvin[bot]`; `atlas_sync.py` and the checkout and
promote skills carry the same pair as one constant each. The User
`i-am-marvin` is the account whose personal access token the app replaced;
the reusable workflows stopped accepting that token on 2026-09-18, revoking
it in the account and deleting the `MARVIN_TOKEN` org secret are the
admin's steps, and the login stays trusted until the account itself is
retired, a separate step. The bot is trusted by login and
never by lookup: the collaborators endpoint reports `none` for an App, its
comments carry no MEMBER or COLLABORATOR association, and so the caller
stubs' `[bot]` exclusions name it as the one exception and the
claude-code-action and codex-action steps carry it in their bot
allow-lists. App-token pushes and comments trigger workflows as a User's do
(only `github.token` events are suppressed), so the `@review` hand-back and
the CI re-run work unchanged.

**No Workflows permission** (decision: Ransom, 2026-09-17). A fine-grained
PAT pushes workflow files under its Contents permission; an App needs the
separate Workflows permission, and the app does not have it. A CI agent's
commit that touches `.github/workflows/` therefore fails at the land job's
push with GitHub's "refusing to allow a GitHub App to create or update
workflow" error. That is the intended boundary: the workflows that hold the
credentials are changed from a maintainer's machine, under a maintainer's
review, never by an agent running in CI. Since 2026-09-18 the land job
enforces it before the push: its `workflows` step lists the paths the
bundle changes under `.github/workflows/` and refuses the bundle with a
one-line report naming the files the agent itself changed — a change the
runner's base merge brought in (the file's content at the bundle's tip
equals the base branch's on origin, or that of the base as last merged into
the branch: the merge base of the tip and origin's base) is not the agent's
and passes (decision: Ransom, 2026-09-18) — and the agent prompts say up
front not to edit them.

The app is not a member of `UKGovernmentBEIS`, so, like the PAT before it, it
cannot open or push to upstream pull requests; promotion to upstream is a
human step.

### 3.5 What the agent job still holds

- **The job token, read-only.** In the four reusable workflows every agent
  job's `permissions:` block is
  `contents: read`, `pull-requests: read`, `issues: read`, `actions: read`
  and `id-token: write`. The checkout runs on it with
  `persist-credentials: false`; the three writing workflows assert right
  after that `http.<server>/.extraheader` is empty
  (`assert-no-persisted-credential`). The `sync-branch` base merge fetches
  with it through a step-scoped helper and never pushes.
- **The Claude GitHub App's own installation token.** No workflow passes a
  `github_token` to `claude-code-action`, so the action exchanges the job's
  OIDC token for its own installation token of the Claude app, requesting
  contents, pull requests and issues write on the caller repository (its
  `src/github/token.ts`), and revokes it when the step ends. The job's
  `permissions:` block does not scope this token: it can push and post
  whatever the job token may not. The action rewrites `remote.origin.url`
  to carry it for the step's duration; the `reset-origin-url` composite puts
  the credential-free URL back immediately after the step, so it sits in
  `.git/config` no longer than the agent runs, and during that time the
  agent's own `Read` tool sees it (the sandbox mask of section 7 covers
  sandboxed commands, and same-repo runs have no sandbox at all). The
  composed settings deny the agent's push and posting commands:
  `Bash(git push:*)` and the action's `scripts/git-push.sh` wrapper, the
  `gh` comment, review, create and merge verbs and the reviewer's
  inline-comment tool are denied at runtime whatever the caller's `settings`
  say. Those denies are guard rails, not a complete prohibition on direct
  writes: `Bash(gh:*)` stays allowed (the log and PR reads need it), `gh
  api` is not denied, `gh` runs outside the sandbox on the sandboxed paths,
  and the token behind it can write, repository refs included. The
  load-bearing property is that nothing the agent can reach is the machine
  account, so a write that slipped past would be `claude[bot]`'s and
  attributable as such. That identity is not inert to the loops. The
  review-fix workflow still accepts it as a verdict author: `reviewer_login`
  defaults to `claude[bot]`, `REVIEWER_LOGINS` names it next to the machine
  account's two logins, and the fix action's `allowed_bots` includes it, so
  a marked verdict comment posted directly with this token can drive a fix
  round or a convergence on an otherwise eligible same-repo `auto` PR
  without going through a landing. Re-review requests follow the configured
  allow-lists: `review_allowed_bots` defaults to `claude[bot]` in the
  review-fix gate, and the reviewer itself admits a bot's `@review` when the
  caller's `allowed_bots` names it, as the inspect_ai fork's stub does. The
  identity used to start a CI-fix round indirectly: a branch update on an
  open same-repo `auto` PR whose CI then fails reaches `claude-auto.yml`
  through `workflow_run`, and until 2026-09-22 the gate checked the PR and
  the label, not who pushed, while the codex step named `claude` in
  `allow-bot-users` for that case. The gate's bind step now refuses a run
  whose actor is any bot but the machine account, on both engines, before
  any write (finding 4628657), so a Claude-App push ends the automatic loop
  for that branch until the machine account or a write-access human pushes;
  the codex step's `claude` entry is unreachable for the run actor and is
  kept only for the step's own workflow-actor check. The dev agent and the
  CI-fix loop ask that token for `actions: read` in addition
  (`additional_permissions`) so `gh run view --log-failed` works.
- **The model credential.** Claude authenticates through Workload Identity
  Federation: the job's OIDC token is exchanged for a short-lived Anthropic
  credential under a rule that matches `repository_owner ==
  "meridianlabs-ai"`; there is no API key. The codex engine has no such
  exchange, so `OPENAI_API_KEY` is the one secret an agent job names,
  consumed only on items labelled `engine:codex`, and codex itself runs as
  an unprivileged `codex` user with no GitHub credential at all.

The agent commits and stops (decision: Ransom, 2026-09-09: every comment the
loop produces is posted by the land job as the machine account after the
push lands, so one identity signs everything and nothing is posted before
the commits it refers to are on the branch). It does not push, open PRs,
comment, reply or resolve threads; where it has something to say it writes a
body file under the landing directory and names it in `manifest-extra.json`.

## 4. The workflows under the pattern

### 4.1 This repository's reusable workflows

**`claude.yml`, the dev agent** (`gate` → `agent` → `land`). The gate's
trigger check matches the phrase or label from the event's own text, then
authorizes the actor: a `TRUSTED_LOGINS` login without a lookup, anyone else
by `repos/{repo}/collaborators/{actor}/permission` on the job token, admin,
maintain or write, fail-closed with one retry. It then refuses fork heads
before any checkout (`isCrossRepository` looked up on comment events, the
payload's head repo compared on review events; a lookup that never answers
refuses too), and only then acknowledges, moves the stage to Agent, applies
the `@auto` opt-in label and resets the loop counters, as the machine
account. The agent job checks out, merges the base branch on the runner,
provisions, composes the prompt and the deny list, runs the action (or codex),
resets the origin URL, and composes the manifest: on an issue run whose HEAD
moved, a `pr.open` with the tip commit's subject as the title, `Fixes #N`, the
gate's label read, and `handback` when the labels carry `auto`; on a PR run,
the base merge and any commits, `handback` when the run is autonomous. The
agent's one manifest key is `comments`, pinned to the run's thread. Nothing is
bundled unless HEAD sits on the run's branch. The land job runs the `land`
composite with `steps.mint.outputs.token || github.token`: on a caller
without the app secrets the push and the PR come from `github-actions[bot]`
and trigger nothing, which is why this land job's token holds
`contents: write` and no other does.

**`claude-review.yml`, the reviewer** (`gate` → `review` → `land`). Every
comment-triggered review needs a trusted commenter whatever the head
repository: a `TRUSTED_LOGINS` login, a bot the caller's `allowed_bots` names
(same-repo heads only; `github-actions[bot]` never), or an account with
write access by lookup, fail-closed. A fork head admitted that way, and every
external-mode review of an outside contributor's PR, takes the sandboxed
path: `persist-credentials: false`, no runner-side provisioning, `.claude/`
and `.mcp.json` deleted and `CLAUDE.md` moved aside so it loads as untrusted
text, the bubblewrap sandbox with the settings overlay, and the Claude engine
even under an `engine:codex` label. The review job's token is
`pull-requests: read`: the reviewer writes `summary.md`, `verdict.txt` and
an optional `inline.json` under `$RUNNER_TEMP/review`, a directory outside
the workspace that is on the sandbox's `denyWrite` list so a contributor's
build hook cannot forge a verdict, and a runner step turns them into
`comments[]` flagged `review`, `review_comments[]` and `review_verdict`
(decision: Ransom, 2026-09-17: option B, review and land jobs both read-only
on the job token, the review posted by the machine account only). The review
job's `emit-landing` runs `read-only` and the land job runs `land` with
`refuse-bundle`, so this workflow can never be turned into a push channel by
a forged manifest. That is the enforced limit, and it is narrower than
"review text only": the review job is not confined to writing those three
files (it runs tests and Python, unsandboxed on same-repo heads, and its
`gh` runs outside the sandbox on the sandboxed paths), and under
`refuse-bundle` the validator still accepts `comments[]` on any thread of
the caller repository, `issues[]` in the caller repository (create,
comment, reopen, assign), a `stage` move and a `pr.open` for a branch that
already exists on origin. A compromised review job can therefore have the
machine account post those, de-fanged, on the caller repository; it cannot
push through its landing job, and its manifest cannot reach another
repository. The Claude action's own installation token in the review job is
the separate, write-capable channel that sections 3.5 and 7 describe. The
`denyWrite` entry on the review directory holds on the sandboxed paths
only.

**`claude-auto.yml`, the CI-fix loop** (`gate` → `fix` → `land`). The gate
first binds the failed run to its PR (`bind-ci-run`, design/auto-agent.md →
Binding the failed run to its PR): the run is the workflow_run event's own,
read back from the API as this repository's failed `pull_request` run on the
named head branch, whose actors (who started it, who re-ran it) are the
machine account or write-access accounts — the model actions' own actor
rule, decided here before any write instead of after the counter and the
base merge — and it binds to the one same-repo PR from that branch open
when the run was created, at the run's head SHA, whose timeline shows no
retarget since — which must be the caller's `pr_number`; none, several or a
disagreement skips. The gate then requires that PR open, same-repo and
on the named head branch, verifies who applied the `auto` label
(`verify-auto-labeler`: the machine account under either login or a
write-access account, from the PR timeline; a GitHub App labeler or a failed
lookup refuses without disarming; positive evidence of an outsider removes
the label), and reads the attempt counter only from a marker comment by a
`TRUSTED_LOGINS` login, else by a write-access account, parsed strictly.
`Record attempt` is the gate's write. The fix job holds the read-only job
token and `actions: read` for the failed run's logs; its manifest carries the
fix commits with `handback: true` and no stage, or a relay of the agent's
final message when it committed nothing; its base sync merges only the base
branch and tip the gate established (`sync-branch`'s `base` and `base-sha`
inputs) and fails on a PR retargeted or a base moved since; a Claude step
that failed — for any reason; the action's `execution_file` output is not
launch evidence, since its error handler publishes a pre-existing
default-path file the PR's provisioning step could create — packages
nothing, the runner's base merge and any commit of the run included, as the
codex failure path does. The land job derives the run's context again
(`bind-ci-run` with `revalidate`) and pushes only when the gate's PR, head
SHA, base ref and run attempt are reproduced, posts exactly
`@review`, and refunds the attempt when the agent did not run and nothing was
pushed, PATCHing only the loop's own counter comment.

**`claude-auto-review.yml`, the review-fix loop** (`gate` → `fix` → `land`).
The gate recovers the loop's state from PR comments and believes each read
only from its rightful author: the verdict from `REVIEWER_LOGINS`, the round
counter and the hand-off marker from `TRUSTED_LOGINS`, a re-review request
from a trusted login, an allow-listed bot or a write-access account, each by
a cached fail-closed lookup; `rounds:` and `auto-review-head:` parse
strictly or count as absent. This is the one workflow whose agent writes into
the manifest: `replies`, `resolve_threads`, `handback` and
`handoff_body_file`, whitelisted and normalized by a runner step, with the
ending contract (exactly one of hand-back or hand-off after a commit)
enforced by that step and a violation posted as an error while the commits
still land. `land` intersects `resolve_threads` with the PR's own threads
before resolving.

**`atlas-sync.yml`, the hourly board sync.** One trusted job, no agent. It
mints a fork-scoped write token and a ts-mono read-only token, and
`atlas_sync.py` counts a hand-back, a `Companion PR:` line or a companion
approval only from a trusted author or a write-access account, so an
outsider's `@review` is never re-issued as the machine account.

**This repository's own stubs** (`claude-stub.yml`, `claude-review-stub.yml`,
`claude-auto-stub.yml`) are the dogfood copies of `examples/` and follow the
caller contract below.

### 4.2 The `actions` repository

**`inspect-ai-ci-perf.yml`** (`analyze` → `publish`, two jobs: there is no
gate, the trigger conditions live on the jobs). The analysis job
checks out upstream `inspect_ai`, collects CI timings and runs the agent
under a read-only job token and a dedicated Anthropic key from a capped
Console workspace (an API key by necessity; the cap bounds a leak), behind a
`harden-runner` egress block with sudo and containers disabled, and uploads
its evidence as an artifact after a step refuses to publish any file that
contains the key or its base64 form. The publish job runs on a fresh runner
only on the schedule or a non-dry-run dispatch of `main`, checks out upstream
at the exact SHA the analysis used (upstream `main` is inside the org's trust
domain: Meridian maintains it, and it is not pinned to a reviewed SHA;
decision 2026-09-08), verifies the collected files against the sha256 values
recorded before the agent ran, validates `findings.json` with the publisher's
own validator (this workflow's manifest), and writes fork issues and Atlas
cards. Its mint step is the job's first step, before the checkout and the
validation, not after them, and it is conditional on the app secrets being
configured (fork-scoped, issues and org projects write); the credential
exists while the evidence is validated, on a runner the agent never had. The `workflow_dispatch` ref stays unrestricted (decision 2026-09-08):
publication requires `inspect_ai_ref == 'main'`, which is what makes an
arbitrary-ref dispatch harmless.

**`triage-test-failures.yml`** (`agent` → `land`, two jobs, the trigger
condition on the agent job). The agent job downloads a
failed scheduled run's logs and its `triage-context` artifact, validates the
artifact's fields (SHA shape, Slack id shapes) before using any, checks out
upstream at the tested commit and runs the agent with reads plus file writes
under the landing directory; it holds the job token and the Anthropic key,
no Slack secret, and passes `github_token: github.token` to the action so no
App token with write is minted. Its `emit-landing` is `read-only`. The land
job mints a fork-scoped token (issues and org projects write) and runs `land`
with `refuse-bundle`, `allowed-issue-repos: meridianlabs-ai/inspect_ai` and
the Slack destination from the agent job's validated outputs, never from the
manifest, which supplies the text alone.

Both trusted jobs in this repository still read `steps.mint.outputs.token ||
secrets.MARVIN_TOKEN` and gate the mint on the app secrets being configured:
the transition fallback the reusable workflows dropped on 2026-09-18 is still
in the `actions` repository's two workflows (their headers say so), so the
retired secret is named there until a follow-up removes it. Nothing reaches
the fallback while the app secrets are granted to the repo.

**`release-please-vscode.yml`** (`build` → `publish`) applies the same split
without an agent: `build` runs with no secrets and no environment and uploads
the `.vsix`; `publish`, gated by an `environment` with required reviewers,
checks out nothing, installs the two publisher CLIs with `--ignore-scripts`
in a step that holds no secret, and publishes the downloaded package with
the marketplace tokens, so no consuming-repo script runs next to them. Those
tokens (`VSCE_PAT`, `OVSX_PAT`) are long-lived secrets by necessity, held as
environment secrets behind required reviewers rather than as repository
secrets; this workflow is where I3 does not apply.

The `actions` repository's own dev, reviewer and loop stubs follow the caller
contract below, with the reviewer stub admitting only OWNER, MEMBER,
COLLABORATOR or Bot commenters as a cost filter ahead of the reusable
workflow's authoritative check.

### 4.3 Other Meridian repositories

`inspect_flow`'s `inspect-update.yml` and `inspect-ai-main-failure.yml` and
`ts-mono`'s `dependabot-fix.yml` are gate/agent/land workflows of their own,
built on this repository's `emit-landing` and `land` composites: each gate
and land job mints a token scoped to its own repository (the inspect_flow
gates conditionally: `inspect-update` after its decide step,
`inspect-ai-main-failure` when the app secret is present), the agent jobs
hold the job token only and pass `github_token: github.token` to the
action so no App token is minted there, and the `dependabot-fix` gate reads
the Dependabot alerts under a `permission-vulnerability-alerts: read` token
so the agent job never holds one. `inspect-ai-main-failure` adds a fourth,
trusted `close-on-green` job.

## 5. The caller contract

A caller repository enables an agent by copying a stub from `examples/` into
`.github/workflows/`. The stub:

- Passes the machine account's two org secrets, `MARVIN_APP_CLIENT_ID` and
  `MARVIN_APP_PRIVATE_KEY`, and optionally `OPENAI_API_KEY`, as explicit
  one-key entries. Never `secrets: inherit`: the reusable workflow is pinned
  to a mutable `@main` and consumes only what is named. The org admin grants
  both secrets to the repository and adds it to the app's installation; a
  repo without them runs the dev agent with the `github-actions[bot]`
  degradation and nothing else.
- Grants the calling job what the reusable workflow's jobs need at most, and
  no job inside takes more than it uses. The dev-agent and loop stubs grant
  `contents`, `pull-requests` and `issues` write, `id-token` write and
  `actions` read; inside, the agent job takes read on all three plus
  `id-token: write` and `actions: read`, and only `claude.yml`'s land job
  takes `contents: write`, for the job-token fallback push. The reviewer stub
  grants `contents`, `pull-requests` and `issues` read, `id-token` write and
  `actions` read: the review is posted by the land job as the machine
  account, so the caller's grant bounds the review job's job token at read
  and no regression in the reusable workflow could hand that token write
  again. The ceiling is on the job token only; the Claude action's own
  installation token is not bounded by it.
- Grants `id-token: write` at the calling job level, because GitHub does not
  pass OIDC tokens to reusable workflows implicitly.
- Filters comment events on `author_association` (OWNER, MEMBER,
  COLLABORATOR, or a Bot commenter, whom the reusable's `allowed_bots`
  decides) as a cost filter, and names `meridian-marvin[bot]` as the one bot
  admitted on the `auto` label path. Neither is the authorization: the
  reusable workflow's trigger check is.

## 6. Verifying that a change keeps the invariants

Run before every change to a workflow, composite or stub, and read the
results against the invariant each one tests.

- `python3 -m pytest` from the repository root. `test_app_token_minting.py`
  checks the workflow text structurally (I1, I3): every gate and land job
  and the Atlas sync mint first, for exactly the repository and permissions
  in the table above, unconditionally except in `claude.yml`; every token
  read in those jobs is `steps.mint.outputs.token` (`|| github.token` in
  `claude.yml` alone); the agent job names none of it; the retired PAT is
  named by no tracked file outside `design/`; the stubs and examples pass the
  two app secrets explicitly and nothing else. `test_validate_manifest.py`
  covers one failing case per validator rule and `test_land_helpers.py` the
  bundle, unbundle, moved-branch refusal and non-force push (I2). The trig
  and gate tests (`test_dev_agent_trig.py`, `test_review_trig.py`,
  `test_ci_fix_gate.py`, `test_review_fix_gate.py`, `test_atlas_sync.py`)
  run the lifted `run:` scripts against a stub `gh` and check that every
  trust decision is by login or permission, fail-closed (I4).
- `grep -n 'secrets\.' .github/workflows/<file>` must hit only the
  `workflow_call` declarations, the `gate` job, the `land` job and the codex
  step's `OPENAI_API_KEY` (I1, I2). `grep -rn 'secrets\.'
  .github/actions/emit-landing` stays empty.
- `grep -n 'persist-credentials' .github/workflows/<file>` shows `false` on
  every checkout (I5); `grep -n '${{ inputs\.' <file>` inside `run:` blocks
  returns nothing (untrusted input is never parsed as shell).
- `actionlint` over `.github/workflows/`.
- On a live caller after a change to the identity or the landing: the land
  job's log shows the mint step with explicit `repositories` and
  `permission-*` inputs and the branch's pusher is `meridian-marvin[bot]`
  (I3); a canary same-repo PR whose `claude-setup` action and `pyproject.toml`
  build hook dump `env` and `.git/config` to a file the agent then reads
  shows no `ghs_`, `ghp_`, `github_pat_`, `sk-ant-` or `xoxb-` prefix and no
  `AUTHORIZATION` header (I1); a fork PR carrying a `SessionStart` hook in
  `.claude/settings.json` leaves no marker file after a maintainer's
  `@review`, and a no-role account's `@review`, `@auto` or forged marker
  comment produces no run past the gate (I4); the run's only artifact is the
  landing directory (I6).

## 7. Costs and residual risks

- **The Claude action's installation token in the review sandbox.**
  `persist-credentials: false` keeps checkout's token out of `.git/config`;
  the action's prepare step then writes its own installation token into the
  origin URL for the duration of the agent step, and sandboxed contributor
  code (a build backend, a collected test) can read that file. On the
  sandboxed paths the settings overlay masks `.git/config` for sandboxed
  commands (`credentials.files` in `mask` mode with an `extract` regex on
  the token and `onExtractNoMatch: deny`; Claude Code 2.1.221 or later,
  checked after the step), denies `~/.config/gh` and `~/.gitconfig`, and
  deletes `network.tlsTerminate` and `credentials.allowPlaintextInject` from
  the merged settings so the proxy can never inject the real token into a
  request to a PyPI host. The mask defends against contributor code, not a
  prompt-injected agent: the agent's own `Read` tool is not sandboxed and
  sees the real file, and its `gh` runs outside the sandbox with that token.
  The proper fix is an action-side git-auth option independent of
  `allowed_non_write_users`, requested as
  [anthropics/claude-code-action#1818](https://github.com/anthropics/claude-code-action/issues/1818).
- **The dev agent's job-token degradation.** A caller without the app
  secrets still runs `claude.yml`: the land job pushes and opens the PR as
  `github-actions[bot]`, which triggers no CI and no review, and fails where
  Actions-created PRs are disallowed. This is the one place a land job's job
  token holds `contents: write`, and the one place the mint step is
  conditional. The reviewer and the loops have no such path: they fail at the
  mint step.
- **A hijacked reviewer cannot push through its landing job, and cannot act
  as the machine account from its own job.** Its job token is read-only and
  the land job refuses bundles. Its normal output is the three review files, landed as the
  review summary, the inline findings and a verdict that is one of two
  fixed bodies, after the de-fang. The enforced limits stop there: the
  review job runs tests and Python (unsandboxed on same-repo heads), its
  workspace stays writable on the sandboxed paths and `gh` runs outside
  the sandbox there, and the landing manifest it uploads is data the
  validator checks for shape, not intent. Under `refuse-bundle` the
  validator accepts `comments[]` on any thread of the caller repository,
  `issues[]` in the caller repository (create, comment, reopen, assign), a
  `stage` move and a `pr.open` for a branch that already exists on origin,
  all of which the land job would post as the machine account. So what a
  steered reviewer can cause through the landing is a wrong review and
  manifest-authorized writes on the caller repository, never a
  machine-account push and never a machine-account write outside it; a
  human reads every review, and the loops believe a verdict only from the
  reviewer identity. Outside the landing, the Claude action's own
  installation token stays write-capable on the caller repository for the
  duration of the review step, and `gh api` through it is not denied
  (section 3.5): the configured command denies are guard rails rather than
  a complete prohibition on direct writes, and a write made that way is
  `claude[bot]`'s, not the machine account's.
- **One push per run, at the end.** Interactive users who relied on the dev
  agent pushing mid-run to watch CI lose that; iterating on CI is the `@auto`
  loop's job (accepted: Ransom, 2026-09-11, with the dev-agent conversion).
- **More jobs, more runner minutes.** A gate and a land job cost a minute or
  two on fresh runners, against agent runtimes of tens of minutes.
- **Two-job failure modes.** The land job runs under `always()` after the
  gate succeeded and decides from the manifest what to land: a Claude step
  that ended in failure after committing still lands those commits with a
  ⚠️ error posted alongside; a cancelled agent job lands nothing. On the
  codex path nothing is bundled unless the unresolved-merge guard
  succeeded, which withholds the push on a failed prep, user setup, codex
  step or reclaim; after a successful guard, a later failure (the
  commit-summary step, for instance) still lands the commits codex had
  already made, with the error posted alongside, so a red codex run does
  not mean an untouched branch. A provisioning
  failure over a stale branch lands the runner's clean base merge and burns
  one loop attempt where it used to burn none; accepted, bounded to one.
- **The validator is security-critical code.** It is one small stdlib
  script shared by every land job, with one failing test per rule, and it
  fails closed on any key it does not know.
- **CI agents cannot change workflow files.** The app's missing Workflows
  permission fails such a push at the land job with GitHub's remote error;
  since 2026-09-18 the land job's `workflows` step refuses the bundle
  before the push with a plain report line, and the agent prompts say not
  to edit them (section 3.4). A run that needs a workflow change still
  spends itself before the refusal is posted.
