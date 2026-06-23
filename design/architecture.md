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

Lesson encoded in the workflows: every run uploads its
`claude-execution-output.json` as an artifact, so auth and model failures are
diagnosable after the fact.

## Model selection: prefer Fable, fall back gracefully

Default is the `fable` alias with `--fallback-model default`. Claude Code's
`--fallback-model` fires not just on overload but on an **unavailable/retired**
primary, and `default` expands to the account default. So `--model fable
--fallback-model default` means "prefer Fable, degrade to the default if it's
gone" in one invocation, with no pre-flight availability probe.

This was verified the hard way: when Fable became unavailable, a run's init
line still reported `claude-fable-5`, but `modelUsage` in the execution-log
artifact showed every token served by `claude-opus-4-8` — the fallback fired
correctly, and **the init line echoes the *requested* model, not the one that
ran.** Always read `modelUsage` to know what actually executed.

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
  carries no meridian files, so the shim is absent and setup is skipped — the
  reviewer stays on static checks there. Giving it a real env would require an
  extra checkout of `meridian`'s `.github/actions` into a fixed subdir; not done,
  since the fork's diffs are validated by upstream's own CI. For normal
  (non-fork) repos both agents provision identically.

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

The merge instruction is **gated to follow-up runs only** — runs that continue
an existing branch, where the branch can have drifted from base. That's exactly
the PR-context triggers: an `@claude` comment on a PR
(`github.event.issue.pull_request` is set) or the review / review-comment events
(`github.event.pull_request` is set). A fresh `@claude` from an issue (or a
plain issue comment) has neither, and the action branches off the base —  which
the hourly sync keeps current — so injecting "merge base in" there is pointless
noise.

### Two prompt-injection sources, one flag

There are two appended-system-prompt sources: `branch_sync_prompt` (the gated
merge instruction above) and `append_system_prompt` (always-on, empty by
default — the channel for **caller-specific** guidance a shared default can't
carry, e.g. the inspect_ai fork's stub setting CHANGELOG.md rules; see
[shared-instructions.md](shared-instructions.md) for why the fork can't ship a
`CLAUDE.md`). Both feed `--append-system-prompt`, and on a PR follow-up both are
non-empty at once.

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
- **Auto-runs on PR `opened`/`reopened`/`ready_for_review`, not `synchronize`.**
  `synchronize` fires on every push, so reviewing on it would re-review (and
  re-bill ~$0.40–1) on every fix commit, including the agent's own. On-demand
  re-review via `@review` is the lighter default. Enabling `synchronize` is the
  knob for continuous review if the manual re-review becomes tedious.

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
hard 3-round cap, an `auto`-label kill-switch, and opt-in-only triggering.

## Branch protection on the fork

`main` (and `meridian`) carry a ruleset blocking deletion, force-push, and
**update**, so PRs into the pristine mirror can't be merged accidentally
(`mergeable_state: blocked`). The bypass is the **repository admin role**, so
admins can still merge deliberately (with GitHub's explicit bypass) and the
sync's pushes get through.

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
Console (set rate limits and spend caps there). Per-run model and cost are in
the `claude-execution-output.json` artifact each run uploads — read `modelUsage`
for the model that actually ran (the init line echoes the *requested* model).

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
