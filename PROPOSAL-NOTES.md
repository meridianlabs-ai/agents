# Proposal notes (for discussion; trim or remove before a PR)

Working notes behind `PROPOSAL.md` and `rules/`. Sources are named here, not in the rule files. "wazoo" is Eric's personal agent config repo; "bot skills" are Eric's cloud-bot rule skills. Both are personal sources: only the generic team policy in them was carried over.

## Where each rule came from

| Rule file / rule | Source |
|---|---|
| pr-review: scope, blocking list, tag blocking/non-blocking, no-blocking = pass, post non-blocking once, fringe = note | Branch `epatey/review-bar` in this repo (`skills/review-pass/SKILL.md`, reviewer prompt in `claude-review.yml`), described there as "the maintainers' agreed review bar". Not merged to main. |
| pr-review: unrun gated test is blocking | wazoo `team/common/gated-tests.md`; bot skills `pr-desk-review` "Gated tests", `pr-desk-approve-and-merge` gate table. **Added to the blocking list by me; not in the review-bar branch.** |
| pr-review: read diff not body; confirm the premise against base | wazoo `memory/pr-review-preferences.md` item 3, `team/common/confirming-the-body.md`; `pr-desk-review` step 2 |
| pr-review: file:line, verify, unverified label | wazoo `team/reviewer/brief_template.md` ("Every finding cites the code"); `pr-desk-review` step 7 |
| pr-review: regression four items | main `claude-review.yml` reviewer prompt (REGRESSION RISK) |
| pr-review: live state | wazoo `memory/reverify-live-state.md`, reviewer brief; `pr-desk-rules` "Re-check live state" |
| pr-review: later-pass status table; wait for handback | wazoo `memory/second-pass-review-status-table.md`, `memory/review-ball-stays-with-author.md`; `pr-desk-review` "Follow-ups" |
| pr-review: second model for security | wazoo `memory/second-model-review-security.md`; `pr-desk-rules` "Claude Code and Codex" (there it is required for every review; softened to Recommended, security only) |
| pr-review: quiesce before human review | wazoo `memory/quiesce-before-human-review.md` |
| pr-review: `### Agent review` section; accept documented maintainer review | wazoo `team/reviewer/brief_template.md` Method 1, `team/README.md` (accept documented review); `pr-desk-review` "Accepting a documented review"; `dev-desk-build` |
| pr-review: redirection as request changes; opinionated; external contributors | wazoo `memory/pr-review-preferences.md` items 1, 4, 5 |
| code-changes: read repo files; check issue open; fresh branch; worktree | wazoo `policies/worker.md`; this repo's `AGENTS.md` (throwaway worktree rule); `dev-desk-rules` |
| code-changes: smallest change, no warning disabling, functional, asserts, one mechanism | wazoo `global/CLAUDE.md` (Eric's coding taste). **Mostly personal taste; I kept the least personal items and put style items under Recommended.** |
| code-changes: no force-push / rebase; no push to default branch | `pr-desk-rules`, `dev-desk-rules`, wazoo reviewer brief; this repo's `AGENTS.md` (main rejects direct pushes) |
| code-changes: merge up only when required | wazoo `team/common/merging-up.md` |
| code-changes: gated tests, per provider, draft until run | wazoo `team/common/gated-tests.md`; `dev-desk-build` |
| code-changes: claims need evidence; lockfile | `pr-desk-rules` "A status claim needs something anyone can check"; wazoo `memory/lockfile-defines-test-env.md` |
| security: untrusted text | `dev-desk-rules`, `canary-wrangler-rules` hard rules |
| security: running contributor code; fork run approval | wazoo `team/common/ci-safety.md` (condensed heavily); `pr-desk-rules` "Running outside contributors' code" |
| security: workflow files need human maintainer approval | `dev-desk-rules`, `canary-wrangler-rules` (Eric 2026-10-26), `pr-desk-approve-and-merge` files check |
| security: `${{ }}` via env; no `secrets: inherit`; least-privilege tokens | this repo's `AGENTS.md` Conventions and README |
| security: pin actions to SHA; 7-day cooldown | this repo's commit 8736ad0 (#208); wazoo `memory/supply-chain-cooldowns.md`; `canary-wrangler-rules` |
| security: admin power | `pr-desk-rules` "Hard rule: never use Eric's admin powers" (generalized) |
| security: no publish/release without approval | `canary-wrangler-rules` |
| security: build/dependency files need review | this repo's README ("let the agents change dependencies", `allow_build_config`) |
| public-writing: ASD-STE100 rules | `dev-desk-rules` "Writing it" (Eric 2026-10-27) |
| public-writing: disclosure prefix, no emoji, approval before posting under a name | `pr-desk-rules` / `dev-desk-rules` "Saying who wrote it"; wazoo `policies/worker.md` publication table |
| public-writing: repo + number references | wazoo `memory/reference-pr-issue-by-repo-name.md` (dropped the inspect_ai fork naming detail) |
| public-writing: trigger tokens and markers | wazoo `memory/auto-token-in-pr-body.md` (by name), `pr-desk-rules`; `claude-auto-review.yml` marker discipline |
| public-writing: no optional feedback; baseline; breaking changes | wazoo `team/README.md` / `no-optional-feedback.md` (referenced, file not in repo), `memory/avoid-grandfathered.md`; `pr-desk-rules` |

## Left out on purpose (Eric-specific or harness-specific)

- Routing (Cletus, PR Desk, Dev Desk, Canary Wrangler stages), Orca/worker lifecycle, STATUS.md, vault, daily notes, memory.
- Author-class table and what each class lets us change on a branch; who may close or request changes; Eric's own-PR latitude. These are team-relevant but encode Eric's authority. See question 9.
- Approve-and-merge policy (when an agent may approve and merge non-workflow PRs). Harnesses differ a lot here. See question 8.
- Tool choices: Claude Code vs Codex launchers, `codex login status`, `new-worktree`, settings deny rules, accounts.
- Slack posting mechanics, bot tokens, channel ids, Work Board ids, Atlas board ids.
- Repo-specific facts: inspect_ai upstream targeting, ts-mono landing order, CHANGELOG placement, restic, branch protection table.
- Chat style rules for talking to Eric (short first reply, etc.).

## Key choices

- **Location `rules/`**, not `templates/AGENTS.md`. `design/shared-instructions.md` proposes vendoring a marker-delimited block into each repo's `AGENTS.md`. That is a repo-level mechanism. This proposal is person/agent-level: each person points their agents at one source, from any harness. The two can coexist; vendoring could later copy from `rules/`.
- **Required / Recommended sections** instead of MUST/SHOULD words, so each sentence stays an STE imperative.
- **Repo files beat shared rules on conflict**, personal rules can only add or tighten.
- **Review bar = the unmerged `epatey/review-bar` bar**, plus the gated-test blocker.
- **Second model review: Recommended, security only.** Eric's bots require it on every review.
- **Rule files carry no source citations**, so they stay short and harness-neutral.

## Open questions for Eric

1. **Layout.** One `rules/` directory with four files, or one file? Separate `rules/` dir, or extend `design/shared-instructions.md` / `templates/AGENTS.md`?
2. **Relation to the vendoring design.** Replace, coexist with, or feed `design/shared-instructions.md`? Should repo `AGENTS.md` files point at `rules/` too?
3. **Who approves changes.** One other human maintainer, or all active maintainers for Required rules? Is there an owner?
4. **Must vs should.** Is the Required/Recommended split right? Which rules move? Candidates to argue about: unrun gated test as blocking; 7-day cooldown as Required; second model review.
5. **Non-blocking findings.** The team bar posts them once. Eric's bots post no optional feedback at all. Which wins in the shared file?
6. **Which repos.** All meridianlabs-ai repos? UKGovernmentBEIS/inspect_ai work by Meridian staff? Personal repos excluded?
7. **Per-person overrides.** Is "add or tighten only; propose loosening as a rule change" acceptable? How does an agent report a conflict?
8. **Agent approve and merge.** Should the shared rules say when an agent may approve or merge (today they only forbid it for workflow PRs and admin bypass)?
9. **Author classes.** Should the author-class table (external / qualified / co-maintainer, what we may change on each branch) be shared policy?
10. **Disclosure format.** Is `<Bot name> (<owner>'s bot):` the team format? Does the GitHub Actions machine account (already a bot login) also need a prefix?
11. **Versioning.** Tags (`rules-vN`) plus CHANGELOG, or track `main` only? Do harnesses pin by default?
12. **First-version review.** One-week window and "approve or no objection" from each active maintainer: right size? Draft PR or GitHub Discussion?
13. **Dogfood.** Should this repo's own `AGENTS.md` and the reusable reviewer prompt load `rules/pr-review.md`, so the CI reviewer uses the same bar?
14. **STE strictness.** STE is Eric's rule for his bots. Is it the team rule for all public text, or Recommended?

## Notes on this repo (surprises)

- The reviewer prompt on `main` (`claude-review.yml`) still asks for "every finding ... including minor and non-blocking nits", and treats any nit as a `suggestions` verdict. The agreed bar (no blocking = pass) lives only on the unmerged `epatey/review-bar` branch (last commit 2026-09-04), with a `skills/review-pass` skill that main does not have.
- `design/shared-instructions.md` (status: proposed, not implemented) already covers cross-repo sharing, by vendoring into each repo's `AGENTS.md`. It concludes that pointing tools at a shared file is not portable. This proposal accepts that cost for person-level setups, where each harness controls its own loading.
- The repo has about 60 remote branches, many stale `claude/issue-*` ones.
- This repo's `AGENTS.md` asks that every standalone PR be added to the Atlas board and assigned. A draft PR for this proposal should state whether that applies.
- Keep trigger tokens out of any PR body for this change. The rule files mention them only as examples in files, which the automation does not read.
