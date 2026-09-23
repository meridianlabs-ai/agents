# Agent jobs get read-only Actions cache access

Status: proposed, 2026-09-23. Issue: none (Claude Security finding 4629157,
MEDIUM, privilege escalation, on the Security board). Author: agent (Claude),
reviewed by Codex; see the PR.

## Why

Every agent job runs in the caller repository's Actions cache. Whatever
such a job saves lands in the scope of the run's ref. For `issue_comment`,
`issues`, `workflow_run` and `schedule` runs that ref is the caller's
default branch, and every branch, pull request and workflow of the
repository can restore from that scope. For `pull_request_review` and
`pull_request_review_comment` runs the ref is the PR's `refs/pull/N/merge`,
and every later run of that PR can restore from it. The finding names two
ways a steered agent can write into those scopes:

- **(a) Through the caller's provisioning.** The workflows run the caller's
  `./.github/actions/claude-setup` in the agent job, before the agent
  (`claude.yml:996-1000`, `claude-auto.yml:1026-1028`,
  `claude-auto-review.yml:1179-1181`, `claude-review.yml:860-865` for
  same-repo heads). The README asks callers to build it by delegating to
  their CI setup "so ... the build cache is shared" (`README.md:79-87`). A
  cache action nested in it (`actions/cache`, `astral-sh/setup-uv`,
  `actions/setup-node` with `cache:`) saves in a post step at job end,
  after the agent ran as `runner` with write access to the cached
  directories. `openai/codex-action@v1` has the same shape of its own. It
  runs `actions/setup-node@v6.3.0` (pinned at `53b8394`), which caches the
  npm store automatically whenever the caller's root `package.json` names
  npm as its package manager (`package-manager-cache`, default `true`; post
  step `dist/cache-save/index.js`).
- **(b) Directly.** The agent runs as the `runner` user in a job holding
  the Actions runtime token, which is the credential the cache service
  accepts. It can create an entry for any key it reads out of the caller's
  CI workflow files.

A trusted workflow that later restores such an entry executes the agent's
content with its own secrets and token. No commit, PR or attribution is
involved. The entry lives until seven days pass without anyone restoring
it. [SECURITY.md](../SECURITY.md) and the threat model in
[credential-separation.md](credential-separation.md) never mention the
cache.

The finding was written against the platform as the scanner assumed it to
be. The platform has since changed in two ways, which this design builds
on. Both were verified in GitHub's documentation (github/docs at
`03d2e24`, 2026-09-23) and in runner logs:

1. **Low-trust triggers get a read-only token for the default-branch
   scope** (changelog 2026-06-26, "Read-only Actions cache for untrusted
   triggers"; docs `actions/reference/workflows-and-actions/dependency-caching.md`
   → "Cache access for low-trust workflow triggers"). Only `push`,
   `workflow_dispatch`, `repository_dispatch`, `delete`,
   `registry_package`, `page_build` and `schedule` may write that scope.
   The docs name `issue_comment`, `workflow_run` and `pull_request_target`
   among the read-only triggers.
2. **A `cache-mode` key** at workflow or job level, with values `read`,
   `write`, `write-only` and `none` (changelog 2026-09-10; docs
   `workflow-syntax.md` → `cache-mode` and `jobs.<job_id>.cache-mode`;
   feature flag `data/features/actions-cache-mode.yml`: `fpt: '*'`). The
   docs describe enforcement as "with scoped cache tokens, so a job cannot
   restore or save caches beyond the mode it is granted".

The implicit default does not close the finding, for three reasons.

- **It is not what our callers' runs get in every case.** The runner prints
  the effective mode in "Set up job" (`actions/runner`
  `src/Runner.Worker/JobExtension.cs:176-180` at `cab9d1c`). On real runs:
  - Three `issue_comment` runs of the review-fix gate logged
    `Cache mode: read`: inspect_flow 35149216964, inspect_ai 35718674015
    and ts-mono 35788684827.
  - A PR-originated `workflow_run` of the CI-fix gate logged
    `Cache mode: read` (inspect_harbor 35803128385).
  - inspect_flow's `triage-agent` job logged `Cache mode: write` (run
    35655501245, 2026-09-21). That job is a caller-owned agent job in
    `inspect-ai-main-failure.yml`, and its run was a `workflow_run` of the
    scheduled "Inspect AI Main CI". Its claude-setup setup-uv post step
    ran after `claude-code-action`. It hit the exact key, so nothing was
    saved that time.
  - So a `workflow_run` whose upstream run was trusted gets write access,
    whatever the docs' list suggests. `schedule` and `workflow_dispatch`
    are trusted triggers and always get write.
