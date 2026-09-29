# The untrusted agent job

Status: proposed, 2026-09-29. Issue: none (task from Ransom, 2026-09-29).
Author: agent (Claude), reviewed by Codex; see the PR. Revisits
[executed-paths-residual.md](executed-paths-residual.md), whose uid
boundary this design keeps as defence in depth and stops relying on.

Disclosure: this design names open Claude Security findings with full
detail in a public repository. Ransom chose that (decision: Ransom,
2026-09-29), despite SECURITY.md's "no public pull request for a security
problem".

## Why

The agent jobs run the agent as an unprivileged user (`claude-agent` or
`codex`). The job's `runner` uid, which runs everything else in the job,
holds more than the agent may have:

- **The Claude GitHub App's installation token.** None of the four
  reusable workflows passes `github_token` to claude-code-action, so the
  action mints the App token itself. That token has contents, pull
  requests and issues write on the caller repository. The agent jobs'
  permission comment says so: `id-token: write  # WIF auth (and the
  action's own App token)` (claude.yml:1120).
- **The means to mint that token again.** The action gets the token by
  exchanging an OIDC JWT with audience `claude-code-github-action` at
  `https://api.anthropic.com/api/github/github-app-token-exchange`. The
  exchange request carries the JWT and nothing else, no secret (source
  below). So any process running as `runner` in a job with
  `id-token: write` can mint a Claude App token for the caller repository
  for as long as the job runs, and can choose the permissions body. The
  four **codex** jobs grant `id-token: write` too (claude.yml:2249,
  claude-review.yml:2222, claude-auto.yml:1777, claude-auto-review.yml:2067),
  although nothing in them uses OIDC.
- **`OPENAI_API_KEY`** in the codex jobs. It sits in the job message, which
  root can read out of `Runner.Worker`'s memory (root-boundary-smoke.yml's
  control step shows that memory reachable through root). The runner keeps
  passwordless sudo.
- **`claude[bot]` is a trusted identity.** The review-fix loop accepts it
  as a verdict author (`REVIEWER_LOGINS: i-am-marvin,meridian-marvin[bot],claude[bot]`,
  claude-auto-review.yml:318; the `reviewer_login` and `review_allowed_bots`
  defaults, :62-82). The inspect_ai fork's reviewer stub admits its
  `@review` (`allowed_bots: "claude[bot]"`, inspect_ai@c6940f0a
  `.github/workflows/claude-review.yml:127`). `skills/promote/promote.sh:101`
  trusts its verdicts. So a minted App token can post a verdict the loop
  believes, as well as push to any branch the App may write.

So the boundary today is the uid: nothing the agent does may reach
`runner`. That boundary has five open findings from the Claude Security
scan of 609e20d (created 2026-09-25). Every one of them is an agent-uid to
`runner` step whose payoff is the App token and, on codex jobs, the key:

| Finding | Severity | What reaches `runner` |
|---|---|---|
| 4773876 | High | `review-codex` never runs a reclaim (claude-review.yml:2285: "no reclaim step"). Provisioning, which runs the PR's build backend as `codex` outside codex's sandbox, can write `.git/config`. actions/checkout's post-job cleanup then runs git as `runner` in that repository. |
| 4773887 | High | reclaim.sh's refusal exits leave the agent's `.git/config` in place for that same post-job git. The kill-loop exhaustion exits at :60-63, the redirected `.git`/`commondir` refusal at :94-97 and the new embedded `.git` refusal at :105-110. All of them come before the restore at :121. |
| 4773888 | High | The reclaim chowns `.git` (GNU default `-P`, :111), restores only `config` (:121) and moves `hooks` aside (:133-139). Other symlinks inside `.git` survive, and later runner-side git writes follow them. |
| 4773340 | High | The agent uid leaves its PID and mount namespace through host cron. `agent_ns.py`'s `build_view` (:467-492) covers only `/home/runner` and `/tmp`, the setgid `crontab` is reachable, nothing denies cron or at, and provisioning runs with no namespace at all. From outside, the wrapper's argv and the action's `Revoke app token` `curl` argv show the App token. |
| 4773338 | Medium | `Compose agent settings` reads `settings` with `[ -f ]` and `cat`, following a symlink committed in the checkout, as `runner`, before the agent user exists: claude.yml:1295-1297, claude-auto.yml:1119-1121, claude-auto-review.yml:1283-1285. |

Each could be patched in turn. But the uid boundary has needed a new
patch for each new runner-side git call, each new post-step and each new
way out of the namespace since 2026-09-22. This design takes the other
route (decision: Ransom, 2026-09-29): **the agent job holds nothing the
agent may not have**. Then a compromise of `runner` in that job gains
nothing, and these findings lose their payoff instead of each needing a
patch.

## Goals and non-goals

Goals:

- An agent job of either engine holds nothing beyond what the agent may
  have: the read-only job token, the declared model credential, and an
  OIDC request token that no relying party exchanges for more than that
  credential. For codex this depends on open question 1.
- No Claude GitHub App token exists in any agent job or caller job, and
  once the App is uninstalled from the Meridian repositories none can be
  minted. `claude[bot]` is trusted nowhere.
- Every decision that used to be made after the agent, in the agent job,
  is made in the land job on a fresh runner, which reads the agent job's
  products only as data. That covers what may be posted, where, how many,
  with which markers, and whether a loop round is refunded.
- The reclaim, the post-agent kill loops and the post-codex PATH checks
  stop being the boundary. The design says which of them stay as defence
  in depth and which are retired.
- Each implementation step is shippable alone, and composite changes are
  backward compatible for the direct callers.

Non-goals:

- **Keeping the model credential from the agent.** That is the declared
  exception (SECURITY.md → Adding or changing a workflow; decision: Ransom,
  2026-09-23) and stays as it is. The codex key is open question 1.
- **Removing the agent user boundary.** It stays as the second line
  (Design → What stays in the untrusted job).
- **meridianlabs-ai/actions** (`isolated-agent` and its `model-broker`):
  see Not this design.
- **Engine-label trust** (4773334, 4774318) and callers' own CI. Both are
  unchanged.

## Current behaviour

Line numbers are from `main` at 609e20d unless another tree is named.
claude-code-action was read at `v1` = fd1c128 (tag v1.0.237, 2026-09-29).
That is a newer tree than the 46a42b4 executed-paths-residual.md read, but
the facts that design relied on have not changed.

### The job graph

Each reusable workflow has four jobs (credential-separation.md → 3.1):
`gate` (trusted: mints the machine account's token, runs the pre-agent
writes, outputs the run's trusted context), one untrusted job per engine
(`agent`/`agent-codex`, `review`/`review-codex`, `fix`/`fix-codex`), and
`land`. The land job is already the shape this design wants for
everything after the agent: a fresh runner that checks out nothing, mints
its own token and acts only on the validated manifest, bundle and body
files. The land composite already assumes the agent job's runner can be
compromised: "A policy that lived only in the caller's post-agent composing
step would be enforced on the runner whose compromise is assumed"
(land/action.yml:125-126).

### What runs after the agent, in the agent job

Every Claude job runs these after the claude-code-action step: the
post-agent reclaim, `reset-origin-url`, the landing-files import (except
claude-auto.yml), model provenance, `Surface agent errors` (reads the
execution file, runs `git status`/`rev-parse`), the manifest composer (git
`rev-parse`, `merge-base`, `log` over the agent's repository) and
`emit-landing` (`git bundle create`, then upload-artifact). The codex jobs
run the reclaim, the final-message import, codex usage, the
unresolved-merge guard and `git add -A && git commit` as `runner`
(claude.yml:2905-2906, claude-auto.yml:2302-2303,
claude-auto-review.yml:2647-2648), then Surface, the composer and emit.
The reviewer's Claude job also runs the re-planted-configuration check (git
with a pinned config), `Derive responsible human` (`who`, `gh` reads) and
`Check whether the auto loop owns this PR` (a label read).
`review-codex` runs no reclaim and no import: its `Prepare Codex review
for landing` reads `$RUNNER_TEMP/codex/codex-review.json` directly from
the codex-owned directory (claude-review.yml:2669).

### What the composers decide that land does not check

The composers turn the agent's `manifest-extra.json` and body files into
the manifest. Some of their rules exist nowhere else:

- **Comment targets and count.** claude.yml's composer drops comments
  whose `number` is not the run's issue or PR, and keeps at most five
  (claude.yml:2084-2131). The validator checks only that `number` is a
  positive integer (validate_manifest.py:663-680) and caps nothing.
  Inline review comments are uncapped in both places
  (validate_manifest.py:698-721, claude-review.yml:1821-1834: finding
  4773341).
- **The codex reviewer's footer.** pr-feedback-context takes "the review"
  to be the newest comment by a Bot-type author or `i-am-marvin` whose body
  matches `<!-- claude-review-comment -->|🤖 engine: codex`
  (pr-feedback-context/action.yml:125). The Bot type includes `claude[bot]`
  and any other App. Land's `defang` deliberately keeps the footer
  (land/lib.sh:14-19), because claude-review.yml's codex path writes it
  into the review body (:2698) and lands that body as an ordinary comment.
  So only the composers split the footer in agent text (claude.yml:2121,
  2147; claude-auto.yml:1698; claude-auto-review.yml:1982). The Surface
  steps' `is_error` relays (claude.yml:1721, claude-auto.yml:1471,
  claude-auto-review.yml:1676, claude-review.yml:1993) and the provenance
  refusal snippet (model-provenance/action.yml:164-172) do not. That is
  4773878 and its siblings 4773339, 4773881, 4773886 and 4773889.
- **The reopen marker.** atlas_sync treats a comment as its own reopen
  record when the author is the machine account and the body starts with
  `Reopened — upstream PR <url> ` (atlas_sync.py:75, 680, 727-731).
  Agent comments post verbatim as the machine account (land/action.yml:1017-1021),
  so an agent can write that prefix. That is 4773875.
- **Hand-back, stage and the mention.** The dev composer decides
  `handback` from the gate's `auto`, whether HEAD moved and whether there
  was an error. It decides `stage` from the hand-back. The reviewer's
  land job cc's `needs.review.outputs.mention`, a login the review job
  computed after the agent ran (claude-review.yml:698-707, 3056).

### What the land jobs believe about the agent job

- **claude.yml** reads only `needs.agent.result` / `needs.agent-codex.result`
  (:3552), to skip landing on a cancelled job and post a note instead.
- **claude-review.yml** reads `needs.review.outputs.claude_outcome`,
  `agent_launched` and `mention` in `Verify a review landed` (:3049-3112),
  and each job's result in the job-level `if:` (:2953).
