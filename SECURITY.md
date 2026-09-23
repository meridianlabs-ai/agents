# Security

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository:
[Report a vulnerability](https://github.com/meridianlabs-ai/agents/security/advisories/new)
(enabled 2026-09-18). Never open a public issue or pull request for a
security problem: this repository is public, and its workflows run on every
Meridian repository the moment a fix merges, so a public report is a
public exploit window.

Include what you can of: the workflow, composite action or script involved;
the event or trigger that reaches it; what an attacker controls (a PR head,
a comment, test output, a manifest field); the write or credential it
reaches; and a run link if you have one. Do not include a live token. A
maintainer acknowledges the report in the advisory thread, confirms or
disputes it against `main`, and fixes it through a pull request from a
maintainer's machine, since agents running in CI cannot change workflow
files here. Disclosure is coordinated with you in the same thread.

## What this repository is

Reusable GitHub Actions workflows that run coding agents (Claude Code, or
OpenAI Codex on request) as a dev agent, a reviewer and two autonomous loops,
plus the composite actions and scripts they share. Meridian repositories and
the public `meridianlabs-ai/inspect_ai` fork run them through thin stubs
pinned to this repository's `main`; the `meridianlabs-ai/actions` repository
builds two more agent workflows on the same composites.

## Trust boundaries

Untrusted: the content of any pull request and every fork head; the text of
issues and comments (the fork is public, so any GitHub account can write
there); test output, CI logs and artifacts; dependency alerts and anything
an agent reads or installs while it works; every file an agent job leaves
behind, the landing manifest included.

Trusted: upstream `main` of `UKGovernmentBEIS/inspect_ai` and the default
branches of Meridian repositories; the machine account under its two logins
(`meridian-marvin[bot]`, the GitHub App, and `i-am-marvin`, the User it
replaced); and accounts whose write access a permission lookup has verified.

## Guarantees

Each of these holds on `main`; the ones that can be read from the workflow
text are checked by the tests under `tests/`.

- A job that runs an agent holds no credential of the machine account and no
  token minted from it; its own job token is read-only. The Claude action's
  own token, present while that step runs, is the exception described under
  "By design" below.
- A job that runs the Claude agent references no `OPENAI_API_KEY`. Each
  reusable workflow runs each engine in a job of its own (`agent` and
  `agent-codex`, `review` and `review-codex`, `fix` and `fix-codex`), the
  trusted gate's `engine` output selecting exactly one at the job level,
  which GitHub evaluates before dispatching a job. A secret a step
  references is delivered to the job's runner whether or not that step's
  `if:` ends up true (the runner builds its `secrets` context from the job
  message before any step runs), so the codex key is referenced only in the
  codex job's codex-action step and a Claude-engine run's job message never
  carries it (Claude Security finding 4629153). Measured, not assumed: the
  hosted canary (design/credential-separation.md → section 6) shows a
  referenced-but-skipped secret in the runner's memory and none in a job
  that references nothing, although the caller passed it and a sibling job
  referenced it — delivery is scoped per job.
- In a codex job nothing from the checked-out tree executes as the runner:
  the caller's `claude-setup` action is not run there, and the shared
  provisioning recipe (uv and a dev-install of the checkout, the tree's own
  build backend, or the caller stub's `codex_provision` recipe) runs as the
  unprivileged `codex` user after that user exists and before the
  codex-action step, so a head the pipeline itself produced from an
  outsider's issue text meets the same boundary as codex itself: no sudo,
  no GitHub token, no OIDC request token, no view of the runner's
  processes (finding 4628446). Between that provisioning and the
  codex-action step every process running as codex is killed and the codex
  home is re-created, and the runner writes nothing into the workspace in
  that interval, so nothing provisioning left behind reaches the action's
  runner-side reads. A hosted canary exercises both this boundary and the
  secret delivery against a hostile checkout and synthetic secrets
  (design/credential-separation.md → section 6).
- Every write an agent asks the machine account for lands through a manifest that a stdlib
  validator accepts in full, in a fresh job on a fresh runner that checked out
  no code; a refused manifest causes none of the actions it requested. The
  trusted gate's own writes before the agent runs (acknowledgement, stage,
  labels, counters) and the land job's final error report are deterministic
  and not agent-controlled.
- The machine account's tokens are minted per job, for one repository and the
  permissions that job uses, from the GitHub App's secrets, and are revoked at
  job end.
- Whether a comment, label or issue-body line is believed is decided by its
  author's login or verified write access, never by the text itself. Text a
  maintainer republishes from the public upstream tracker (`/import`) is
  de-fanged before it is posted under their login, and `claude.yml`'s trigger
  check reads no body or title text on an opened issue whose first line is
  the import's `Upstream issue:` line.
- A label the machine account applies is a write, not a decision, and never
  starts the dev agent: `claude.yml`'s trigger check refuses both of its
  logins on the `auto`/`claude` label path as it does on text. The machine
  account's `auto` label on a PR is accepted by the loop gates because it
  was written for a trusted decider: a write-access human's opt-in on the
  issue or in a comment, verified before the label was written, or a
  trusted caller workflow's standing policy (a maintainer's decision in a
  workflow file, such as inspect_flow's scheduled PRs), never a per-item
  decision from untrusted input. Two checks keep that true for the PR the
  dev agent opens for an issue: the gate counts the issue's `auto` label as
  the opt-in only after reading who applied it most recently — a human
  account holding write access, looked up fail-closed; a triage account's
  label, a bot's or the machine account's own is not an opt-in and the run
  stays one-shot — and the land job's validator refuses a manifest whose
  `pr.labels` names a label the gate did not read (`allowed-pr-labels`, the
  gate's read passed verbatim), so a manifest rewritten in the agent job
  after the composing step cannot have the machine account label the PR
  `auto` or switch its engine (Claude Security findings 4628438 and
  4628441, 2026-09-22). Where a caller's agent job is untrusted
  after it has read third-party input (the triage workflow in
  `meridianlabs-ai/actions`), the land job's `allowed-issue-labels`,
  `allowed-issue-assignees`, `max-issues` and `refuse-pr` inputs are
  enforced by the validator on the fresh runner, so its manifest cannot
  commission the autonomous agent — through an issue label, or through a
  PR opened, adopted or labelled for a branch already on origin — however
  the artifact was produced (Claude Security finding 4628345, 2026-09-22).
- Only the reviewer's land job (`claude-review.yml`, the one caller that
  passes `land`'s `allow-review`) may carry a review: the validator refuses
  `review_verdict`, a `review`-flagged comment and inline review comments on
  every other caller, so a forged manifest on the dev agent's or a loop's
  land job cannot post the machine account's verdict. Under the reviewer's
  `refuse-bundle` it refuses the hand-back, hand-off, thread resolutions,
  replies and PR operations a read-only reviewer never owes, so a forged
  review manifest cannot re-trigger the reviewer from its own land job
  (Claude Security findings 4628442 and 4628439, 2026-09-22).
- The GitHub App has no Workflows permission, so a CI agent's commit that
  touches `.github/workflows/` fails at the push.
- No runner-side step after a Codex run resolves a command, or its shell
  interpreter, through a directory the codex user can write or replace. A
  job that may run codex puts nothing under its workspace on
  `GITHUB_PATH`; before the codex user is given write access to the
  checkout, every hop of every PATH entry (symlink targets included) and
  every file inside an entry that the user owns, can write or reaches
  through a symlink is checked: one inside the workspace fails the job, one
  the user owns or can write outside it — the hosted image ships `/opt` and
  `/usr/local/bin` world-writable — is made runner-only first and fails the
  job if it stays the user's, so codex never runs behind a hijackable
  search path; the
  reclaim step repeats the check after codex without the repair, and every
  post-codex composite runs its commands with `PATH` pinned to the
  root-owned system directories. Codex cannot add to the job PATH itself:
  the per-step `GITHUB_PATH` file is runner-only. A hosted smoke workflow
  runs the check and the post-codex steps against a planted venv with the
  real codex user (Claude Security finding 4628448, 2026-09-22;
  design/codex-engine.md → Runner-side search path).
- The reviewer runs only when a trusted commenter asks for it, except inside
  the `@auto` loop, where the machine account requests each round's review.
- A CI-fix round acts on the PR its failed run is bound to, not on the
  number the caller forwarded: the gate reads the run back from the API
  (this repository's own failed `pull_request` run, on the forwarded
  branch, its latest attempt), requires the run's actors — who started it
  and who re-ran it — to be the machine account or write-access accounts,
  and binds it to the one same-repo PR from that branch that was open when
  the run was created, at the run's head SHA, whose timeline records no
  retarget since the run was created; that base is what the runner merges,
  and the land job derives the whole context again before pushing. Two PRs
  sharing a branch, a run started or re-run by an account without write
  access, a run re-run, a branch pushed or a PR retargeted at any point
  after the run stop the round rather than guess, before any counter, stage
  move, merge or model work; the runner merges exactly the base tip the
  gate read. A Claude step that fails lands nothing and keeps its attempt —
  the action's execution-file output is not launch evidence, so the step's
  outcome decides what lands, and a round is refunded only when the agent
  step was never entered (the engine's agent step `skipped` — each engine
  runs in its own fix job — a step outcome the runner settled before any
  agent code ran, delivered by that job's own outputs — a cancelled job is refunded on that evidence alone, and a
  pending job cancelled before it started, which delivers none, keeps its
  round), never on how the agent's own step ended (Claude Security 4628734 and
  4628735, 2026-09-22); in both loops a manifest with no bundle posts the
  `@review` hand-back only when the agent step succeeded, so a round that
  landed nothing and did not complete cannot re-arm the loop, and the
  review-fix stall check keys on the recorded tip, not on a count a refund
  may have taken to 0. What this establishes is the run's head commit, the base
  branch it was merged into and the base tip that is merged now, and that
  no other PR can be the run's origin — not which PR GitHub considered the
  trigger, not the merge commit the run built, and not the base tip it
  merged, since the API carries none of them. Those limits are accepted as
  the loop's, and two PRs sharing a head fail closed until one is closed and
  a new commit pushed (decision: Ransom, 2026-09-22; design/auto-agent.md →
  Binding the failed run to its PR → Decisions).

## By design, not a finding

- The Claude GitHub App's own installation token is present in the agent job
  while the action step runs: no workflow passes a token to the action, so it
  mints its own, with contents, pull requests and issues write on the caller
  repository whatever the job's own permissions say, and revokes it when the
  step ends. It sits in `.git/config` for that step. On sandboxed reviews it
  is masked there for sandboxed commands, not for the agent's own file
  reads; same-repo reviews run without the sandbox. Everywhere the agent's
  push and posting verbs are denied by settings, but `gh api` is not, so
  those denies are guard rails rather than a complete prohibition on direct
  writes. A write that slipped past would be attributed to `claude[bot]`,
  which the review-fix workflow still accepts as a verdict author: a marked
  verdict comment posted with this token can drive that loop on an otherwise
  eligible `auto` PR. Re-review requests follow the configured allow-lists
  (`review_allowed_bots` defaults to `claude[bot]` in the review-fix gate;
  the reviewer admits that bot when the caller's `allowed_bots` includes it,
  as in the inspect_ai fork). It no longer reaches the CI-fix loop
  indirectly: a CI run started by a push from `claude[bot]` — or any bot
  other than the machine account — is refused by the CI-fix gate's actor
  check before any write, on both engines (the Codex step's own allow-list
  admitted `claude` until 2026-09-22; the gate now decides first). A push the
  Claude App made itself therefore ends the automatic loop for that branch
  until the machine account or a write-access human pushes (decision:
  Ransom, 2026-09-22; design/auto-agent.md → Binding the failed run to its
  PR → Decisions).
- A caller without the two app secrets still runs the dev agent, degraded:
  pushes and PRs come from `github-actions[bot]` and trigger nothing. The
  reviewer and the loops fail at their mint step instead.
- CI agents cannot edit workflow files. Changes to `.github/workflows/` are
  made from a maintainer's machine, under a maintainer's review. The land
  job refuses a bundle in which the agent changed them before pushing, with
  a report naming the files; changes the runner's base merge brought in
  pass. The files it checks are the ones the push would change on origin
  (the bundle's tip against the branch's live tip, or against the base
  branch's tip when the push creates the branch), never a range the
  manifest names: the manifest's `start_sha` is the agent job's to write,
  and origin serves any reachable commit — a fork PR's head, an old base
  commit — as a start (Claude Security finding 4628444, 2026-09-22).
- The same refusal covers the entry points and configuration files a later
  automated job on the branch executes or loads — not everything those
  run (see the accepted gap below) — since the machine account's push would
  otherwise move agent-written files into the same-repo tree the next run
  provisions from as `runner` or reads as instructions
  (Claude Security finding 4628446, criterion 2, 2026-09-23): anything
  under `.github/` (the composite actions a workflow runs with `uses:
  ./…`, the callers' `claude-setup` among them); agent instructions and
  settings at any depth (`CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`,
  `AGENTS.override.md`, `.claude/`, `.mcp.json`, `.claude.json`,
  `.codex/`, `.agents/`, `.gitmodules`, `.ripgreprc`, `.husky/`); and
  build and dependency configuration at any depth (`pyproject.toml`,
  `setup.py`, `setup.cfg`, `uv.lock`, `uv.toml`, `.python-version`,
  `requirements*.txt`, `package.json`, the npm, pnpm and yarn lockfiles,
  `pnpm-workspace.yaml`, `.pnpmfile.cjs`, `.npmrc`, `.yarnrc`,
  `.yarnrc.yml`, `.yarn/`). Symlink targets and `CLAUDE.md` `@path`
  imports reached from these paths are refused too. **Accepted gap**
  (decision: Ransom, 2026-09-23): this does not close the class. The list
  names entry points and loaded configuration, not the code they run, so
  an agent's edit to an ordinary file that UNCHANGED configuration
  executes still lands: a script a `claude-setup` step, a settings hook or
  an MCP server command runs, a build backend on `backend-path`, a module
  a `setup.py` or build hook imports, any file a caller's recipe reads.
  The next automated run executes it during provisioning or at agent start
  — as the codex user on codex jobs (finding 4628446, criterion 1), as
  `runner` on Claude jobs, where the Claude agent itself already runs the
  tree's code (tests, `conftest.py`) as `runner`. Closing it is a separate
  design follow-up (Ransom, 2026-09-23) comparing approval gating (a head
  the machine account pushed gets fork-head treatment in every later
  automated job until a write-access human approves that exact head),
  heuristic dependency following at landing, and running the Claude engine
  as an unprivileged user that also provisions, as codex does.
- A Claude reviewer steered by hostile PR content cannot push through its
  landing job, cannot act as the machine account from its own job, and
  cannot have the machine account write outside the caller repository. The
  Claude action's own installation token in the review job remains
  write-capable, and the command denies on it are guard rails rather than a
  complete prohibition on direct writes. Its normal output is the review
  files in its landing
  directory, posted as the review after trigger tokens and loop markers are
  removed, with a verdict that is one of two fixed bodies. Its landing
  manifest is still data the land job acts on: a comment on another thread,
  a caller-repository issue write, a stage move or the adoption of an
  existing branch's PR that a compromised review job requested would post
  as the machine account. A human reads every review.

## Adding or changing a workflow

- No secret of the machine account, and no other secret this repository
  controls, in any job that runs an agent or code from a checkout the org
  does not fully control. Not in `env:`, not as an action input, not through
  a composite. The model credential (Workload Identity Federation, or
  `OPENAI_API_KEY` in the codex job alone) and the Claude action's own token
  are the known exceptions. Never reference `OPENAI_API_KEY` in a job that
  runs the Claude agent — a referenced secret reaches the job's runner
  whatever the referencing step's `if:` says — and never run code from the
  checkout as the runner in a codex job: those jobs provision with
  `provision-fallback` `user: codex` after `create-codex-user`, and no
  `./`-local action.
- No `${{ inputs.* }}`, event text or step output inside a `run:` block; pass
  it through `env:` and expand it as a quoted variable.
  The skills under `skills/` that a maintainer's local agent runs with their
  `gh` login follow the same rule for text from a PR or its tree — entry
  text, file names, branch names, titles: a quoted variable, the environment
  or stdin, never a value pasted into a command template (finding 4628737).
- Every author check goes through `TRUSTED_LOGINS` or a permission lookup
  that fails closed; never a substring of a comment or issue body.
- Every write the agent asks the machine account for goes through the
  landing manifest and the `land` composite, from a job that checks out
  nothing. A trusted gate may
  write before the agent runs (acknowledgement, stage, labels, counters),
  never after.
- Mint the machine account's token first in each trusted job, with the
  narrowest `repositories` and `permission-*` inputs, and read it from
  `steps.mint.outputs.token` only.
- Every checkout runs `persist-credentials: false`; a git command that must
  authenticate gets a step-scoped credential helper keyed to
  `github.server_url`.
- Stubs pass secrets as explicit one-key entries, never `secrets: inherit`.
- Run `actionlint` and `python3 -m pytest` before opening the PR, and ask for
  a review on every workflow-editing PR.

## Further reading

[design/credential-separation.md](design/credential-separation.md) describes
the pattern, the manifest contract, the identity and the caller contract as
they stand today. [design/architecture.md](design/architecture.md) holds the
history and the reasoning behind each decision.
