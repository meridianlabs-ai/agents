# Meridian agent infrastructure — design & history

This document records *why* the agent infrastructure is shaped the way it is —
the constraints, the alternatives considered, and the tradeoffs taken. The
README covers *how to use* it; this covers *why it's built this way*, so future
changes don't relitigate settled decisions or quietly break load-bearing ones.

## Goal

Let Claude Code do automated work across Meridian repos: pick up tasks
(ideally from the org project board), run autonomously, surface a reviewable
result, and let a human continue the work in VS Code with history intact. Much
of the real work targets `inspect_ai`, which lives in an org we do **not**
control (`UKGovernmentBEIS`). That single constraint shapes most of what
follows.

## Centralized reusable workflows + thin stubs

GitHub only delivers a repo's events (issue comments, labels, PR reviews) to
workflows that live **on that repo's default branch**. There is no org-wide
"inject this workflow everywhere" mechanism. So each repo needs at least a stub
workflow. We keep the stubs minimal (~15 lines) and put all real config in
reusable workflows in this (`agents`) repo, referenced `@main`. Editing the
reusable workflow changes every repo's behavior on the next run.

Consequence: **this repo must be public.** A public caller repo (the inspect_ai
fork is public) cannot call a reusable workflow in a private repo. The repo
holds no secrets — only workflow YAML and non-secret WIF identifiers — so public
is fine. (We discovered this the hard way: the first fork run failed with
"workflow was not found" until `agents` was made public.)

## The inspect_ai fork strategy

We can't install the Claude GitHub App, add workflows, or store secrets in
`UKGovernmentBEIS`. The standard open-source answer applies: **do all
automation on a fork in our own org, and open cross-repo PRs to upstream.**

### Branch layout: `main` pristine, `meridian` default

The tension: event/scheduled workflows only fire from the default branch, so
the Claude workflows must live on the default branch — but if the default
branch is a mirror of upstream `main`, adding workflow commits to it makes it
diverge from upstream and contaminates every PR branched off it.

Resolution: a **two-branch fork**.

- `main` stays a byte-pristine mirror of upstream `main`. Claude branches from
  it and targets it, so PR diffs are exactly what upstream will see.
- `meridian` is the **default branch** = `main` + meridian-only workflows. It's
  where events fire from.

Alternatives considered and rejected:
- *Workflows on a mirror `main`*: contaminates PRs, breaks fast-forward sync.
- *Workflows only in a separate `claude-tasks` repo operating on the fork*:
  loses `@claude`-on-the-PR (events in the fork can't trigger workflows in
  another repo), which is the whole point of working where the PRs live.

Naming: the delta branch is `meridian` (named for *who owns the delta*), not
`agents`/`actions` (which describe transient contents and collide with repo
names). Ownership is stable; contents grow.

### The two-stage PR flow

The eventual PR must target `UKGovernmentBEIS/inspect_ai`, but interaction
(`@claude`, `@review`, CI) must happen in the fork where our workflows fire.
The same branch backs both:

1. Claude opens an internal fork PR (`claude/xyz` → fork `main`) as the **review
   surface**. Never merged. Since fork `main` ≡ upstream `main`, its diff is the
   upstream diff.
2. All iteration happens on the fork PR.
3. A **human** promotes by opening the upstream PR from the same branch. This is
   deliberately manual: the agent's fork-scoped token can't push cross-repo, and
   a human gate before publishing into an org we don't control is the right
   default. (A `meridian-claude` machine account with a cross-repo PAT could
   automate promotion later; deferred.)
4. Upstream review feedback is handled by commenting on the fork PR; pushes to
   the shared branch update the upstream PR automatically. The one thing that
   doesn't work is the agent reacting to upstream events on its own — a human
   bridges that with one comment.

### Keeping the fork in sync

`sync-upstream.yml` runs hourly: fast-forward `main` from upstream, merge
upstream into `meridian`. Hourly (not daily) because Actions minutes are free
on public repos, no-op merges don't create empty commits, and it shrinks the
"Claude branched from stale code" window from a day to an hour. A manual
`workflow_dispatch` exists for "I need it now."

## Authentication: the journey to Workload Identity Federation

We evaluated, in order:

1. **`ANTHROPIC_API_KEY`** (org secret) — works, org-owned, metered. Fine, but a
   long-lived secret.
2. **`CLAUDE_CODE_OAUTH_TOKEN`** from `setup-token` — tied to one person's
   subscription seat; all org automation bills against and shares that person's
   rate limits; not recommended by Anthropic as a shared credential. Rejected.
3. **A bot subscription seat** + extra-usage overflow — fixes the personal-seat
   coupling, but seats are for humans, overflow-for-CI is undocumented, and a
   seat throttles exactly when automation is busy. Rejected.
4. **Workload Identity Federation (chosen)** — each run exchanges its GitHub
   OIDC token for a short-lived Anthropic credential. **No long-lived secret
   anywhere.** Usage lands in a workspace we can track, rate-limit, and cap.
   This is the "trusted publisher" model (same as PyPI/npm OIDC).

### Why the IDs aren't secrets

The federation rule, org, service-account, and workspace IDs are *addresses*.
Security rests on the OIDC token GitHub signs: Anthropic verifies the signature
against GitHub's keys, then evaluates the rule's CEL condition
(`repository_owner == "meridianlabs-ai"`) against the *verified* claims. A
stranger copying the IDs into their own repo presents a token whose
`repository_owner` is their org → rejected. The other half of the trust
boundary is claude-code-action's own check that the triggering user has write
access (so a stranger's `@claude` on a public-repo issue does nothing).

### Debugging history (so the failure modes are recognizable)

Getting WIF working surfaced a sequence of distinct failures, each with a
distinct signature:

- **"workflow was not found"** → the public fork couldn't call the private
  `agents` reusable workflow. Fix: make `agents` public.
- **401, no events in the Console's Workload Identity → Authentication events**
  → wrong `anthropic_organization_id` (a placeholder from an example). Requests
  were hitting the wrong org entirely. Fix: correct org UUID.
- **401 with `reason: sa_not_in_workspace`** in the auth events → the service
  account wasn't a member of the requested workspace. Fix: add it (Console).
- **Subject pattern wildcard** (`repo:meridianlabs-ai/*`) was suspected to not
  match multi-segment subjects; we moved the rule to a **CEL expression**
  (`repository_owner == "meridianlabs-ai"`) which is also the cleaner org-wide
  constraint regardless.

Lesson encoded in the workflows: a "Surface agent errors" post-step reads
`is_error` from the run's local `claude-execution-output.json` (e.g. a model
overload/529 or 404) and posts a visible comment + fails the run — because
claude-code-action otherwise exits 0 on a model-API error, leaving a misleading
green run with no result (and a stale "I'll get back to you" comment). Auth and
model failures are diagnosed from that comment and the job log; there is no
transcript artifact (see No transcript artifacts, below).

