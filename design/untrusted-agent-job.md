# The untrusted agent job

Status: proposed, 2026-09-29. Issue: none (task from Ransom, 2026-09-29).
Ransom decided the two open questions on 2026-09-29: the codex jobs use
OpenAI API Platform workload identity federation, and loop refunds are
dropped.
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
| 4773338 | Medium | `Compose agent settings` reads `settings` with `[ -f ]` and `cat`, following a symlink committed in the checkout, as `runner`, before the agent user exists: claude.yml:1295-1297, claude-auto.yml:1119-1121, claude-auto-review.yml:1283-1285. Fixed by step 0. |

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
  credential. For codex, that credential is OpenAI API Platform workload
  identity federation (decision: Ransom, 2026-09-29).
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
  exception (AGENTS.md → Adding or changing a workflow; decision: Ransom,
  2026-09-23). It stays as it is, and it now covers codex's federated
  OpenAI token too (Design → Codex jobs).
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
design (Claude jobs after step 6; codex jobs after step 7):

| Held in the job | Reachable by `runner`/root | The agent may have it because |
|---|---|---|
| Job token: contents, pull requests, issues and actions read | yes | it is the agent's own `GH_TOKEN` (the launcher gives it) |
| OIDC request token (`ACTIONS_ID_TOKEN_REQUEST_*`) | yes | no relying party grants more than the model credential. Anthropic WIF and, for the four reusable workflows' jobs, the OpenAI federation mapping are the declared exception. The Claude App exchange has no installation to issue from after step 6. PyPI and npm publishers pin their own workflow file (step 5 verifies). Codex jobs lose `id-token: write` in step 1 and regain it in step 7, after step 6 |
| WIF JWT and the Anthropic access token | yes | the declared model-credential exception |
| The exchanged OpenAI token (codex jobs, in the `openai-wif-proxy` process) | yes | the declared model-credential exception, job-bound like Anthropic's: at most an hour, never beyond the OIDC token |
| `ACTIONS_RUNTIME_TOKEN` | yes | cache access is read-only (`cache-mode: read`). Artifact upload in this run reaches only the `landing` artifact, which land already treats as untrusted, and no trusted job consumes an artifact (the rule below) |
| `OPENAI_API_KEY` | **no job references it** after step 7 | — |
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
`agent_skipped` outputs are removed, and every round counts, with no
bounded refund budget (decision: Ransom, 2026-09-29).

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
  in a codex job mint the App token. Step 7 restores it for the OpenAI
  federation, after step 6 has removed the App.
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

| Mechanism | Before | After (both engines; codex jobs from step 7) |
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

Until step 7 the codex jobs still hold `OPENAI_API_KEY`, so there these
rows remain a boundary with its five open findings. That is the status
quo, and the reason step 7 follows step 6 closely.

**4773340 (cron).** With no App token on any argv after steps 1 and 4, an
escape from the namespace reaches the host as `claude-agent`. From there
it can read world- or group-readable runner files and see the argv of
runner processes, and holds nothing more than the job token. Denying
cron and at to the agent users (`/etc/cron.deny`, `/etc/at.deny`, written
by `create-codex-user`) is cheap depth for both engines.

**4773338 (settings path).** This is independent of the rest and ships
first. The three `Compose … settings` steps keep accepting a path, because
ts-mono uses `.github/claude-settings.json`, but read it through a small
Python helper (`compose-settings`, Implementation plan → step 0). The helper resolves the path under `$GITHUB_WORKSPACE`,
refuses it if any component is a symlink or the resolved path leaves the
workspace, opens it `O_NOFOLLOW`, requires a regular file and caps its
size. inspect_harbor's inline JSON is unaffected. `.github/` is tier 1,
so no agent can land a change to ts-mono's file.

### Codex jobs: OpenAI API Platform workload identity federation

