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
                 push fix ◀── CI fails / review requests changes  (≤ 3 review rounds)
                          │
                          ▼
        CI green + review satisfied ─▶ auto-merge / ping human
                          │
              3 rounds unresolved ─▶ drop `auto` label, ping human
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

## Autonomy ceiling and the 3-round cap

**Decision: drive to mergeable, but cap reviewer rounds at 3.** `@auto` may take
a PR all the way to merge (auto-merge once green + approved), with one hard
limit: **at most 3 review→fix rounds.** If the PR still isn't satisfied after the
third round, `@auto` stops, removes the `auto` label, and hands off to a human
with a summary of what's unresolved.

A "round" is one *review→fix* cycle. After each fix push, `@auto` explicitly
**requests re-review** (`@review` does not run on `synchronize` by design —
architecture.md → reviewer — to avoid re-billing every push), and the resulting
review is the next round. Counting explicit review requests, not raw pushes,
keeps CI-only fix iterations from burning the budget.

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

- **Hard round cap (3)** then mandatory human handoff — bounds spend and
  guarantees a human sees anything the agent can't resolve.
- **Label kill-switch** — instant human override at any point.
- **Opt-in only**, by a write-access author — nothing runs unattended without
  someone asking for it.
- **Stuck-detection** — if a fix round doesn't change the CI/review outcome (no
  forward progress), escalate early rather than spending the full 3 rounds.
- **Flake handling** — don't treat infra/flaky CI failures as fix work; retry
  once, then escalate, so the loop doesn't chase non-determinism.
- **Cost visibility** — the per-run `claude-execution-output.json` artifacts
  already make spend auditable after the fact.

## Event-driven implementation sketch

Each is a trigger on the caller repo (delivered to the workflow on its default
branch) that, after re-checking the `auto` label + author + round cap, invokes
the dev agent authenticated as `AUTO_TOKEN`:

1. **Kickoff** — `issues` labeled `auto` / `@auto` comment → fix, push, open PR
   via the deterministic post-step, carry the label onto the PR.
2. **CI completed** — `check_suite`/`workflow_run` completed=failure on the PR's
   head → if failing, fix and push (CI re-runs because PAT).
3. **Review posted** — `pull_request_review` submitted requesting changes → if
   under the cap, address and push, then re-request review (= next round).
4. **Converged** — CI green + review approved + no unresolved threads → enable
   auto-merge (or ping a human to merge, per repo policy).
5. **Exhausted** — 3rd round still unresolved → summary comment @mentioning the
   author, remove `auto` label, stop.

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
- **Phase 2 — review→fix loop. _Verified on inspect_flow._** Reusable
  `claude-auto-review.yml`: on `pull_request_review` submitted by the automated
  reviewer (`reviewer_login`, default `claude[bot]`) on an `auto`-labeled PR,
  wake the fixer agent (as marvin) to address the feedback, push, and re-post
  `@review` — closing the loop. Bounded by a 3-round cap (deterministic
  sticky-comment counter, sharing claude-auto.yml's per-branch `concurrency`
  group); at the cap it comments and removes the `auto` label. **Decisions:**
  only the automated reviewer drives the loop (human reviews are the escalation
  endpoint, not a turn); whether a review needs another round is the fixer
  agent's judgment (fix + re-request, or "no changes needed" + stop), so
  `@review` is left unchanged. **`allowed_bots` gotcha:** the trigger actor is
  the reviewer bot, so claude-code-action's default non-human-actor guard aborts
  the run — the workflow sets `allowed_bots: "claude"` (never `*`) to lift it for
  that one bot; the gate does the real authorization first. Smoke test (planted
  CI-passing clamp upper-bound bug): `@review` flagged it, marvin fixed it
  correctly + added the suggested test + re-requested review, then `@review`
  came back satisfied and the loop idled at round 1 — clean convergence, no
  runaway, no escalation. **Fork caveat:** `pull_request_review` resolves
  from the PR base branch, so it never fires on the pristine-base fork — that
  surface needs a different re-review trigger, handled with the fork rollout.
- **Phase 3** — convergence → auto-merge / escalation.

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
