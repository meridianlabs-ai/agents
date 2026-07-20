# Agent work tracking on Atlas (design)

This document designs how agent-driven work becomes **visible** — on a board, an
issue list, and a dashboard — by maintaining each issue's pipeline stage on the
org's **Atlas** GitHub Project as work progresses. It builds on the existing
agents ([`claude.yml`](../.github/workflows/claude.yml),
[`claude-review.yml`](../.github/workflows/claude-review.yml),
[`claude-auto.yml`](../.github/workflows/claude-auto.yml)); read
[auto-agent.md](auto-agent.md) first — most stage transitions hang off the same
lifecycle events that drive `@auto`.

Status: **designed, not built.** Recorded so implementation doesn't relitigate
the model.

## Goal

Today an issue's agent state is only legible by reading its comments and hunting
for the PR. We want, at a glance across every Meridian repo:

- a **board** — columns = pipeline stage (one pile of issues, no iterations);
- an **issue list** view — the stage readable in each repo's native issue list;
- a **dashboard** — counts / flow over time.

All three come from one signal if we put it in the right place.

## Substrate: Atlas already fits

Atlas is an **org-level GitHub Projects v2 board** (`meridianlabs-ai` project #1,
`PVT_kwDOC7YMCM4BU68p`) that already spans every repo agents touch (ts-mono,
inspect_ai, inspect_flow, the fork, agents, …) and already carries the fields we
need: a single-select **Status**, **Labels**, **Linked pull requests**,
**Priority/Size**, and **Parent/Sub-issue**. So this is a *layer on Atlas*, not
new infrastructure.

## The stage model