- **PR-scope writes are unaffected.** A run in a non-default scope keeps
  read-write access (docs: "The `pull_request` event is not affected"). The
  dev agent's `pull_request_review` and `pull_request_review_comment`
  triggers (`examples/claude-stub.yml`) save into `refs/pull/N/merge`,
  which that PR's own CI restores.
- **It can be switched off by the workflow itself.** When the calling job
  sets no `cache-mode`, a called workflow may declare `write` and get it
  on a low-trust trigger (docs `how-tos/reuse-automations/reuse-workflows.md`
  → "Controlling cache access in reusable workflows"). Nothing in this
  repository stops a future edit from doing that.

## Goals and non-goals

Goals:

- No agent job of the four reusable workflows can save to the Actions
  cache, on any trigger or engine, in any scope. This covers the dev
  agent (`claude.yml`), both loops (`claude-auto.yml`,
  `claude-auto-review.yml`), the reviewer (`claude-review.yml`, same-repo
  heads and all other paths), and the codex jobs #136 adds. The platform
  enforces it on the job's token. It does not depend on what the caller's
  claude-setup contains or on what the agent can read.
- A test fails when any of those workflows grants itself cache write.
- The caller contract in the README and the security statements say what
  agents may do with the cache.
- A canary proves, once, that the key does what the docs say for a called
  workflow on a trusted trigger. That is the one case the implicit default
  does not cover.
- Name the caller-owned agent jobs that need the same line, with the
  evidence for each.

Non-goals:

- Withholding the runtime or OIDC request token from the agent process;
  see Design → "Criterion 2".
- Making release builds independent of caches (Ransom, 2026-09-21). See
  "Interaction with the release-build decision".
- Running the Claude engine as an unprivileged user, or any other change
  to the agent's own privileges. The Claude job stays as
  [credential-separation.md](credential-separation.md) §3.5 describes it.
- Changing callers' claude-setup composites. They keep working unchanged
  (see Compatibility).

## Current behaviour

Verified against `main` at `1e3a949` unless noted.

- **None of the four reusable workflows sets `cache-mode`.** Their
  top-level keys are `name`, `on`, `env` and `jobs` (`claude.yml:1,41,275,278`,
  `claude-auto.yml:1,60,231,234`, `claude-auto-review.yml:1,52,229,242`,
  `claude-review.yml:1,41,262,265`). No job sets it either. No caller repo
  surveyed sets it on any ref (`git grep cache-mode` on inspect_flow,
  inspect_harbor, inspect_sandboxes, inspect_scout, inspect_swe, ts-mono and
  actions `origin/main`, and inspect_ai `meridian`: no hits).
- **No reusable workflow or shared composite uses a cache action.**
  `provision-fallback` installs uv by script and saves nothing
  (`.github/actions/provision-fallback/action.yml`; its tiktoken warm writes
  `/tmp`, not the Actions cache). Among the nested third-party actions,
  `claude-code-action` disables setup-bun's cache (`no-cache: true` in its
  `action.yml`). The one that caches is codex-action's setup-node (above).
- **Agent jobs hold `id-token: write`** (`claude.yml:879`,
  `claude-auto.yml:868`, `claude-auto-review.yml:1047`,
  `claude-review.yml:664`), for the Workload Identity Federation exchange and
  the Claude App token. #136 keeps it on the codex jobs it adds.
- **What the runner puts in an agent's environment** (`actions/runner` at
  `cab9d1c`):
  - A `run:` step, including one inside a composite action, gets
    `ACTIONS_ID_TOKEN_REQUEST_URL`/`_TOKEN` when the job may mint OIDC
    tokens. It never gets `ACTIONS_RUNTIME_TOKEN`, `ACTIONS_CACHE_URL` or
    `ACTIONS_RESULTS_URL`. The code is `Handlers/ScriptHandler.cs:314-319`,
    and `CompositeActionHandler.cs` / `HandlerFactory.cs` send composite
    `run:` steps there.
  - A node action gets all of them, plus `ACTIONS_CACHE_MODE`
    (`NodeScriptActionHandler.cs:55-91`).
  - The request token and the runtime token are the same value; every
    handler assigns both from the job's system connection access token.
  - The handler sets these values after the step's `env:` is applied, so a
    workflow cannot blank them for a step.
