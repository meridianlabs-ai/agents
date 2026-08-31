# The codex engine (`engine:codex`)

Status: v1, built 2026-08-31 (decision: Ransom). OpenAI Codex as an
alternative engine for the dev agent, the reviewer, and both @auto
loops — selected per issue/PR by label, defaulting to Claude.

## Why a label, and why inside the same workflows

The @ commands encode the **verb** (@claude = make changes, @review =
review, @auto = drive the loop); which model runs is **configuration**,
an orthogonal axis. New mentions would multiply tokens across both axes
(@codex, @codex-review, …), and every token is another substring agents
must never emit — the trigger-token-leakage rules already fight that
battle and scale badly with token count. So:

- **`engine:codex` label** on the anchor item (the issue for
  issue-triggered dev runs; the PR for PR runs, reviews, and loops)
  routes that item's runs to Codex. Absent label = Claude. Namespaced
  (`engine:`) because bare labels here already have *trigger* semantics
  (`claude` starts the dev agent, `auto` opts into the loop) — a bare
  `codex` would read as "run codex now" rather than "whichever verb
  fires, use codex". Read via the API at gate time, not from the event
  payload (stale on comment triggers). Relabeling mid-loop deliberately
  switches subsequent rounds.
- **Same reusable workflows**, engine picked by step conditionals. The
  scaffolding around the agent step — trust gates, TOCTOU head-pinning,
  board staging, round counters, hand-back backstops, the landed-work
  guard — is engine-neutral and hard-won; parallel codex workflows
  would fork all of it. Caller stubs are unchanged except for passing
  one extra secret.

## The structural difference: Codex cannot post or push

`openai/codex-action` runs the Codex CLI in a sandbox with **no network
access and no GitHub credentials** (permission profiles `:read-only` /
`:workspace`). Claude Code posts its own comments and pushes its own
branches; Codex cannot. Every GitHub side effect on the codex path
therefore moves into deterministic workflow steps:

| effect            | Claude path                  | Codex path                                      |
| ----------------- | ---------------------------- | ----------------------------------------------- |
| review comment    | agent posts via gh           | workflow posts the structured output            |
| verdict marker    | agent posts (prompt-enforced)| workflow posts from schema verdict (guaranteed) |
| branch + commits  | agent pushes                 | workflow commits/pushes the workspace edits     |
| hand-back @review | agent posts (+ backstop)     | the existing backstop posts it (always owed)    |
| task context (CI logs, review findings) | agent fetches via gh | workflow pre-fetches into the prompt/workspace |

This is a feature as much as a constraint: the verdict marker and the
hand-back become guaranteed instead of prompt-enforced, and the
landed-work-guard class of failures (work stranded on the runner)
cannot happen — the workflow either lands the edits or fails loudly.

### Marker/author contract

Codex output is posted **by the machine account** (MARVIN_TOKEN) so the
marker comments still trigger downstream stubs (github-actions[bot]
comments trigger nothing — recursion guard). The review-fix loop's gate
therefore accepts verdict-marked comments from `i-am-marvin` in
addition to `reviewer_login`; everything downstream keys on the same
`<!-- claude-review-summary -->` / `<!-- claude-review-verdict:* -->`
markers regardless of engine. All codex text posted to GitHub is
de-fanged first (`@review`/`@claude`/`@auto` → backticked) since it
posts from a write-access account.

## Auth — the accepted policy exception

OpenAI has no Workload Identity Federation equivalent, so the codex
engine uses an **`OPENAI_API_KEY` org secret** (created by Ransom,
2026-08-31) — the one exception to this repo's no-API-key-secrets
invariant. Containment: the reusable workflows declare it as an
optional secret, stubs pass it explicitly (never `secrets: inherit`),
and it is exposed ONLY to the `openai/codex-action` step, which uses it
server-side to mint a scoped proxy credential; it never appears in
prompts, sandboxed command environments, or other steps. Repos without
the secret: codex-labeled runs fail at the codex step with a clear
error rather than silently falling back (a silent Claude fallback would
misattribute output).

## v1 limitations (deliberate)

- **External proxy reviews stay on Claude** — their contributor-code
  sandbox overlay is Claude-settings-specific.
- **No inline review comments** from codex reviews: one summary comment
  with file:line references in the body.
- **No review-thread resolution** in codex fix rounds (needs gh); the
  handoff notes it so humans resolve threads at sign-off.
- **No branch sync on dev runs**: codex can't fetch; conflicts surface
  in CI/review as before. (A deterministic pre-merge step is the
  obvious v2 if this bites.)
- **Model**: codex CLI default (no per-repo model input yet).
- The claude-* file/marker names stay — historical, and renaming them
  is churn across every consumer.

## Testing

Create the label per repo (`gh label create engine:codex -c 8250DF -d
"route agent runs to Codex"`), apply it to a scratch issue/PR, then
exercise verbs exactly like the Claude paths (AGENTS.md → Testing a
change): @claude on a labeled issue, @review on a labeled PR, the auto
loop on a labeled PR. The codex-action step log replaces
claude-execution-output.json as the run forensics on codex runs.
