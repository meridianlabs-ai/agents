# Shared agent instructions across Meridian repos — design (proposed)

**Status: proposed, not yet implemented.** This documents the plan for sharing
`CLAUDE.md` / `AGENTS.md` instructions across Meridian repos. Nothing here is
built yet; it's the agreed design to implement when we're ready.

## Goal

Maintain a common set of agent instructions once and have every Meridian repo
pick them up, while (a) supporting both Claude Code and other agents (Cursor,
Codex, Copilot, Gemini CLI, …) and (b) still allowing per-repo additions.

## Constraints (these drive the whole design)

Verified against Claude Code docs and the AGENTS.md spec (June 2026):

- **Claude Code reads `CLAUDE.md`, not `AGENTS.md`.** The supported bridge is a
  `CLAUDE.md` that imports `@AGENTS.md` (or a symlink if there's no
  Claude-specific content).
- **`@path` imports are a Claude-only feature.** The AGENTS.md standard has no
  import syntax — other tools read `AGENTS.md` as flat text. So for cross-tool
  sharing, the shared content must **physically live in each repo's
  `AGENTS.md`**; you cannot point other tools at a shared file.
- **There is no native cross-repo instruction mechanism.** No remote/org-level
  `CLAUDE.md`, no plugin-injected instructions. Managed-settings files and
  `~/.claude/rules` symlinks exist but require per-machine provisioning and
  don't reach CI runners or non-Claude tools — wrong fit.
- Conclusion: the only portable, CI- and tool-universal option is **vendoring a
  copy of the shared content into each repo**, kept in sync by automation.

## Recommended approach: vendor a marker-delimited shared block

Each consumer repo's `AGENTS.md` is a **managed shared block** plus a free
repo-specific section:

```markdown
<!-- BEGIN MERIDIAN SHARED (managed by meridianlabs-ai/agents — do not edit) -->
…shared meridian conventions…
<!-- END MERIDIAN SHARED -->

## <repo>-specific
…build/test commands, architecture, gotchas for this repo…
```

- The canonical shared content lives once here, in `templates/AGENTS.md`.
- A sync mechanism replaces **only** the content between the markers in each
  repo, leaving the repo-specific section untouched, and opens a PR when the
  shared block has drifted.
- Because claude-code-action checks out the repo and Claude Code reads the
  vendored files from it, **CI agents, local VS Code, and other tools all get
  the same shared instructions from the files** — no `--append-system-prompt`
  injection needed for shared content. (Injection stays reserved for genuinely
  dynamic / CI-only context, like the fork's two-stage-flow rules.)

Rejected alternatives: git submodule/subtree (operationally painful);
`@import` of a shared file (Claude-only, breaks other tools); managed-settings
machine file (needs MDM, misses CI and other tools).

## What goes in each file

- **`AGENTS.md`** — tool-agnostic instructions any agent should follow: run
  tests/lint before opening a PR, commit/PR conventions, code-style baseline,
  "don't commit secrets," how the `@claude` / `@review` infra works. This
  carries the *real* shared content, since non-Claude tools can't import.
- **`CLAUDE.md`** — `@AGENTS.md` (pulls in all of the above) plus
  Claude-Code-specific bits that wouldn't apply to other tools: which skills to
  reach for, subagent/hook guidance, plugin pointers. A repo with no
  Claude-specific extras can make `CLAUDE.md` a one-line `@AGENTS.md` import or
  a symlink.

## Keeping the agents repo's own instructions separate from the shared ones

Two distinct things in two locations:

- **`templates/AGENTS.md`** — the canonical *shared* content distributed to
  other repos. A payload, not this repo's own instructions.
- **Root `AGENTS.md` + `CLAUDE.md`** — the agents repo's *own* working
  instructions (workflow-YAML conventions, the `@main` reference contract, how
  to test changes). The agents repo is also a consumer of the shared block, so
  its root `AGENTS.md` would embed the same marker block + its own section
  (eating its own dog food).

Editing `templates/AGENTS.md` changes what every repo gets; editing root
`AGENTS.md` changes only how agents behave when working on the agents repo.

## The inspect_ai fork is a special case

We can't add `CLAUDE.md` / `AGENTS.md` to the fork the normal way: committing to
`main` breaks the pristine mirror, and a file only on `meridian` wouldn't be
present on PR branches cut from `main` (so it'd be invisible during most agent
work). That's why the fork's instructions go through `--append-system-prompt`
today, and they should stay there. Vendor-and-sync applies to the other
Meridian repos we fully control, not the fork.

## Open decisions (resolve at implementation time)

1. **Initial shared content** — what goes in the first cut of
   `templates/AGENTS.md` (basics: test-before-PR, commit/PR style, the
   `@claude`/`@review` workflow — or a fuller set).
2. **Sync direction** — push-based (a workflow in `agents` fans out PRs to
   consumer repos when `templates/AGENTS.md` changes; needs a token with PR
   access to the others) vs. pull-based (each repo re-runs an update step in
   `enable-claude.sh`). Push is more hands-off; pull avoids the cross-repo
   token.

## Implementation sketch (when ready)

- Add `templates/AGENTS.md` (canonical shared content).
- Add root `AGENTS.md` + `CLAUDE.md` to the agents repo (its own instructions;
  the root `AGENTS.md` may embed the shared block once the sync exists).
- Extend `scripts/enable-claude.sh` to also vendor the shared block into a
  repo's `AGENTS.md` (between markers) and add a `CLAUDE.md` importing it.
- Add a drift-check (scheduled workflow or a `--check` mode) that flags repos
  whose shared block lags `templates/AGENTS.md`, or opens update PRs.
