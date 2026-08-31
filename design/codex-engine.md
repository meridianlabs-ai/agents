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

Pre-fetching covers the dev verb's PR runs too: the prompt-compose step
embeds the PR title/body and bounded newest-last comment slices (last
12 discussion + 40 inline, the review-fix prep's bounds), so a trigger
like "address the feedback above" carries its referent — codex cannot
read the thread at runtime the way the Claude path does.

This is a feature as much as a constraint: the verdict marker and the
hand-back become guaranteed instead of prompt-enforced, and the
landed-work-guard class of failures (work stranded on the runner)
cannot happen — the workflow either lands the edits or fails loudly.

### Landing semantics

The land steps commit a dirty tree, then treat **HEAD having moved past
the run's start SHA** — not just a dirty tree — as landable work: the
prompts call local commits unnecessary but they are possible, and a
status-only check would silently discard them with the runner. The push
always uses an explicit refspec (`HEAD:refs/heads/<branch>`), so the
intended branch is updated even if codex left HEAD on some other local
ref; a diverted HEAD that isn't genuinely new work fails the push
non-fast-forward — loud, not lossy. Failure parity mirrors the Claude
path throughout: a failed codex step *or* any of its prep steps
(workspace/context prep, and the dev verb's separate prompt-compose)
surfaces a visible error comment and, in the loops, refunds the review
round / CI-fix attempt (infra failures — e.g. a missing
`OPENAI_API_KEY` — must not march a PR toward spurious escalation);
the loops' hand-back backstop keys on the codex *run* step, so a push
that landed before the comment post died still gets its owed
`@review`.

### Marker/author contract

Codex output is posted **by the machine account** (MARVIN_TOKEN) so the
marker comments still trigger downstream stubs (github-actions[bot]
comments trigger nothing — recursion guard). The review-fix loop's gate
therefore accepts verdict-marked comments from `i-am-marvin` in
addition to `reviewer_login`; everything downstream keys on the same
`<!-- claude-review-summary -->` / `<!-- claude-review-verdict:* -->`
markers regardless of engine. All codex text posted to GitHub is
de-fanged first, since it posts from a write-access account:
`@review`/`@claude`/`@auto` are backticked, and the loop's marker
substrings (`claude-review-summary`/`-verdict`, plus the `auto-handoff`
/ `auto-review-rounds` / `auto-review-head` / `auto-fix-attempts`
family) are broken — the gates match them with plain substring
`contains`, so codex merely *quoting* a marker (its prompts embed
verdict-bearing comments) would otherwise forge a verdict or suppress
an owed hand-back. The deterministic workflow-posted comments are the
only marker-bearing ones. Because the widened gate accepts
machine-account verdict comments on Claude-engine loops too, the Claude
fix prompts carry the same rule prompt-side: never emit the marker
substrings verbatim in a comment (the `auto-handoff` first line of the
handoff comment is the one sanctioned use) — the codex paths enforce
this with sed, the Claude path by instruction.

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
- **Codex reviews are static**: the `:read-only` profile means codex
  cannot install dependencies or execute tests to verify findings the
  way the Claude reviewer does — its review is analysis of the checkout
  only. The prompt says so explicitly (so codex doesn't fight the
  sandbox), and the reviewer's claude-setup provisioning step is
  skipped on codex reviews — nothing could use it.
- **CI-trigger parity depends on MARVIN_TOKEN**: codex-path pushes fall
  back to `github.token` where the secret is absent, and those pushes
  do not trigger CI (the Claude path pushes via the app token, which
  does). Repos without the machine account can't run the loops anyway,
  so the gap is dev-verb runs only.
- **No inline review comments** from codex reviews: one summary comment
  with file:line references in the body.
- **No fork-head PRs on codex dev runs**: the landing step pushes to
  origin, where a fork's branch doesn't exist — the prep step declines
  fork PRs loudly rather than failing mid-run.
- **No review-thread resolution** in codex fix rounds (needs gh); the
  handoff notes it so humans resolve threads at sign-off.
- **No branch sync on dev runs**: codex can't fetch; conflicts surface
  in CI/review as before. (A deterministic pre-merge step is the
  obvious v2 if this bites.)
- **Model**: `codex_model` input, default `gpt-5.6-sol` (the strongest
  OpenAI tier; the `gpt-5.6` alias routes there). Reviews additionally pin
  `codex_effort: xhigh` — correctness over turnaround; implementation runs
  (dev agent, both loops) keep the codex default reasoning effort
  (decision: Ransom, 2026-08-31).
- The claude-* file/marker names stay — historical, and renaming them
  is churn across every consumer.

## Testing

Create the label per repo (`gh label create engine:codex -c 8250DF -d
"route agent runs to Codex"`), apply it to a scratch issue/PR, then
exercise verbs exactly like the Claude paths (AGENTS.md → Testing a
change): @claude on a labeled issue, @review on a labeled PR, the auto
loop on a labeled PR. The codex-action step log replaces
claude-execution-output.json as the run forensics on codex runs.
