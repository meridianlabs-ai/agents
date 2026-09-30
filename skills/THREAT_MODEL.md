# Trust model: the local skills

The skills in this directory (`checkout`, `import`, `merge-approved-prs`,
`post-upstream-review`, `promote`, `resolve-board`) run in a maintainer's
own Claude Code or Codex session, on their machine. This page says what
that session holds, which inputs the skills can trust, and the rules the
skills follow. The workflows have their own threat model:
[THREAT_MODEL.md](../THREAT_MODEL.md).

CI agents cannot change this directory. `.claude/skills` and
`.agents/skills` link to it, and the land job refuses any change under
those paths, so skill changes are made from a maintainer's machine.

## What the session holds

The actor is the maintainer, and every command a skill runs acts as them:

- Their `gh` login, with write access to every repository they can write:
  the fork, the other Meridian repositories and, for an upstream
  maintainer, `UKGovernmentBEIS/inspect_ai`. Anything posted is posted
  under their name.
- Their git configuration: hooks path, fsmonitor, filter and diff drivers,
  includes, credential helpers. Git runs these on the commands the skills
  make.
- Their machine: the home directory, other clones and worktrees, and the
  session's project directory, from which Claude Code and Codex load
  `.claude/`, `.mcp.json`, `CLAUDE.md` and `AGENTS.md`.

The agent in the session reads the inputs below, and text it reads can
steer it. The scripts are the part that does not depend on the agent
reading carefully, which is why the mechanics live in them (rule e).

## Inputs

| Input | Trust | Used by |
|---|---|---|
| An upstream PR's head, tree, branch name, title, body and commit messages | Untrusted: the contributor's | checkout, merge-approved-prs, post-upstream-review |
| A fork PR's head, branch name, title and body | Untrusted until its head repository is the fork and its author is verified | checkout, promote |
| Issue bodies and comments on the public fork | Untrusted unless the author is verified | promote, post-upstream-review, checkout |
| Machine-account comments | Came through a land job, but the content is not vouched for | post-upstream-review, promote |
| ts-mono PRs | Untrusted unless the head repository is `meridianlabs-ai/ts-mono` and the author is verified | promote, checkout, merge-approved-prs |
| Upstream issues | Untrusted text | import |
| Upstream `main` of inspect_ai and the default branches of Meridian repositories | Trusted | all |
| What GitHub reports about an object: a head SHA, a permission, an author login, a review state | Trusted as GitHub's statement | all |

A machine-account comment proves only that it came through a land job.
Land posts agent text as that account, and the agent wrote it after
reading untrusted input, so the content is not trusted for being the
machine account's. When a skill relays such a comment, the control is the
maintainer's read before they invoke it.

A verified author is one in `TRUSTED_LOGINS` (the machine account, as
`i-am-marvin` and `meridian-marvin[bot]`) or one the collaborator
permission API reports with write, maintain or admin access on the
repository in question.

## Rules

(a) Data, never syntax. A value from an untrusted input reaches a command
only through a file, stdin, the environment or a quoted variable, never
pasted into the command text. This covers branch names, file names,
CHANGELOG entries, titles, bodies and review text. A skill that posts text
writes it to a file and sends the file (`gh api --input`,
`--body-file`).

(b) Trust by verified author, never by name, recency or text. A PR, issue
or comment counts only when its author is verified, and a lookup that
fails or answers anything else is untrusted. An App login other than the
machine account's is never trusted and never looked up, so
`github-actions[bot]` and `claude[bot]` do not count. A branch name, a
comment being the newest, or a marker in a body proves nothing: anyone
can choose them. Where a candidate is picked from several, two that
qualify are refused as ambiguous.