Six states in a mostly-linear pipeline. (We deliberately fold the finer pipeline
— separate design vs. implement phases, and separate design-review vs.
implementation-review gates — into one "Agent working" and one "Human review";
see [Deferred](#deferred).)

```
Todo ─▶ Agent working ─▶ Human review ─▶ Sign-off ─▶ Awaiting Merge ─▶ Done
              ▲                │
              └─── re-engage ──┘   (@claude / @auto again)
```

| Stage | Meaning | Ball is in… |
|---|---|---|
| **Todo** | On the board, no agent has picked it up (the existing `Status: Todo`) | — |
| **Agent working** | An agent is designing/implementing — including opening the PR and running the automated `@review` loop to get it green | agent |
| **Human review** | The agent has handed back and *you* (the driver) review: re-engage the agent, or send it onward. Always the post-agent gate | driver |
| **Sign-off** | You've sent it to an **independent second reviewer**; awaiting their approval. On the fork: promoted upstream, awaiting upstream | second reviewer |
| **Awaiting Merge** | Approved; waiting for the merge action. `hold:release` (below) marks a deliberate hold vs. just-not-merged-yet | whoever merges |
| **Done** | Issue closed / PR merged | — |

Two things that were easy to get wrong:

- **There is no `Agent working → Sign-off`.** Every `@auto` exit — *converged*
  (CI green, bot-review clean) *or* *escalated* (couldn't converge) — hands back
  to **Human review**. From there *you* decide: re-engage the agent (back to
  Agent working) or send it to a reviewer (Sign-off). The automated `@review`
  loop is part of **Agent working**, not a stage — it's the agent iterating on
  its own PR.
- **`Sign-off` and `Awaiting Merge` are separated by the approval.** `Sign-off` =
  *awaiting* the reviewer; `Awaiting Merge` = *got* the approval, awaiting the
  merge. This split is what lets you distinguish "held for a stable release
  point" from "just haven't merged yet" — see [Flags](#orthogonal-flags-human-set).

## Data model: track the issue, link the PR

- **The issue is the unit of work** and the thing on the board. It carries the
  stage through its whole life.
- **The PR is not a separate board item.** Two rows would drift. Link it to the
  issue with a fully-qualified `Fixes owner/repo#N` (the fork already mandates
  this; make it universal). That populates Atlas's **Linked pull requests**
  field, and — because it's a *same-repo* reference — merging the PR auto-closes
  the issue, giving `Awaiting Merge → Done` for free. (Genuinely cross-repo
  issue↔PR pairs don't auto-close; those are rare and fall to the fork-style
  sync or a manual close.)
- **In practice, agent PRs are usually NOT linked** (learned reconciling by
  hand — do not assume the link exists). claude-code-action's "Claude finished"
  comment opens a *draft* PR (or offers a "Create PR" link) with a generic body
  and **no `Fixes` ref**, so most agent PRs have no native issue link and won't
  auto-`Done` on merge. Two consequences for the build:
  - **Enforce the closing ref at PR-open** — the dev agent (or the PR-event
    hook) must add `Fixes owner/repo#N`, or `merge → Done` silently never fires.
    There is **no public API for a non-closing link** (the UI's Development-panel
    link uses a private mutation; verified by schema introspection). The only
    scriptable link is the closing keyword — which also auto-closes, so it's
    correct only for a PR that actually *resolves* the issue, not a design-only
    PR.
  - **The reliable join key is the branch name** `claude/issue-<N>-<timestamp>`,
    which embeds the issue number — use it to associate a PR with its issue when
    no `Fixes` link exists (the same key the fork upstream sync already relies on).
- **Board membership needs no new mechanism.** The team already files most work
  items on Atlas directly, and `set-stage` adds any agent-touched issue itself
  (its `addProjectV2ItemById` step is idempotent). So an issue is on the board
  either because it was filed there or the moment an agent first engages it — no
  dependency on anyone remembering to add it. (A Projects *auto-add workflow* is
  optional; see Prerequisites.)

## Two surfaces for the stage

Put the stage in **two places** that stay in sync, because no single GitHub
surface gives all three views:

1. A dedicated single-select project field **`Stage`** (do **not** overload
   `Status` — it's the org-wide `Todo/In progress/Done` used by lots of
   non-agent work; reshaping it would disrupt everyone). `Status` stays the
   coarse rollup; `Stage` is the agent pipeline. A saved **board view grouped by
   `Stage`** gives the columns-by-stage board over the whole issue pile; Insights
   over `Stage` is the dashboard.
2. A **`stage:*` label** on the issue, because project fields don't render in a
   repo's native issue list but labels do — and agents already manage labels
   fluently. The label is the issue-list view and the agent's cheap write
   surface.

Concrete mapping:

| Stage | `Status` (existing) | `Stage` field (new) | Label |
|---|---|---|---|
| Todo | Todo | *(empty)* | *(none)* |
| Agent working | In progress | Agent working | `stage:agent-working` |
| Human review | In progress | Human review | `stage:human-review` |
| Sign-off | In progress | Sign-off | `stage:sign-off` |
| Awaiting Merge | In progress | Awaiting Merge | `stage:awaiting-merge` |
| Done | Done | *(empty)* | *(none)* |

Todo = open + no `stage:*` label; Done = closed. Only the four active middle
stages carry a label. Human-only items that never involve an agent simply never
get a `Stage`/label and keep using `Status` as before — this overlay is opt-in
by virtue of an agent (or the local promote skill) touching the issue.

## Orthogonal flags (human-set)

Two situations aren't pipeline *positions* — they're pauses/holds that can
overlay whatever stage the work is in. Model them as **labels with a distinct
prefix** (`blocked:` / `hold:`) so they read as modifiers, not stages. Both are
set by a human, not the agent:

- **`blocked:input`** — waiting on another human's input before proceeding
  (common right after Human review). Overlays the current stage. The agent
  **respects** it: `@claude`/`@auto` no-op while it's present, so pinging the
  agent doesn't resume work until you clear it.
- **`hold:release`** — overlays `Awaiting Merge`: approved, but deliberately held
  for a stable release point. Its presence is the differentiator:

  | Situation | How it looks |
  |---|---|
  | Just haven't merged yet | `Awaiting Merge`, no hold label |
  | Deliberately waiting for a stable point | `Awaiting Merge` + `hold:release` |

## Who moves the card, and when

| Transition | Trigger | Driven by |
|---|---|---|
| Todo → Agent working | kickoff run starts | agent post-step (`claude.yml`/`@auto`) |
| Agent working → Human review | `@auto` hands back (converged *or* escalated) | agent post-step (`claude-auto-review.yml`) |
| Human review → Agent working | you re-engage (`@claude`/`@auto`) | agent post-step, next run |
| Human review → Sign-off | you request a second reviewer on the PR | **PR-event hook** (`review_requested`) |
| Sign-off → Awaiting Merge | that reviewer approves | **PR-event hook** (`pull_request_review` approved) |
| Awaiting Merge → Done | PR merged → issue auto-closes | native Projects automation |

- **Native Projects automations** handle the endpoints: item added → `Status:
  Todo`; issue closed / linked PR merged → `Status: Done` + clear `Stage`.
- **Agent post-steps** call `set-stage` at the `@auto` lifecycle events that
  already exist (kickoff, hand-back).
- **The PR-event hook is new.** `review_requested` and `approved` are events on
  the *PR*, but the board item is the *issue* — so a small workflow resolves the
  PR's linked issue and calls `set-stage`. Merge → Done needs no hook (native via
  issue-close).

## The fork: promotion and the terminal sync

The fork is the exception, because its PRs are never merged locally — a human
promotes work by opening an **upstream** PR (`UKGovernmentBEIS/inspect_ai`), and
`Done` comes from upstream merging.

- **`Human review → Sign-off` = the promotion, driven from the local session.**
  You do fork review-and-promote from your local Claude Code session, so that
  session updates Atlas directly: a **skill/instructions** has the local agent,
  when it opens the upstream PR, set the issue to `Sign-off` and record the
  upstream PR URL. No GitHub `@promote` workflow is needed (see
  [Deferred](#deferred)). This is why the owned-repo `review_requested` hook
  doesn't apply on the fork — there's no fork PR reviewer request; the promotion
  is the signal, and it's set locally.
- **The upstream tail needs a poll, not events.** Upstream isn't our repo, so we
  can't hook its reviews/merge (and can't push workflows into it — even the
  narrower authorize-marvin-upstream idea in
  [upstream-review.md](upstream-review.md) is unbuilt). Instead a scheduled job
  in the fork (reuse the existing **Sync upstream** cadence) reads upstream's
  *public* PR state — matching on the shared head branch `meridianlabs-ai:<branch>`
  as the join key — and advances the fork issue's stage from the upstream PR's
  **`reviewDecision` + merge state**: open & unapproved → `Sign-off`,
  **`APPROVED` → `Awaiting Merge`**, merged → `Done`. An upstream merge does
  **not** auto-close the fork issue (cross-org, no `Fixes` link), so the sync
  sets `Done` / closes it explicitly. This is a backstop and can come after the
  initial rollout.
  - **Don't auto-discover the upstream PR via the issue's closing-PR
    references** (learned the hard way). Once promoted, the fork PR is typically
    *closed* (superseded), and the upstream PR usually references the fork issue
    *non-closingly* — so it never appears in `closedByPullRequestsReferences`
    (which surfaces the stale closed fork PR instead). Match on the **recorded
    upstream URL** (from the promote step) or the **head branch**, and read the
    live upstream PR — don't trust the issue's link graph here.

The fork therefore runs the **full** `Sign-off → Awaiting Merge → Done` tail —
driven by the upstream PR's review state, not merge alone (so the poll must read
`reviewDecision`, not just open/merged). Only `hold:release` stays an owned-repo
concept — we don't control upstream's merge timing.

## Stage signals (from hand-reconciling the backlog)

Reconciling existing issues by hand surfaced which signals actually carry the
stage — useful both for the reconcile pass below and as a cross-check for the
event-driven transitions:

- **Don't infer stage from PR draft/open state.** Agents open *draft* PRs and
  then hand off while still draft, so "draft" does not mean Agent working. The
  authoritative signal is the **handoff comment**, not the PR's status.
- **Handoff signals, and where they live** (they differ by trigger):
  - `@claude` one-shot → **`Claude finished @<user>'s task`** comment on the
    **issue** (often with a "Draft PR #NN" / "Create PR" link) → **Human review**.
  - `@auto` converged → **`<!-- auto-converged -->`** comment on the **PR** →
    **Human review**.
  - `@auto` escalated → "**handing this to a human**" comment on the **PR** →
    **Human review**.
  - Loop still running → last agent action is a bare **`@review`** and the
    reviewer's latest verdict is `suggestions` → **Agent working**.
  - Promoted upstream → an **open upstream PR** matches the branch → **Sign-off**.
  A reconcile must read **both** the issue and the PR — `@claude` posts its
  handoff on the issue, `@auto` on the PR.
- **Not every in-progress item is agent work.** Some carry a human-authored WIP
  PR (e.g. a design doc you drafted); those correctly get **no** stage — the
  overlay is opt-in to agent-touched work.

## Reconcile / backfill

The transitions above are event-driven and only fire *going forward*. Existing
issues (and any that drift) need a **reconcile pass** that derives the current
stage from the signals above — handoff comments, the reviewer verdict, the
branch-name join, and upstream branch-match. Worth keeping as a re-runnable
sweep (not just a one-time backfill) to catch items that predate the automation
or slip through. The manual reconcile that seeded the board is exactly this
logic done by hand.

## Implementation sketch

**`set-stage`** — a composite action in this repo
(`.github/actions/set-stage/action.yml`), referenced fully-qualified
(`meridianlabs-ai/agents/.github/actions/set-stage@main`) so the reusable
workflows can call it as a step. Given `issue` (or a PR to resolve to its linked
issue) and `stage`:

1. Ensure the board item: `addProjectV2ItemById` (idempotent) → item id.
2. Set the field: `updateProjectV2ItemFieldValue(projectId, itemId, StageFieldId,
   {singleSelectOptionId})`, and nudge `Status` to `In progress`.
3. Reconcile labels: remove other `stage:*`, add the one for `stage`.

Best-effort/non-fatal: a board-sync hiccup emits a `::warning::` but does not
fail the agent's real work.

**PR-event hook** — a small workflow (in the reusable set, or a caller stub)
triggered on `pull_request` `review_requested` and `pull_request_review`
`submitted`(approved); it resolves the PR's linked issue and calls `set-stage`
(`Sign-off` / `Awaiting Merge`).

Stable IDs to bake in as constants (queried at setup, not per-run):
- project `PVT_kwDOC7YMCM4BU68p`
- `Status` `PVTSSF_lADOC7YMCM4BU68pzhKizZM` + its `In progress` option id.
- `Stage` field `PVTSSF_lADOC7YMCM4BU68pzhYZEwY` — options: Agent working
  `18c9cd89`, Human review `d261eb6b`, Sign-off `da6137e6`, Awaiting Merge
  `add17478`.

## Prerequisites

- **`MARVIN_TOKEN` needs `project` scope** (`read:project` + `project`). The
  built-in `GITHUB_TOKEN` cannot mutate an org Projects v2 board; the agents
  already run as the machine account, so reuse that identity. Verify/rotate the
  PAT before building.
- **Create the `Stage` field** on Atlas with the four active options
  (Agent working / Human review / Sign-off / Awaiting Merge) and capture its
  field id + option ids.
- **Create the labels** in each participating repo (labels must exist per-repo to
  be applied): the four `stage:*` labels plus the `blocked:input` and
  `hold:release` flags. A one-time script — or fold into
  [`scripts/enable-claude.sh`](../scripts/enable-claude.sh) so newly-onboarded
  repos get them.
- **No auto-add workflow needed.** `set-stage` puts agent-touched issues on the
  board itself, and most items are created on Atlas directly anyway. A Projects
  *auto-add workflow* (Atlas → Settings → Workflows) is optional — its only extra
  value is putting a brand-new issue on the board (as *Todo*) before any agent
  touches it — and on GitHub Team a project is capped at **5** auto-add workflows
  (one repo each), fewer than the repos on the board. Skipping it for now.

## Rollout

1. Prereqs above; add `set-stage`.
2. Wire the agent post-step transitions (`Todo`/`Agent working`/`Human review`)
   into the reusable workflows so every caller gets them via `@main`. Pilot on
   one repo (inspect_flow), confirm the board tracks a lifecycle end-to-end.
3. Add the PR-event hook (`Sign-off`/`Awaiting Merge`).
4. Fork: the local promote skill (updates `Sign-off`), then the upstream `→ Done`
   sync as a backstop.

## Deferred

- **Fork `@promote` GitHub automation.** Initially the promotion is driven from
  the local session via a skill (above). A repo-triggered `@promote` command
  (issue interaction → workflow opens the upstream PR + advances the stage) is a
  possible later convenience, not part of the initial implementation.
- **Designing vs. Implementing.** Needs a distinct design phase in the agent flow
  (agent produces a design, not just code). Today it's all "Agent working"; the
  `Stage` single-select can gain options without breaking anything.
- **Design review vs. Implementation review.** Needs a pre-implementation human
  gate (e.g. a `design-approved` label the agent waits on before coding).

## Open questions

- **Draft issues.** Atlas carries many draft items (project-only, no repo issue).
  Agents can't be triggered from them and can't be `Fixes`-linked; treat as out
  of scope — a draft must graduate to a real issue to enter the pipeline.
- **Field vs. label as source of truth.** The `set-stage` step writes both
  atomically, so neither drifts. If they ever disagree, the `Stage` field wins
  (the label is a projection for the issue-list view).
- **Small-team role overlap.** `Human review` (you) and `Sign-off` (a second
  reviewer) may be the same person; the states still read correctly because they
  differ by what's being asked (resume the agent vs. approve to merge), not by
  identity.