- **The loops** read `agent_outcome` and `agent_skipped` from the fix
  job's outputs (claude-auto.yml:2725-2727, claude-auto-review.yml:3108-3110).
  - `agent_outcome` decides `allow-no-change-handback` (:2860, :3204).
  - `agent_skipped == 'true'` refunds the round (`Refund infra-crashed
    attempt`, claude-auto.yml:2943-3017; `Refund infra-crashed round`,
    claude-auto-review.yml:3260-3329).
  - The rationale (claude-auto.yml:2890-2928) calls `agent_skipped` "a step
    outcome the runner settled before any agent code ran". That holds only
    while the runner is trusted. The job writes its own outputs, so a
    compromised `runner` can set `agent_skipped=true` on every round.

### claude-code-action's token and modes (at fd1c128)

- **No `github_token`.** `setupGitHubToken` (src/github/token.ts:158-185)
  calls `core.getIDToken("claude-code-github-action")` (:58) and POSTs the
  JWT to the exchange (:107-123). The body is empty unless
  `additional_permissions` is set, in which case it asks for contents,
  pull requests and issues write plus the extras (:69-101). Nothing but
  the JWT authenticates the request.
- **With `github_token`.** The exchange is skipped (token.ts:160-165), and
  so is the `Revoke app token` post-step (action.yml:448-459,
  `inputs.github_token == ''`). The only other OIDC request is Workload
  Identity (base-action/src/workload-identity.ts:121-196, audience
  `https://api.anthropic.com`). So a job that uses WIF keeps
  `id-token: write` either way. Passing `github_token` stops the action
  from minting an App token. It does not stop anything else in the job
  from minting one: only uninstalling the App does that.
- **Tag mode needs a write token.** `prepareTagMode` always calls
  `createInitialComment` (modes/tag/index.ts:45-47; create-initial.ts:19-111).
  With a read-only token the create fails, its fallback fails, and prepare
  throws before the CLI starts. No input turns the comment off:
  `use_sticky_comment` only reuses it, and `track_progress` forces tag
  mode. The context builders (`fetchGitHubData`, `createPrompt`) are
  reachable only through tag mode.
- **Agent mode (a `prompt` input) creates no comment.** It uses the
  `prompt` input alone as the prompt, runs `configureGitAuth` (the token
  into `remote.origin.url`) and on PR events restores `.claude/` and
  `.mcp.json` from the base (run.ts:263-276). It still runs the actor
  checks:
  - `checkHumanActor` (validation/actor.ts:30-88) calls `GET /users/{actor}`
    and requires a non-User actor to be in `allowed_bots`.
  - `checkWritePermissions` (validation/permissions.ts:57-163) passes any
    `[bot]` login with no API call (:114-117). For a User it calls the
    collaborator-permission endpoint (:124-139), which GitHub serves to a
    token with `metadata: read`, and every job token has that.

  So the reviewer's and the loops' bot and User actors pass with the job
  token. executed-paths-residual.md → Alternatives considered listed this
  as unverified. It is now verified from the source. ts-mono's
  `dependabot-fix.yml:341-348` runs agent mode this way today.
- **Tag mode's post-CLI git.** `checkAndCommitOrDeleteBranch`
  (branch-cleanup.ts:86-103) runs `git status`, `add -A`, `commit` and
  `push` as `runner` inside the action step, after the CLI and before any
  step this repository adds. It runs only when the branch the prepare
  created exists on origin with no commits. The launcher's wrapper refuses
  to start the agent in that state (executed-paths-residual.md → launch
  step 6). Agent mode never reaches this code.

### Who else relies on the Claude App

- **`claude[bot]` posts on all ten Meridian repositories.** The newest
  posts are claude.yml tracking comments ("Claude finished …"): inspect_harbor
  on 2026-09-29, inspect_ai on 2026-09-28. That is indirect evidence the
  App is installed on all ten. The org's installation list needs
  `admin:org`, which the research token lacked.
- **Direct callers of claude-code-action** already pass `github_token`:
  inspect_flow `inspect-ai-main-failure.yml:348` and `inspect-update.yml:330`,
  and ts-mono `dependabot-fix.yml:348`. They hold no App token today, but
  their jobs have `id-token: write` and so could mint one.
- **Claude Code features that need the App on a repository**, per the
  docs (code.claude.com/docs/en/claude-code-on-the-web,
  /web-quickstart, /claude-projects, /code-review):
  - cloud sessions on a private repository connected through the browser
    onboarding (`/web-setup` with a `gh` token works without the App);
  - auto-fix of pull requests;
  - project cloud threads;
  - `claude --cloud` cloning, rather than uploading a bundle;
  - managed Code Review.

  The App is one App shared by all of these features and the Action. The
  claude.ai chat GitHub integration is read-only file sync and does not
  use it.
- **Other OIDC relying parties.** None of the nine caller repositories
  uses cloud federation (no `configure-aws-credentials`,
  `google-github-actions/auth` or `azure/login`). Five use PyPI trusted
  publishing:
  - inspect_flow `publish.yaml` and `release.yaml`;
  - inspect_harbor `publish.yaml` and `release.yaml`;
  - inspect_sandboxes `publish.yaml` and `release.yaml`;
  - inspect_scout `publish.yaml`;
  - inspect_swe `publish.yml`.

  Three use npm trusted publishing: ts-mono, the inspect_ai fork and
  inspect_scout, each through `npm-publish.yml`. Their publisher
  configurations live on pypi.org and npmjs.com, which this research could
  not read.
- **Callers passing `settings`.** Only two: inspect_harbor passes inline
  JSON (`claude.yml:179`, `:310`), and ts-mono passes the path
  `.github/claude-settings.json` (`claude.yml:136`, `:287`;
  `claude-auto.yml:73`, `:138`).

## Design

### The boundary

The agent job, meaning every job that runs an agent or code from a head an
agent may have written, is untrusted as a whole, including its `runner`
uid, root on its VM and every output it reports. Nothing in it may be
worth more than what the agent may have. What such a job holds after this
design (Claude jobs after step 6; codex jobs under open question 1):

| Held in the job | Reachable by `runner`/root | The agent may have it because |
|---|---|---|
| Job token: contents, pull requests, issues and actions read | yes | it is the agent's own `GH_TOKEN` (the launcher gives it) |
| OIDC request token (`ACTIONS_ID_TOKEN_REQUEST_*`) | yes | no relying party grants more than the model credential: Anthropic WIF is the declared exception; the Claude App exchange has no installation to issue from after step 6; PyPI and npm publishers pin their own workflow file (step 5 verifies). Codex jobs lose `id-token: write` altogether (step 1) |
| WIF JWT and the Anthropic access token | yes | the declared model-credential exception |
| `ACTIONS_RUNTIME_TOKEN` | yes | cache access is read-only (`cache-mode: read`). Artifact upload in this run reaches only the `landing` artifact, which land already treats as untrusted, and no trusted job consumes an artifact (the rule below) |
| `OPENAI_API_KEY` (codex jobs) | yes, from the job message | open question 1 |
| Machine account's or Claude App's token | **none in the job** | — |

**The artifact rule.** A trusted job never consumes an artifact of a run in
which an untrusted job ran, other than as untrusted data. The runtime token
lets the untrusted job upload under any name first, and upload-artifact v4
names are unique per run. So data from the gate to land travels only as
the gate's job outputs (as `start_sha`, `pr_labels` and `head_branch` do
today), never as an artifact.

**What crosses from the untrusted job to land, all of it untrusted:**

- the `landing` artifact: the manifest proposal, `commits.bundle` and the
  body files. Land already materializes the bundle into an empty bare
  repository with hooks off and `GIT_CONFIG_GLOBAL=/dev/null`
  (land/action.yml fetch step), and that stays as it is;
- the job outputs that remain: `claude_outcome` and `agent_launched`
  (reviewer), and `agent_outcome` (loops);
- `needs.<job>.result`.

**How land may use the job's word.** Land takes its decisions from the
gate's outputs, the event, its own API reads and what the validated
manifest requests. It uses the untrusted job's outputs and result only to
choose between outcomes that are all safe whatever the job reports. The
task framed the requirement as "use `needs.<job>.result`, not step
outcomes the job reports". The result is a better signal than outputs,
since the service alone sets `skipped` for a job it never dispatched. But
a running job's `success`/`failure`, and plausibly `cancelled` once root
can drive the worker, are settled from what that job's runner reports, so
this design treats the result as untrusted too. The uses that remain:

| Use | Values the job can force | Why every value is safe |
|---|---|---|
| Skip landing on `result == 'cancelled'` (all four) | land nothing and post the cancellation note | the agent can already make its run land nothing |
| `allow-no-change-handback` from `agent_outcome` (loops) | a no-bundle `@review` on a counted round | once refunds go (below), every round counts against the cap, so a hand-back cannot re-arm the loop without bound (the reason 4628734 was a finding) |
| `Verify a review landed` from `claude_outcome`/`agent_launched` (reviewer) | suppress or trigger the "no review landed" nudge | a fixed text, whichever way |

**Refunds go.** `agent_skipped` is the only job output a security decision
rests on today, and the job writes it. No evidence of "the agent step was
never entered" is out of the untrusted job's reach: the Actions API's step
list is the same runner's report. Both refund steps and the
`agent_skipped` outputs are removed, and every round counts. Open
question 2 is whether to keep a bounded refund budget instead.

### The job graph after the change

The job graph stays the same: `gate → agent | agent-codex → land`. No new
job is added. What changes is what each job does:

- **gate** (trusted) also creates claude.yml's tracking comment as the
  machine account, and outputs its id.
- **The untrusted job** keeps its work up to and including the agent, then
  *packages*: the bundle, the agent's landing files, the Surface error text
  and the provenance note. It still runs its composer, but the composer's
  output is a proposal whose every field land re-checks or re-derives. It
  holds the job token as its only GitHub credential (`github_token:
  ${{ github.token }}` on every claude-code-action step).
- **land** (trusted, fresh runner) does everything that decides:
  - the validator and land composite enforce the rules that lived only in
    the composers;
  - the reviewer's `who` and loop-ownership reads move here;
  - claude.yml finishes its tracking comment here;
  - the refund steps are gone.

This is "move the post-agent work to a fresh-runner job" with one choice
made: what moves is every **decision**. The packaging stays, because it
needs the agent's workspace: the bundle needs the repository, and
Surface's uncommitted-edits check needs the working tree. Alternatives
considered covers moving the composers whole.

### Land enforces what the composers enforced

The land composite and validator gain the rules below. Each is an input
with a backward-compatible default, and the reusable workflows pass the
strict value. The defaults flip once the direct callers are checked, as
`allowed-pr-labels` did in #143.

