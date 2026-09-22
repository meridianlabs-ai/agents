# Security

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository:
[Report a vulnerability](https://github.com/meridianlabs-ai/agents/security/advisories/new)
(enabled 2026-09-18). Never open a public issue or pull request for a
security problem: this repository is public, and its workflows run on every
Meridian repository the moment a fix merges, so a public report is a
public exploit window.

Include what you can of: the workflow, composite action or script involved;
the event or trigger that reaches it; what an attacker controls (a PR head,
a comment, test output, a manifest field); the write or credential it
reaches; and a run link if you have one. Do not include a live token. A
maintainer acknowledges the report in the advisory thread, confirms or
disputes it against `main`, and fixes it through a pull request from a
maintainer's machine, since agents running in CI cannot change workflow
files here. Disclosure is coordinated with you in the same thread.

## What this repository is

Reusable GitHub Actions workflows that run coding agents (Claude Code, or
OpenAI Codex on request) as a dev agent, a reviewer and two autonomous loops,
plus the composite actions and scripts they share. Meridian repositories and
the public `meridianlabs-ai/inspect_ai` fork run them through thin stubs
pinned to this repository's `main`; the `meridianlabs-ai/actions` repository
builds two more agent workflows on the same composites.

## Trust boundaries

Untrusted: the content of any pull request and every fork head; the text of
issues and comments (the fork is public, so any GitHub account can write
there); test output, CI logs and artifacts; dependency alerts and anything
an agent reads or installs while it works; every file an agent job leaves
behind, the landing manifest included.

Trusted: upstream `main` of `UKGovernmentBEIS/inspect_ai` and the default
branches of Meridian repositories; the machine account under its two logins
(`meridian-marvin[bot]`, the GitHub App, and `i-am-marvin`, the User it
replaced); and accounts whose write access a permission lookup has verified.

## Guarantees

Each of these holds on `main`; the ones that can be read from the workflow
text are checked by the tests under `tests/`.

- A job that runs an agent holds no credential of the machine account and no
  token minted from it; its own job token is read-only. The Claude action's
  own token, present while that step runs, is the exception described under
  "By design" below.
- Every write an agent asks the machine account for lands through a manifest that a stdlib
  validator accepts in full, in a fresh job on a fresh runner that checked out
  no code; a refused manifest causes none of the actions it requested. The
  trusted gate's own writes before the agent runs (acknowledgement, stage,
  labels, counters) and the land job's final error report are deterministic
  and not agent-controlled.
- The machine account's tokens are minted per job, for one repository and the
  permissions that job uses, from the GitHub App's secrets, and are revoked at
  job end.
- Whether a comment, label or issue-body line is believed is decided by its
  author's login or verified write access, never by the text itself.
- The GitHub App has no Workflows permission, so a CI agent's commit that
  touches `.github/workflows/` fails at the push.
- The reviewer runs only when a trusted commenter asks for it, except inside
  the `@auto` loop, where the machine account requests each round's review.
- A CI-fix round acts on the PR its failed run is bound to, not on the
  number the caller forwarded: the gate reads the run back from the API
  (this repository's own failed `pull_request` run, on the forwarded
  branch, its latest attempt) and binds it to the one same-repo PR from
  that branch that was open when the run was created, at the run's head
  SHA; the land job derives the same context again before pushing. Two
  PRs sharing a branch, a run re-run, a branch pushed or a PR retargeted
  mid-round stop the round rather than guess. This is a proof that no other
  PR can be the run's origin, not a record of which PR GitHub considered the
  trigger — the API carries none (design/auto-agent.md → Binding the failed
  run to its PR).

## By design, not a finding

- The Claude GitHub App's own installation token is present in the agent job
  while the action step runs: no workflow passes a token to the action, so it
  mints its own, with contents, pull requests and issues write on the caller
  repository whatever the job's own permissions say, and revokes it when the
  step ends. It sits in `.git/config` for that step. On sandboxed reviews it
  is masked there for sandboxed commands, not for the agent's own file
  reads; same-repo reviews run without the sandbox. Everywhere the agent's
  push and posting verbs are denied by settings, but `gh api` is not, so
  those denies are guard rails rather than a complete prohibition on direct
  writes. A write that slipped past would be attributed to `claude[bot]`,
  which the review-fix workflow still accepts as a verdict author: a marked
  verdict comment posted with this token can drive that loop on an otherwise
  eligible `auto` PR. Re-review requests follow the configured allow-lists
  (`review_allowed_bots` defaults to `claude[bot]` in the review-fix gate;
  the reviewer admits that bot when the caller's `allowed_bots` includes it,
  as in the inspect_ai fork). The identity can also reach the CI-fix loop
  indirectly, since a CI failure on an already-authorized `auto` PR starts a
  fix round whatever identity pushed the branch, on the Codex path included.
- A caller without the two app secrets still runs the dev agent, degraded:
  pushes and PRs come from `github-actions[bot]` and trigger nothing. The
  reviewer and the loops fail at their mint step instead.
- CI agents cannot edit workflow files. Changes to `.github/workflows/` are
  made from a maintainer's machine, under a maintainer's review. The land
  job refuses a bundle in which the agent changed them before pushing, with
  a report naming the files; changes the runner's base merge brought in
  pass.
- A Claude reviewer steered by hostile PR content cannot push through its
  landing job, cannot act as the machine account from its own job, and
  cannot have the machine account write outside the caller repository. The
  Claude action's own installation token in the review job remains
  write-capable, and the command denies on it are guard rails rather than a
  complete prohibition on direct writes. Its normal output is the review
  files in its landing
  directory, posted as the review after trigger tokens and loop markers are
  removed, with a verdict that is one of two fixed bodies. Its landing
  manifest is still data the land job acts on: a comment on another thread,
  a caller-repository issue write, a stage move or the adoption of an
  existing branch's PR that a compromised review job requested would post
  as the machine account. A human reads every review.

## Adding or changing a workflow

- No secret of the machine account, and no other secret this repository
  controls, in any job that runs an agent or code from a checkout the org
  does not fully control. Not in `env:`, not as an action input, not through
  a composite. The model credential (Workload Identity Federation, or
  `OPENAI_API_KEY` for codex) and the Claude action's own token are the
  known exceptions.
- No `${{ inputs.* }}`, event text or step output inside a `run:` block; pass
  it through `env:` and expand it as a quoted variable.
- Every author check goes through `TRUSTED_LOGINS` or a permission lookup
  that fails closed; never a substring of a comment or issue body.
- Every write the agent asks the machine account for goes through the
  landing manifest and the `land` composite, from a job that checks out
  nothing. A trusted gate may
  write before the agent runs (acknowledgement, stage, labels, counters),
  never after.
- Mint the machine account's token first in each trusted job, with the
  narrowest `repositories` and `permission-*` inputs, and read it from
  `steps.mint.outputs.token` only.
- Every checkout runs `persist-credentials: false`; a git command that must
  authenticate gets a step-scoped credential helper keyed to
  `github.server_url`.
- Stubs pass secrets as explicit one-key entries, never `secrets: inherit`.
- Run `actionlint` and `python3 -m pytest` before opening the PR, and ask for
  a review on every workflow-editing PR.

## Further reading

[design/credential-separation.md](design/credential-separation.md) describes
the pattern, the manifest contract, the identity and the caller contract as
they stand today. [design/architecture.md](design/architecture.md) holds the
history and the reasoning behind each decision.