(c) An outsider's tree is data. It is checked out detached, at the commit
GitHub reported and the maintainer approved, and every git command on it
runs with the command-running configuration pinned off: hooks, fsmonitor,
every filter driver the clone or the worktree defines, and submodule
recursion. Nothing the outsider names becomes a local branch or a
`branch.*` key, and a push names its destination in full
(`HEAD:refs/heads/<branch>`). Nothing from the tree is run, installed or
tested in the session.

(d) Text republished under the maintainer's name is guarded. Before it is
posted, the agents' trigger phrases are backticked and the loops' markers
split, and a reference GitHub would resolve in the destination's tracker
is refused unless it is the one intended: a closing keyword there closes
an issue, and a bare `#N` written on the fork names a different issue
upstream. The text is printed as it will be posted, and `--dry-run` shows
it without posting. Printing during the post is a record, not a review:
promote shows the upstream PR body in `--dry-run`, and pauses on a verdict
that is not clean. post-upstream-review posts the agent's rewrite in the
same run that prints it. Its control is the maintainer reading the source
findings before invoking the skill; the final wording is not approved, and
the review's footer says so (decision: Ransom, 2026-09-30).

(e) Security mechanics live in tested scripts. A check that guards the
maintainer's login or machine is a script next to the skill or a helper in
`lib/`, with tests in `tests/`. SKILL.md prose keeps the judgment: what to
relay, how to resolve a conflict, when to ask.

(f) Fail closed and ask. When a check cannot be made (a lookup fails, a
listing is incomplete, a render fails, a head moved), the script stops
before any write and says why, and the agent asks the maintainer.

## Where the rules live

`lib/common.sh` holds the shared bash helpers: `TRUSTED_LOGINS`,
`trusted_login` and `check_pr` (rule b), `genuine_proxy` and
`proxy_upstream_pr` for External proxies, `pin_git_config` (rule c), and
`render_markdown` and `rendered_refs`, which find references with GitHub's
own renderer (rule d). `lib/outbound.py` holds `defang` and the plain-text
reference scan for commit messages (rule d). `tests/test_skills_lib.py`
tests them and fails when a skill script defines its own copy.

| Skill | Script | What it enforces |
|---|---|---|
| checkout | `checkout.sh` | b: the chip's head repository and author; c: an External PR lands detached, pinned, in a worktree outside the clone |
| import | `import.sh` | d: the copied title and body are defanged before the fork issue is created under the maintainer's name |
| merge-approved-prs | `approval_at_head.py`, `checks_at_head.py`, `companion_mergeable.py`, `external.sh`, `conflict_residue.py`, `changelog_check.sh`, `conflicts.sh` | b: approvals and companions by write-access reviewers at the head; c: the External flow; a: CHANGELOG entries and conflicted file names |
| post-upstream-review | `gather.sh`, `post.sh` | b: the proxy and the findings comment by author; a and d: the review sent as a file, defanged, footer added, references checked |
| promote | `promote.sh` | b: the fork PR, the import header, the review verdict and the ts-mono companion by author; d: the upstream PR body and the branch's commit messages |
| resolve-board | none | Reads only the workflow run and runs the chip sweep |

## Known gaps

- A merge driver (`merge.<name>.driver`) in the maintainer's config, chosen
  by the outsider's `.gitattributes`, still runs during
  `external.sh start` and `merge`. `external.sh` passes `--no-ext-diff
  --no-textconv` to its diffs, so diff drivers do not.
- `checkout.sh` picks the ts-mono companion for a trusted pick by branch
  name and author shape, not by `check_pr`. The branch it switches to is
  fetched from ts-mono's own `origin`, which only write-access accounts
  can push to.
- The agent reads untrusted text. The scripts bound what the mechanics
  they cover can do; they cannot stop an agent that has been steered into
  running something else. The maintainer watching the session is the
  control for that.

## Changing a skill

- Put a new check in a script or in `lib/`, and test it.
- Reuse the helpers in `lib/` rather than writing another trust check or
  pin list.
- Name every untrusted value the change handles and say which rule covers
  it, in the script's header comment.