Decision (Ransom, 2026-09-29): "let's go with API Platform WIF". The codex
jobs stop using the long-lived `OPENAI_API_KEY` secret. They authenticate
to OpenAI with OpenAI API Platform workload identity federation: the job's
GitHub OIDC token is exchanged for a short-lived bearer token of a project
service account. That makes the codex credential job-bound like the
Anthropic WIF token, so the codex jobs fall under the same declared
model-credential exception. They then hold nothing the agent may not have,
and the uid machinery is hygiene there too (What stays in the untrusted
job). This is the API Platform flavour, not Codex's ChatGPT-workspace
federation (beta, `chatgpt_account_id`, in openai/codex
`codex-rs/workload-identity`). openai/codex-action has no federation
support of its own.

Two alternatives were on the table: a project-scoped, spend-capped
long-lived key, and keeping the codex jobs on the uid boundary with a
fail-closed teardown. Both are recorded under Alternatives considered.

**What the OpenAI documentation says** (developers.openai.com, guides
`workload-identity-federation` and `…/github-actions`, and reference
`workload-identity-federation`, read 2026-09-29):

- **The exchange.**
  - The request goes to `POST https://auth.openai.com/oauth/token`, with
    `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`,
    `identity_provider_id`, `service_account_id`, `subject_token_type`
    (`urn:ietf:params:oauth:token-type:jwt` or `…:id_token`) and
    `subject_token` (the GitHub OIDC JWT).
  - The response carries `access_token`, `token_type: "Bearer"`,
    `expires_in`, `expires_at` and, when the mapping sets permissions,
    `scope`. **No refresh token** is issued: renewal is a fresh exchange
    with a fresh subject token.
- **Lifetime.** The token lasts "at most one hour", and "a JWT exchange
  token never outlives its external subject token". GitHub does not
  document its OIDC token's lifetime (docs.github.com → Actions → OIDC
  reference lists `exp` without a duration). claude-code-action rewrites
  its identity token every 4 minutes (workload-identity.ts), which points
  to a lifetime of minutes. So **the design assumes the exchanged token
  lives only minutes**, not an hour. The canary measures the real value.
- **Authorization.** Tokens authorize "like service-account API
  credentials", backed by the service account's project roles and
  narrowed by any mapping permissions. The exchange "never creates
  principals, projects, or workspace membership".
- **Mappings.**
  - A mapping matches raw claims (`sub`, `aud`, `iss`) or `openai.*`
    attributes derived with CEL.
  - Mapping values must be scalar JSON values. A string may end in one
    trailing wildcard after a non-empty prefix. Mid-value and path-style
    wildcards are unsupported.
  - A CEL transformation reads the verified claim set as `assertion` (for
    example `assertion.repository`), and may yield a string, `true` or
    `false`, an integer or a number. The guide's own boolean example is
    `assertion.ref == "refs/heads/main"`.
  - Conditions within one mapping are ANDed, and "if more than one enabled
    mapping matches an exchange, OpenAI rejects it".
  - The GitHub guide names `job_workflow_ref` among the "run and job
    identifiers that can help with auditing or more advanced trust rules",
    beside `run_id`, `run_number` and `run_attempt`. It recommends
    `workflow_ref` over `workflow` for privileged mappings.
- **Availability.** The API Platform guide carries no beta label; only the
  Codex flavour is marked beta. The GA date (2026-05-26) is the
  coordinator's and was not found on the pages read.
- **Spend limits.** The federation pages say nothing about budgets or
  spend limits. Usage by a project service account is the project's usage,
  so project spend limits are expected to apply (OpenAI documents enforced
  project spend limits, developers.openai.com/api/docs/guides/spend-limits).
  Step 7 verifies it on the project's usage page after the first runs.
  Until then it is an expectation, not a fact.

**What codex-action's proxy accepts** (codex-action f367b1e `action.yml`
"Start Responses API proxy"; openai/codex `codex-rs/responses-api-proxy`
at 2a34aef, `read_api_key.rs`):

- The proxy reads the key **once**, from stdin at start, into a fixed
  1,024-byte buffer.
- It sends it as a static `Authorization: Bearer <key>` header.
- It accepts only ASCII letters, digits, `-` and `_`
  (`validate_auth_header_bytes`).

So an exchanged token cannot simply be passed as `openai-api-key`. Any
token containing `.`, as a JWT would, is refused at start, and a token that
does start expires within minutes, with no way to renew it. The design
therefore keeps codex-action's proxy and gives it a dummy key. It points the
proxy's `responses-api-endpoint` at a renewing forwarder of our own:

