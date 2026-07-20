# Agent work tracking on Atlas (design)

This document designs how agent-driven work becomes **visible** — on a board, an
issue list, and a dashboard — by having the agents maintain the state of each
issue on the org's **Atlas** GitHub Project as work progresses. It builds on the
existing agents ([`claude.yml`](../.github/workflows/claude.yml),
[`claude-review.yml`](../.github/workflows/claude-review.yml),
[`claude-auto.yml`](../.github/workflows/claude-auto.yml)); read
[auto-agent.md](auto-agent.md) first — the stage transitions hang off the same
lifecycle events that drive `@auto`.

Status: **designed, not built.** Recorded so implementation doesn't relitigate
the model.

## Goal

Today an issue's agent state is only legible by reading its comments and hunting
for the PR. We want, at a glance across every Meridian repo:

- a **sprint board** — columns = pipeline stage, sliced by iteration;
- an **issue list** view — the stage readable in each repo's native issue list;
- a **dashboard** — counts / flow over time.

All three come from one signal if we put it in the right place.

## Substrate: Atlas already fits

Atlas is an **org-level GitHub Projects v2 board** (`meridianlabs-ai` project #1,
`PVT_kwDOC7YMCM4BU68p`) that already spans every repo agents touch (ts-mono,
inspect_ai, inspect_flow, the fork, agents, …) and already carries the fields we
need: a single-select **Status**, an **Iteration** (sprint) field, **Labels**,
**Linked pull requests**, **Priority/Size**, and **Parent/Sub-issue**. So this is
a *layer on Atlas*, not new infrastructure.

## The stage model

