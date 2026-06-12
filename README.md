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

## One-time org setup

These are already done (or need doing once), not per-repo:

1. **Claude GitHub App** installed org-wide:
   <https://github.com/apps/claude> → install on all `meridianlabs-ai` repos.
2. **`ANTHROPIC_API_KEY` org secret** with visibility that includes private
   repos: <https://github.com/organizations/meridianlabs-ai/settings/secrets/actions>.
3. **Actions access policy** on this repo set to "organization" so other repos
   can call the reusable workflow (done — via
   `gh api -X PUT repos/meridianlabs-ai/agents/actions/permissions/access -f access_level=organization`).

## Updating Claude behavior across all repos

Edit `.github/workflows/claude.yml` here and merge to `main`. All caller repos
pick up the change immediately (stubs reference `@main`).