- **New composite `openai-wif-proxy`**, with its script at
  `.github/actions/openai-wif-proxy/openai_wif_proxy.py` (stdlib only).
  - **Where it runs.** It runs as `runner`, started in a step before
    `Create codex user` and after `Set up Node for codex-action`, which
    must run before `Create codex user` (design/codex-engine.md →
    Runner-side search path), as a background process in the runner's
    session.
    It gets the step's `ACTIONS_ID_TOKEN_REQUEST_*` environment. The codex
    user never shares its uid, environment or memory: the launcher's
    and `create-codex-user`'s process-isolation checks already cover
    `/proc/<pid>/environ` and memory.
  - **Inputs.** Identifiers, not secrets, like the Anthropic WIF IDs:
    `identity-provider-id`, `service-account-id` and `audience`. They are
    fixed values in the reusable workflows.
  - **Start-up.** It performs the first exchange synchronously. It
    requests a GitHub OIDC token for `audience` and exchanges it with the
    fields above. The step fails with the exchange's error (never the
    token) if that fails, and the Surface step names it, so codex never
    starts without a credential.
  - **Listening.** It binds `127.0.0.1` on an ephemeral port and writes
    the port to a runner-only file. The step outputs
    `endpoint=http://127.0.0.1:<port>/v1/responses`.
  - **Forwarding.** It forwards only `POST /v1/responses`, the one route
    codex-action's proxy itself allows (`lib.rs:170-173`), to
    `https://api.openai.com/v1/responses`. It drops the incoming
    `Authorization` and sets `Bearer <current token>`. It streams the
    response through chunk by chunk, since codex uses server-sent events,
    and caps the request body at 32 MB.
  - **Renewal.** It re-exchanges, with a fresh GitHub OIDC token, whenever
    the current token is within 60 seconds of `expires_at`. On an upstream
    `401` it re-exchanges once and retries that request, whose body it
    buffered. A failed renewal answers `502` with a fixed message. So runs
    of any length are covered, those over an hour included, as long as
    the job can still mint OIDC tokens. If GitHub or OpenAI refuses, codex
    fails its step, the round fails, and with refunds gone it counts.
  - **The token stays in memory.** It is never written to disk, never
    logged, and never returned to a client. The script masks it
    (`::add-mask::`) anyway.
- **The codex-action step** gets `openai-api-key:
  "meridian-wif-placeholder"`, a fixed non-secret value in the proxy's
  charset, and `responses-api-endpoint: ${{ steps.openaiwif.outputs.endpoint
  }}`. codex-action's proxy injects the placeholder, and ours replaces it.
  codex still gets only codex-action's proxy address.
- **What the agent can reach.** Codex, as the codex user, can reach our
  forwarder on loopback directly. It gets model responses, the credential's
  use, which the exception allows, and never the token itself. A runner
  or root compromise can mint and exchange tokens itself. That yields the
  same short-lived model credential, which is exactly the exception
  Anthropic's WIF already makes.

**The mapping** (one OpenAI project for CI agents, one service account,
exactly one enabled mapping):

- **Conditions, ANDed:**
  - `iss == "https://token.actions.githubusercontent.com"`;
  - `aud ==` a Meridian-specific audience (for example
    `openai-wif:meridianlabs-ai`), so a token minted for Anthropic's WIF
    or PyPI cannot be replayed here;
  - `repository_owner == "meridianlabs-ai"`, plus `repository_owner_id`,
    so a renamed or recreated organization does not match;
  - `openai.agents_workflow == true`, where the provider derives
    `openai.agents_workflow` with the CEL expression:

    ```
    assertion.job_workflow_ref in [
      "meridianlabs-ai/agents/.github/workflows/claude.yml@refs/heads/main",
      "meridianlabs-ai/agents/.github/workflows/claude-review.yml@refs/heads/main",
      "meridianlabs-ai/agents/.github/workflows/claude-auto.yml@refs/heads/main",
      "meridianlabs-ai/agents/.github/workflows/claude-auto-review.yml@refs/heads/main"
    ]
    ```

    That is exact membership, with no wildcard. So a different reusable
    workflow, another ref of these, or a caller's own workflow file
    derives `false`, and the mapping does not match;
  - `openai.agents_event == true`, derived as `assertion.event_name in
    ["issue_comment", "issues", "pull_request_review",
    "pull_request_review_comment", "workflow_run", "pull_request"]` (the
    events the stubs use), as depth.

  Scalar values plus derived booleans are exactly what the mapping
  interface documents. No wildcard and no change to GitHub's subject
  claim is involved. `sub` is not matched at all, so its format (default,
  or the immutable form GitHub adopts after 2026-07-15) does not matter.
  If `job_workflow_ref` were absent from a token, as for a job not in a
  reusable workflow, the CEL `in` yields an error or `false`. Either way
  the exchange is refused. The negative canaries in step 7 cover a
  non-reusable workflow, another reusable workflow of this repository,
  a wrong audience and a wrong event.