- **The Claude CLI's own environment lacks the OIDC request variables.**
  `claude-code-action` deletes them before it spawns the session
  (`base-action/src/parse-sdk-options.ts:287-290`, since
  anthropics/claude-code-action#1011, 2026-04-04; the floating `@v1` tag
  was at `cfc3eb2` on 2026-09-19). The action's own process runs as the
  same user and keeps them for its background OIDC refresh.
- **The reviewer's sandbox overlay denies `ACTIONS_ID_TOKEN_REQUEST_TOKEN`
  and `ACTIONS_RUNTIME_TOKEN` to sandboxed commands**
  (`claude-review.yml:1150-1155`). That applies only on the sandboxed
  paths (external mode and fork heads), and only to Bash under the sandbox.
- **The cache service** authenticates with the runtime token and reads the
  scope and permission from the token itself, not from anything the client
  sends:
  - The v2 `CreateCacheEntry` request carries only `{key, version}`
    (actions/toolkit `packages/cache/src/cache.ts:694-697` at `193fa46`).
  - `GITHUB_REF` appears in the client only in a log line
    (`internal/cacheHttpClient.ts:141`).
  - The token carries its scopes as `CacheScope {scope, permission}`
    (`generated/results/entities/v1/cachescope.ts:17-29`).
  - A read-only token's refusal is reported as `cache write denied:`
    (`cache.ts:53-58`).
  - `@actions/cache` 6.2.0 and later also read `ACTIONS_CACHE_MODE` and
    skip a save the mode forbids, with an informational message
    (`internal/config.ts:24-39`).
- **Post steps of actions nested in a composite run at job end**, after
  every main step, in reverse order (`ExecutionContext.cs:335-369`,
  `CompositeActionHandler.cs:46-62`, `StepsRunner.cs:71`). So a claude-setup
  save always follows the agent.

## Design

### The change

Each of the four reusable workflows gets one top-level key, placed between
`on:` and `env:`:

```yaml
# Agents never write the Actions cache (Claude Security 4629157;
# design/agent-cache-scope.md). The token every job of this workflow
# gets can restore caches but not save them, in any scope, on any
# trigger: the platform enforces it, so it holds whatever the caller's
# claude-setup nests and whatever the agent runs. Never raise it; a
# caller that caps its calling job at `read` or wider keeps working.
cache-mode: read
```

The key goes at workflow level, not on each agent job:

- It covers every job the file has or will have, including the codex jobs
  #136 adds and any job a later PR splits out. None of them needs a
  per-job line that could be forgotten.
- The gate and land jobs restore nothing and save nothing. `read` costs
  them nothing, and they hold no runner-side agent to protect against.
- A single line at column 0 is easy to check with a text test.

The value is `read`, not `none`:

- Restores keep working, so the caller's cache-backed claude-setup still
  hits the entries its trusted CI saves. Provisioning time does not change.
- Reading adds no exposure. Any PR author can already read default-branch
  caches through a PR's CI (docs → "Best practices": "Anyone who can open
  a pull request against your repository can read the contents of caches
  in the base branch").

### What each route becomes

| Route | Today | After |
| --- | --- | --- |
| (a) claude-setup or codex-action post-step save | Refused by the platform on `issue_comment`, `issues` and PR-originated `workflow_run` in default scope. Allowed in PR scope on review events, and on any trusted-origin run. | The save is skipped (clients on `@actions/cache` ≥ 6.2.0 read `ACTIONS_CACHE_MODE`) or refused by the service (older clients; a warning, and the step and job continue). |
| (b) direct write with the job's token | Same as (a). | Refused: the token has no write permission in any scope. |

### Criterion 2 of the finding

The finding's second criterion asks that the agent "cannot obtain
ACTIONS_ID_TOKEN_REQUEST_TOKEN, ACTIONS_RUNTIME_TOKEN or
ACTIONS_RESULTS_URL/ACTIONS_CACHE_URL on any path". This design meets the
criterion's purpose, no direct cache writes. It does not meet the text,
because the text asks for something this job shape cannot deliver:

