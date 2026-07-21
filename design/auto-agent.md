# `@auto` — the autonomous PR-driving agent (design)

This document designs `@auto`: an agent that takes a task from an issue, opens a
PR, and **drives it toward mergeable on its own** — fixing its own CI failures,
requesting and addressing review, and escalating to a human when it can't
converge. It builds on the existing dev agent (`@claude`,
[`claude.yml`](../.github/workflows/claude.yml)) and reviewer (`@review`,
[`claude-review.yml`](../.github/workflows/claude-review.yml)); read
[architecture.md](architecture.md) first — `@auto` deliberately revisits two
decisions settled there (no automatic reviewer→fixer loop; "secret-free").

Status: **designed, not built.** The chosen approach and the decisions behind it
are recorded here so implementation doesn't relitigate them.

## What `@auto` is

The loop, where every arrow that "wakes" the agent is a *separate* Actions run
(GitHub gives an Actions run no way to pause and wait — see architecture.md
→ Notifications):

```
issue (auto) ─▶ fix ─▶ open PR ─▶ CI + @review fire
                          ▲              │
                          │              ▼
                 push fix ◀── CI fails / review requests changes  (≤ 10 review rounds)
                          │
                          ▼
        CI green + review satisfied ─▶ auto-merge / ping human
                          │
     10 rounds, or a round with no new commit ─▶ drop `auto` label, ping human
```

`@auto` is an **orchestration layer over the existing agents**, not a new model
persona. The code work is still the dev agent; `@auto` is the triggering,
identity, gating, and stop-condition logic around it. `@review` is reused as-is
for the review half.

## The load-bearing constraint: identity

