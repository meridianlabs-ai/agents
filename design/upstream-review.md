# Reviewing upstream `inspect_ai` PRs with Marvin (design)

Goal: let an authorized Meridian person invoke Claude **directly on a PR in
`UKGovernmentBEIS/inspect_ai`** — starting with `@review`, later `@claude`/`@auto`
— with the work billed to Meridian's Anthropic workspace and gated so that **only
named Meridian people** can trigger it.

Status: **designed, not rolled out.** Two prerequisites are external (upstream's
agreement to host a workflow; Marvin's credential authorized upstream), so this
records the architecture and threat model to revisit when those land. It is the
concrete form of the "cross-repo `meridian-claude`" capability that
[architecture.md](architecture.md) → two-stage PR flow / Open items deferred.

## What changed since the fork strategy

The fork ([architecture.md](architecture.md) → The inspect_ai fork) exists
because we assumed we **cannot run workflows, store secrets, or react to events**
in `UKGovernmentBEIS/inspect_ai`. If upstream will instead **merge a workflow we
author** and **authorize Marvin's credential**, that assumption is lifted for
this use case: upstream becomes a (near) normal *caller repo*, and an `@review`
comment on an upstream PR can fire our reusable reviewer there directly — the
@-mention-on-the-PR UX the fork-bridge alternative (below) can't provide.

This does **not** retire the fork. The fork still owns: pristine-`main` so PR
diffs equal the upstream diff, the hourly sync, scheduled tests, and the place
Meridian's autonomous *dev* work happens. This design adds upstream *review
interaction* on top; extending dev/`@auto` upstream is a riskier later step
(see Phasing).

## Architecture: upstream as a caller repo

1. A `claude-review.yml` **stub on upstream's default branch** (merged by
   upstream), triggering on `issue_comment` (`@review`).
2. A Meridian person comments `@review` on an upstream PR → the stub fires →
   calls the reusable `claude-review.yml@main` in this repo → the reviewer reads
   the PR, posts its review on the upstream PR.

`issue_comment` resolves workflows from the repo's default branch, so it fires
for comments on any PR — no per-PR-branch placement needed (unlike
`pull_request_review`). The same mechanics we already run on inspect_flow.

### Why not the fork-bridge alternative
Without a workflow upstream, the only option is a `workflow_dispatch` in a
Meridian repo that checks out the public upstream PR and posts back via Marvin's
token. That works but **can't be invoked by `@`-mentioning the upstream PR**
(nothing upstream reaches us), which is the whole ask. Keep it documented as the
fallback if upstream declines to host a workflow.

## Authentication: extend the WIF rule (this is "bill Meridian")

The federation rule today accepts OIDC only where
`repository_owner == "meridianlabs-ai"`; an upstream run (owner
`UKGovernmentBEIS`) is rejected. Extend the rule's CEL to also accept the **one
repo**:

```
repository_owner == "meridianlabs-ai" || repository == "UKGovernmentBEIS/inspect_ai"
```

Then upstream runs authenticate to **Meridian's Anthropic workspace via OIDC** —
metered/capped on our side, **no API-key secret living in a repo we don't own**.
Scope to the single repo, never `UKGovernmentBEIS` org-wide. (Console change;
see architecture.md → Operations.)

Consequence: anyone who can *trigger a run* in upstream-`inspect_ai` spends
Meridian's Anthropic budget — which is exactly why the trigger must be tightly
gated (below).

## Identity for posting

- **Read-only review:** the upstream workflow's default `GITHUB_TOKEN` can post
  the review (as upstream's `github-actions[bot]`) — no Marvin credential needed.
- **Posting as Marvin** (recognizable identity, and the basis for a future
  write-loop): pass Marvin's upstream-authorized PAT as `github_token`. If
  upstream adds Marvin and enables fine-grained PATs, a **fine-grained PAT scoped
  to just `inspect_ai`** is far tighter than the classic `public_repo` PAT the
  no-cooperation path would need. Prefer fine-grained.

## Authorization: Meridian-only (the load-bearing control)

This is **not** automatic and is the key difference from our own repos.
claude-code-action's default gate is "the commenter has **write access** to the
repo" — upstream, that's **UKGovernmentBEIS maintainers**, not Meridian. Since
runs bill our budget and act under our identity, we must restrict to *our* people:

- Gate the stub's `if` to an **explicit Meridian allowlist** (usernames, or a
  Meridian team via API check) — *instead of* a write-access check.