### No transcript artifacts

The workflows used to upload `claude-execution-output.json` as a build artifact
on every run, unredacted and with no retention limit. They no longer do
(decision: Ransom, 2026-09-08, after Claude Security finding 4122328; #65,
superseding #60/#62): the file is Claude Code's verbose stream-json transcript,
so it carries every `tool_result` — full Bash stdout/stderr and the contents of
every file the agent read. Three of the four workflows hand the agent
`MARVIN_TOKEN` as its `github_token`, so an `env` or `gh auth token` in the
transcript put a write-access token in an artifact GitHub does not mask and
anyone can download on a public caller repo. Nothing automated consumed the
artifact — every in-job consumer (`model-provenance`, `Surface agent errors`,
the `Refund` steps, `push-base-merge`'s `require-file`) reads the local file on
the runner — and no reduced, scrubbed, or opt-in form was judged both safe and
useful, so none was added. The human uses it had are covered elsewhere:
failures by the `Surface agent errors` comment and the job log, cost and
served-model by the `model-provenance` job summary. Anything more is a
deliberate, temporary local change on a branch, not a workflow feature.

### No persisted git credentials

`actions/checkout` persists its token in the workspace for the rest of the
job (an `http.<server>/.extraheader` entry in `.git/config`; since v4.3.1/v6
a file under `RUNNER_TEMP` pulled in via `include.path`). The loop workflows
checked out with `MARVIN_TOKEN` and leaned on that for every later
runner-side git call — `push-base-merge` with an empty `push-token`, the
codex landing pushes, the hand-back fetches — and `claude.yml` persisted the
job token and had to override the header (`-c http.<origin>.extraheader=`)
wherever the machine account was meant to authenticate instead. On the codex
path that contradicted the engine's security model: `Create codex user`
group-grants the whole workspace, so codex could read the PAT off disk
(Claude Security findings 4121988/4121984; issue #61).

Since #61 (decision: Ransom, 2026-09-09) **no credential is written to the
workspace**. All three checkouts run `persist-credentials: false` with the
job token (the fetch is a read); an assertion step right after fails the job
if `git config --get-all http.<server_url>/.extraheader` finds anything
(git follows includes, so the check survives checkout's layout changes, and
the key comes from `github.server_url` because checkout keys the header on
the server origin — a literal `github.com` would pass vacuously on GHES); and
every runner-side git network operation authenticates itself, scoped to its
step, through git's environment config — `GIT_CONFIG_COUNT` /
`GIT_CONFIG_KEY_n` / `GIT_CONFIG_VALUE_n` define a credential helper that
answers a 401 from a masked env var — `GIT_TOKEN`, the one variable name
every copy of the block reads (the composites map their token input onto
it), so a block copied into a new step cannot silently name a variable the
step never set — with an empty first `credential.helper` entry resetting any
helper the runner image's global config might carry. The helper itself is
keyed `credential.${{ github.server_url }}.helper`: git's URL-scoped
credential config, so only this server's 401s are answered and a `git fetch`
aimed at any other host (the dev agent's allow-list permits the verb) gets no
credential. A helper rather than the base64
`AUTHORIZATION` header #61 sketched, for two reasons: expressions have no
base64, so a header cannot be composed in YAML `env:` (and a prior step's
masked base64 output would be dropped by the runner's "may contain secret"
output filter), whereas the helper form is plain YAML and works on `uses:`
steps; and URL-embedded credentials take precedence over helpers, so wherever
claude-code-action has rewritten the origin URL (below) the helper never
changes who pushes. Per step:

| step | token |
| --- | --- |
| `sync-branch` (all three workflows) | job token — fetches only |
| `push-base-merge` (all three) | `push-token`, now required and validated: `MARVIN_TOKEN` (the push must trigger CI) |
| codex landing steps | `MARVIN_TOKEN` — in `claude.yml` `\|\| github.token` where the caller lacks it (degrading as every marvin-less push does); the loops have no fallback, their gate already exited on a missing secret |
| hand-back, unlanded-work, open-PR and verify fetches | job token — best-effort reads that would otherwise fail silently on a private caller |
| `unresolved-merge-guard` | none — it only reads the local index and tree |
| the claude-code-action step | `MARVIN_TOKEN` (`\|\| github.token` in `claude.yml`) — see below |

The agent's own pushes never depended on the persisted credential:
claude-code-action's prepare step (`configureGitAuth`, agent mode included —
its `src/modes/agent/index.ts`) removes checkout's header and rewrites the
origin URL to carry its `github_token`, so the agent pushes as the machine
account either way. The action step still gets the helper env. In
`claude.yml` it is load-bearing: tag mode's `setupBranch` runs `git fetch
origin <branch>` (and `git ls-remote` on issue runs) *before*
`configureGitAuth`, which rode on the persisted header and would fail on a
private caller with nothing persisted. In the loops it is belt and braces —
the fallback #61 named. What this hands the agent: on repos with
`MARVIN_TOKEN`, nothing new — the same token is already its `GITHUB_TOKEN`
(what its `gh` calls use). On a marvin-less `claude.yml` caller the helper's
`GIT_TOKEN` is the *job* token, which the agent could not previously read
(its `GITHUB_TOKEN` there is the Claude App token, and the action's
`replaceCheckoutCredentials` removed checkout's copy) — a bounded addition:
the job token carries only the caller's `permissions:` block, on a repo the
agent already writes to. The URL precedence above means that job-token helper
leaves the push identity with the Claude App.

Two things this does not change. The action's URL rewrite still leaves *its*
token in `.git/config` for the remainder of a Claude run — the action's
behavior, noted as a residual risk under Untrusted checkouts — but the codex
path, where the exposure was, never runs the action. And the codex landing
steps now trust nothing under `.git` at all: see design/codex-engine.md →
Hook-safe landing.

## Model selection: prefer Fable, fall back gracefully

Default is the `fable` alias with `--fallback-model default`. Claude Code's
`--fallback-model` fires not just on overload but on an **unavailable/retired**
primary, and `default` expands to the account default. So `--model fable
--fallback-model default` means "prefer Fable, degrade to the default if it's
gone" in one invocation, with no pre-flight availability probe.

This was verified the hard way: when Fable became unavailable, a run's init
line still reported `claude-fable-5`, but `modelUsage` in the execution log
showed every token served by `claude-opus-4-8` — the fallback fired
correctly, and **the init line echoes the *requested* model, not the one that
ran.** Always read `modelUsage` to know what actually executed. The
`model-provenance` composite action does that on every Claude-path run: it
writes the served-model token table to the job summary (plus the run's cost,
duration, turn count, `is_error` and result subtype — numbers and booleans
only, never the free-text `.result`), and posts a note on
the issue/PR (as the machine account, first line `<!-- model-provenance -->`)
when an **unexplained** non-requested model served output tokens — or when the
result errored with text that reads like a classifier refusal. Haiku is
excluded outright (Claude Code's own subagent/summarization traffic on every
run). Other non-requested models are first attributed against the `Agent`/
`Task` tool-use `model` inputs in the log: a subagent the agent launched on
`opus` lands in `modelUsage` exactly like a per-request fallback would, but it
is the agent's own choice, so it is recorded in the job summary only
(decision: Ransom, 2026-09-02, after fork #398's two opus subagents drew a
fallback-shaped note). The note posts in two shapes: when the requested model
served nothing, "fallback fired" outright — nothing is attributed then, since a
whole-run fallback landing on a model a subagent also asked for is still the
fallback; when the requested model also served, a hedge, because only the
Agent tool's `model` *input* is attributable — a subagent model set in
`.claude/agents/*.md` frontmatter, a built-in agent default, or the Workflow
tool's `agent({model})` leaves no such input and is indistinguishable from a
per-request fallback. It only judges the fallback when the log has an init
line to compare against; without one it reports the table and says so.
Best-effort: every path exits 0.

The `fable` alias (not a pinned `claude-fable-5[1m]`) is used so the model
auto-updates if Fable returns under a new version.

## Permissions: settings.json, allow-list, layered separation

### Two layers

- **Layer 1 (hard, GitHub-enforced): job `permissions:`** — the token scope.
  Dev = `contents: write`; reviewer = `contents: read`. This is the *real*
  privilege boundary and it cannot be changed by settings.json (it's the OAuth
  token's scope). Reusable-workflow `permissions:` are static, which is *why*
  the reviewer is a separate workflow — you can't downgrade permissions per
  input.
- **Layer 2 (soft, Claude-enforced): settings.json `permissions`** — which
  shell commands Claude will attempt. Largely shared between the agents; the
  token scope does the real separating.

### Why settings.json over `--allowedTools`

We migrated from `--allowedTools` (a comma-string in `claude_args`) to inline
`settings.json` because:
- It's a clean JSON array, easier to maintain.
- It supports `deny` rules, which `--allowedTools` cannot. The reviewer's
  `deny` of `Edit`/`Write`/`git push`/`git commit` makes its read-only intent
  explicit and survives even a `claude_args` override (deny beats allow,
  absolutely).

Important precedence facts (verified): `claude_args --allowedTools` *overrides*
settings.json permissions, so when migrating you must **remove** `--allowedTools`
or settings get ignored. settings.allow is additive to the action's defaults
(it doesn't silently drop the file-editing tools the dev agent needs).

### Why an allow-list, not allow-all + deny

These workflows are triggered by partially attacker-controllable text (an
outsider can author an issue body; an insider labeling it runs Claude on that
text). An allow-list caps the blast radius of prompt injection; a denylist
inverts that (anything not explicitly denied runs). So we enumerate allowed
commands rather than allowing everything and blocking the dangerous bits.

### Sandboxing and the verify loop

claude-code-action denies `Bash` by default (and headless runs auto-deny
anything not allow-listed — there's no human to prompt, so the permission
system degenerates to a pure allow-list; "auto mode" / `bypassPermissions`
would only *remove* that backstop, which we don't want for injection-triggered
runs). The cost showed up immediately: an early run wrote a full implementation
**plus tests it couldn't run**, shipping a latent bug ("CI will validate"). We
granted a **scoped Bash allow-list** for the test/lint loop (`pytest`, `ruff`,
`mypy`, `pip`, `python`/`python3`, `uv`) so the agent verifies its work, and
told it to run the trio before opening a PR. A re-run then caught and fixed a
real test bug. We deliberately did **not** grant full Bash.

Allow-list brittleness is real: `Bash(python:*)` does not match `python3 ...`,
and `gh` was initially missing (so the dev agent's `gh pr create` was silently
denied and it fell back to a compare link). Both invocation forms and `gh` are
now allow-listed.

Checkout is `fetch-depth: 0` (full history + tags): setuptools-scm needs tags
to compute a version (a shallow clone produced a version that conflicted with a
pinned dependency), and the agent uses `git log`/`blame` to understand code.

### Provisioning the environment for the verify loop

The allow-list grants permission to run `pytest`/`ruff`/`mypy`, but a bare
`ubuntu-latest` runner has neither the dev tools nor the package installed — so
without a setup step the agent can only fall back to `py_compile` and reasoning
(observed: a reviewer reported "env not provisioned: no pytest/inspect_flow" and
LGTM'd on static checks alone). Both workflows therefore run a setup step
between checkout and the agent.

The mechanism is a **convention, not a duplicated command**. A caller repo opts
in by adding a `.github/actions/claude-setup` composite action; the workflows
run it via `uses: ./.github/actions/claude-setup`, guarded by
`hashFiles(...) != ''` so repos that don't define it are unaffected. Three
GitHub-Actions facts make this work and are worth recording (they were verified
against the docs, not assumed):

- **`./` resolves against the checked-out workspace** (the *caller* repo), not
  the repo that owns the reusable workflow. So the caller's own action runs. The
  shim should delegate to the repo's existing CI setup
  (`uses: ./.github/actions/<their-setup>`) rather than re-spelling the install,
  so the install logic lives in one place.
- **`uses:` must be a literal** — no `${{ … }}` interpolation — which is why the
  path is a fixed convention rather than a configurable input.
- **The Actions cache is scoped to the run's repo** (the caller), even though
  the step lives in our reusable workflow. So the caller's normal CI and these
  runs share cache entries when keys match, and the default-branch (`main`)
  cache is readable from feature branches, PR heads, and `issue_comment` runs —
  i.e. all of our trigger types. Reuse of the cache is automatic; we don't
  manage keys here.

The step is **fatal on failure** (no `continue-on-error`): a broken setup config
should surface loudly rather than silently degrade every run to static-only
review.

**Fork asymmetry.** What's in the workspace — not which branch the workflow was
*resolved* from — decides whether the shim is found, and the two agents check
out different things on the inspect_ai fork:

- The **dev agent** checks out no explicit ref, so on the fork's live triggers
  (`issue_comment`/`issues`, resolved from the default branch) the workspace is
  `meridian`. A `claude-setup` action placed on `meridian` *is* present, so the
  shim fires. Because `meridian`'s source ≈ pristine `main` ≈ upstream, the
  installed env matches the `main`-cut branch the agent then edits.
- The **reviewer** deliberately checks out `refs/pull/{N}/head` (so it reviews
  exactly the upstream-bound diff). That branch is cut from pristine `main` and
  carries no meridian files, so the shim is absent — and the reviewer used to
  stay on static checks there. A **fallback provisioning step** now covers this
  case (decision: Ransom, 2026-09-01): when the shim is absent but the checkout
  has a root `pyproject.toml` (and the trigger passed the same gate that guards
  the shim — never in external mode), the workflow step itself bootstraps uv
  and dev-installs the project, mirroring the fork's claude-setup recipe. The
  earlier alternative — an extra checkout of `meridian`'s `.github/actions`
  into a fixed subdir — remains not done; the generic fallback needed no
  fork-specific wiring. Like the shim, the fallback is fatal on failure, with
  its own clause in the error-surfacing step. Non-Python repos (no
  `pyproject.toml`) still degrade to static review. For normal (non-fork)
  repos with claude-setup, both agents provision identically as before.

### Untrusted checkouts: sandboxed execution of untrusted code

External-review mode (`@review` on an `External` proxy issue) checks out an
outside contributor's PR head — **untrusted code** — into a job that holds
credentials: `id-token: write` (any process in the job can mint an OIDC token
and exchange it through the WIF rule for Anthropic API access),
`pull-requests: write` via the app token, and the agent's own Anthropic auth
in its step env. Installing the package (`pip install -e .` runs the
contributor's build hooks) or running pytest (collection imports every test
module) would execute that code next to those credentials — the
`pull_request_target` anti-pattern by another route. External reviews were
therefore originally static: the prompt forbade test runs outright.

**Fork-head PRs are the same problem on the repo's own PRs** (issue #59,
from the 2026-09-04 security scan). A PR whose head lives in a fork contains
whoever's code, and both workflows used to execute it unsandboxed:

- The **dev agent** checked out `refs/pull/N/merge` on review events (no
  `ref:`), ran the tree's `claude-setup` action gated only on `hashFiles`,
  and on `issue_comment` let claude-code-action fetch the fork head and then
  ran pytest/pip/uv on it — all *before* the action's write-access check on
  the commenter, in a job holding `MARVIN_TOKEN`, the contents-write job
  token and the OIDC request token. It now **refuses fork heads in the
  trigger gate**, before any checkout: `issue_comment` on a PR looks up
  `isCrossRepository` (a lookup that never answers refuses too, as
  `fork_head=unknown`), review events compare the payload's head repo with
  the repository (an empty head repo — a deleted fork — counts as a fork
  head, not as unknown: no API was asked, so a retry cannot change the
  answer), and anything but a known same-repo head forces `ok=false`
  so nothing downstream runs — checkout, sync, provision, agent, `@auto`
  opt-in and stage moves are all gated on `ok`. The only visible effect is
  one `github-actions[bot]` comment, posted only when the commenter has write
  access (outsiders must not be able to make the workflow post): a known fork
  head gets "use `@review` / push the branch here", once per PR (stubs fire
  on comment edits too); an unknown head gets "the lookup failed, re-trigger"
  every time, and is never told it is from a fork. No sandboxed dev path for
  forks: reviewing fork PRs is the reviewer's job, and "push the branch to
  this repository" is the route to agent work on it.
- The **reviewer** admitted a fork head when a write-access user commented
  `@review`, then checked it out with credentials persisted and executed it
  on the runner before the agent started (its `claude-setup` if the fork
  added one, else the `uv pip install -e` fallback — the fork's build
  backend, as `runner` with sudo and unrestricted egress). The gate now emits
  `fork_head`, and an admitted fork head takes **the external-mode path on
  the PR itself**: `persist-credentials: false` (all modes now), no
  runner-side provisioning, the sandbox install + settings overlay, the
  sandbox provisioning guidance appended to the pr-mode prompt (findings and
  the verdict marker still post to the PR), and the Claude engine only — a
  codex label on a fork head logs a notice and falls through to Claude,
  because codex's `:workspace` profile is not the bubblewrap sandbox.

**Project configuration is stripped from every untrusted checkout** (external
and fork-head alike), at every depth and whatever the entry's type. `.claude/`
(settings with hooks such as `SessionStart` and `PreToolUse` command entries,
`apiKeyHelper`, `env`, sandbox keys, plus `.claude/CLAUDE.md` and
`.claude/rules/`) and `.mcp.json` are *deleted*: Claude Code loads these from
its working directory — the checkout — and hooks and `apiKeyHelper` run
*outside* the Bash sandbox, so the overlay below cannot contain them: a
contributor's `settings.json` could turn the sandbox off or run a command with
the job's credentials before the first prompt. `CLAUDE.md` / `CLAUDE.local.md`
are only instruction text, and the hazard is Claude Code *auto-loading* them
with instruction authority, so they are *moved aside* to `<name>.untrusted` — a
name Claude Code does not load — and the prompt tells the reviewer it may read
them as untrusted data (the project's documented test and lint commands) but
must take no instruction from them; that keeps the convention knowledge
external reviews of the inspect_ai upstream relied on. After the strip, the
caller's `settings` input plus the sandbox overlay are the only configuration
Claude Code sees; changes to any of these files are reviewed from the diff.
The overlay also carries `disableAllHooks: true` as a second, independent
barrier: if the strip's predicates ever miss a hooks-bearing file (a name a
later Claude Code release starts loading), the switch still stops the hooks.
It is defense in depth, not a replacement — the action writes the merged
settings to the *user* scope, which any surviving project-scope settings file
could override, so the strip is what keeps that scope empty.

That gave up real verification, so the reviewer now gets **interactive test
execution inside Claude Code's OS-level Bash sandbox** (bubblewrap + network
proxy on Linux) instead. The principle: the danger was never the agent
*running* code, it's untrusted code running in a context with something to
steal — so isolate the execution, not the agent. Enforcement is on the
running process (and all children), not on the command string, unlike the
permission allow-list.

The pieces, and why each is load-bearing (all in `claude-review.yml`,
external mode and fork heads only — normal same-repo reviews are untouched):

- **Install step**: `bubblewrap` + `socat`, plus the
  `@anthropic-ai/sandbox-runtime` seccomp filter. The filter is *not*
  optional on a GitHub runner: without it sandboxed code can connect to Unix
  domain sockets, and runners expose `/var/run/docker.sock` — a full escape.
  Also lifts Ubuntu ≥ 24.04's AppArmor user-namespace restriction if enforced.
- **Settings overlay** (jq-merged into the caller's `settings` at runtime):
  `failIfUnavailable` (a broken sandbox is a hard failure, never a silent
  fallback to today's exposure); `allowUnsandboxedCommands: false` (kills the
  escape hatch that retries sandbox-blocked commands *outside* the sandbox,
  where the pytest allow rule would wave them through with credentials
  intact); egress limited to PyPI (the proxy decides by client-supplied
  hostname without TLS inspection, so broad domains like `github.com` open
  domain-fronting exfiltration); env-var `deny` entries unsetting the GitHub
  and Anthropic tokens inside the sandbox (sandboxed commands inherit the
  parent env by default — the sandbox alone does not hide it). Explicit
  denies were chosen over `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` so nothing
  changes for normal-mode reviews on caller repos.
- **`excludedCommands: ["gh *"]`**: `gh` runs outside the sandbox so the
  agent can post its findings to the proxy issue. Safe because exclusion
  applies to commands the *agent* issues — a `gh` spawned from sandboxed
  contributor code is a child of a sandboxed process and stays confined and
  credential-less.
- **`persist-credentials: false`** on every reviewer checkout: checkout
  otherwise writes the job token into `.git/config`, inside the workspace the
  sandbox lets contributor code read. Nothing after checkout needs an
  authenticated remote (claude-code-action's agent mode does no fetch; the
  codex path has no network). Since #61 the dev agent and both loops run
  the same way, with their runner-side git calls authenticating per step —
  see No persisted git credentials above.

Residual risks, accepted deliberately: this defends against malicious
contributor *code*, not a prompt-injected *agent* — the agent itself still
holds credentials and an unsandboxed `gh`, a channel that existed in
static-review mode too (it reads untrusted text either way). The PyPI
egress needed for `pip install` is a (narrow) exfiltration path for code
running during the install itself. And `persist-credentials: false` removes
only *checkout's* token: claude-code-action's own prepare step then rewrites
the origin URL to embed the **app token** (`replaceCheckoutCredentials` in
its `git-config.ts`, all modes), so `.git/config` holds a `contents: read` /
`pull-requests: write` credential for the duration of the agent run,
readable by sandboxed code. Its credential-helper alternative is tied to the
action's `allowed_non_write_users` input, which would widen who may trigger
the run — not a trade worth making here; a fix belongs upstream in the
action.

### Branch sync before work

A `@claude merge my branch` run exposed two gaps. First, `git fetch`/`git
merge` weren't allow-listed, so the agent couldn't merge `main` locally and
fell back to the GitHub merges API — and reported the checkout as "shallow,"
which is wrong (`fetch-depth: 0` is full history; a local merge resolves a
common ancestor fine). Second, nothing told the agent to start from current
code, so a stale PR branch produces changes (and test runs) against old code.

Both are fixed in the dev workflow: a **scoped git allow-list**
(`fetch`/`merge`/`rebase`/`push`/`checkout`/`switch`/`branch` plus read-only
`status`/`log`/`diff`/`show`/`rev-parse`/`remote`) lets the agent sync its
branch, and a **`branch_sync_prompt` input** (default: "merge the base branch
in before starting; include that merge when you push") is spliced as
`--append-system-prompt` so it adds to — not replaces — the comment-derived
task. We scoped git rather than granting blanket `git:*`, consistent with the
allow-list-not-denylist stance above; the `contents:write` token is the real
privilege boundary and the fork's branch protection still blocks force-push to
`main`/`meridian`, so the scoping is about injection blast-radius, not the git
plumbing itself.

The merge instruction was originally **gated to follow-up runs only** — runs
that continue an existing branch, where the branch can have drifted from base.
That's exactly the PR-context triggers: an `@claude` comment on a PR
(`github.event.issue.pull_request` is set) or the review / review-comment events
(`github.event.pull_request` is set). A fresh `@claude` from an issue (or a
plain issue comment) has neither, and the action branches off the base —  which
the hourly sync keeps current — so injecting "merge base in" there is pointless
noise. *Since 2026-09-01 the gate is narrower still* (next subsection): the
merge itself happens on the runner, and the prompt is spliced only when the
runner had to hand the merge back to the agent (a conflict, or a fork head).

#### The merge moved to the runner (2026-09-01)

A prompted merge is **advisory**, and the loop workflows never carried the
prompt at all — `claude-auto.yml` and `claude-auto-review.yml` call
claude-code-action directly rather than through `claude.yml`, so
`branch_sync_prompt` never reached a fix round. The codex engine could not
comply even in principle: its sandbox has no network, and its prompt tells it
so. inspect_ai#392 is what this cost — 20 commits over ~15 hours on a branch
11 commits behind `main`, with a one-line `CHANGELOG.md` conflict nothing in
the pipeline could resolve. Because GitHub cannot compute a merge ref for a
conflicted PR, real CI never ran on any of it, and every review round read
stale code.

So the merge is now a **deterministic `Sync branch with base` step** in all
three workflows, running before either engine starts, on PR-context runs only.
The step body lives once, in the `.github/actions/sync-branch` composite
(referenced `@main` like `set-stage`); the two enforcement pieces below are
composites too — `unresolved-merge-guard` and `push-base-merge` — so the three
workflows differ only in their inputs (`claude.yml` passes `checkout: true`
because its checkout is not on the PR head and a `require-file` fence because
its backstop runs `always()` — see below; all three pass the machine account
as `push-token`, since #61 left nothing persisted for a push to ride on), never
in the logic. Four details are load-bearing:

- **The pre-merge tip is what gets stamped.** The landing steps treat "HEAD
  moved past the recorded SHA" as landable work. Stamped *after* the merge, a
  clean merge with no agent edits reads as "nothing to push" and dies with the
  runner — recreating the drift. Stamped before, a merge-only round pushes,
  which is exactly what restores CI.
- **A conflict is handed on differently per engine, and the split is forced.**
  claude-code-action's `setupBranch` runs `git checkout <branch> --`, which
  aborts with "you need to resolve your current index first" whenever the index
  holds unmerged entries. The Claude path therefore must be handed a clean
  index: its conflicted merge is aborted and delegated back to the agent via
  `branch_sync_prompt` (now spliced *only* in that case — on the clean path the
  branch is already current and re-merging is noise). Strictly, the action
  forces this only in `claude.yml`, which runs it in tag mode; the loop
  workflows run it in agent mode (bare `prompt:`, no `track_progress`), whose
  prepare path does not call `setupBranch` (verified in the action's
  `src/modes/agent/index.ts` vs `src/modes/tag/index.ts`). The loops mirror
  the split anyway — one mental model and one prompt across all three
  workflows. Codex, which cannot
  fetch, is handed the merge still in progress and resolves the markers in the
  working tree — and must `git add` each resolved file, because the landing
  step's unmerged-index check is the only thing that catches binary and
  modify/delete conflicts, which have no markers. Caller-specific merge rules
  ride along via a `branch_sync_append` input on all three workflows, and
  reach the agent on both engines **whenever a merge happened this run** —
  not only on a conflict. The fork's append is about a *non-conflicting*
  mis-merge (git auto-merges `CHANGELOG.md` cleanly and lands the entry under
  the wrong heading), so on a clean runner merge it is spliced behind a
  "a workflow step already merged; review the result" lead-in, and on the
  delegated path it follows the merge instruction.
- **Closed and merged PRs are skipped.** claude-code-action's `setupBranch`
  does not continue the old head there — it cuts a fresh branch off base — so
  there is nothing to sync, and the head ref may already be deleted (an
  unconditional fetch would have killed a previously working "`@claude` do X"
  follow-up on a merged PR). The step checks the PR `state` and exits early
  unless it is `OPEN`.
- **The landing step is the enforcement point.** `git add -A` would happily
  stage conflict markers and `git commit` would produce a valid merge commit
  containing them, so an `unresolved-merge-guard` step immediately before
  codex's landing refuses to proceed when `git ls-files --unmerged` is
  non-empty or a `<<<<<<< `/`>>>>>>> ` marker survives in one of the reported
  files (a failed guard skips the landing step; the "Surface agent errors"
  steps read the guard's outcome and post the cause). `=======` is deliberately not matched:
  a bare seven-equals line is a legitimate rST/Markdown heading underline, and
  a repo-wide grep for it would fail honest docs changes.

A `Push base merge if unpushed` backstop covers the Claude path's remaining
hole: if HEAD is still *exactly* the runner's merge commit when the agent
finishes, nothing else will push it, so the workflow does. Gating on that exact
SHA means an agent that committed on top — pushed or deliberately not — is
never second-guessed. If the remote tip moved during the run while HEAD stayed
at the merge (the agent landed its change server-side via the API, or a human
pushed mid-round), the backstop skips instead of failing red on a
non-fast-forward push — the next round re-merges on top of the new tip. In
`claude.yml` the backstop is additionally fenced on the agent having actually
*started* — the composite's `require-file` input names the execution file,
which exists only once Claude ran (the loops leave the input empty: their gate
authorized the actor before checkout). The workflow's own
trigger check deliberately does not mirror claude-code-action's write-access
check on the commenter (the action is the authorizer, and it fails its step
for an outsider), so an unfenced `always()` would have let a non-collaborator's
`@claude` on a public repo's behind PR produce a machine-account merge push
and a CI re-run: a deterministic, low-harm payload, but a write reachable
without write access that did not exist before. A declined or never-started
agent leaves the merge on the runner for the next authorized run. That push
also posts the `@review` re-review request on a successful `@auto` run: the
Claude path's prompt tells
the agent not to request re-review when it made no code changes, so a
merge-only round would otherwise move the head with nobody owing the hand-back
(the loop workflows already cover this with "Ensure hand-back after push"). The
post is its own step, gated on the composite's `pushed` output; like the loops'
step it first checks for a hand-back already posted since the run started (the
agent posting `@review` anyway, or a human requesting a review mid-run) so it
never double-posts, and it retries with backoff. That check matches the hand-off
marker only as a comment's leading line, the same shape as the loops' selfhandoff
detector — claude-code-action's tracking comment is rewritten with the agent's
summary, which on a run editing these workflows may well mention the marker in
prose, and a substring match would read that as a posted hand-back and skip the
one owed. In all three workflows the
backstop's outcome is read by "Surface agent errors": a failed push (or, in
`claude.yml`, hand-back) is commented on the PR and fails the job. In
`claude.yml` that is what lets "Stage - Review (hand-back)" move the board
instead of treating the run as a successful autonomous PR round; in the loops
it keeps the unlanded-work check from misreading the runner's own merge commit
(in `rev-list BASE_SHA..HEAD` with origin still at `BASE_SHA`) as agent edits
that never landed — while still reporting uncommitted tracked-file edits the
agent did leave behind, since that clause preempts the check that would
otherwise have flagged them. One consequence in the CI-fix loop: when the agent gives up
on a failure on a *behind* branch, the merge-only push re-runs CI, which fails
the same way and spends another `fix_attempt_cap` attempt. On a static base
that is exactly one: the next attempt finds the branch up to date, sets no
`merge_sha`, pushes nothing, and stops. On a base that keeps moving (the
fork's `main` is re-synced from upstream hourly, comparable to the length of a
CI-fix cycle) each attempt can merge again, give up again, and push another
merge-only commit, so the bound is `fix_attempt_cap` attempts (default 3), one
per base move. That is the intended cost of restoring a computable merge ref,
not a loop bug. The review loop does not have the analogous leak: its
no-progress escalation compares the tip against the one recorded at the start
of the previous round, which a merge-only push would defeat while the base
moves, so a round whose only product is the runner's merge concludes with the
hand-off rather than a re-review on both engines — the Claude path's prompt
tells a no-change round to post the hand-off (which "Ensure hand-back after
push" honors), and the codex landing step posts it itself when HEAD is still
exactly the runner's merge. A
merge that reports "Already up to date" sets no `merge_sha` at all, so neither
the backstop nor the "review the runner's merge" note fires on a branch that
already contains its base.

The agent's *own* push owes the same hand-back, and in `claude.yml` a second
step, "Ensure hand-back after agent push", guarantees it (the loops' "Ensure
hand-back after push" already does). Observed on fork PR #416 (2026-09-03): an
`@auto` run on a closed-PR continuation pushed its fix to the branch shared
with the upstream PR, then reasoned that the upstream PR was the review surface
and skipped the `@review`; nothing requested a review and the board sat at
Agent. The step compares the head branch's live tip after the run with the
live tip `sync-branch` recorded before the agent ran (its `head_sha`, emitted
*before* the composite's closed-PR skip, since that skip leaves `branch` and
`start_sha` empty in exactly the continuation case — and not the PR's
`headRefOid`, which is frozen at close). A tip comparison, like the loops',
also catches a fast-forward of pre-existing commits, which a commit-date
filter would miss. Fork heads are skipped (nothing can push there, and the
fork's branch name would be looked up in the base repo), and every API read is
fail-safe — `gh api` prints the error body to stdout on a non-2xx, so a
deleted branch or a 5xx skips the check instead of posting a review on a PR
nobody pushed to. The same since-the-run-started check as the backstop's
hand-back keeps the two from double-posting.

A failed sync step (a transient `gh`/fetch error, or the deliberate loud
failure when `git merge` dies without content conflicts) skips every
downstream step rather than failing them, so the "Surface agent errors" steps
read the sync step's outcome explicitly and post the failure to the PR. In
the loop workflows the round/attempt is recorded *after* the sync, so nothing
is burned; without the surfacing, though, the board would sit at Agent with
the label on and nothing running. The sync's API surface is one PR read,
shared by all three callers inside the composite, and it is retried (three
attempts with backoff) — so a single transient API error is not a new way to
lose a round.

### Two prompt-injection sources, one flag

There are two appended-system-prompt sources: `branch_sync_prompt` (the gated
merge instruction above, plus its `branch_sync_append` companion) and
`append_system_prompt` (always-on, empty by default — the channel for
**caller-specific** guidance a shared default can't carry, e.g. the inspect_ai
fork's stub setting CHANGELOG.md rules; see
[shared-instructions.md](shared-instructions.md) for why the fork can't ship a
`CLAUDE.md`). Both feed `--append-system-prompt`, and on a PR follow-up where
the runner handed the merge to the agent (or merged cleanly with an append
configured) both are non-empty at once.

A "Compose appended system prompt" step joins them into **one** value (space-
separated, on one line) so we emit a *single* `--append-system-prompt` flag.
Two flags would bet on undocumented CLI behavior (do repeated flags accumulate,
or does the last win? — the docs don't say), and the gate for `branch_sync_prompt`
lives in that step's env rather than in the claude_args splice. One line also
keeps the `toJSON(...)` splice a clean single-quoted arg: a newline survives
`toJSON` as a literal `\n`, which the action's shell-style arg parsing would
pass through verbatim rather than as a line break.

## The reviewer: auto-review tradeoffs

The reviewer is a separate persona (`@review`, distinct from `@claude` to avoid
substring collision in trigger gates). Design choices:

- **Read-only by token scope** (`contents: read`), not just by prompt — the
  hard boundary. A `deny` overlay on edits/git is belt-and-suspenders.
- **Can run tests** to verify findings, but no write tools. This required
  allow-listing `gh` and the inline-comment MCP so it can actually *post* the
  review — an early version produced a good review that went nowhere because no
  posting tool was allowed.
- **Auto-review triggers on `pull_request`; a `pull_request_target`
  switch was attempted 2026-08-26 and REVERTED 2026-08-27**: Anthropic's
  workload-identity token exchange rejects prt-shaped OIDC subjects
  ("Invalid OIDC token", 2/2 on first exercise — the subject shape had
  never been minted org-wide before). Until the console allowlists that
  shape, reviewer-file PRs skip auto-review (workflow validation) and get
  a manual top-level @review comment instead (decision: Ransom). The
  verify step makes that skip visible (issue #27): when the no-execution-file
  path fires on a pull_request run whose PR touches `.github/workflows/`, it
  posts a nudge comment asking for the manual re-trigger (token backticked so
  the nudge itself can't start a run; once per PR via a marker comment) —
  the bot-actor skip on ordinary PRs stays a log-level notice. The
  reusable KEEPS its dual-event handling and the prt fork-head refusal —
  inert under pull_request, correct if prt ever returns. The original
  prt rationale, kept for that day: The workflow — prompt, permissions, args — resolves from the
  BASE branch, so a PR editing the reviewer's own files still auto-reviews
  and the PR's copy never runs; this also satisfies the app-token exchange's
  server-side workflow validation, which refuses OIDC tokens attesting a
  workflow that differs from the default branch. The trust model: prt runs
  carry base-repo secrets and the reviewer checks out and exercises the PR
  head, so the same-repo gate (enforced twice — the stub's `if:` and the
  reusable trig step's fork-head refusal) is a SECURITY boundary, not an
  ergonomic skip. Same-repo heads imply write-access authors — the same
  trust level the `@review` comment path enforces in the reusable's trig
  check (issue #13): before checkout, the comment path requires a same-repo
  head or a commenter with write access, and external mode always requires
  the trusted commenter (its checkout is an untrusted upstream head; the
  `claude-setup` step is additionally mode-gated so an upstream tree can
  never supply it). No TOCTOU on either path: prt's head *repo* is
  immutable, so its payload gate cannot be raced by later pushes (those
  only add same-repo commits), and the comment path checks out the head
  SHA captured at trust-check time rather than re-resolving
  refs/pull/N/head after the gate. One caveat on the pin: it fixes what
  the gate decided on, so a fork push landing *before* the gate's lookup —
  in the seconds after the maintainer's `@review` — is still the head that
  gets admitted; irreducible, since the trigger comment carries no head
  SHA to compare against. Fork PRs get no auto-review run at all; the
  comment path is the explicit human-decision route, a maintainer's
  `@review` being the same trust decision made explicitly — and since
  issue #59 that admission is about *who may ask*, not about trusting the
  code: an admitted fork head takes the sandboxed path described under
  "Untrusted checkouts" above. The dev agent, which has no sandbox and
  holds write credentials, refuses fork heads outright (same section).
- **Auto-runs on PR `opened`/`reopened`/`ready_for_review`, not `synchronize`.**
  `synchronize` fires on every push, so reviewing on it would re-review (and
  re-bill ~$0.40–1) on every fix commit, including the agent's own. On-demand
  re-review via `@review` is the lighter default. Enabling `synchronize` is the
  knob for continuous review if the manual re-review becomes tedious.
- **Comprehensive per pass, not one finding at a time.** The prompt asks for
  *every* confident finding in a single review (nits included), still behind a
  high-confidence bar. This is mostly for the `@auto` loop: the reviewer runs
  statelessly each round, so a "report only the top issue" bias made it surface
  findings serially — one per round — which burned a whole review→fix round per
  finding and pushed multi-issue PRs into escalation. Batching lets the fixer
  clear them in one round. Some tail is irreducible (a finding only visible
  after an earlier fix lands), which is what the round cap in auto-agent.md
  absorbs.

### Why no automatic reviewer → fixer loop

Acting on a review is intentionally human-mediated. Two mechanisms already
prevent an automatic handoff (the reviewer's comments don't contain `@claude`,
and bot-authored comments are ignored as triggers by default), and we keep it
that way because:
- A fully automatic review↔fix loop risks running unbounded and spending tokens
  unattended.
- The reviewer is confidence-filtered but not infallible; a human deciding
  which findings to act on is the right quality gate (its first real finding was
  a minor typing nit one might reasonably skip).

The loop is: reviewer posts → human triages → `@claude address the feedback`
**on the PR** (so the dev agent pushes to the existing branch rather than
spawning a new one from the issue).

The `@auto` agent ([auto-agent.md](auto-agent.md)) deliberately revisits this
decision — automating the review→fix loop, with the human gate replaced by a
hard round cap (10) plus a no-progress check, an `auto`-label kill-switch, and
opt-in-only triggering.

## Branch protection on the fork

`main` carries a ruleset (the "Meridian Branch" ruleset, scoped to
`refs/heads/main`) blocking deletion, force-push, and **update**, so PRs into
the pristine mirror can't be merged accidentally (`mergeable_state: blocked`).
The bypass is the **repository admin role**, so admins can still merge
deliberately (with GitHub's explicit bypass) and the sync's pushes get through.

`meridian` (the default branch, holding the agent stubs) was originally covered
too — the ruleset included `~DEFAULT_BRANCH` — but that was dropped: protecting
it only forced every operational stub change through a PR (and blocked even
admin-token API writes) without protecting anything pristine, since `meridian`
is not the upstream mirror. Only `main` needs the guard.

The sync therefore pushes via **`SYNC_TOKEN`** — an admin-owned fine-grained
PAT (Contents read/write, that repo only) — because the default workflow
`GITHUB_TOKEN` (the github-actions identity) is not an admin and can't bypass
the ruleset. We wanted a **deploy key** (no personal coupling) but the org
disables them; a PAT is the fallback. It authenticates as the admin user, so
the admin-role bypass applies. The decisive confirmation that the bypass holds
for the PAT comes on the first real upstream-advancing sync after the rule
landed (loud red failure within the hour if not — recoverable, since admin
settings access isn't gated by branch rules).

## Notifications (planned)

The intended Slack story, mostly off-the-shelf:
- **GitHub's official Slack app** gives per-person, self-service notifications:
  `/github signin` links an account → mention DMs; `/github subscribe ...
  workflows` in a channel or DM → failure notifications. No infra.
- "Agent needs input" maps to: agent `@mention`s the issue author/assignee when
  blocked → that person gets a Slack DM. Routing follows GitHub assignment,
  which is already the project-board mechanism.
- No layer gives *mid-run* interactivity (an Actions run can't pause and wait
  for a Slack reply). The reply loop is: notification → comment `@claude <answer>`
  on the PR → new run with full thread context. Tasks needing true
  back-and-forth are better run as Claude Code on the web (which also
  teleports to local VS Code with history — the one thing the Actions path
  can't do).

## Operations reference

### One-time org setup (done; listed for reference / disaster recovery)

1. **Claude GitHub App** installed on `meridianlabs-ai` repos
   (<https://github.com/apps/claude>). Members can request the install; org
   owners (`dragonstyle`, `jjallaire`) approve. Not all repos are covered yet —
   extend access as repos are onboarded.
2. **Workload Identity Federation rule** in the Anthropic Console → Workload
   identity. Issuer: GitHub Actions OIDC. Match: CEL
   `repository_owner == "meridianlabs-ai"`. Target service account
   `claude-code-agent` (`svac_01RL4wYD7ikbypwYKf4wFojv`), which **must be a
   member of** the "Claude Code Agent" workspace (`wrkspc_01RKCQ5DTPBatQ7kHLkaEueD`).
   Org id `be5d0086-bc43-45d2-9184-20ecdd647aa7`, rule `fdrl_01GpNgJm9jE6ZfvcqoJYQL2Y`.
3. **This repo is public** and its Actions access policy is
   organization-accessible (`gh api -X PUT
   repos/meridianlabs-ai/agents/actions/permissions/access -f
   access_level=organization`), so any caller (including public repos like the
   inspect_ai fork) can call its reusable workflows.
4. **`SYNC_TOKEN`** secret on the inspect_ai fork: an admin-owned fine-grained
   PAT (Contents read/write, that repo only) for the upstream-sync workflow.
   Renew before it expires — the sync fails loudly when it lapses.

### Caller requirements (handled by the stubs)

- Grant `id-token: write` at the calling job level — GitHub does not pass OIDC
  tokens to reusable workflows implicitly.
- WIF can't authenticate fork-PR-triggered runs (GitHub withholds OIDC tokens
  from them). The stubs' triggers run in base-repo context, so this only
  affects external-contributor fork PRs, never our internal PRs.

### Spend / model visibility

Usage is attributed to the "Claude Code Agent" workspace in the Anthropic
Console (set rate limits and spend caps there). Per-run served model is in the
job summary ("Model provenance" table; a note lands on the issue/PR when the
fallback fired — see Model selection). Cost is in the same job summary
(`total_cost_usd` from the result, alongside duration and turn count), not an
artifact — the transcript is not uploaded (see No transcript artifacts).

## Open items

- **Slack rollout** — team-side `/github signin`; optional @mention-when-blocked
  instruction in the dev prompt.
- **Full dev-path test** — the dev agent's write path (edit → verify →
  `gh pr create` a draft PR) has been smoke-tested but never run end-to-end
  under the current settings.json permissions. A throwaway "edit a doc + open a
  PR" issue would prove it.
- **Slow tests on the fork** — move the scheduled slow-test suite + triage to
  the fork's `meridian` branch and close the triage → fix loop. Designed in
  [scheduled-tests-on-fork.md](scheduled-tests-on-fork.md); not yet implemented.
- **`meridian-claude` machine account** — enables board assignment
  (`assignee_trigger`) and automated upstream PR promotion. The identity that
  powers `@auto` ([auto-agent.md](auto-agent.md)): a non-`GITHUB_TOKEN` PAT is
  the only way an agent-opened PR triggers CI and `@review`. (Provisioned as
  `marvin` — see [auto-agent.md].) Authorizing it upstream + a workflow there
  would let `@review` run directly on `UKGovernmentBEIS/inspect_ai` PRs —
  designed in [upstream-review.md](upstream-review.md); not rolled out.
- **Shared CLAUDE.md/AGENTS.md across repos** — designed in
  [shared-instructions.md](shared-instructions.md); not yet implemented.
- **Agent work tracking on Atlas** — agents maintain each issue's pipeline stage
  on the org Atlas board (sprint board / issue list / dashboard visibility).
  Designed in [atlas-tracking.md](atlas-tracking.md); not yet implemented.