- **Callers need no mapping of their own.** The exchange runs in the
  caller repository's job, so the token's `repository` is the caller's.
  Its `repository_owner` is `meridianlabs-ai` and its `job_workflow_ref`
  names this repository's reusable workflow at `main`, which is what the
  mapping matches.
  - A caller's own workflow cannot exchange: its `job_workflow_ref` is its
    own file.
  - A workflow that calls one of the four reusable workflows can. That is
    the stubs' design, and the gate decides whether the codex job runs at
    all.
  - Fork pull requests do not reach a codex job: the gates refuse fork
    heads before the engine is chosen. GitHub also caps a fork pull
    request's token permissions. The negative canary in step 7 checks
    that a job outside the mapping is refused.

**`id-token: write` on the codex jobs.** Step 1 removes it, because until
the App is uninstalled it lets a runner compromise in a codex job mint the
App token. Step 7 restores it, for the federation, only after step 6 has
removed the App. No agent job then references `OPENAI_API_KEY`, and the
agent stubs stop passing it.

**The org secret stays.** The org secret `OPENAI_API_KEY` has consumers
outside the agent jobs. In meridianlabs-ai/actions at 3916d26,
`inspect-ai-scheduled-tests.yml:325` and `inspect-swe-nightly-tests.yml:121`
inject it into their model test suites, and the federation mapping, which
covers only the four reusable workflows, does not serve them. This design
removes every agent-job reference and every agent-stub forwarding, and
**does not delete the secret**. Deleting it needs an audit of every
consumer across the organization, and replacement credentials for those
tests. That is a separate migration, listed under Not this design.

**What this removes.** The option (b) machinery from the earlier rounds
is not built: the cgroup jail, `jail-exec`, the `sudo` shim, the
codex-action pin, the runner-private git dir, the self-made checkout and
the index-evidence guard. 4773876 (no reclaim in `review-codex`), 4773887
(refusal exits before the restore) and 4773888 (symlinks inside `.git`)
close with the boundary. A runner compromise they enable in a codex job
now reaches the read-only job token, a runtime token whose cache access is
read-only, and the exchangeable model credential, all of which the agent
may have. The App token is gone after step 6, and no key is left to steal.
The existing codex uid machinery stays as hygiene, as on the Claude jobs.

### What changes in THREAT_MODEL.md and AGENTS.md

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
- **Guarantees, the codex PATH bullet**: kept, reworded as hygiene
  (step 7).