- Set the action's **`allowed_non_write_users`** to that same list, so Meridian
  people can invoke even though they're not upstream collaborators.
- Result: only the named Meridian people's `@review` fires it; upstream
  maintainers and the public are ignored. Removing someone from the list revokes
  their access.

A username allowlist is maintenance (people change); a Meridian GitHub **team**
membership check is the more durable form if we can read team membership from
the run.

## Threat model

The risk profile is higher than our own repos because the workflow runs inside a
repo we don't own, on PRs from the public, billed to us.

- **Untrusted PR code + credentials (pwn-request).** Reviewing a PR means
  checking out — and, to verify findings, *running* — a contributor's code while
  WIF/model creds and a GitHub token are in scope. A malicious PR can attempt to
  exfiltrate them. Worst cases: abuse Meridian's Anthropic budget; misuse the
  PR-write token. Mitigations: minimal job `permissions` (`contents: read`,
  `pull-requests: write` only); the reviewer is already read-only (deny
  edit/git-write); prefer **not executing untrusted tests** for upstream review,
  or split *analyze* (no GitHub-write token, untrusted code) from *post* (token,
  no code execution). The OIDC→Anthropic credential is short-lived and not a
  GitHub-write token, which limits — but doesn't eliminate — the blast radius.
- **Budget abuse.** WIF-broadening means upstream runs draw our Anthropic
  budget; the Meridian-only trigger gate is the primary control, backed by the
  workspace's rate/spend caps.
- **Identity misuse.** Anything posted runs as our `github-actions[bot]` or
  Marvin; a trigger-gate bypass would let someone speak as us on a public repo.
  The allowlist + write-gate are the defense.
- **Trigger-gate bypass surface.** A cheap `if` allowlist in the stub plus the
  action's `allowed_non_write_users` are belt-and-suspenders; neither alone
  should be trusted (the action's own actor check still applies).

## What it takes (prerequisites)

1. **Upstream agrees to host the workflow** — a governance ask: a Meridian-run,
   OIDC-to-our-workspace, Meridian-gated Claude reviewer living in their repo.
2. **Marvin authorized upstream** — collaborator access and/or a fine-grained
   PAT grant for `inspect_ai` (only if we want to post as Marvin; read-only
   review can use the default token).
3. **WIF rule extended** to accept `UKGovernmentBEIS/inspect_ai` (Console).
4. **The gated stub** authored: `issue_comment` trigger, Meridian-only `if` +
   `allowed_non_write_users`, minimal permissions, read-only reviewer.

None of these is done; (1) and (2) are external and gate the rest.

## Phasing

- **Phase A — `@review` only.** Read-only, lowest risk, highest value, and the
  thing asked for. Validate the trigger-gate, billing, and posting before more.
- **Phase B — `@claude`/`@auto` upstream.** Adds a *write* loop (push fixes,
  open/iterate PRs) on a public repo we don't own — materially higher risk
  (untrusted-code execution with write creds, recursion-guard/identity concerns
  from [auto-agent.md](auto-agent.md)). Only after Phase A is proven and with
  explicit upstream agreement on scope.

## Open questions

- Allowlist as static usernames vs a Meridian team membership check at runtime.
- Whether to run the reviewer's verify step (tests) on untrusted upstream code
  at all, or restrict upstream review to static analysis + the analyze/post
  split.
- Whether upstream prefers the workflow target only *Meridian-promoted* PRs
  (lower risk) rather than arbitrary community PRs.