1. **Every post lands on the run's own issue or PR.** A manifest can name
   a posting target in five places. Today only the composers constrain
   them, and the reviewer's land job still accepts `issues[]` in this
   repository with no count limit (claude-review.yml:2973, where the
   default `allowed-issue-repos` applies). So a forged review manifest can
   carry one legitimate review comment and 51 `issues[].comment_on: 999`
   entries, which post through the same issue-comment endpoint
   (land/action.yml:1227). The review's validator probe confirmed that
   such a manifest passes today. The rule covers all five paths:
   - **`comments[].number`.** A new input, **`comment-numbers`**, defaults
     to `*` (today). With `event`, the validator refuses any number other
     than the `pr-number` or `issue-number` input. That is claude.yml's
     composer rule, applied on the trusted runner.
   - **`pr.issue`**, where land posts the "opened a pull request" note
     (land/action.yml:956; validate_manifest.py:661). Under
     `comment-numbers: event` it must equal the `issue-number` input.
   - **`issues[]`**, including `comment_on` and `reopen`. All four reusable
     land jobs pass `allowed-issue-repos: ""`, the reviewer's included.
     Its composer never writes `issues[]`, so the validator refuses any
     manifest that carries one. Direct callers that file issues keep their
     own allow-lists (actions' triage: `max-issues: 1`, the label and
     assignee lists).
   - **`replies[]`.** Land already posts them under `pulls/<pr_number>`,
     and `pr_number` is pinned. It now also lists the PR's own review
     comments first, as it lists the PR's threads before resolving
     (land/action.yml:1187-1200). It skips and reports a
     `review_comment_id` that is not on the PR, and drops repeats. So
     replies are bounded by the PR's own review comments and land nowhere
     else, whatever the reply endpoint does with a foreign id.
   - **Everything else land posts** (the hand-back, hand-off, provenance
     note, verdict, error report and the resolved threads) already goes to
     the pinned `pr_number`/`issue_number` or the event's number. No change.

   All four reusable workflows pass `comment-numbers: event`. Of the direct
   callers, inspect_flow's two and ts-mono comment only on their own event
   numbers, and step 3 checks their recent manifests. actions' triage
   comments on other issues only through `issues[]`, under its own
   allow-lists, so it keeps `comment-numbers` at `*`.
2. **`max-comments`** and **`max-review-comments`**, default empty (no cap).
   Together with item 1, every posting path is either capped, bounded by
   the PR's own objects (replies and thread resolutions), a single fixed
   post, or refused.
   - The dev agent and loops pass `5` for comments, the composers' cap.
   - The reviewer passes `5` for comments and `50` for inline review
     comments. This closes 4773341. The reviewer's prompt asks for
     findings, not nits, and 50 is well above any review this repository
     has posted.
3. **`defang` splits the codex footer and the reopen prefix.**
   - lib.sh's `defang` and `defang_str` gain `s/engine: codex/engine  codex/gI`
     and a rule that breaks a body starting `Reopened — upstream PR`. That
     covers every agent-authored body land posts: comments, replies,
     inline comments, the error, the provenance note, the hand-off, issue
     bodies and titles, and Slack.
   - The one legitimate bearer of the footer is the codex review. It
     changes to what the Claude review already does: claude-review.yml's
     codex composer flags its comment `review: true`, which the validator
     already admits next to `review_verdict` under `allow-review`, and
     stops writing the footer into the body (a plain `🤖 Reviewed by
     Codex` line keeps the attribution). Land then appends
     `<!-- claude-review-comment -->` after the de-fang
     (land/action.yml:1020), exactly as for a Claude review.
   - pr-feedback-context keeps accepting the footer as an anchor. After
     this step no new machine-account comment can carry it, so it matches
     only codex reviews posted before the change. Its anchor authors
     narrow from "any Bot type or `i-am-marvin`" to the machine account's
     two logins. The Bot clause only ever meant `claude[bot]` and the
     machine App, and `claude[bot]` stops being trusted in step 1.
   - This fixes the root of 4773878, its is_error siblings (4773339,
     4773881, 4773886, 4773889) and 4773875 on the trusted side. The
     composers' own footer seds stay as harmless duplicates.
   - A test pins the class. Every string a consumer keys on in a
     machine-account comment must be in defang's list or appended by land
     after it. The consumers are pr-feedback-context's anchors,
     atlas_sync's reopen marker and the loop markers already listed.
4. **The dev agent's hand-back and stage are bounded by trusted inputs.**
   - claude.yml's land job passes `allow-no-change-handback: "false"`,
     so a hand-back needs a bundle.
   - It passes a new `allow-handback` input, default `"true"`, set from
     the gate: `auto` or `request_review_after_open`. With `"false"` the
     plan step drops a manifest's `handback` the way it already drops a
     no-bundle one.
   - Stage values are already constrained by the validator (:768-770). A
     forged `stage` can move the board only between the values an honest
     run uses, and that is accepted.
