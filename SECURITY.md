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

Each of these holds on `main` and is checked by the tests under `tests/`.

- A job that runs an agent holds no credential of the machine account and no
  token that can push: its job token is read-only.
- Every write an agent causes lands through a manifest that a stdlib
  validator accepts in full, in a fresh job on a fresh runner that checked out
  no code; a manifest with one bad field lands nothing.
- The machine account's tokens are minted per job, for one repository and the
  permissions that job uses, from the GitHub App's secrets, and are revoked at
  job end.
- Whether a comment, label or issue-body line is believed is decided by its
  author's login or verified write access, never by the text itself.
- The GitHub App has no Workflows permission, so a CI agent's commit that
  touches `.github/workflows/` fails at the push.
- The reviewer runs only when a trusted commenter asks for it, except inside
  the `@auto` loop, where the machine account requests each round's review.

## By design, not a finding

- The Claude GitHub App's own installation token is present in the agent job
  while the action step runs: no workflow passes a token to the action, so it
  mints its own, scoped to the caller repository and revoked when the step
  ends. On sandboxed reviews the token is masked in `.git/config` for
  sandboxed commands, and everywhere the agent's push and posting verbs are
  denied. Anything that slipped past would be attributable to `claude[bot]`
  and could not start a loop.
- A caller without the two app secrets still runs the dev agent, degraded:
  pushes and PRs come from `github-actions[bot]` and trigger nothing. The
  reviewer and the loops fail at their mint step instead.
- CI agents cannot edit workflow files. Changes to `.github/workflows/` are
  made from a maintainer's machine, under a maintainer's review.
- A Claude reviewer steered by hostile PR content can write only into its
  landing directory. The land job posts that as the review, after removing
  trigger tokens and loop markers; the verdict is one of two fixed bodies.
  It cannot push, open a PR or write outside the caller repository.

## Adding or changing a workflow

- No secret in any job that runs an agent, or that runs code from a checkout
  the org does not fully control. Not in `env:`, not as an action input,
  not through a composite.
- No `${{ inputs.* }}`, event text or step output inside a `run:` block; pass
  it through `env:` and expand it as a quoted variable.
- Every author check goes through `TRUSTED_LOGINS` or a permission lookup
  that fails closed; never a substring of a comment or issue body.
- Every write goes through the landing manifest and the `land` composite,
  from a job that checks out nothing.
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