- On the Claude engine the agent runs as `runner`, unsandboxed on every
  path except the reviewer's external and fork-head paths. Hosted runners
  grant passwordless `sudo` (docs `github-hosted-runners.md`: "The Linux
  and macOS virtual machines both run using passwordless `sudo`").
- Code running as that user can recover the job's runtime token without
  it ever being in the agent's environment, from later steps of the same
  job or from the worker. This is published research (Adnan Khan, "The
  Monsters in Your Build Cache", 2024-05-06) and follows from the handler
  facts above. A `credentials.envVars` denial is a sandbox setting and
  covers only sandboxed commands (`claude-review.yml:1150-1155`); on an
  unsandboxed path it does nothing.
- `claude-code-action` already keeps the OIDC request variables out of the
  Claude CLI's environment (Current behaviour). An "agent prints its
  environment" check would pass today and prove nothing.

The token itself therefore has to be harmless for the cache, and that is
what `cache-mode: read` makes it: the service enforces the mode on the
token, so a recovered token can restore and nothing more. On the codex
engine, #136 runs codex and its provisioning as the `codex` user, which
cannot read the runner's environment. That is a real barrier, but this
design does not depend on it.

The token's other capabilities (the run's artifacts, for one) are outside
this finding; see "Not this design".

### Caller-owned agent jobs

The four reusable workflows cover every caller stub. Three caller
repositories also run agents in workflows of their own, on triggers that
get write access by default. They need the same line on their agent job,
by a PR in that repository after this one merges (memory: stub PRs follow
the agents merge).

| Repo, workflow, job | Triggers | Today | Change |
| --- | --- | --- | --- |
| inspect_flow `inspect-update.yml`, agent job | `schedule`, `workflow_dispatch` | Write (trusted triggers). claude-setup (setup-uv@v7, `enable-cache: true`, key over `**/uv.lock`) saves the key `build.yaml`'s push jobs restore, and those jobs hold `contents: write`. Route (a) is live. | `cache-mode: read` on the job |
| inspect_flow `inspect-ai-main-failure.yml`, `triage-agent` | `workflow_run` of scheduled CI, `workflow_dispatch` | Write, observed (run 35655501245). Same claude-setup and same restore targets. | `cache-mode: read` on the job |
| ts-mono `dependabot-fix.yml`, agent job | `schedule`, `workflow_dispatch` | Write. setup-node `cache: pnpm` (pnpm store) and `actions/cache` `.turbo` under `turbo-${{ github.sha }}` with `restore-keys: turbo-`, before the agent. `ci.yaml` restores both, and `npm-publish.yml` restores the pnpm store before `npm publish` with `id-token: write`. | `cache-mode: read` on the job |
| actions `inspect-ai-ci-perf.yml`, `triage-test-failures.yml` | `schedule` / `workflow_run`, `workflow_dispatch` | Write, but no cache action. The agent runs as its own unprivileged user (`isolated-agent`), so route (b) needs a boundary crossing first. | `cache-mode: read` at workflow level, defence in depth; low priority |

### Per caller, for the reusable workflows

What each caller's claude-setup does inside the agent job, what its
trusted workflows restore, and what this design changes. The survey read
`origin/main` of each repo (inspect_ai: fork `meridian` at `6553c08` and
upstream `main` at `06537c3`) on 2026-09-23.

| Caller | claude-setup saves | Trusted restores that matter | Route today | What changes |
| --- | --- | --- | --- | --- |
| inspect_ai (fork) | Nothing: plain `run:` uv install, no nested action | Fork `build.yml` and `log_viewer.yml` (setup-uv default cache, setup-node pnpm, exact keys). Upstream caches are a separate repository's, unreachable. | (b) only, in the fork's `meridian` and PR scopes | Saves refused in every scope. No caller change. |
| inspect_flow | setup-uv@v7 cache, same key as CI's 3.11 legs | `build.yaml` push jobs (`contents: write`), `inspect_ai_main.yaml` (schedule). No PyPI job restores. | (a) and (b) on review-event PR scope, and via the caller-owned jobs above | Saves refused. Caller PR for the two caller-owned jobs. |
| inspect_harbor | setup-uv@v8.1.0 default cache, same key as CI's 3.12 legs | `build.yaml` (`contents: write`), `update-registry.yml` (schedule, RELEASE_PLEASE_TOKEN). No PyPI job restores. | (a) and (b) on review-event PR scope and on its reviewer's `pull_request: synchronize` trigger | Saves refused. No caller change. |
| inspect_sandboxes | No claude-setup (fallback, saves nothing) | None: setup-uv@v3 without `enable-cache`, setup-python without `cache:` | None | Nothing restores, so nothing to protect today; the key still covers a future setup-uv bump (v6+ turns caching on). |
| inspect_scout | No claude-setup | `build.yaml` pip cache with `restore-keys: Linux-pip-` (prefix), setup-uv default, setup-node pnpm; `npm-publish.yml` restores pnpm before publishing | (b) on review-event PR scope | Saves refused. No caller change. |
| inspect_swe | No claude-setup | `build.yaml` pip cache with prefix `restore-keys` | (b) on review-event PR scope | Saves refused. No caller change. |
| ts-mono | setup-node pnpm store; `actions/cache` `.turbo` with `restore-keys: turbo-` | `ci.yaml` (all jobs); `npm-publish.yml` restores the pnpm store | (a) and (b) on review-event PR scope, and via `dependabot-fix.yml` | Saves refused. Caller PR for `dependabot-fix.yml`. |
| actions | No claude-setup, no `pyproject.toml` | None of its own; `release-please-vscode.yml` restores pnpm in its callers' build job | None | Nothing. Optional line on its own agent workflows (table above). |

"Review-event PR scope" means the dev agent's `pull_request_review` and
`pull_request_review_comment` triggers. They are in every stub surveyed
except inspect_ai's fork stub, which has only `issue_comment` and `issues`.

### The caller contract (README)

Replace the claude-setup paragraph's cache sentence (`README.md:79-87`)
with the rule and what it means for the composite. Proposed text:

> ... ideally by delegating to your existing CI setup
> (`uses: ./.github/actions/<your-setup>`), so nothing is duplicated. Agent
> runs **restore** your CI's caches but never save them: every agent
> workflow declares `cache-mode: read`, so a cache action in your setup
> restores as usual and its save is skipped (or refused with a warning, on
> older cache actions). Keep a trusted workflow, such as CI on push to the
> default branch, saving the entries you want agents to hit. If you set
> `cache-mode` on the job that calls an agent workflow, use `read` or
> `write`: `none` or `write-only` there makes the run fail validation.

Callers need not change anything. Switching a composite to restore-only
(`actions/cache/restore`, setup-uv `save-cache: false`) only silences the
message, and the README says so in one clause rather than asking for it.

### Examples and this repository's stubs

The three example stubs (`examples/claude-stub.yml`,
`examples/claude-auto-stub.yml`, `examples/claude-review-stub.yml`) and
this repository's own `*-stub.yml` get `cache-mode: read` on each calling
job, with a one-line comment pointing here. This is a cap: if a future
edit here ever declared `write`, a caller copied from these stubs would
fail validation instead of silently granting it. Existing callers do not
need to adopt it; the reusable workflow's own key is the control.

### Documentation

- `SECURITY.md` → Guarantees: a new bullet. "No job of the agent workflows
  can save to the GitHub Actions cache: every reusable workflow declares
  `cache-mode: read`, which the platform enforces on the job's token, so
  nothing an agent does or a caller's setup nests can place content in a
  cache a trusted workflow restores." The Trust boundaries paragraph gains
  "Actions cache entries saved by any run an outsider can influence" among
  the untrusted items.
- `design/architecture.md:1105-1110`: the bullet "The Actions cache is
  scoped to the run's repo" gains the rule. Agent runs restore the caller's
  entries, and since this change never save them. The "reuse is automatic"
  sentence then describes restores only.
- `design/credential-separation.md` §3.5: a bullet for the runtime token.
  It is reachable by runner-user code, is cache-read-only by `cache-mode`,
  and can also upload the run's artifacts, which the land job already
  treats as untrusted. §7 gains a residual line: a caller that
  sets `cache-mode: write` on the calling job cannot widen what the reusable
  workflow declares, but its own non-agent jobs are its own concern.
- `AGENTS.md` → Conventions: one bullet. "Every reusable agent workflow
  declares top-level `cache-mode: read`; never add a write-capable
  `cache-mode` anywhere in `.github/workflows/` (tests/test_cache_mode.py)."

### Interaction with the release-build decision

Ransom decided on 2026-09-21 that upstream inspect_ai release builds
should be independent of caches. This design removes the agent jobs as
cache writers, in every scope. The writers left in a caller's
default-branch scope are its trusted-trigger workflows, `push`,
`schedule` and `workflow_dispatch`, running merged code, plus any job a
caller explicitly grants `write`. Cache-independent release builds remove
the trust a release puts in those remaining writers. The two decisions
cover different writers and do not depend on each other.

Release jobs that restore a cache today, from the survey:

- ts-mono `npm-publish.yml` (pnpm store)
- inspect_scout `npm-publish.yml` (pnpm store)
- upstream inspect_ai `npm-publish.yml` (pnpm store) and `docker.yml`
  (buildx `type=gha`)
- the build job of meridianlabs-ai/actions `release-please-vscode.yml`,
  whose VSIX the publish job ships (pnpm)

No PyPI publish job in any surveyed repository restores a cache. This
design changes none of them.

## Alternatives considered

- **Rely on the platform's low-trust default.** It lost because it is not
  uniform. A `workflow_run` from a trusted upstream run logs
  `Cache mode: write`, and review-event runs keep PR-scope write. Any
  called workflow can also opt back in. One explicit line makes this
  repository independent of how GitHub classifies a trigger.
- **Contract only: claude-setup must be restore-only under agents.** This
  is the finding's criterion 1 as written. It is not enforceable from
  here, since callers own the composite and a nested action's post step
  cannot be switched off from the calling workflow. It does nothing
  against route (b) or codex-action's own setup-node save.
- **Extend the sandbox overlay's `credentials.envVars` denial to every
  path** (the finding's suggested criterion 2 fix). The setting governs
  sandboxed Bash only, and the dev agent, the loops and same-repo reviews
  are unsandboxed. Even sandboxed, the Claude process and its unsandboxed
  tools run as `runner`. It lost because it would look like a control
  without being one.
- **Drop `id-token: write` from agent jobs.** WIF and the Claude App token
  need it, and removing it would leave `ACTIONS_RUNTIME_TOKEN` in every
  node action of the job, where runner-user code can reach it anyway.
- **Provision in a separate job and pass the environment as an
  artifact** (the finding's other criterion-1 option). That job still runs
  head-controlled code (the PR's claude-setup, its lockfile) with a token
  that could save. So it would need `cache-mode: read` as well, and would
  add a job, an artifact round trip and a venv relocation for nothing
  further.
- **Run the Claude engine as an unprivileged user,** as #136 does for codex
  and the actions repository's `isolated-agent` does. This would keep the
  runtime token away from the agent. It is a large change to the Claude job
  (a wrapper through `path_to_claude_code_executable`, the settings and
  identity-token files, workspace grants, a reclaim step), and the cache
  does not need it once the token cannot write. It may be worth doing for
  other reasons; see "Not this design".
- **Dispatch agent runs onto a dedicated ref** so their saves land in a
  scope nothing trusted restores. This would need the stub on that ref in
  every caller, and would split each run across two runs. It would also
  lose `claude-code-action`'s tag mode, which reads the triggering event.
  It lost as far heavier than a platform key that exists.
- **Audit and delete in the land job:** list entries created during the
  agent job and delete them (`actions: write`). The entries cannot be
  attributed to a run, so concurrent trusted CI saves would be deleted
  too, and the window between save and delete stays open for as long as
  the agent job runs. It lost as a race, not a control.
- **`cache-mode: none`.** It works and is never an over-request against a
  caller's cap. It lost because it throws away every provisioning hit, and
  reading is no new exposure (Design).
- **Job-level keys on the agent jobs only.** Equivalent today. It lost
  because every new job (#136 adds four) would need its own line, where the
  workflow-level key covers them without one.

## Compatibility and migration

- **Stored formats, schemas, the viewer:** none.
- **Workflow inputs and outputs:** none change. The `@main` contract holds.
- **Callers with no `cache-mode`:** runs start as before. A cache action in
  claude-setup restores as before, and its save is skipped with an
  informational message (`@actions/cache` ≥ 6.2.0: current `actions/cache`,
  `actions/setup-node`, `actions/setup-python`, `astral-sh/setup-uv` main)
  or refused by the service with a warning (older bundles such as setup-uv
  v7 may predate 6.2.0). Either way the step and the job continue (docs:
  "the save fails but the step and the job do not").
- **A caller that sets `cache-mode` on the calling job:** `read` or `write`
  keeps working. `none` or `write-only` makes the run fail validation
  before it starts (docs: an over-request "does not start"). No surveyed
  caller sets it. The README states the rule.
- **Cache warmth:** agent runs no longer create entries, so a key only
  agents ever computed stays cold. In every surveyed repo, a trusted CI
  on push or schedule saves the same keys. The one exception is
  inspect_harbor's claude-setup, which runs `uv sync` with the doc group
  while CI runs `--no-group doc`. The saved uv cache is CI's, and the
  agent's install downloads the doc group's wheels on every run. That is a
  small cost, and correct.
- **GHES:** not applicable; the feature is github.com and GHEC only, and no
  caller is on GHES.

## Security

- **Untrusted input reaching new code:** none. The change is a static
  workflow key that no expression composes. Nothing from an event, a
  checkout or the agent reaches it.
- **What it removes:** every write from an agent job into any cache scope,
  on both engines, whatever the caller's claude-setup nests and whatever
  the agent runs as. The service enforces it on the token, so a runtime
  token recovered by runner-user code is read-only for the cache.
- **What it leaves:**
  - Agent jobs still restore trusted entries. That exposes nothing
    beyond what any PR's CI can read, and the agent job is already
    untrusted.
  - A caller's other workflows still save and restore under their own
    triggers. The release-build decision covers the release end of that.
  - The runtime token can still upload the run's artifacts. The land job
    already treats the landing artifact as untrusted and validates it
    ([credential-separation.md](credential-separation.md) §3.2).
  - Agent jobs can still mint OIDC ID tokens (see "Not this design").
- **Trigger classification:** the design no longer depends on it, which
  is the point. The test pins the declaration, and the canary pins the
  enforcement.

## Testing

- **`tests/test_cache_mode.py`** (new; text checks, no PyYAML, in the
  style of `tests/test_app_token_minting.py`):
  - Each of `claude.yml`, `claude-auto.yml`, `claude-auto-review.yml` and
    `claude-review.yml` has exactly one column-0 `cache-mode: read` line,
    before `jobs:`.
  - No line in `.github/workflows/` or `examples/` sets `cache-mode` to
    `write` or `write-only`, at any indentation. The canary's positive
    control is the one listed exception, by file and job.
  - No job in those four files overrides the key.
  - Each calling job in `examples/*-stub.yml` and this repo's `*-stub.yml`
    carries `cache-mode: read`.
  - Runs under `python3 -m pytest` from the root. The staged
    `tests/workflow-tests.yml` already triggers on `tests/**` and the four
    workflows.
- **Canary: `cache-mode-canary.yml` plus `cache-mode-canary-reusable.yml`**
  in `.github/workflows/` (new; `workflow_dispatch` only, like #136's
  `engine-isolation-canary.yml`). It needs no secrets, no model and no
  network beyond GitHub.
  - The caller workflow is dispatched, which is a trusted trigger with
    default write, so the canary tests the one case the implicit default
    does not cover. Its calling job sets no `cache-mode`, like a caller
    stub.
  - The called workflow declares the same top-level `cache-mode: read`.
    Its `probe` job does three things:
    1. It asserts `ACTIONS_CACHE_MODE` is `read` in a node step and that
       "Set up job" logged `Cache mode: read`.
    2. It saves `canary-<run_id>-<attempt>-new` with current
       `actions/cache/save`. This must be skipped.
    3. It saves `canary-<run_id>-<attempt>-old` with an `actions/cache/save`
       tag whose bundle speaks the v2 cache service (the v1 service is
       retired) but predates `@actions/cache` 6.2.0, and so ignores
       `ACTIONS_CACHE_MODE`. The implementer picks the tag and records why.
       This save must be refused by the service. This is
       the check on server-side enforcement: a skipped client save proves
       only that the client read the variable.
  - A `control` job in the caller workflow, with default write, saves
    `canary-<run_id>-<attempt>-ctl`. It must succeed, which proves the
    canary can see a save.
  - A final `verify` job in the caller workflow (`actions: write`) checks
    with `gh cache list --key canary-<run_id>-<attempt>`:
    - exactly the `-ctl` entry exists;
    - a `lookup-only` restore of `-old` and `-new` from that job misses.
      This is the finding's criterion 3, restoring an agent-run key from
      a default-branch workflow.
    - It then deletes the `-ctl` entry.
  - The canary runs once by dispatch before the change is called done, and
    again whenever GitHub changes cache-mode semantics.
  - The run link is recorded in `design/architecture.md` beside the cache
    bullet.
- **Dogfood check after merge:** trigger each workflow once in this
  repository (`@claude` on a scratch issue, `@review` on a scratch PR,
  and the two loops when next exercised). Confirm that every job's "Set up
  job" log shows `Cache mode: read`, both engines included once #136 has
  landed.
- **Caller PRs:** after merge, one `workflow_dispatch` of each changed
  caller-owned workflow (inspect_flow `inspect-update.yml` with a dry run
  where it has one, ts-mono `dependabot-fix.yml` with `dry_run`). Confirm
  `Cache mode: read` and, where claude-setup missed its key, the skipped
  or refused save in the post step.

## Implementation plan

1. **agents PR: the key, the test, the docs.** It adds the top-level
   `cache-mode: read` block to the four reusable workflows and
   `tests/test_cache_mode.py`. It adds the `cache-mode: read` cap to
   `examples/claude-stub.yml`, `examples/claude-auto-stub.yml`,
   `examples/claude-review-stub.yml` and this repo's
   `.github/workflows/*-stub.yml`. Documentation changes: `README.md` (the
   claude-setup paragraph), `SECURITY.md` (the guarantee and the trust
   boundary line), `design/architecture.md` (the cache bullet),
   `design/credential-separation.md` (§3.5, §7) and `AGENTS.md` (the
   convention bullet). The PR edits workflow files, so it is made from a
   maintainer's machine.
2. **agents PR (same PR or the next): the canary.** It adds
   `.github/workflows/cache-mode-canary.yml` and
   `cache-mode-canary-reusable.yml`, plus a `tests/README.md` line. It is
   dispatched once, and the run is recorded.
3. **Caller PRs, after step 1 merges:**
   - inspect_flow: `inspect-update.yml` and `inspect-ai-main-failure.yml`
     agent jobs.
   - ts-mono: `dependabot-fix.yml` agent job.
   - actions: `inspect-ai-ci-perf.yml` and `triage-test-failures.yml`, if
     Ransom takes the optional line.
   - Each PR is one line per job, plus a comment pointing here.
4. **Close the finding** once steps 1 to 3 are merged and the canary is
   green. Ransom marks it fixed on the Security board, with this document
   and the canary run as the evidence.

**Order with the open PRs.** Step 1 is independent of #136, #131 and #137
and should land first. It closes a live write path in the smallest
change, and it touches none of their lines: a top-level key between `on:`
and `env:`, and one bullet in each shared document.

- **#136, each engine in its own job:** it adds `agent-codex`,
  `review-codex`, `fix-codex` ×2. They inherit the workflow-level key with
  no edit. The test's "no job overrides it" rule then covers them on the
  rebase. If #136 lands first, nothing in this design changes.
- **#131, post-codex PATH:** no interaction.
- **#137, trusted start SHA:** no interaction.
- **#124,** which removes the reviewer's `pull_request`/`pull_request_target`
  path, only shrinks the trigger set this design already covers.

Documentation conflicts with #136 in `SECURITY.md`, `architecture.md` and
`credential-separation.md` are one-bullet merges.

## Open questions

1. **Criterion 2 as written.** This design meets its purpose (no direct
   cache writes) through the token's scope. It does not meet the text:
   withholding the variables is not achievable for a `runner`-user agent,
   for the reasons in Design. Recommendation: accept that and record it
   on the finding when closing it. The alternative is the unprivileged-user
   Claude engine, which is a separate design.
2. **`read` or `none` for agent jobs.** Recommendation: `read`. It keeps
   provisioning hits, and reading adds no exposure.
3. **Caller-owned agent jobs in the actions repository.** Recommendation:
   add the line in the same caller round. It costs nothing, but it is not
   required, because the agent there already runs as its own user and no
   cache action is involved.

## Not this design

- **OIDC ID tokens from agent jobs.** Runner-user code in a job with
  `id-token: write` can mint ID tokens for any audience with the caller
  repository's claims. Worth an inventory of which federations trust those
  claims beyond Anthropic's WIF rule. PyPI trusted publishing binds the
  workflow file and environment, which appears to rule it out for an agent
  job's token; that is unverified here.
- **Running the Claude engine as an unprivileged user** (the Alternatives
  entry), for the runtime token's other capabilities and for the reasons
  [credential-separation.md](credential-separation.md) §3.5 already lists.
- **Release jobs that restore caches** (the list under "Interaction with
  the release-build decision"), for Ransom's release-build decision. The
  actions repository's `release-please-vscode.yml` affects inspect_vscode,
  which this survey did not read.
- **Stale comment in inspect_ai's fork claude-setup:** "the fork's CI
  doesn't cache uv either" is no longer true; `build.yml` uses setup-uv's
  default caching.
- **inspect_scout's dev-agent stub still passes the retired `MARVIN_TOKEN`
  secret.**
- **#136's documentation:** it deletes the "Untrusted checkouts" heading
  in `design/architecture.md` while cross-references to it remain (head
  `e39faa6`, around L260 and L1725).
