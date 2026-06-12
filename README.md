# agents

Shared agent infrastructure for Meridian repos: Claude Code workflows, rollout
scripts, and (eventually) shared skills and plugins.

## Layout

- `.github/workflows/claude.yml` — reusable workflow wrapping
  [claude-code-action](https://github.com/anthropics/claude-code-action).
  All Claude configuration lives here; caller repos carry only a thin stub.
- `examples/claude-stub.yml` — the stub to copy into a repo's
  `.github/workflows/claude.yml`.
- `scripts/enable-claude.sh` — opens a PR adding the stub to a repo.

## Enabling Claude in a repo

```sh
scripts/enable-claude.sh meridianlabs-ai/<repo>
```

Then merge the PR. Trigger Claude by:

- Mentioning `@claude` in an issue or PR comment
- Adding the `claude` label to an issue (works from project board views)

## Authentication

Auth uses **Anthropic Workload Identity Federation** — no API keys or secrets
anywhere. Each workflow run exchanges its GitHub OIDC token (`id-token: write`)
for a short-lived Anthropic credential scoped to the meridian service account
and workspace, where usage can be tracked, rate-limited, and capped in the
Anthropic Console. The federation rule / org / service account / workspace IDs
in the reusable workflow are identifiers, not secrets.

Caveats:

- Callers must grant `id-token: write` at the calling job level (the stub
  does); GitHub does not pass OIDC tokens to reusable workflows implicitly.
- GitHub does not issue OIDC tokens to workflows triggered by fork PRs, so
  fork-PR-triggered runs cannot authenticate. The stub's triggers all run in
  base-repo context, so this only matters for custom `pull_request` triggers.

## One-time org setup

These are already done (or need doing once), not per-repo:

1. **Claude GitHub App** installed org-wide:
   <https://github.com/apps/claude> → install on all `meridianlabs-ai` repos.
2. **Workload Identity Federation rule** in the Anthropic Console trusting
   GitHub Actions OIDC for this org (done — IDs are in
   `.github/workflows/claude.yml`).
3. **Actions access policy** on this repo set to "organization" so other repos
   can call the reusable workflow (done — via
   `gh api -X PUT repos/meridianlabs-ai/agents/actions/permissions/access -f access_level=organization`).

## Updating Claude behavior across all repos

Edit `.github/workflows/claude.yml` here and merge to `main`. All caller repos
pick up the change immediately (stubs reference `@main`).