Five states. (We deliberately fold the finer pipeline — separate design vs.
implement phases, and separate design-review vs. implementation-review gates —
into one "agent working" and one "human review"; see
[Deferred](#deferred--the-finer-pipeline).)

```
Unstarted ─▶ Agent working ─▶ Sign-off ─▶ Done
                  ⇅                │
             Human review          └── changes requested ─▶ Agent working
```

| Stage | Meaning | Ball is in… |
|---|---|---|
| **Unstarted** | On the board, no agent has picked it up | — |
| **Agent working** | An agent is designing/implementing — including opening the PR and running the automated `@review` loop to get it green | agent |
| **Human review** | The driving human must make a decision: a design/approach gate, or an `@auto` escalation the agent couldn't resolve. Loops back to Agent working when they re-engage | driving human |
| **Sign-off** | Work is complete and green; an **independent second human** approves it to merge — on the fork, promotes it upstream | second human |
| **Done** | Issue closed / PR merged | — |

The automated `@review` review→fix loop is **part of Agent working**, not a
stage of its own — it's the agent iterating on its own PR. `Human review` and
`Sign-off` are the two distinct human touchpoints: `Human review` is the driver
*unblocking* the agent (and loops back to Agent working), `Sign-off` is an
*independent* approver taking the final step. `@auto`'s convergence handoff
enters `Sign-off`; its escalation/cap handoff enters `Human review`. Today's
simple flow can go **Agent working → Sign-off directly** (no pre-PR gate); the
design gate that would routinely use `Human review` is
[Deferred](#deferred--the-finer-pipeline).

## Data model: track the issue, link the PR

- **The issue is the unit of work** and the thing on the board. It carries the
  stage through its whole life.
- **The PR is not a separate board item.** Two rows would drift. Link it to the
  issue with a fully-qualified `Fixes owner/repo#N` (the fork already mandates
  this; make it universal). That populates Atlas's **Linked pull requests**
  field, works cross-repo because Atlas is org-level, and gives
  PR-merge → issue-close → `Done` for free.
- **Auto-add to Atlas** via a per-repo Projects *auto-add workflow* (filter to
  `auto`/`claude`-labelled issues, or all new issues) so nothing depends on a
  human remembering to add the item.

## Two surfaces for the stage

Put the stage in **two places** that stay in sync, because no single GitHub
surface gives all three views:

1. A dedicated single-select project field **`Stage`** (do **not** overload
   `Status` — it's the org-wide `Todo/In progress/Done` used by lots of
   non-agent work; reshaping it would disrupt everyone). `Status` stays the
   coarse rollup; `Stage` is the agent pipeline. A saved **board view grouped by
   `Stage`, sliced by `Iteration`** is the sprint board; Insights over `Stage` is
   the dashboard.
2. A **`stage:*` label** on the issue, because project fields don't render in a
   repo's native issue list but labels do — and agents already manage labels
   fluently. The label is the issue-list view and the agent's cheap write
   surface.

Concrete mapping:

| Stage | `Status` (existing) | `Stage` field (new) | Label |
|---|---|---|---|
| Unstarted | Todo | *(empty)* | *(none)* |
| Agent working | In progress | Agent working | `stage:agent-working` |
| Human review | In progress | Human review | `stage:human-review` |
| Sign-off | In progress | Sign-off | `stage:sign-off` |
| Done | Done | *(empty)* | *(none)* |

Unstarted = open + no `stage:*` label; Done = closed. Only the three active
middle states carry a label. Human-only items that never involve an agent simply
never get a `Stage`/label and keep using `Status` as before — this overlay is
opt-in by virtue of an agent touching the issue.

## Who moves the card, and when

Split by what GitHub can detect natively vs. what needs agent knowledge.

**Native Projects automations** (configured once on Atlas) handle the endpoints:
- item added → `Status: Todo` (Unstarted);
- issue closed / linked PR merged → `Status: Done` + clear `Stage`.

**Agents** set the middle stages from the reusable-workflow **post-steps**, at
the lifecycle events that already exist:

| Transition | Where it fires (existing step) |
|---|---|
| → Agent working | `claude.yml` / `@auto` kickoff, at run start (covers PR open + the `@review` loop) |
| → Sign-off | `claude-auto-review.yml` convergence handoff (CI green, reviewer satisfied) |
| → Human review | `claude-auto-review.yml` escalation/cap handoff (agent couldn't converge); future design gate |
| Human review → Agent working | next `@claude`/`@auto` run (human re-engaged) |

The agent side is a single idempotent operation — *set stage S on issue I* —
which (a) ensures the issue is on Atlas, (b) sets the `Stage` field option, and
(c) reconciles the `stage:*` labels. Factor it into **one reusable step** (a
composite action or a tiny `set-stage.yml` reusable workflow) that every agent
workflow calls, rather than duplicating GraphQL in each.

## Implementation sketch

A reusable `set-stage` step, given `issue` (owner/repo#N or PR) and `stage`:

1. Resolve/ensure the board item:
   `addProjectV2ItemById` (idempotent — returns the existing item if present),
   giving the item id.
2. Set the field:
   `updateProjectV2ItemFieldValue(projectId, itemId, fieldId, {singleSelectOptionId})`.
3. Reconcile labels: remove other `stage:*`, add the one for `stage`.

Stable IDs to bake in as constants (queried at setup, not per-run):
- project `PVT_kwDOC7YMCM4BU68p`
- `Status` `PVTSSF_lADOC7YMCM4BU68pzhKizZM`, `Iteration`
  `PVTIF_lADOC7YMCM4BU68pzhKjCbs`, `Linked pull requests`
  `PVTF_lADOC7YMCM4BU68pzhKizZk`
- `Stage` field id + per-option ids — **created at setup** (see Prerequisites).

## Prerequisites

- **`MARVIN_TOKEN` needs `project` scope** (`read:project` + `project`). The
  built-in `GITHUB_TOKEN` cannot mutate an org Projects v2 board, and the agents
  already run as the machine account for their pushes — reuse that identity.
  Verify/rotate the PAT before building.
- **Create the `Stage` field** on Atlas with the four active options and capture
  its field id + option ids.
- **Create the `stage:*` labels** in each participating repo (labels must exist
  per-repo to be applied). A one-time script — or fold into
  [`scripts/enable-claude.sh`](../scripts/enable-claude.sh) so newly-onboarded
  repos get them.
- **Per-repo auto-add workflow** on Atlas (mind the auto-add workflow cap).

## Rollout

Pilot on one repo (inspect_flow or the fork), confirm the board reflects a full
issue lifecycle end-to-end, then fold the `set-stage` step into the reusable
workflows so every caller gets it via `@main`.

## Deferred — the finer pipeline

The `Stage` single-select can gain options without breaking anything, so the
folded states can re-expand when the workflow supports them:

- **Designing vs. Implementing** — needs a distinct design phase in the agent
  flow (agent produces a design, not just code). Not built; today it's all
  "Agent working."
- **Design review vs. Implementation review** — needs a pre-implementation human
  gate (e.g. a `design-approved` label the agent waits on before coding). See the
  gated-workflow option discussed when this was scoped.

## Open questions

- **Draft issues.** Atlas carries many draft items (project-only, no repo issue).
  Agents can't be triggered from them and can't be `Fixes`-linked; treat as out
  of scope — a draft must graduate to a real issue to enter the pipeline.
- **Two human states, distinguished by *who*.** `Human review` is the driver
  unblocking the agent (and loops back to Agent working); `Sign-off` is an
  independent second human approving the finished work. On a small team the same
  person may play both roles — the states still read correctly, since they
  differ by what's being asked (a decision that resumes the agent vs. a final
  approval that ends the work), not strictly by identity.
- **Field vs. label as source of truth.** Proposed: the `set-stage` step writes
  both atomically, so neither drifts. If they ever disagree, the `Stage` field
  wins (the label is a projection for the issue-list view).