5. **The reviewer's cc and loop ownership are read by land.**
   - `Derive responsible human` (claude-review.yml:1860-1904 and the codex
     twin at :2709) and `Check whether the auto loop owns this PR`
     (:2062, :2840) are pure API reads. They move into the reviewer's land
     job, before the land composite.
   - `engaged` decides only the manifest's `stage` (Review when the loop
     does not own the PR; claude-review.yml:2097-2160). The land job reads
     the label itself and passes the result to a new land input,
     `stage-override` (default empty, meaning the manifest's `stage`): the
     reviewer's land job sets it to `Review` or `none` from its own read.
     The `mention` job output is removed.

A caller that leaves every new input at its default keeps today's land
behaviour exactly.

### Stop trusting `claude[bot]`, and pass `github_token`

In the reviewer and both loops (agent mode already):

- **claude-review.yml, claude-auto.yml, claude-auto-review.yml.**
  - `github_token: ${{ github.token }}` on the claude-code-action step.
  - `additional_permissions: "actions: read"` is dropped where it is set
    (claude-auto.yml:1285 here; claude.yml:1529 in step 4). With
    `github_token` it is ignored (token.ts only sends it to the exchange),
    and the job token already has `actions: read`.
  - `allowed_bots` stays `TRUSTED_LOGINS` plus the caller's list:
    `checkHumanActor` needs the machine App's bot login in it, with any
    token.
- **claude-auto-review.yml.**
  - `REVIEWER_LOGINS` becomes `i-am-marvin,meridian-marvin[bot]`.
  - `reviewer_login` defaults to `""`, which adds nothing to
    `REVIEWER_LOGINS`.
  - `review_allowed_bots` defaults to `""`.
  - No stub passes either input (research above), so no caller change is
    needed.
- **pr-feedback-context.** Anchor authors narrow to the machine logins (Land
  enforces, item 3).
- **skills/promote/promote.sh.** `REVIEWER_BOT` and its use at :402 go.
  Verdicts count only from trusted logins or write-access accounts. A fork
  PR whose only verdict predates #114 (a `claude[bot]` verdict) now reads
  "no verdict", and the skill pauses for confirmation as it does for any
  missing verdict.
- **Codex jobs.** `id-token: write` is removed from `agent-codex`,
  `review-codex` and both `fix-codex` jobs. Nothing in them requests an
  OIDC token, and until the App is uninstalled it lets a runner compromise
  in a codex job mint the App token.
- **The inspect_ai fork's reviewer stub** drops `allowed_bots: "claude[bot]"`
  (`claude-review.yml:124-127` on `meridian`). That is a companion PR in
  the fork, made from a maintainer's machine. Since 2026-09-16 no dev
  agent posts `@review` (`request_review_after_open: "false"`), so the
  line admits nothing today.

### claude.yml: agent mode, and a tracking comment by the machine account

Tag mode cannot run on a read-only token (Current behaviour). So
`claude.yml`'s Claude job moves to **agent mode with a prompt it builds
itself**, which is what its codex job already does.

- **Context prompt.** A new composite, `dev-agent-context`, writes the
  event's context to one file under `$RUNNER_TEMP`. Both engines' prompt
  steps cat it and add their engine-specific parts (the Claude job's
  `LANDING` and `ENVIRONMENT` text and `REVIEW_ETIQUETTE`; codex's ending
  contract). The action's `trigger_phrase` and `label_trigger` inputs go,
  since the gate's trigger check is the trigger, and the `prompt` input
  carries the composed text. The system-prompt append stays as it is.

  Tag mode's context is the bar, not the codex prompt's. The codex prompt
  gives an issue run only the triggering comment and the issue's title and
  body (claude.yml:2683-2713), and reads the PR body and thread live
  (:2703; pr-feedback-context/action.yml:116, 149). Tag mode gives more,
  and protects it. The composite reproduces both:
  - **A trigger-time snapshot.** Tag mode resolves a trigger time
    (`resolveTriggerTimestamp`, modes/tag/index.ts:49). It takes the title
    and body from the webhook payload (`extractOriginalTitle`/`Body`), and
    drops every comment, review and review comment created or edited at or
    after that time (fetcher.ts:241-300, 435-465, 532-541, 571-576). So an
    author cannot swap the context after a maintainer authorized the run.
    The composite does the same:
    - The gate outputs `trigger_time`, resolved as the action's
      `resolveTriggerTimestamp` and `extractTriggerTimestamp` do
      (fetcher.ts:38-100):
      - the triggering comment's `created_at`, or the review's
        `submitted_at`;
      - for `issues`/`pull_request` `opened`, the entity's `created_at`;
      - for `labeled`/`assigned`, the matching entry's `created_at` in the
        issue's event history (`issues/<n>/events`), falling back to the
        entity's `updated_at`.

      Step 4 lifts these cases into a test.
    - The title and body come from the event payload
      (`github.event.issue` on issue and issue-comment events, a PR's
      issue object included; `github.event.pull_request` on PR events),
      never from a live read.
    - Every comment, review, review thread and thread comment whose
      creation or last edit (`lastEditedAt`, else `updated_at`) is at or
      after `trigger_time` is dropped.

    pr-feedback-context gains an optional `trigger-time` input, which
    applies that filter to its comments and threads. It defaults to empty,
    which is no filter, so the loops' use of it is unchanged.
  - **Issue discussion history.** On an issue run (the `issue_comment`,
    `issues` opened/labeled/assigned and `issue` label triggers), the
    composite includes the issue's comments (`issues/<n>/comments`, all
    pages), after the trigger-time filter. It drops machine control
    comments the same way pr-feedback-context does (loop markers,
    provenance, bare triggers, `<!-- dev-agent-status -->` and "Claude
    finished"). It renders them oldest first, with author and time. It
    keeps the newest 30 comments and at most 40,000 characters, and when
    it cuts it says so in one line at the top ("N earlier comments
    omitted"). So "implement the second option above", an answer to an
    earlier clarification, and a label or assignment trigger after a
    discussion keep their referents. PR runs get the same through
    pr-feedback-context (anchored on the newest review, bounded), which
    already includes the PR's top-level comments.
  - **Images.** Tag mode downloads the images in the context bodies
    (utils/image-downloader.ts). It reads each body's `body_html`
    (`Accept: application/vnd.github.full+json`) through read APIs,
    `issues.getComment` and `pulls.getReviewComment` (:140-160), and
    fetches the signed attachment URLs in it. The job token has the read
    permissions those calls need. So the composite does the same, for the
    bodies that survived the filter:
    - only the attachment hosts the action accepts;
    - at most 20 images, 10 MB each and 50 MB in all;
    - no redirects off those hosts;
    - into `$RUNNER_TEMP/agent-context/`.

    The launcher gains an optional `context-dir` input, which it binds
    read-only into the agent's namespace. The context names each file
    beside the link it replaced. Whether the job token resolves a
    *private* repository's attachments is checked on a private test
    repository in step 4. If it cannot, the fallback is links only, which
    goes to Ransom as a regression before step 4 merges.
  - **Fetch failures.** Every fetch is retried three times, then fails the
    step, as the codex prompt's fetches do (claude.yml:2646-2656): the
    agent is skipped, and the Surface step names the failed fetch. A
    failed image download is not fatal. It leaves the link and logs a
    warning.

  The codex dev path uses the same composite, so it gains the history and
  the snapshot too. It keeps its current behaviour of taking no images.
- **Branch.** A `Prepare branch` step, runner-side and before `Create agent
  user`, replaces `setupBranch`. It outputs `branch` and `mode`
  (`commit` or `comment-only`). The composer and Surface read those in
  place of `steps.claude.outputs.branch_name`. It works from the gate's
  pins (`head_branch`, `start_sha`) and sync-branch's outputs, and never
  from a new API read:
  - **Issue run.** It creates `claude/issue-<N>-<run_number>` from
    `origin/<base>` with `git checkout -b`, next to codex's
    `claude/issue-<N>-codex-<run_number>`. The land job's `branch-prefix`
    (`claude/issue-N-`) is unchanged.
  - **Open, same-repository PR.** sync-branch has already fetched the head,
    checked it out at the gate's pinned SHA (`head-sha`) and merged the
    base, and it may have left a conflicted merge for the agent
    (`agent_merge=1`). The step verifies that HEAD is on `head_branch` and
    records it, touching neither the index nor `MERGE_HEAD`.
  - **Closed or merged PR whose head branch is still on origin.**
    sync-branch returns before checking anything out
    (sync-branch/action.yml:233-241) and emits no `branch`, but it does
    emit `head_sha`, the live tip. The step fetches `head_branch` and
    refuses unless its tip is the gate's `start_sha`, the head pin. It
    then checks it out (`git checkout -B <head_branch> <start_sha>`) and
    merges nothing, as the codex prep step already does for any PR
    (claude.yml:2434-2445). This covers the inspect_ai fork's closed PR
    whose same-repository branch still backs an upstream PR. The
    continuation commits onto that branch, and land pushes it there,
    because the validator pins `branch` to the PR's live head ref.
    Today's tag mode instead cut a fresh branch off base, which the
    composer then rejected ("the agent left its branch") unless the agent
    checked the head out itself.
  - **Closed or merged PR whose head branch is gone** (`start_sha` empty:
    the gate's read 404'd). Nothing can land, since land refuses bundles
    with no trusted start. The step outputs `mode=comment-only` and leaves
    HEAD where the checkout put it. The prompt tells the agent that the
    branch no longer exists and that it answers by comment only. The
    composer emits read-only. Today this case ends in a rejected-work
    error.
  - **Fork heads** stay refused by the gate (claude.yml:685).
- **Base for the configuration restore.** Agent mode restores `.claude/`
  and `.mcp.json` from `base_branch` or the default branch on an
  `issue_comment` event (run.ts:254; modes/agent/index.ts:98). Tag mode
  used the PR's actual base. So claude.yml's action step passes
  `base_branch: ${{ steps.sync.outputs.base || inputs.base_branch }}`:
  the PR's base on PR runs, the configured base on issue runs.
- **Tracking comment.**
  - The gate posts it right after the 👀 acknowledgement
    (claude.yml:1002-1018), with the machine account's token. It is a
    top-level comment on the issue or PR, including for a
    `pull_request_review_comment` trigger, where tag mode replied in
    thread. The body is fixed text:
    `🤖 **The dev agent is working on this** — [run](<run url>)` plus
    `<!-- dev-agent-status -->`. Its id becomes the gate output
    `status_comment_id`.
  - The land job's last step, `always()`, PATCHes it with a body composed
    from trusted context and the land step's outputs only, never agent
    text. The first line keeps the action's form,
    `**Claude finished @<requester>'s task** — [run](…)`, so
    pr-feedback-context's existing filter (:123) and the checkout skill's
    "Claude finished … branch" heuristic keep working. The second line is
    one of:
    - the PR (`pr_number`, pushed);
    - the pushed branch (validated, prefix-pinned);
    - "no code changes";
    - "the landing failed — see the note below";
    - "the agent job was cancelled".
  - pr-feedback-context also drops `<!-- dev-agent-status -->`.
  - The agent never had a tracking-comment tool here: the wrapper already
    dropped the `github_comment` server. The LANDING sentence about the
    tracking comment changes to "the workflow keeps a status comment; do
    not try to edit it".
- **`github_token: ${{ github.token }}`.** With that and agent mode, no App
  token is minted, no `Revoke app token` step runs, and the action's
  post-CLI branch cleanup (tag mode only) is gone. The launcher's
  refuse-if-branch-on-origin check (launch step 6) then has nothing to
  guard and is retired.
- **What changes for the requester.** The status comment comes from the
  machine account, top-level. Issue-run branch names change form. A
  closed PR with a live branch is continued rather than refused, and one
  whose branch is gone gets an answer and no commits. The context, its
  trigger-time snapshot and the images are what tag mode gave.

executed-paths-residual.md → Alternatives considered rejected D2 (run the
CLI without claude-code-action) because `claude.yml` depends on tag mode:
the trigger check, the tracking comment, the context prompt, `setupBranch`
and the base-configuration restore. That reasoning held then. The codex
job has since grown partial equivalents: the gate's trigger check, the
codex prep step's branch handling and its prompt context. This design
completes them. `dev-agent-context` reproduces tag mode's context
(snapshot, history, images), `Prepare branch` covers every PR state, and
the tracking comment is a gate step and a land step. Agent mode keeps the
restore (run.ts:263-276), given the right base. So agent mode costs a
bounded amount of work, and claude-code-action still earns its place: it
provides the WIF setup, the pinned CLI, the settings merge, the SDK loop
and the execution file. D2 stays unnecessary. The same section called
`github_token` "worthwhile for the three agent-mode workflows" and
blocked in claude.yml by `createInitialComment`. This design takes it in
all three, and removes the block by moving the comment to the machine
account.

### What stays in the untrusted job

The uid machinery stays where it is cheap and where it still serves an
honest run. It is no longer claimed as the boundary.

| Mechanism | Before | After (Claude jobs, and codex jobs under open question 1 (a)) |
|---|---|---|
| Agent user; provisioning as that user; the launcher's namespace, env allow-list, by-value token check and isolation checks | boundary | **kept, as defence in depth.** It keeps the OIDC request token, the runtime token, sudo and the runner's files away from the agent. It also stays the tier-2 requirement (below). The wrapper already has a job-token mode (`claude`:65-75, used by ts-mono), so the by-value check stays meaningful for a direct caller that passes a PAT |
| Post-agent reclaim: kills, WIF ACL removal, `.git` refusal/restore | boundary | **kept, as hygiene.** It makes the bundle and landing files the agent's final state, and on a redirected `.git` it still packages nothing. Its refusals no longer need to be airtight: 4773887's exits and 4773888's `.git` symlinks now let `runner`, or the checkout post-step, reach nothing the agent lacks |
| Pre-agent reclaim (Claude) | boundary for the action's prepare git | **kept, as hygiene** (kills what provisioning left). It is not extended |
| `import-codex-final` (owner, `O_NOFOLLOW`, caps) | boundary | **kept, as hygiene.** It keeps honest runs from posting files provisioning left, and land's validator and caps are the real bounds |
| Post-codex PATH checks (`assert-runner-only-path` in the reclaim; `system-path` pins) | boundary | **kept, as hygiene.** For the Claude jobs they are claude-agent's walk (create-codex-user, launcher, reclaims); they cost nothing and are shared with codex |
| Hook-safe landing env pins (`GIT_DIR`, `GIT_CONFIG_GLOBAL`, hooksPath, fsmonitor) | boundary | **kept, as hygiene.** They are cheap and make honest runs deterministic |
| `reset-origin-url` after the Claude agent | boundary | **retired.** Nothing after the reclaim fetches or pushes, and with `github_token` the URL carries only the job token. The wrapper's pre-launch reset stays |
| Launch step 6 (refuse when the prepare's branch is on origin) | boundary | **retired** with tag mode |
| `Revoke app token`, `classify_inline_comments: "false"` | boundary | the revoke skips itself with `github_token`. `classify_inline_comments: "false"` stays, since the buffered post-step would still post with the job token |
| `drop-runner-root` | unused in the reusable workflows | unchanged |

Under open question 1 (b), every "boundary → hygiene" row above stays a
boundary **in the codex jobs**, under the fail-closed teardown described in
option (b) below.

**4773340 (cron).** With no App token on any argv after steps 1 and 4, an
escape from the namespace reaches the host as `claude-agent`. From there
it can read world- or group-readable runner files and see the argv of
runner processes, and holds nothing more than the job token. Denying
cron and at to the agent users (`/etc/cron.deny`, `/etc/at.deny`, written
by `create-codex-user`) is cheap depth for the Claude jobs, and is
required under open question 1 (b), where codex's kill loops are part of
the boundary.

**4773338 (settings path).** This is independent of the rest and ships
first. The three `Compose … settings` steps keep accepting a path, because
ts-mono uses `.github/claude-settings.json`, but read it through a small
Python helper. The helper resolves the path under `$GITHUB_WORKSPACE`,
refuses it if any component is a symlink or the resolved path leaves the
workspace, opens it `O_NOFOLLOW`, requires a regular file and caps its
size. inspect_harbor's inline JSON is unaffected. `.github/` is tier 1,
so no agent can land a change to ts-mono's file.

### Codex jobs: open question 1

`OPENAI_API_KEY` is the one secret an agent job references. It stays
behind codex-action's `codex-responses-api-proxy`, which runs as `runner`,
and codex gets only the proxy address. So it is the one model credential
the agent cannot read today. And it is in the job message from the first
step, so a runner compromise at any point in a codex job, before or after
codex, reads it.

- **(a) Extend the declared exception to a project-scoped, spend-capped
  OpenAI key** (recommended).
  - The key moves to a dedicated OpenAI project for CI agents, with a hard
    project budget (verify that the budget is enforced as a hard limit
    and not only an alert) and a rotation cadence.
  - SECURITY.md's exception reads: the model credential (WIF, or that
    key).
  - Then the codex jobs hold nothing the agent may not have, every row of
    the table above is hygiene there too, and 4773876, 4773887 and 4773888
    close with the boundary rather than with patches.
  - Cost: a key taken from a compromised job is usable until rotation or
    the budget. That is longer than the WIF token's job-long window.
- **(b) Keep the codex jobs on the uid boundary, with a fail-closed
  teardown.** Making the reclaim's refusals fatal is not enough, and it is
  the reason today's refusal exits are unsafe (4773887).
  actions/checkout registers a post action, and the runner runs post
  actions under `always()` by default (actions/checkout `action.yml`;
  actions/runner `ActionManager.cs`). A failed or cancelled job still
  runs checkout's cleanup git in the workspace. And a codex process that
  survives can race any replacement of `.git` while it can still write
  the workspace. So option (b) is a protocol whose safety does not depend
  on any step succeeding:
  1. **No runner-side git ever opens the workspace's `.git` after the
     grant.**
     - Before the grant, `create-codex-user` copies `.git` to a
       runner-only `$RUNNER_TEMP/runner-git`. It is a real copy (`cp -a`,
       not a hardlinked clone: the grant makes object files
       group-writable, and a hardlink would share them). The copy is taken
       after sync-branch, so it holds the base merge's `MERGE_HEAD` and
       index when a conflict was left for codex.
     - Every post-codex git call runs with `GIT_DIR` and `GIT_COMMON_DIR`
       on that copy, `GIT_WORK_TREE=$GITHUB_WORKSPACE` and the env pins it
       already has: the guard, the commit, the composer and emit-landing.
     - The workspace's own `.git` is never read again and never replaced,
       so there is nothing to race. That fixes 4773888 (symlinks inside
       `.git`) by construction.
  2. **No post action runs git.** The codex jobs stop using
     `actions/checkout` and check out with a `run:` step (`git init`,
     then fetch with the step-scoped credential helper, then check out the
     pinned SHA), which registers no post action. A unit test pins every
     action a codex job uses to an allow-list of actions known to register
     no post step that runs git: `upload-artifact`, and codex-action,
     whose post step implementation must verify. That fixes 4773876 and
     the post-step half of 4773887, whatever the reclaim's outcome, a
     cancellation included.
  3. **Kills that cannot be outrun: every codex-uid process the job starts
     begins in a root-owned cgroup.** `create-codex-user` creates
     `/sys/fs/cgroup/<the job's cgroup parent>/meridian-codex`, owned by
     root, before the user's first process. A process can only move
     between cgroups by writing the destination's and the common
     ancestor's `cgroup.procs`, and those files are root's. So a process
     born inside cannot leave, and a process outside cannot enter
     without root. The integration therefore puts the process in the
     cgroup while it is still root, before it drops to `codex`:
     - **`jail-exec`**, a new root-owned helper in `/opt/meridian-codex/bin`
       (dir and file root-owned, `0755`, verified like the launcher's
       `/opt/meridian-agent/bin`). The runner runs it through its sudo, as
       root. It accepts exactly `-u codex [--] <argv>`, and anything else
       exits non-zero. The optional `--` is there because codex-action
       omits it on one call (below). It writes its own PID to the jail's `cgroup.procs`,
       reads `/proc/self/cgroup` back and requires the jail path. On any
       failure it exits non-zero **before** dropping privileges, so no
       codex process starts outside the jail. Then it `exec`s `/usr/bin/sudo
       -u codex -- <argv>`, and every descendant inherits the cgroup.
     - **Our composites' codex-uid launches** all go through `jail-exec`:
       - provision-fallback's `sudo -u codex -H -- env -i …`
         (provision-fallback/action.yml:226) becomes `sudo -n
         /opt/meridian-codex/bin/jail-exec -u codex -- env -i …`, with the
         same arguments;
       - `codex-home.sh`'s `sudo -u "$user" tee` and `printenv` calls
         (:54, :84, :101);
       - `assert-runner-only-path`'s `runuser -u "$USER_NAME" --` probes
         (:161), when the user is `codex`; `claude-agent` keeps `runuser`.
         `create-codex-user` creates the jail before its first PATH walk.
         They are short, fixed-argv helpers, and they are routed anyway so
         that the claim below has no exception.

       `codex-home.sh`'s kill step becomes the jail's `cgroup.kill`.
     - **codex-action's launch.** At f367b1e, the `unprivileged-user`
       strategy resolves `codex` with `which` and prepends `sudo`, `-u`,
       `<user>`, `--` **by name** (`runCodexExec.ts:233-253`), so the
       `sudo` it runs is the first one on the step's PATH. The codex jobs
       put `/opt/meridian-codex/bin` first on `GITHUB_PATH`. That is
       allowed because it is root-owned and outside the workspace; the PATH
       walk in `create-codex-user` checks it the same way. The directory
       holds a `sudo` shim. For an argv that begins `-u codex`, with or
       without a following `--`, it `exec`s `/usr/bin/sudo -n
       /opt/meridian-codex/bin/jail-exec "$@"`. That includes codex-action's
       own `sudo -u codex cat <output-file>` finalizer, which has no `--`
       (runCodexExec.ts:345-356). Any other argv it passes to
       `/usr/bin/sudo` unchanged. That covers
       the action's own root commands, such as its `sudo chown`/`sysctl`
       lines (codex-action action.yml:262-314). Every
       post-codex composite pins `PATH` to the system directories anyway,
       so it never meets the shim.
     - **The API-key proxy stays outside.** codex-action starts
       `codex-responses-api-proxy` as the runner in its own earlier
       sub-step, not through `sudo -u codex`, so it runs in the runner's
       cgroup, and `cgroup.kill` on the jail cannot reach it. The hosted
       test asserts both memberships.
     - **Pin codex-action to f367b1e under (b).** The shim depends on the
       by-name `sudo`. A structural test refuses any other ref, and
       the hosted test fails closed if the action ever calls
       `/usr/bin/sudo` directly: the stand-in would then be outside the
       jail, and membership is asserted.
     - **Every other `sudo -u codex` or `runuser -u codex`** in the
       workflows and composites goes through `jail-exec` (a structural test
       over `sudo`, `runuser`, `su` and `setpriv` with the codex user).
       Cron and at are denied (point 7), which closes the one route the
       kernel does not. The reclaim's whole-uid `pgrep -u codex` check
       below remains the backstop: a process that any missed route
       started fails the reclaim instead of surviving it.

     The reclaim writes `1` to the jail's `cgroup.kill`, which is atomic
     against forks. It then requires `cgroup.procs` to be empty and no
     process of uid `codex` anywhere (`pgrep -u codex`: a survivor outside
     the jail means the integration failed, so the reclaim fails).
  4. **Any failure skips, and skipping is safe.**
     - The reclaim does its checks and then only revokes codex's write
       grant on the workspace. It restores nothing into `.git`.
     - Kill exhaustion, a redirected git dir, a new embedded repository, a
       failed revocation or a cancelled reclaim all leave its outcome
       `!= success`. Every later git-running step is gated on `==
       success` (they already are), so no step runs git after it.
     - Because of 1 and 2, no post action does either.
     - The landing then carries only the Surface error, which needs no
       git.
  5. **The merge state survives, and the guard keeps its evidence.** The
     existing guard checks the index **before** the blanket `git add -A`
     on purpose. Binary and modify/delete conflicts have no markers, and a
     blanket add would silently "resolve" them to whatever is in the work
     tree (unresolved-merge-guard/action.yml:70-91). The prompt requires
     codex to `git add` or `git rm` each resolved path. With the private
     git dir, that evidence lives in codex's index file. It is read as
     **imported data**, never through the codex-owned git dir, and only
     when the run needs it:
     - **When the index is needed.** The guard first lists the paths the
       trusted snapshot's index holds unmerged (`git --git-dir=<private>
       ls-files --unmerged`), which is the conflict list sync left: `U`.
       It also lists the snapshot's gitlink (mode 160000) paths: `G`. When
       both are empty, which is every run with no base-merge conflict in a
       repository without submodules, nothing is imported or checked. The
       commit step stages the work tree as below. So an ordinary unstaged
       edit and a no-change answer never depend on the index file, which
       codex is allowed to leave untouched (the prompts at
       claude.yml:2668, claude-auto.yml:2117, claude-auto-review.yml:2388).
     - **The import, when `U` or `G` is non-empty.** After the kill and the
       revocation, the reclaim copies `.git/index` to
       `$RUNNER_TEMP/codex-index`. It uses the `O_NOFOLLOW`, regular-file,
       single-link, size-capped import of `import-codex-final`, with the
       owner set to either `runner` or `codex`. `create-codex-user` leaves
       the original index owned by `runner` and group-writable
       (create-codex-user/action.yml:290), and a codex `git add` replaces
       it with a codex-owned file. Both are legitimate, and ownership
       says nothing about trust here: the copy is data either way.
       `import_codex_final.py` gains an owner list for this one call. A
       missing, refused or unparsable copy fails the guard, because the
       evidence is required.
     - **Evidence per conflicted path (`U`).** Codex's index is read as
       data: `GIT_INDEX_FILE=$RUNNER_TEMP/codex-index git
       --git-dir=<private> ls-files --stage -z -- <path>`, with the
       private config (fsmonitor off, no hooks,
       `GIT_CONFIG_GLOBAL=/dev/null`). Each path must have exactly one
       stage-0 entry, or none at all (a `git rm`). Any stage 1-3 entry
       means codex left the path unmerged, and the round is refused as
       today. The entry's mode must be one of `100644`, `100755`, `120000`
       or `160000`, and the mode and object id are what the check below
       compares.
     - **Staging, mode by mode, never inside a nested repository.** The
       commit step stages into the private index:
       - Everything but gitlinks: `git add -A -- . ':(exclude)<each path
         in G and each 160000 path in codex's index>'`. Git computes each
         entry as it would for codex:
         - regular and executable files are hashed with the attributes'
           conversions, and the mode comes from the executable bit;
         - a symlink is recorded as mode `120000` with the link text as
           its blob, never followed;
         - a deleted file is removed.
       - Gitlinks: never by reading the nested repository. Git would
         resolve the submodule's HEAD, and runner-side git must not enter
         a codex-writable repository. Each gitlink comes from codex's
         index as data. For every `160000` stage-0 entry there, and for
         every path in `G`, the commit step runs `git update-index
         --cacheinfo 160000,<oid>,<path>`. It uses `--force-remove` when
         codex's index has no entry and the path is gone from the work
         tree. A gitlink in `U` must have its oid among the snapshot's
         stage 1-3 oids for that path: base, ours or theirs, the only
         commits sync's conflict offered. Any other oid is refused. A
         gitlink outside `U` (a submodule bump codex staged) is taken as
         codex staged it, like any other agent change, and lands in the
         bundle for land's usual checks. `.gitmodules` stays tier 1. A
         pointer codex moved in the nested checkout without staging it is
         not picked up. The codex prompt says to stage submodule
         pointers, and that is the only behaviour change for honest runs.
     - **The final tree must equal the evidence.** After staging, for each
       path in `U`, the private index's entry (`ls-files --stage -z`) must
       equal codex's stage-0 entry in mode **and** object id, or both must
       be absent. Because the private index was built by the same `git
       add` codex ran, a staged resolution that the work tree still holds
       compares equal for every mode. That covers "ours unchanged", a
       symlink, an executable bit and a submodule choice. A file edited
       again after staging, a symlink replaced by a file, or a changed mode
       compares unequal and is refused rather than silently re-resolved.
     - The existing marker scan over sync's conflict list stays, for
       regular files only.
     - Only then does the commit step commit, completing the merge with
       the snapshot's `MERGE_HEAD`. Any mismatch, parse error or failed
       staging command fails the guard. That is fail-closed: nothing lands,
       and the Surface error says why.

     The index is a git-format data file. Git parses it but executes
     nothing from it: its extensions name files in the git dir (the
     private one) and config the private dir does not set. Its object ids
     need not exist in the private object store for `ls-files --stage`.
     A gitlink oid needs no object at all, and a staged blob's content is
     recomputed from the work tree by the private `git add`, so the
     comparison needs no object codex wrote.
  6. **`review-codex`** gains the reclaim and the import. Its `Prepare
     Codex review for landing` reads the imported copy, not the codex-owned
     file (claude-review.yml:2669).
  7. **Cron and at are denied** to `codex` (`/etc/cron.deny`,
     `/etc/at.deny`, written before the user's first process).

  The hosted test (codex-path-smoke) runs a hostile codex stand-in that:
  - plants `core.fsmonitor` and a hook in the workspace `.git` config,
    each writing a marker file;
  - plants a symlink inside `.git` aimed at a runner file;
  - leaves a respawning process;
  - tries a crontab.

  The stand-in is launched **through the real codex-action step** at
  f367b1e, with a dummy API key: a root-owned test `codex` first on PATH
  is what the action's `which codex` finds, so the action's own `sudo -u
  codex --` launch path is exercised. The stand-in records
  `/proc/self/cgroup` for itself and a forked child, then keeps
  respawning. The test also records the proxy's cgroup. It passes when:
  - the stand-in and its child are in the jail, and the proxy is not;
  - the provisioning recipe's process is in the jail too;
  - the reclaim either empties the jail or fails;
  - the guard, commit and emit steps run, or are skipped, without writing
    either marker;
  - the runner file is untouched;
  - the job's last `always()` step finds neither marker.

  The unit test in point 2 covers the post actions, which run after that
  last step. A second case forces the reclaim to fail and asserts that
  every git-running step was skipped. A third case makes `jail-exec`'s
  attachment fail, by pointing it at a missing cgroup, and asserts that
  the action step failed with no process of uid `codex` ever started.

  Cost:
  - a full `.git` copy per codex run (seconds to tens of seconds on
    inspect_ai's history);
  - a cgroup jail entered through a `sudo` shim that depends on
    codex-action's by-name `sudo`, which ties the codex jobs to a pinned
    action ref;
  - a checkout step of our own;
  - an index-evidence guard;
  - the maintenance of all of it, for the codex jobs only.

  Each of these adds to the case for option (a).

**Recommendation: (a).** It gives both engines one boundary, and it
retires the code the five findings live in rather than patching it. The
key's longer exposure window is the price. It is bounded by the project's
hard budget and scope, and it is Ransom's to accept. OpenAI documents
enforced project spend limits, with a short enforcement delay
(developers.openai.com/api/docs/guides/spend-limits). Deployment still
has to confirm the limit is set to enforce on the project the key belongs
to. If he declines (a), step 7 below is (b).

### What changes in SECURITY.md

The implementation PRs change these (the text lands with the step that
makes it true):

- **Guarantees, first bullet** (step 6): "A job that runs an agent holds
  nothing the agent may not have: its read-only job token, the declared
  model credential, and an OIDC request token no relying party exchanges
  for more (the Claude GitHub App is not installed on Meridian
  repositories). No token of the machine account or of any App reaches
  it."
- **Guarantees, provisioning/agent-user bullet and "no root for the agent
  uid"**: kept, reworded as defence in depth. They are no longer the
  reason a runner compromise is harmless.
- **Guarantees, the codex PATH bullet**: hygiene under open question 1
  (a), unchanged under (b).
- **Guarantees, manifest bullet**: adds that on the reusable workflows'
  land jobs every post lands on the run's own issue or PR. That covers
  `comments[]`, `pr.issue`, `replies[]` (own-PR review comments only) and
  thread resolutions, with `issues[]` refused. Posts are capped where they
  are not bounded by the PR's own objects, and every body passes the
  de-fang registry. These rules are enforced by the validator and land,
  and no policy lives only in the agent job. Direct callers get the same
  rules through the inputs, and keep today's defaults until those flip.
- **Guarantees, CI-fix bullet**: the refund sentences go. Every round
  counts, and refunds rest on nothing the agent job reports.
- **By design, first bullet** (the Claude App's token in the agent job)
  and the `claude[bot]` verdict-author text: deleted.
- **By design, the reviewer bullet**: "cannot post as `claude[bot]`
  either" becomes moot and goes. "A comment on another thread, a
  caller-repository issue write" become refused (`comment-numbers: event`,
  `allowed-issue-repos: ""`), leaving the stage move as the one board
  write a forged review manifest can make.
- **Adding or changing a workflow, first bullet**: the exceptions list
  loses "the Claude action's own token". Every claude-code-action step
  passes `github_token: ${{ github.token }}`. A job requests
  `id-token: write` only where it uses WIF.
- **Build and dependency configuration** (tier 2), below.

### The tier-2 opt-in's premise

SECURITY.md → Build and dependency configuration lets tier-2 files land
because "every automated agent job that executes them does so only as an
unprivileged user holding the read-only job token (and the model
credential)". Its premise was the uid boundary. Before step 6 that
premise is weaker than written: the five findings are routes from the
unprivileged user to `runner`, and `runner` reaches the App token. After
step 6 the premise holds at the job level. A job that executes tier-2
files holds nothing beyond the read-only token and the model credential,
whoever inside it is compromised. The rule's text becomes those two
conditions:

1. the job holds nothing beyond that (the boundary);
2. its provisioning and agent run as an unprivileged user (depth, kept as
   a requirement because every agent job already meets it).

The CI half of the rule (no secret, App token, `id-token: write` or write
permission in a workflow that runs agent-landed code) is unchanged.
Uninstalling the App also removes the one way a CI job that broke that
rule by holding `id-token: write` could reach a write token. That is a
side benefit, not a relaxation. Finding 4628446's accepted criterion 2
text is updated to name the job-level premise.

## Alternatives considered

- **Patch the uid boundary instead** (fix the five findings in place).
  That is open question 1 (b), applied to both engines. It keeps the App
  token one uid away from every agent-written file, the argument that has
  needed a patch every few days since 2026-09-22, and it leaves
  `claude[bot]` trusted. Rejected for the Claude jobs, where the other
  route is cheap. It is still an option for codex.
- **Move the composers whole into a new trusted `compose` job** (or into
  the land job). The composers read the agent's repository (`rev-parse`,
  `merge-base`, `log`, the local branch name), step outcomes and the
  execution file. The execution file is the transcript, and I6 forbids
  uploading it. The land job would need a materialized repository before
  composing, a second copy of the workspace facts, and the transcript or
  a digest of it. The composers' **policy** is small, and that is what
  this design moves: comment targets, caps, the footer, hand-back
  eligibility, the mention. A separate job also costs a runner start per
  run and a trusted-to-trusted hand-over. That hand-over would itself
  have to travel as job outputs (the artifact rule). This would also work,
  but moving the rules into land is simpler, and it puts them next to the
  validator where every direct caller gets them too.
- **Keep claude.yml in tag mode with a write-capable token.**
  - The job token with issues and pull requests write would give the
    agent (whose `GH_TOKEN` is the job token) a write channel.
  - A second, write-capable token passed only to the action would be a
    credential the agent may not have, in the job.
  - Either breaks the boundary.
- **Keep tag mode and let `createInitialComment` fail softly.** There is no
  input for that, and prepare throws on the failure (Current behaviour).
  It would take a fork of the action.
- **Drop the tracking comment entirely.** The 👀 reaction and land's own
  posts already tell the requester that the run started and how it ended.
  The comment is also where a human finds the run link while it runs, and
  the checkout skill's fallback reads it. Ransom decided to move it
  (decision: Ransom, 2026-09-29), so it moves.
- **Pass `github_token` without uninstalling the App.** This stops the
  action from minting. It does not stop a runner compromise, which
  exchanges the job's OIDC token itself (token.ts:107-123). Uninstalling
  is what closes it (step 6).
- **Keep refunds on API evidence.** The Jobs API's step conclusions and
  the job's `runner_id` are either reported by the same runner or, for a
  job that never got a runner, cover a case that already keeps its round
  (a pending job cancelled before start). There is no trusted "never
  entered" signal. Open question 2 keeps a bounded variant open.

## Compatibility and migration

- **Stub callers** (actions, inspect_flow, inspect_harbor,
  inspect_sandboxes, inspect_scout, inspect_swe, inspect_vscode, ts-mono,
  the inspect_ai fork).
  - No stub passes `github_token`, `reviewer_login` or
    `review_allowed_bots`, so the new defaults need no stub change. The
    fork's reviewer stub loses one line (step 2).
  - Stub jobs keep granting `id-token: write`. The reusable codex jobs
    request less, which is allowed.
- **Direct callers of the composites @main** (inspect_flow's two
  workflows, ts-mono `dependabot-fix.yml`, actions
  `triage-test-failures.yml`).
  - Every new land input defaults to today's behaviour, and
    emit-landing, the launcher, the reclaim and the import keep their
    interfaces.
  - The defang additions apply to their bodies too: the codex footer and
    the reopen prefix now read as `engine  codex` and a broken prefix.
    None of them relies on the footer, since only the codex reviewer
    writes it.
  - Flipping the `comment-numbers` and cap defaults later is a separate,
    announced change.
- **Loop behaviour.** Removing the refunds means an infra failure before
  the agent step spends a round. `fix_attempt_cap` defaults to 3
  (claude-auto.yml:99-106), so a flaky provisioning costs one attempt in
  three and escalates to a human sooner. Open question 2.
- **claude.yml users.**
  - The status comment is posted by the machine account, not
    `claude[bot]`, and is top-level on review-comment triggers.
  - The context now follows tag mode's (the trigger-time snapshot, the
    issue history, the images), so what the agent sees is unchanged,
    unless step 4's private-repository check shows that the job token
    cannot fetch attachments (Design → Context prompt).
  - A closed PR with a live head branch is continued on that branch,
    where tag mode cut a new branch that could not land. One with a
    deleted branch gets a comment-only run.
  - Agent mode's configuration restore uses the PR's base, since
    claude.yml passes it as `base_branch`.
  - The issue-run branch name changes from the action's timestamp form
    to `claude/issue-N-<run_number>`, still under the land prefix.
- **Claude GitHub App uninstall** (step 6). These stop working on the
  repositories it is removed from:
  - web sessions on private repositories reached through the App
    (`/web-setup` sessions keep working);
  - PR auto-fix;
  - project cloud threads;
  - `claude --cloud` cloning;
  - managed Code Review.

  Step 5 checks whether anyone uses them. Historical `claude[bot]`
  comments stay but are no longer trusted.
- **Stored formats.** None. The manifest schema gains no field.
  `comments[].review` on the codex review uses an existing field.
  Existing codex reviews keep their footer, and pr-feedback-context keeps
  anchoring on it.

## Security

Untrusted input reaching the new or moved code:

- **The whole agent job and its outputs.** The design's premise. Land's
  uses are listed under The boundary. The removed `mention` and
  `agent_skipped` outputs were the two whose forgery mattered.
- **Event and API text in the context prompt** (issue and PR bodies,
  comments, reviews, file names in review threads). It reaches the prompt
  through a file, never a `run:` expansion, and is data to the model
  (prompt injection is the agent's existing threat model).
  - The trigger-time snapshot keeps an author from replacing the context
    after a maintainer authorized the run. Today's codex dev path, which
    reads the PR body and thread live, gains the same protection.
  - Anything created or edited at or after the trigger is dropped, so a
    racing edit is lost, not trusted.
- **Downloaded images.** The runner fetches them only from the attachment
  hosts, with no redirects off them, within count and size caps, into a
  runner-owned directory. It never opens or decodes them. The agent sees
  them through a read-only bind.
- **Agent text in bodies land posts.** Now de-fanged for every marker any
  consumer keys on. The class test fails when a consumer adds a marker
  the de-fang misses.
- **The tracking comment.** Its bodies are composed from the event (the
  requester's login, the run URL) and land's outputs (PR number, the
  validated branch name). No agent text is included. The branch name has
  passed the validator's charset rule and the `claude/issue-N-` prefix.
- **The settings path** (4773338). It comes from a trusted stub input,
  but the file is the checkout's. The helper refuses symlinks and
  out-of-tree paths.
- **OIDC.** After step 6 the job can still mint JWTs for any audience.
  Step 5's audit is what makes "no relying party grants more" true, and
  it has to stay true. A new OIDC trust (a cloud role, a publisher) that
  does not pin its workflow must not be added for Meridian repositories.
  SECURITY.md → Adding or changing a workflow gains that line. Sigstore
  accepts any token, but binds the certificate to the agent workflow's
  identity, so a verifier pinned to a release workflow rejects it.
- **What this does not close.**
  - A compromised agent job can still land what the agent itself could
    have requested: commits (validated, tier-1 refused), comments on its
    own thread, and a hand-back on a counted round.
  - Before step 6, while the App is installed, a runner compromise can
    still mint an App token. Steps 1 to 4 remove every reason to trust
    `claude[bot]` and every token the action mints, but not the exchange.
    So step 6 should follow step 4 promptly.

## Testing

Unit tests (`python3 -m pytest`, CI `tests / pytest`):

- **test_engine_job_isolation.py.**
  - Every claude-code-action step in the four workflows passes
    `github_token: ${{ github.token }}` and no `trigger_phrase` or
    `label_trigger`.
  - No codex job requests `id-token: write`.
  - No agent job declares an output named `mention` or `agent_skipped`.
  - No land job reads `needs.<agent job>.outputs.*` other than
    `claude_outcome`, `agent_launched` and `agent_outcome`.
- **test_review_fix_gate.py, test_review_trig.py.**
  - `REVIEWER_LOGINS` and the defaults name no `claude[bot]`.
  - A `claude[bot]` verdict is ignored.
  - The reviewer still admits the machine App's `@review` (`allowed_bots`
    composition).
- **test_skill_resolution.py.** promote.sh ignores a `claude[bot]`
  verdict.
- **test_validate_manifest.py.** Adversarial manifests, one per posting
  path:
  - `--comment-numbers event` refuses a comment on another number and
    accepts the event's.
  - It also refuses a `pr.issue` other than the event's issue.
  - The reviewer's land inputs (`allowed-issue-repos: ""`) refuse the
    review's probe manifest: one review comment plus 51
    `issues[].comment_on: 999`. They also refuse a single `issues[]`
    entry, and a `reopen`.
  - `--max-comments` and `--max-review-comments` refuse over-cap
    manifests (4773341's forged manifest).
  - The defaults keep today's acceptance.
- **test_land_helpers.py.**
  - `defang` splits `engine: codex` and a leading `Reopened — upstream
    PR`, case-insensitively.
  - A `review` comment gets `<!-- claude-review-comment -->` after the
    de-fang on a codex review too.
  - The plan step drops `handback` under `allow-handback: "false"`, and
    `stage-override` replaces or removes the manifest's `stage`.
  - The post step, against a stub `gh`, skips and reports a reply to a
    review comment id that is not on the PR, and posts one reply per id.
  - A new class test extracts every marker pr-feedback-context,
    atlas_sync and the loop gates key on in machine-account comments, and
    requires each to be in defang's list or on land's appended-after list.
- **test_review_composer.py.**
  - The codex composer flags its comment `review` and writes no footer.
  - The reviewer's land job runs the moved `who` and loop-ownership reads
    (lifted, against a stub `gh`).
- **test_ci_fix_gate.py, test_ci_fix_composer.py, test_review_fix_composer.py.**
  - The refund steps are gone.
  - The cap sequence (gate count → escalation) holds with every round
    counted.
  - The existing refund-sequence tests are rewritten as "a skipped agent
    step keeps its round".
- **test_dev_agent_composer.py, test_dev_agent_trig.py.**
  - The composer reads the `Prepare branch` output.
  - `Prepare branch`, lifted and run against local repositories:
    - an issue run;
    - an open PR with a clean merge, and one with a conflicted merge left
      for the agent (index and `MERGE_HEAD` untouched);
    - a closed PR whose head is live at the gate's `start_sha` (checked
      out, no merge), including a fork-shaped case: a closed `meridian`
      PR whose same-repository branch backs an upstream PR;
    - a closed PR whose live tip moved past `start_sha` (refused);
    - a closed PR with a deleted head (`comment-only`, read-only
      landing).
  - The gate's `trigger_time` step against stub payloads: comment, review,
    review comment, `issues` opened, labeled (event-history lookup and
    its fallback) and assigned.
  - The gate's tracking-comment step and land's finishing step, lifted
    and run against a stub `gh`, produce the fixed bodies for pushed, no
    change, failed and cancelled runs, and never include agent text.
- **test_codex_path.py** (the prompt steps), and a new
  **test_dev_agent_context.py** for the composite, run against a stub
  `gh`:
  - Both engines' prompts include the context file.
  - Issue runs include the issue's comments, oldest first, with machine
    control comments dropped, the 30-comment and 40,000-character bounds,
    and the "N earlier comments omitted" line. The cases are a comment
    that says "implement the second option above", a `labeled` trigger
    after a discussion, and an `assigned` trigger.
  - Post-trigger content is excluded:
    - a body edited after the trigger (the payload's body is used);
    - a comment created after it;
    - a comment edited after it;
    - a review submitted after it;
    - a review-thread comment added or edited after it. For PR runs this
      goes through pr-feedback-context's `trigger-time` input.
  - pr-feedback-context with no `trigger-time` input produces exactly
    today's output (the loops).
  - A failed comment fetch fails the step after three attempts.
  - Images: only the allowed hosts are fetched, the caps hold, a failed
    download leaves the link, and nothing is followed off-host.
- **test_claude_agent_launcher.py.** Launch in job-token mode (the loops'
  and dev agent's new shape) passes, and launch step 6 is gone.
- **The settings helper.** A new test lifts it and covers a regular file,
  a symlinked file, a symlinked directory component, `..` out of the
  workspace, a FIFO and an oversize file.
- **Codex, under option (b) only.**
  - No codex job uses `actions/checkout`, or any action outside the
    allow-list of actions known to register no git-running post step.
  - Every post-codex git step sets `GIT_DIR`/`GIT_COMMON_DIR` to
    `$RUNNER_TEMP/runner-git`, and is gated on the reclaim's `== success`.
  - The guard and commit step, lifted and run against a local repository
    with a conflicted `MERGE_HEAD` snapshot and a separately staged
    "codex" index copy.
    - Refused:
      - an untouched text conflict (markers), binary conflict or
        modify/delete conflict;
      - a staged file edited again after staging;
      - a staged symlink replaced by a regular file;
      - a submodule conflict resolved to an oid outside the snapshot's
        stages;
      - a missing or corrupt index copy when the snapshot has a conflict.
    - Accepted:
      - a staged text resolution and a staged binary choice;
      - a `git rm` of a modify/delete path;
      - a staged "ours unchanged";
      - a staged symlink resolution, with its link text as the blob;
      - an executable-bit resolution;
      - a real submodule conflict, a repository with a `.gitmodules` like
        inspect_ai's and inspect_scout's ts-mono, resolved to theirs with
        `git add <submodule>`, with no runner-side git process ever
        started inside the submodule (asserted with a planted
        `core.fsmonitor` in the nested repository's config).

      Each commits a two-parent merge whose tree equals the staged
      resolution, mode and oid for mode.
    - With no conflict and no submodule, the index is never imported:
      - an ordinary unstaged edit, with the runner-owned index untouched,
        commits;
      - a no-change answer lands nothing and raises no error;
      - a codex-owned replacement index is not read.
    - With a submodule and no conflict, an index owned by `runner` and one
      owned by `codex` are both imported. A staged submodule bump lands,
      and an unstaged one is ignored.
  - `jail-exec`, lifted: it refuses every argv but `-u codex [--] …`, and
    exits non-zero before dropping privileges when the cgroup write or
    the read-back fails. The `sudo` shim routes `-u codex -- …` and `-u
    codex cat <file>` (the finalizer's form) through it, and passes every
    other argv through unchanged.
  - Structurally: every codex-uid launch (`sudo`, `runuser`, `su`,
    `setpriv`) in the workflows and composites goes through `jail-exec`;
    `/opt/meridian-codex/bin` is the only `GITHUB_PATH` entry in the codex
    jobs; codex-action is pinned to f367b1e.
  - `review-codex` runs the reclaim and the import.

Hosted canaries and smoke runs (these need real runners; none needs a
model or a real secret):

- **engine-isolation-canary.yml, pipeline-probe.**
  - Add a probe in the Claude agent job's shape that, as `runner`,
    requests an OIDC token for audience `claude-code-github-action`, POSTs
    it to the exchange and records only the HTTP status, never a token.
  - Before step 6, on this repository, the exchange succeeds. That is the
    positive control and the evidence for this design. If a token comes
    back the probe revokes it at once (`DELETE /installation/token`) and
    prints nothing from it.
  - After step 6 it must fail, and that failure is the proof of step 6.
  - Add the same probe to a codex job, where it must fail after step 1
    because the job has no `id-token: write`.
- **root-boundary-smoke.yml.** Unchanged (the uid depth).
- **codex-path-smoke.yml.**
  - Under (a), unchanged, as hygiene.
  - Under (b), extended with the hostile stand-in described under Design
    → Codex jobs (b), run with the real codex user, cgroup and sudo: the
    planted fsmonitor and hook never fire, the planted `.git` symlink's
    target is untouched, and the respawning process is killed or the
    reclaim fails with every git step skipped.
- **A private test repository** (step 4). A `@claude` run on an issue
  with an uploaded image checks that the job token resolves the
  attachment and that the agent sees the file.
- **Live runs** on the inspect_ai fork and this repository after steps 1
  and 4: a `@claude` issue run and a PR follow-up (the status comment,
  branch and PR), a `@review` on each engine, and a loop round of each
  kind. What to look for: no `claude[bot]` post, and the codex review
  anchored for the next round.

## Implementation plan

Each step is one PR in this repository unless it says otherwise, runs
`actionlint` and `python3 -m pytest`, and updates the design and
SECURITY.md text that its change makes true.

0. **Settings path (4773338).** The helper in the three `Compose …
   settings` steps (claude.yml, claude-auto.yml, claude-auto-review.yml)
   and its test. It is independent of everything else.
1. **Stop trusting `claude[bot]`, and pass `github_token` in the reviewer
   and both loops.**
   - Workflows: claude-review.yml, claude-auto.yml, claude-auto-review.yml
     (`github_token`, `REVIEWER_LOGINS`, defaults, `additional_permissions`),
     plus `id-token: write` removed from the four codex jobs (claude.yml
     too).
   - Composites and skills: pr-feedback-context (anchor authors),
     skills/promote (promote.sh, SKILL.md).
   - Docs: README.md:257, credential-separation.md → 3.5.
   - Tests: test_review_fix_gate, test_review_trig, test_skill_resolution,
     test_engine_job_isolation.
   - The canary's OIDC exchange probe lands here too, recording today's
     positive result.
2. **The fork's reviewer stub** drops `allowed_bots: "claude[bot]"`: a
   companion PR on meridianlabs-ai/inspect_ai `meridian`, from a
   maintainer's machine. It can merge before or after step 1.
3. **Land enforces the composers' rules; refunds go.**
   - The land composite and validator: `comment-numbers`, `max-comments`,
     `max-review-comments`, `allow-handback`, `stage-override`, the de-fang
     additions and
     the codex review's `review` flag.
   - The four land jobs pass the strict values, and the reviewer's also
     passes `allowed-issue-repos: ""`.
   - Land checks reply ids against the PR's own review comments.
   - The reviewer's `who` and loop-ownership reads move to its land job.
   - Both loops lose their refund steps and `agent_skipped` outputs.
   - Tests as listed.
   - Before merging, check that inspect_flow's two workflows, ts-mono's
     and actions' triage pass `comment-numbers: event` cleanly against
     their recent manifests (their composers pin numbers already). This
     is the evidence for flipping the default in a later PR.
4. **claude.yml to agent mode.**
   - The `dev-agent-context` composite, used by both jobs, with the gate's
     `trigger_time` output.
   - pr-feedback-context's `trigger-time` input.
   - The launcher's read-only `context-dir` bind.
   - The `Prepare branch` step, `base_branch` from the PR's base, the
     prompt input and `github_token`.
   - The private-repository image check. If the job token cannot fetch
     attachments there, stop and ask Ransom before merging.
   - The gate's status comment and land's finishing step.
   - pr-feedback-context's `dev-agent-status` filter.
   - Launch step 6 and the post-agent `reset-origin-url` retired, in all
     three Claude workflows.
   - The composites stay backward compatible: the launcher keeps working
     for a caller that still passes no `github_token`.
   - After this step, check that no `claude[bot]` post appears on any
     Meridian repository for a week of normal use.
5. **Check nothing else relies on the App.** A checklist in the step-6
   PR, each item with its evidence:
   - Claude Code on the web: who uses browser-onboarded sessions on
     private Meridian repositories, auto-fix, project threads,
     `claude --cloud` or managed Code Review (ask the team; the org's
     Claude admin console lists Code Review).
   - The org's installation list (`admin:org`: Ransom): which
     repositories have the App.
   - PyPI and npm trusted publishers: each pins its own workflow file
     (and environment where set), read on pypi.org and npmjs.com.
   - No other OIDC trust names a Meridian repository.
   - A `claude[bot]` post search over the step-4 week returns nothing.
6. **Uninstall the Claude GitHub App** from the Meridian repositories
   (org admin: Ransom).
   - Run the canary: the exchange now fails.
   - Then land the SECURITY.md rewrite (What changes in SECURITY.md), the
     tier-2 premise text, credential-separation.md's I1 and I5, and
     AGENTS.md's paragraphs that describe the reclaim as the boundary.
7. **Codex, per open question 1.**
   - (a): Ransom creates the project-scoped, capped key and replaces
     `OPENAI_API_KEY`. This repository's PR updates the exception text and
     reclassifies the codex rows as hygiene in AGENTS.md and
     codex-engine.md.
   - (b): the fail-closed teardown under Design → Codex jobs (b).
     - The runner-private git dir copy (create-codex-user).
     - The codex jobs' own checkout step.
     - The jail cgroup, `jail-exec` and the `sudo` shim
       (create-codex-user, a new `/opt/meridian-codex/bin`), the
       provision-fallback launch through `jail-exec`, codex-action pinned
       to f367b1e, and the reclaim's `cgroup.kill`
       (reclaim-codex-workspace).
     - The reclaim's conditional index import, with the `runner`/`codex`
       owner list in `import_codex_final.py`.
     - The guard's mode-aware evidence check, the gitlink staging through
       `update-index --cacheinfo`, and the final-tree comparison
       (unresolved-merge-guard and the three commit steps).
     - `codex-home.sh` and `assert-runner-only-path` routed through
       `jail-exec`.
     - The reclaim reduced to checks plus revocation.
     - The post-codex steps' `GIT_DIR` pins.
     - `review-codex`'s reclaim and import.
     - The cron and at denial.
     - codex-path-smoke's hostile stand-in.

   It is independent of steps 3 to 6 and can go in parallel.

## Open questions

1. **Codex key: (a) extend the model-credential exception to a
   project-scoped, spend-capped OpenAI key, or (b) keep the codex jobs on
   the uid boundary with the fail-closed teardown?** Recommendation: (a),
   with the project's spend limit confirmed as enforcing (OpenAI documents
   enforced project limits, with a short delay). Design → Codex jobs has the
   trade-off.
2. **Refunds: drop them, or keep a bounded refund budget?** Recommendation:
   drop them. With them gone every round counts, and nothing the agent job
   reports decides the count. The alternative is one refund per loop
   engagement, recorded by the land job in the trusted counter comment
   (`refunds: 0/1`). It is bounded whatever the job forges, but it adds a
   field to two counter formats and to the reset composite for a blip that
   costs one attempt.

## Not this design

- **meridianlabs-ai/actions ci-perf and triage** (`actions/isolated-agent`,
  `model-broker`).
  - The broker holds an Anthropic API key, which is not a declared
    exception, as the `model-broker` uid on the agent's VM. A job split
    cannot move it off that VM.
  - Options: (i) move those workflows to WIF, the declared exception, and
    drop the broker; or (ii) keep the broker and drop `runner`'s sudo
    before the agent starts (`drop-runner-root`), so that a runner
    compromise cannot read the broker's memory.
  - Related findings:
    - 4773274: post-agent symlink reads;
    - 4773473: an unpinned `npm install -g @anthropic-ai/claude-code` as
      root after the broker holds the key;
    - 4773275: an unpinned `pip install pytest` as the sudo-capable
      runner after the broker holds the key.
- **Engine labels on PRs are unverified** (4773334, 4774318). A PR's
  `engine:codex` label is read without a labeller check
  (claude-review.yml:634-656, claude.yml:787-896, claude-auto.yml:538-540,
  claude-auto-review.yml:584-586). Under open question 1 (a) this routes
  to a job that holds nothing more. Under (b) it routes to the job step 7
  fixes.
- **Pinning claude-code-action** (from executed-paths-residual.md). Still
  worth doing. It is less urgent once the action holds no App token.
- **Retiring the uid machinery outright.** Once the job is the boundary,
  the reclaims, PATH walks and env pins could go, simplifying every agent
  job. This design keeps them (cheap, shared with codex, and required
  under (b)). Whether to remove them is a later cleanup.
- **`Verify a review landed`** still trusts `claude_outcome` and
  `agent_launched` for a nudge. Deriving it from the manifest in land
  (verdict present or not) would drop two more outputs. It is harmless as
  it stands.