Everything about `@auto` is shaped by one GitHub rule: **actions performed with
the built-in `GITHUB_TOKEN` do not trigger further workflow runs** (GitHub's
recursion guard). A PR opened — or a branch pushed — by `GITHUB_TOKEN` gets **no
CI and no `@review`**. That kills the loop before it starts. (This is *also* why
the pre-`@auto` "just open the PR" attempts failed: see the
[trigger/permission history](#history-how-we-got-here) below.)

Only two identities both create a PR *and* trigger the downstream loop:

1. **The live Claude App token**, *during* a claude-code-action run. No secret,
   triggers CI. But it's **revoked the instant the action's composite step
   ends** ([action.yml "Revoke app token"](https://github.com/anthropics/claude-code-action),
   `if: always() && inputs.github_token == ''`), so no deterministic post-step
   can use it — and getting the agent to reliably run `gh pr create` itself via
   prompt proved unreliable (the action's built-in flow posts a "Create PR" link
   and stops instead).
2. **A machine-account PAT** — a real GitHub identity, so its pushes and PRs
   fire workflows like any human's.

### Decision: machine account + PAT, passed as the action's `github_token`

Provision a `meridian-claude` machine account (already anticipated in
architecture.md → Open items) with a **fine-grained PAT** (`AUTO_TOKEN`:
Contents R/W, Pull requests R/W, Issues R/W, Checks R, on the target repos),
stored as an **org-level Actions secret** scoped to caller repos.

The key move: **pass `AUTO_TOKEN` to claude-code-action as its `github_token`
input.** Per the action's own logic, a caller-provided token is **not revoked**.
That single change yields, in one stroke:

- the agent operates *as* the machine account, so its pushes trigger CI and its
  PR-open triggers `@review`;
- the token stays valid after the action, so a **deterministic post-step opens
  the PR reliably** (no dependence on the model choosing to);
- the same identity later unblocks automated upstream-PR promotion on the fork
  (architecture.md → two-stage PR flow), today a manual human step.

So **"how do we reliably open the PR" and "how does `@auto` drive the loop" have
the same answer** — the machine account. There is no way to get *both*
deterministic PR creation *and* CI on that PR without a non-`GITHUB_TOKEN`
identity; that's a GitHub constraint, not a gap in our code.

### Doesn't this break "secret-free"?

The secret-free invariant is specifically that **the `agents` repo holds no
secrets** (AGENTS.md → Don't). `AUTO_TOKEN` lives as an org/caller-repo Actions
secret — exactly like the fork's `SYNC_TOKEN` already does — and never in this
repo. The invariant holds. Model auth stays Workload Identity Federation,
untouched; the machine account is purely a GitHub identity and needs **no
Anthropic seat**.

## Trigger surface: the `auto` label is opt-in *and* kill-switch

`@auto` activates on either:

- an **`auto` label** on an issue or PR, or
- an **`@auto` mention** in a comment.

The same write-access-author boundary as the other agents applies (an outsider's
label/mention does nothing) — the injection blast-radius argument in
architecture.md → Permissions is unchanged.

The **`auto` label is the canonical "this loop is live" state**, which makes it a
one-click **kill-switch**: every turn re-checks the label *before* doing work, so
**removing the label halts all further turns** immediately. This is cleaner than
a separate stop command and is the mechanism `@auto` itself uses to stop — on
escalation it **removes the label and pings a human**. On kickoff from an issue,
the label is carried onto the PR so the switch stays in one place across the
issue→PR boundary.

## Autonomy ceiling and the round cap

**Decision: drive to mergeable, but cap reviewer rounds at 10.** `@auto` may take
a PR all the way to merge (auto-merge once green + approved), with one hard
limit: **at most 10 review→fix rounds.** If the PR still isn't satisfied after the
tenth round, `@auto` stops, removes the `auto` label, and hands off to a human
with a summary of what's unresolved. (The cap started at 3, was raised to 7 once
the reviewer was made comprehensive per-pass — see architecture.md → reviewer —
and then to 10, so a PR with several genuine, serially-surfaced findings can
converge instead of escalating mid-productive-streak. The no-progress check below
stops a *stuck* loop early, so the higher ceiling only costs rounds when the loop
is actually moving.)

A "round" is one *review→fix* cycle. After each fix push, `@auto` explicitly
**requests re-review** (`@review` does not run on `synchronize` by design —
architecture.md → reviewer — to avoid re-billing every push), and the resulting
review is the next round. Counting explicit review requests, not raw pushes,
keeps CI-only fix iterations from burning the budget.

**Documentation-only nit rounds end the loop instead of re-reviewing** (added
2026-07-21): when every suggestion the fix agent acted on was explicitly
non-blocking (nit-level) AND every change it pushed is documentation-only
(markdown/doc files, code comments, docstrings — zero behavioral code), it
skips `@review` and hands off to a human directly — another review round would
just re-read prose. The judgment lives in the agent's prompt; the *detection*
is deterministic: any agent self-handoff comment starts with the
`<!-- auto-handoff -->` marker, which the workflow keys on (it moves the Atlas
stage to Human Review — see atlas-tracking.md). The same marker covers the
older self-handoff case (all remaining feedback declined with rationale),
which previously ended the loop without any deterministic trace.

**Counting must be deterministic, not LLM-maintained** — it gates whether the
agent runs at all. The orchestration step counts completed review cycles for the
PR via the API (`@review` submissions on this PR) and compares to 3 before
spawning the agent; an `auto-round` indicator is also surfaced in the sticky
status comment for human visibility. (Implementation may instead carry the count
in a label or the comment's hidden marker — open item below — but the *decision*
is: a deterministic gate owns the cap.)

## Re-introducing the bounds the human gate protected

architecture.md → "Why no automatic reviewer→fixer loop" *deliberately* blocked
this loop, for two reasons: unbounded token spend, and keeping a human as the
quality gate. `@auto` opens the loop, so it must replace those protections:

- **Hard round cap (10)** then mandatory human handoff — bounds spend and
  guarantees a human sees anything the agent can't resolve.
- **Label kill-switch** — instant human override at any point.
- **Opt-in only**, by a write-access author — nothing runs unattended without
  someone asking for it.
- **Stuck-detection (implemented)** — the gate records the branch tip at the
  start of each round in the sticky marker (`auto-review-head:<sha>`); if the
  next review fires with the tip unchanged, the last fix round pushed nothing,
  so escalate immediately rather than re-running the agent against an unchanged
  tree. This is what makes the higher round cap safe: a productive loop (every
  round lands a commit) runs to convergence, a stuck one bails on the first
  no-progress round.
- **Flake handling** — don't treat infra/flaky CI failures as fix work; retry
  once, then escalate, so the loop doesn't chase non-determinism.
- **Gate resilience — no silent stall (implemented)** — the deterministic
  "Gate and count" step runs under `set -euo pipefail`, so an unguarded `gh`
  call that fails is fatal. A transient GitHub API failure (5xx / secondary
  rate limit) returns an HTML error page, which `gh --jq` rejects with
  `invalid character '<'`; that aborted the gate *before* it set `act`, so the
  fix/escalate/error-surfacing steps (all gated on `act`) skipped and the loop
  stalled with nothing posted and the label still on — indistinguishable from
  "converged" to an observer (seen on inspect_ai#101 round 4). Two-part fix:
  (a) a `ghr` retry wrapper rides out transient blips on every gate `gh` call;
  (b) a `Surface gate failure` step (`if: always() && steps.gate.outcome ==
  'failure'`) posts a comment when the gate crashes anyway, so a stall is
  visible and recoverable. That comment is deliberately **trigger-free** (no
  literal `@review`/`@auto`): a bot-authored comment carrying a live trigger
  would re-fire the loop and, on a persistent gate failure, spin.
- **Cost visibility** — the per-run `claude-execution-output.json` artifacts
  already make spend auditable after the fact.

## Event-driven implementation sketch

Each is a trigger on the caller repo (delivered to the workflow on its default
branch) that, after re-checking the `auto` label + author + round cap, invokes
the dev agent authenticated as `AUTO_TOKEN`:

1. **Kickoff** — `issues` labeled `auto` / `@auto` comment → fix, push, open PR
   via the deterministic post-step, carry the label onto the PR. An `@auto`
   comment on an *existing* PR runs the same kickoff dev agent against that PR;
   since it fixes-and-pushes rather than opening a PR, claude.yml must, for that
   `@auto`-triggered PR run, both (a) apply the `auto` label to the PR — the
   review-fix loop gates on it, and an existing/human-authored PR won't have it
   — and (b) post `@review` to re-engage the loop (auto-review does not fire on
   push; the more so on the fork, where the `pull_request` family never fires).
   Both are `@auto`-gated PR-context steps in claude.yml.

   **Ordering matters — `@review` must come after the label.** The reviewer's
   summary comment is what fires the review-fix loop, and that loop gates on the
   `auto` label; if `@review` is posted before the label lands, a fast review can
   post its summary while the PR is still unlabeled and the loop silently skips
   it (observed on inspect_ai#52). So: for the **existing-PR** path the label
   step runs *before* the agent (the PR already exists, so it can be labeled up
   front, and the agent's later `@review` is safe). For the **issue→PR-open**
   path the PR doesn't exist until the agent opens it mid-run, so the agent must
   *not* post `@review` itself; instead the Open-or-adopt post-step posts it
   *after* propagating the label (gated by the `request_review_after_open` input,
   which the fork sets — elsewhere PR-open auto-review already covers it).
2. **CI completed** — `check_suite`/`workflow_run` completed=failure on the PR's
   head → if failing, fix and push (CI re-runs because PAT).
3. **Review posted** — `pull_request_review` submitted requesting changes → if
   under the cap, address and push, then re-request review (= next round).
4. **Converged** — CI green + review approved + no unresolved threads → enable
   auto-merge (or ping a human to merge, per repo policy).
5. **Exhausted** — the round cap (10) is reached still unresolved, or a fix round
   makes no progress (no new commit) → summary comment @mentioning the author,
   remove `auto` label, stop.
6. **Re-engaged** — a human explicitly asks `@auto` to keep going on an exhausted
   PR (an `@auto` comment). The kickoff re-applies the `auto` label *and resets
   the sticky round/attempt counters*, so the loop gets a fresh cap (another 10
   review rounds) rather than re-escalating on the leftover count. Without the
   reset, "continue" only buys the single kickoff fix, then the next review sees
   the old count (7) and immediately re-escalates (observed on inspect_ai#53).

## Open questions — verify against claude-code-action before building

Per CLAUDE.md, confirm the action's surface rather than assuming:

- **Programmatic invocation.** `@auto`'s orchestrated turns aren't human
  comments; confirm how to invoke claude-code-action with a constructed prompt
  (and whether its built-in trigger/actor gating — which ignores bot-authored
  events — must be bypassed for machine-account-driven turns).
- **`github_token` ⇒ no revoke** across the action version we pin (currently
  inferred from `action.yml`; pin and re-verify on upgrades).
- **CI-completion trigger** — _resolved: `workflow_run`._ `check_suite` /
  `check_run` are suppressed when the suite was created by GitHub Actions (the
  recursion guard, per GitHub's docs), so they never fire for Actions-based CI.
  `workflow_run(completed)` is the supported reaction; cost is that each caller
  stub names its CI workflow(s). The 3-level `workflow_run` chaining cap doesn't
  bite — marvin's fix push re-triggers CI via a fresh `pull_request` event, not
  a `workflow_run` chain.
- **Auto-merge / merge permission** — whether the machine account's *write*
  level can enable auto-merge under each repo's branch protection (the fork's
  pristine `main` stays human-merged regardless — architecture.md → branch
  protection).
- **Round-count storage** — label vs sticky-comment marker as the deterministic
  counter's home.

## Incremental rollout

The simple case ships first and is independently useful:

- **Phase 0 — reliable PR + CI (the original problem). _Done._** The
  machine-account PAT (`MARVIN_TOKEN`, owned by `marvin@meridianlabs.ai`) is
  passed as `github_token` in `claude.yml`, and the deterministic post-step
  reuses it. This gives reliable issue→PR creation *with* CI and auto-`@review`,
  no loop yet. Stubs pass it explicitly (a one-key `secrets:` map, not `secrets:
  inherit` — least privilege, so an external `@main`-pinned workflow never sees
  the caller's other secrets); each caller repo's deployed stub needs that and
  the org secret scoped to it. (The roadmap called this token `AUTO_TOKEN`; the
  provisioned secret is `MARVIN_TOKEN`.)
- **Phase 1 — CI-failure → fix trigger. _Verified on inspect_flow._** Reusable
  `claude-auto.yml` + `examples/claude-auto-stub.yml`: on `workflow_run` failure
  for an `auto`-labeled same-repo PR, the dev agent (as marvin) reads the failing
  logs, fixes on the branch, and pushes — re-triggering CI. Bounded by a 3-attempt
  cap (deterministic sticky-comment counter, serialized by a per-branch
  `concurrency` group); at the cap it comments and removes the `auto` label. The
  auto-stub needs a per-repo CI-workflow-name edit, so it's installed manually
  (not via `enable-claude.sh`) until @auto matures. Smoke test (planted ruff
  F401, PR authored by a human, `auto`-labeled): marvin fixed it on attempt 1 and
  turned CI green, confirming the prompt-mode push goes out as marvin (so CI
  re-triggers). _Not yet exercised:_ the cap/escalation path (no failure has
  survived 3 attempts) and the fork (only inspect_flow so far).
- **Phase 2 — review→fix loop. _Verified on inspect_flow via the unified
  `issue_comment` trigger._** Reusable `claude-auto-review.yml`: on the
  reviewer's **dedicated marker comment** (`issue_comment`, body contains
  `<!-- claude-review-summary -->`) on an `auto`-labeled, same-repo PR, wake the
  fixer agent (as
  marvin) to address the feedback, push, and re-post `@review` — closing the
  loop. Bounded by a 10-round cap plus a no-progress check (deterministic
  sticky-comment counter that also records the per-round branch tip, sharing
  claude-auto.yml's per-PR `concurrency` group); at the cap — or on the first
  round that pushes no new commit — it comments and removes the `auto` label. **Decisions:** only the automated reviewer drives the
  loop (human reviews are the escalation endpoint, not a turn); whether a review
  needs another round is the fixer agent's judgment. The fixer is **aggressive**:
  it addresses *minor / explicitly non-blocking* suggestions too (nits, naming,
  small refactors, reuse) — the goal is the best-shape PR, not just unblocking —
  and only declines an item it disagrees with / that's out of scope / not a net
  improvement, replying with a rationale. It hands off when nothing remains worth
  improving (clean review, or all remaining items declined-with-rationale). This
  can use more rounds on a nitty review, but the 10-round cap (and the
  no-progress check) still bounds it (then escalate to a human). **Why `issue_comment`, not `pull_request_review`:**
  the latter resolves workflows from the PR base branch, so it never fires on the
  pristine-base fork; `issue_comment` resolves from the default branch and fires
  everywhere — one mechanism for all repos. The reviewer marks its summary
  comment so @auto keys on it, then reads the real review/inline findings via the
  API; cross-repo (fork-of-our-repo) PRs are skipped in the gate (the agent runs
  PR head code under MARVIN_TOKEN). **`allowed_bots` gotcha:** the trigger actor
  is the reviewer bot, so the workflow sets `allowed_bots: "claude"` (never `*`)
  to lift claude-code-action's non-human-actor guard; the gate authorizes first.
  **Atomic-marker gotcha (load-bearing):** `issue_comment:[created]` delivers the
  comment body *as it was at creation*. `@review` builds its summary comment and
  edits it, so the marker often wasn't present yet when `created` fired → the
  trigger silently missed it (a review-fix run skipped despite the final body
  having the marker; the culprit was `created != updated`). Fix: `@review` posts
  the marker as a **dedicated standalone comment via a single `gh pr comment`
  call, never edited**, so the `created` event reliably carries it. We do NOT add
  `types: [edited]` — the reviewer may edit/stream repeatedly, which would fire
  the loop multiple times and blow the round cap. Verified end to end on the
  unified trigger: planted clamp bug → marker comment → review-fix → fix +
  re-request → clean re-review → handoff.
- **Phase 3 — convergence → human handoff. _Verified on inspect_flow (unified
  trigger)._** **Decision: @auto hands off, it does NOT auto-merge** (the
  merge is the one irreversible step; a human keeps it). Two changes, no new
  workflow: (1) `@review` **always posts the dedicated marker comment** (see the
  atomic-marker note above), findings and clean alike — this fires the unified
  `issue_comment` trigger so the clean case no longer goes silent (the Phase 2
  finding). (2) In
  `claude-auto-review.yml`, when the review has converged, it hands off: posts a
  handoff comment @-mentioning the originating human, and does not merge or
  re-request. Grounding: inspect_flow's `main` requires no approvals and
  native auto-merge is off, so no `APPROVE` state or merge machinery is needed.
  _Fork:_ the handoff wording becomes "ready to promote upstream" via the fork's
  append-prompt (fork PRs are never merged in-fork).
  - **Convergence signal is deterministic (revised).** The original v1 used the
    fixer agent's "I pushed no commit" as the clean signal and counted every
    re-review as a round. That under-converged: with the fixer told to address
    non-blocking nits too, it kept finding one more nit each round, re-`@review`d,
    and walked clean PRs to the cap — then escalated with a misleading "still has
    feedback" message (observed on inspect_flow#745, four rounds, all reviews
    `COMMENTED`/clean). Fix, matching the "counting must be deterministic, not
    LLM-maintained" principle: `@review`'s marker comment now carries a
    **verdict** (`<!-- claude-review-verdict:clean|suggestions -->`). The gate
    reads it: `clean` → converge + human handoff **without** spending a round;
    `suggestions` → spend a fix round as before. Absent verdict (older reviewer,
    or a caller overriding `review_prompt`) falls back to the round-count path, so
    the change is backward-compatible. A clean review no longer consumes a round.
  - **Handoff @-mentions the originating human** (author of the PR's
    `Fixes/Closes #N` issue, bots skipped; overridable via the `handoff_mention`
    input) — on **both** the convergence handoff and the cap escalation. The
    escalation previously pinged no one.
- **Fork rollout — _done; verified end to end on meridianlabs-ai/inspect_ai._**
  Auto-stub on the `meridian` branch (Phase 1 keyed to the fork's `Build`; Phase
  2/3 on the `issue_comment` marker — both resolve from the default branch, so
  they fire despite PRs targeting pristine `main`). Validated: planted ruff F401
  → `ci-fix` (workflow_run from `meridian`) fixed it as marvin → `Build` green;
  then `@review` → marker comment → `review-fix` → "ready to promote upstream"
  handoff, no merge, PR left open. Note the fork's `Build` is ~10 min, so each
  loop turn is slower than on a light repo.

## Open follow-ups

- **Stub-gate hardening (non-blocking).** The deployed `review-fix` stub gates
  are routers (`issue_comment` + marker); authoritative gating (reviewer
  identity, same-repo, `auto` label, cap) is in `claude-auto-review.yml` and is
  verified. The example stub adds `github.event.issue.pull_request != ''` as a
  cheap pre-filter (consistency with `claude-review.yml`, reduces forged-marker
  surface); the deployed inspect_flow and fork stubs can be synced to match when
  convenient — purely surface-reduction, not a security gap.

## Kickoff: `@auto` as a distinct trigger

Initially only the *loop* was gated by the `auto` label (on PRs); kickoff stayed
`@claude`/`claude`, so labeling an *issue* `auto` did nothing (the dev agent's
gate didn't match it, and claude-code-action's `label_trigger`/`trigger_phrase`
are single-valued). Now `@auto`/`auto` is a **distinct kickoff that coexists
with `@claude`**: a second dev-agent job (in the caller stub) invokes the same
reusable `claude.yml` with `trigger_phrase: '@auto'`, `label_trigger: 'auto'`,
and the dev agent's PR-open post-step **propagates the `auto` label from the
issue onto the new PR** so the loop engages. `claude`/`@claude` remains the
one-shot assisted mode (PR opened, human drives); `auto`/`@auto` is the
autonomous mode (PR opened, labeled `auto`, loop runs to handoff).

## History: how we got here

The path to "the machine account is required" was a sequence of distinct
failures on issue-triggered runs, each worth recognizing:

- **Prompt nudge ignored** — instructing the agent (via `--append-system-prompt`)
  to run `gh pr create` lost to claude-code-action's built-in issue flow, which
  pushes a branch, posts a "Create PR" link, and stops. → moved to a
  deterministic post-action step.
- **`401 Bad credentials`** — the post-step used the action's `github_token`
  *output*, but the action revokes that app token inside its own composite step,
  so it was already dead. → switched to the default `GITHUB_TOKEN`.
- **`GitHub Actions is not permitted to create or approve pull requests`** —
  `GITHUB_TOKEN` is blocked from creating PRs unless the repo enables
  `can_approve_pull_request_reviews`; and even with it enabled, the resulting PR
  would get **no CI** (the recursion guard). → only a non-`GITHUB_TOKEN`
  identity escapes both, hence the machine-account PAT.