- **Guarantees, the OpenAI-key bullet** ("A job that runs the Claude agent
  references no `OPENAI_API_KEY`") becomes "no job references
  `OPENAI_API_KEY`". The codex jobs federate (step 7). The engine split
  stays for its other reasons (one agent user and one boundary shape per
  job).
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
- **AGENTS.md → Adding or changing a workflow, first bullet**: the
  exceptions list loses "the Claude action's own token". The
  model-credential exception reads: "the model credential, which is
  workload identity federation
  only: Anthropic's, and OpenAI API Platform's for the codex jobs (a
  project service-account token exchanged from the job's OIDC token, at
  most an hour and never beyond it, held by the `openai-wif-proxy`
  forwarder). No long-lived model key is referenced by any job." Every
  claude-code-action step passes `github_token: ${{ github.token }}`. A
  job requests `id-token: write` only where it uses WIF. A new OIDC trust,
  whether a cloud role, a publisher or a model provider, pins
  `job_workflow_ref` or its own workflow file and never matches on
  `repository_owner` alone.
- **Build and dependency configuration** (tier 2), below.

### The tier-2 opt-in's premise

THREAT_MODEL.md → Build and dependency configuration lets tier-2 files land
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
  It keeps the App token one uid away from every agent-written file, the
  argument that has needed a patch every few days since 2026-09-22, and it
  leaves `claude[bot]` trusted. Rejected.
- **Codex: a project-scoped, spend-capped long-lived OpenAI key** under an
  extended exception (earlier rounds' recommended option (a)). It is
  simpler, since codex-action's proxy takes the key as it is. But a key
  taken from a compromised job stays usable until rotation or the budget,
  where the federated token dies with the job. Superseded by federation
  (decision: Ransom, 2026-09-29).
- **Codex: keep the uid boundary with a fail-closed teardown** (earlier
  rounds' option (b), reviewed in rounds 2-4). It keeps the key out of
  reach of every codex-uid process, so no exception has to be extended.
  It needs all of the following, for the codex jobs only:
  - a runner-private copy of `.git` for all post-codex git;
  - a `run:` checkout, so no post action runs git;
  - a root-owned cgroup, entered through `jail-exec` and a `sudo` shim
    that relies on codex-action's by-name `sudo`, which pins the action;
  - cron and at denial;
  - a mode-aware conflict guard that reads codex's index as imported
    data.

  Not chosen (decision: Ransom, 2026-09-29). Federation removes the
  credential that machinery protected.
- **Pass the exchanged token straight to codex-action as `openai-api-key`.**
  That fails twice. The proxy refuses any character outside
  `[A-Za-z0-9_-]`, and it reads the key once, so a token that lives
  minutes cannot be renewed (Design → Codex jobs). Hence the forwarder.
- **Give codex the token directly** (codex's own config, no proxy). The
  exception would allow it, but codex cannot renew the token either, and
  that would put the credential on disk in the codex home. The forwarder
  keeps it in one runner process.
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
  entered" signal. A bounded refund budget (one refund per engagement,
  recorded by land as `refunds: 0/1` in the counter comment) was offered
  and declined: refunds go, and every round counts (decision: Ransom,
  2026-09-29).

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
  three and escalates to a human sooner. Accepted (decision: Ransom,
  2026-09-29).
- **Codex credential** (step 7).
  - Ransom creates the OpenAI project, service account, identity provider
    and mapping. The four reusable workflows name their IDs (identifiers,
    like the Anthropic WIF IDs) and restore `id-token: write` on the codex
    jobs.
  - The stubs keep granting `id-token: write`, which they already do, and
    stop passing `OPENAI_API_KEY`. A stub still passing it is harmless:
    no agent job references it. The org secret stays for its non-agent
    consumers (actions' scheduled and nightly model tests).
  - Callers need no OpenAI configuration.
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
- **The OpenAI forwarder** (step 7). It parses the token endpoint's JSON
  and relays codex's requests and OpenAI's responses as bytes. It never
  interprets a response body, and never follows a redirect off
  `api.openai.com`. Its only client-facing route is `POST /v1/responses`
  on loopback. Error messages it returns or logs never include the token,
  the subject token or the exchange response.
- **OIDC.** After step 6 the job can still mint JWTs for any audience.
  Step 5's audit is what makes "no relying party grants more" true, and
  it has to stay true. A new OIDC trust (a cloud role, a publisher) that
  does not pin its workflow must not be added for Meridian repositories.
  AGENTS.md → Adding or changing a workflow gains that line. Sigstore
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
- **test_openai_wif_proxy.py** (new), with the forwarder's script run
  against local stub servers for GitHub's OIDC endpoint, OpenAI's token
  endpoint and the upstream:
  - The first exchange sends exactly `grant_type`, `identity_provider_id`,
    `service_account_id`, `subject_token_type` and `subject_token`, the
    subject token having been requested for the configured audience. A
    failed first exchange fails start-up, with no token in the output.
  - Renewal happens before `expires_at - 60s`, with a fresh subject token
    each time. The stub's tokens live 90 seconds and the stub run lasts
    several minutes.
  - An upstream `401` triggers exactly one re-exchange and a retry of the
    buffered body.
  - A failed renewal answers `502` with the fixed message.
  - Only `POST /v1/responses` on loopback is forwarded. Any other method,
    path or query is refused, and so is a body over the cap.
  - The incoming `Authorization` is replaced, never forwarded.
  - A server-sent-events response is relayed chunk by chunk, with no
    buffering until the end.
  - The token appears in no file under the runner's temp or home, in no
    log line and in no response to a client (grep of every artifact of
    the test).
- **test_engine_job_isolation.py**, for the codex credential:
  - No job references `OPENAI_API_KEY`.
  - Every codex-action step passes the fixed placeholder as
    `openai-api-key` and `steps.openaiwif.outputs.endpoint` as
    `responses-api-endpoint`.
  - The `openai-wif-proxy` step precedes `Create codex user`.
  - The codex jobs request `id-token: write` from step 7 on, and do not
    before it (the step-1 assertion is flipped by step 7's PR).

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
  - The exchange answers only an event and a workflow file it accepts.
    Measured on the step-1 branch on 2026-09-30:
    - a `push` run: `401`, "Invalid OIDC token";
    - a `workflow_dispatch` run of the branch, whose canary files differ
      from `main`'s: `401`, "Workflow validation failed. The workflow file
      must exist and have identical content to the version on the
      repository's default branch";
    - the codex job: no OIDC request token, so no request.

    So the probe expects a token only on a `workflow_dispatch` from `main`
    and records the outcome on every other run. The probe's own positive
    result (exchange `200`, revocation `204`) therefore cannot be recorded
    before step 1 merges. It is run immediately after the merge, with step
    1's live runs (decision: Ransom, 2026-09-30; Implementation plan →
    step 1).
    Real agent runs show the exchange succeeding from runner-side code
    today: the action's own exchange logs "App token successfully
    obtained" in this repository's reviewer job 107763176352 (2026-09-24,
    claude-review.yml at 0aa1f20) and in an inspect_ai fork dev-agent job,
    109170645052 (2026-09-28, claude.yml at 609e20d).
  - After step 6 it must fail, and that failure is the proof of step 6.
  - Add the same probe to a codex job, where it must fail after step 1
    because the job has no `id-token: write`, and again after step 7
    because the App is gone.
  - The secret-delivery probe drops its OpenAI-key sentinel role: no job
    references the key any more.
- **root-boundary-smoke.yml.** Unchanged (the uid depth).
- **codex-path-smoke.yml.** Unchanged, as hygiene.
- **The OpenAI mapping** (step 7), on hosted runners:
  - Positive: a codex job of each reusable workflow exchanges, and a live
    codex round runs past at least two renewals. The job summary records
    the measured `expires_in` and the GitHub OIDC `exp - iat`, which
    settles the lifetime assumption.
  - Negative: each of these requests a token and is refused:
    - a canary job that runs in no reusable workflow, with the right
      audience;
    - a canary reusable workflow of this repository that is not one of
      the four;
    - one of the four with a wrong audience;
    - a job on an event outside the list.
  - Usage from the canary appears on the CI project's usage page, under
    its spend limit. That is the spend-limit verification.
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
THREAT_MODEL.md text that its change makes true.

0. **Settings path (4773338).** The helper in the three `Compose …
   settings` steps (claude.yml, claude-auto.yml, claude-auto-review.yml)
   and its test. It is independent of everything else.
   - The three steps are now one composite, `compose-settings`, with the
     workflow's deny list as its `deny` input. The step ids and outputs
     are unchanged.
   - The helper is `.github/scripts/read_settings.py`. It refuses a `..`
     component, an absolute path outside the workspace, a symlink at any
     step of the path, a file that is not regular, one over 64 KiB and
     one that is not UTF-8. The cap leaves room under Linux's 128 KiB
     limit on one environment string, which the value passes through
     twice (`jq`'s environment, then the action's `settings` input). It walks the path one directory at a time with
     `O_NOFOLLOW`. A refusal fails the step with no output, so the agent
     step is skipped and the Surface step reports it.
   - `tests/test_compose_settings.py` runs the composite's step against
     each refusal, an in-tree path and inline JSON, and pins the three
     workflows to the composite and their deny lists.
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
   - Verified after merge, not before (decision: Ransom, 2026-09-30): the
     exchange refuses runs from a PR branch, and a live run of the branch
     would need a stub pointed at it. Immediately after the merge:
     - `gh workflow run engine-isolation-canary.yml --repo
       meridianlabs-ai/agents --ref main`: the Claude probe must print
       `minted (exchange HTTP 200; revoked, HTTP 204)`, the codex probe
       `refused`;
     - `@review` on a PR on each engine: the review lands as the machine
       account, and no `claude[bot]` post appears;
     - one round of each loop (CI-fix and review-fix): the Claude CI-fix
       agent reads the failed run's log with the job token, the round
       lands as the machine account, and a codex review anchors the next
       fix prompt.

     The run URLs and outcomes are recorded here, under this step, in a
     follow-up PR. If a check fails, step 1 is reverted.

     Results (2026-09-30, #179 merged as 23913b0):
     - Canary, dispatched on `main`:
       [run 36729558388](https://github.com/meridianlabs-ai/agents/actions/runs/36729558388),
       success. The Claude probe printed `minted (exchange HTTP 200;
       revoked, HTTP 204)` and the codex probe `refused (no OIDC request
       token in this job)`. The push-triggered run the merge started
       ([36729058076](https://github.com/meridianlabs-ai/agents/actions/runs/36729058076))
       was green too, but on a push the Claude probe only records: the
       exchange answered `401 Invalid OIDC token`.
     - `@review`, Claude engine: meridianlabs-ai/ts-mono#697,
       [run 36729713075](https://github.com/meridianlabs-ai/ts-mono/actions/runs/36729713075),
       success. The action logged `Using provided GITHUB_TOKEN for
       authentication` and skipped `Revoke app token` (no App token was
       minted). The review and its verdict were posted by
       `meridian-marvin[bot]`; nothing was posted as `claude[bot]`.
     - `@review`, codex engine: the same PR with a temporary
       `engine:codex` label (created in ts-mono for this),
       [run 36730675006](https://github.com/meridianlabs-ai/ts-mono/actions/runs/36730675006),
       success. `review-codex` ran and `review` was skipped; the review
       and verdict were posted by `meridian-marvin[bot]`; nothing was
       posted as `claude[bot]`.
     - CI-fix round (Claude): #180, triggered by a deliberately failing
       probe test ([tests run 36731690917](https://github.com/meridianlabs-ai/agents/actions/runs/36731690917)),
       [run 36732043829](https://github.com/meridianlabs-ai/agents/actions/runs/36732043829),
       success. The gate bound the failed run to #180. The `fix` job's
       action logged `Using provided GITHUB_TOKEN for authentication`
       and skipped `Revoke app token`; the agent's output is hidden, but
       its commit (888ca63) deleted exactly the failing probe, which it
       could only find from the failed run. The land job pushed 888ca63,
       and the attempts counter, the `@review` re-review request and the
       review that followed were all posted by `meridian-marvin[bot]`. The
       tests run the push triggered
       ([36733176986](https://github.com/meridianlabs-ai/agents/actions/runs/36733176986))
       has `meridian-marvin[bot]` as actor and passed. The commit's git
       author is `claude[bot]`: that is the action's default commit
       identity (earlier loop commits on `main` carry it too), not the
       push actor, so it does not fail this check, which is about who
       pushed and posted.
     - Review-fix round, Claude engine: the Claude review of #180
       ([run 36733173930](https://github.com/meridianlabs-ai/agents/actions/runs/36733173930))
       had suggestions; its fix round
       ([run 36733648937](https://github.com/meridianlabs-ai/agents/actions/runs/36733648937))
       ran on the job token, the land job pushed a8b35e2, and the round
       handed off as `meridian-marvin[bot]` (documentation-only nits).
     - Review-fix round, codex engine: with `engine:codex` on #180, the
       codex review
       ([run 36738095760](https://github.com/meridianlabs-ai/agents/actions/runs/36738095760))
       ran as `review-codex` and landed as `meridian-marvin[bot]` with
       suggestions. The `fix-codex` round
       ([run 36738559681](https://github.com/meridianlabs-ai/agents/actions/runs/36738559681))
       built its prompt from that review, and codex ran, but the
       post-codex reclaim refused: `job PATH entry
       '/opt/hostedtoolcache/node/24.21.0/x64/bin' is writable by the
       codex user`. codex-action's own `actions/setup-node` step adds that
       directory to the job PATH inside the codex step, after
       `create-codex-user` has checked and repaired the PATH, and the
       post-codex check does not repair. Nothing landed, and land posted
       the error as `meridian-marvin[bot]`. This is not caused by step 1
       (#179 removed only `id-token: write` from the codex jobs), so step 1
       stands; codex fix and dev rounds need the PATH fix before they can
       land.

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
   - Then land the THREAT_MODEL.md and AGENTS.md rewrite (What changes in
     THREAT_MODEL.md and AGENTS.md), the tier-2 premise text,
     credential-separation.md's I1 and I5, and AGENTS.md's paragraphs
     that describe the reclaim as the boundary.
7. **Codex to OpenAI workload identity federation** (after step 6).
   - Ransom:
     - creates the CI project and its service account, with a hard spend
       limit set;
     - creates the GitHub identity provider (issuer
       `https://token.actions.githubusercontent.com`, the Meridian
       audience);
     - creates the one mapping (Design → Codex jobs): the issuer, audience
       and owner conditions, and the two CEL-derived booleans
       (`openai.agents_workflow`, `openai.agents_event`) required to be
       `true`.
   - This repository:
     - the `openai-wif-proxy` composite and its test;
     - the four codex jobs: the forwarder step before `Create codex user`
       (and after the Node setup step),
       the codex-action inputs, `id-token: write` restored and every
       `OPENAI_API_KEY` reference removed;
     - the negative canaries: a job outside a reusable workflow, another
       reusable workflow of this repository, a wrong audience and a wrong
       event, each refused;
     - AGENTS.md's exception, codex-key and PATH-boundary paragraphs,
       THREAT_MODEL.md's OpenAI-key bullet, codex-engine.md and
       credential-separation.md (3.1, 3.5, I1) rewritten to match.
   - Companion PRs in the caller repositories drop `OPENAI_API_KEY` from
     their agent stubs. The org secret is **not** deleted: actions'
     `inspect-ai-scheduled-tests.yml` and `inspect-swe-nightly-tests.yml`
     still use it (Design → Codex jobs).

## Open questions

None. The two left for Ransom were decided on 2026-09-29:

- **The codex credential:** OpenAI API Platform workload identity
  federation ("let's go with API Platform WIF"). Design → Codex jobs.
- **Loop refunds:** dropped, with no bounded refund budget. Design → The
  boundary.

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
  claude-auto-review.yml:584-586). After step 7 this routes to a job
  that holds nothing more than the Claude job.
- **Pinning claude-code-action** (from executed-paths-residual.md). Still
  worth doing. It is less urgent once the action holds no App token.
- **Retiring the uid machinery outright.** Once the job is the boundary,
  the reclaims, PATH walks and env pins could go, simplifying every agent
  job. This design keeps them (cheap, and shared by both engines).
  Whether to remove them is a later cleanup.
- **The org `OPENAI_API_KEY` secret's other consumers.** actions'
  `inspect-ai-scheduled-tests.yml` and `inspect-swe-nightly-tests.yml`
  inject it into model test suites. Moving them to federation, or to
  another credential, and then deleting the secret after an organization-
  wide consumer audit, is its own migration.
- **Native federation in codex-action** (a renewing credential source
  for its proxy, upstream) would make `openai-wif-proxy` unnecessary. It is
  worth asking for, but not this design.
- **`Verify a review landed`** still trusts `claude_outcome` and
  `agent_launched` for a nudge. Deriving it from the manifest in land
  (verdict present or not) would drop two more outputs. It is harmless as
  it stands.
