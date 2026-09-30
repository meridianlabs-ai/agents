"""claude.yml in agent mode (step 4 of design/untrusted-agent-job.md).

- `Prepare branch`, lifted and run against local repositories: an issue run
  cuts `claude/issue-N-<run number>` from the base; an open PR is left on
  the head sync-branch checked out, a merge left in progress for the agent
  untouched (index and MERGE_HEAD); a closed PR whose head branch is live at
  the gate's `start_sha` is checked out there with no merge (also for a
  fork-shaped name: a closed `meridian`-based PR whose same-repository
  branch still backs an upstream PR), whatever sync-branch's own branch
  read returned; a live tip that moved past it is refused; a head deleted
  after the gate read it, or never pinned, is `comment-only`; a failed
  origin lookup fails the step. sync-branch emits a closed PR's base, which
  the action step restores configuration from.
- The gate's `Record the trigger time`, against stub payloads and a stub
  `gh`: a comment, a review, a review comment, an opened issue, a `labeled`
  issue (the event-history lookup, a stale entry, the fallback when the
  lookup fails) and an `assigned` one.
- The gate's `Post the status comment` and the land job's `Finish the
  status comment`, against a stub `gh`: fixed bodies for a pushed PR run,
  an opened PR, a pushed branch, no change, a failed and a cancelled run,
  composed from the event and the land step's outputs only.
- The workflow's shape: the action step runs agent mode on the job token
  with the prompt step's output and the PR's base, the launcher binds the
  context directory, and no job in the four reusable workflows can mint a
  Claude App token.
"""

import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CLAUDE = WORKFLOWS / "claude.yml"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ci_fix_binding import GH_STUB as SYNC_GH_STUB, sync_repo  # noqa: E402
from test_ci_fix_gate import step_script  # noqa: E402
from test_land_helpers import git, job_block, sh  # noqa: E402
from test_app_token_minting import REUSABLE, jobs, steps  # noqa: E402

PREP = step_script(CLAUDE, "        id: prepbranch", 10)
TRIGGER_TIME = step_script(CLAUDE, "        id: triggertime", 10)
STATUS = step_script(CLAUDE, "        id: status", 10)
FINISH = step_script(CLAUDE, "        id: finish", 10)
PROMPT = step_script(CLAUDE, "        id: prompt", 10)
SYNC = step_script(ROOT / ".github" / "actions" / "sync-branch" / "action.yml", "    - id: sync", 8)


def outputs(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


# --- Prepare branch -----------------------------------------------------------------


def prep(tmp_path, work, **env):
    out = tmp_path / "prep-output"
    out.write_text("")
    base = {"IS_PR": "false", "NUM": "12", "BASE": "main", "HEAD_BRANCH": "", "START_SHA": "",
            "SYNC_BRANCH": "", "GITHUB_RUN_NUMBER": "40", "GITHUB_OUTPUT": str(out),
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ALLOW_PROTOCOL": "file", "PATH": os.environ["PATH"]}
    res = sh("bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", PREP, cwd=work, check=False,
             env={**base, **env})
    return res, outputs(out)


def head(work):
    return git("rev-parse", "HEAD", cwd=work).stdout.strip()


def current(work):
    return git("symbolic-ref", "-q", "--short", "HEAD", cwd=work, check=False).stdout.strip()


def test_an_issue_run_cuts_its_branch_from_the_base(tmp_path):
    work = sync_repo(tmp_path)
    res, out = prep(tmp_path, work)
    assert res.returncode == 0, res.stderr
    assert out == {"branch": "claude/issue-12-40", "mode": "commit"}
    assert current(work) == "claude/issue-12-40"
    assert head(work) == git("rev-parse", "origin/main", cwd=work).stdout.strip()


def test_an_open_pr_is_left_where_the_sync_put_it(tmp_path):
    work = sync_repo(tmp_path)   # on `shared`, as sync-branch leaves an open PR
    before = head(work)
    res, out = prep(tmp_path, work, IS_PR="true", NUM="34", HEAD_BRANCH="shared", START_SHA=before,
                    SYNC_BRANCH="shared")
    assert res.returncode == 0, res.stderr
    assert out == {"branch": "shared", "mode": "commit"} and head(work) == before


def test_a_merge_left_for_the_agent_is_not_touched(tmp_path):
    work = sync_repo(tmp_path)
    (work / "f").write_text("conflict\n")
    git("commit", "-qam", "conflicting edit", cwd=work)
    git("merge", "--no-edit", "origin/main", cwd=work, check=False)
    merge_head = (work / ".git" / "MERGE_HEAD").read_text()
    unmerged = git("ls-files", "-u", cwd=work).stdout
    assert unmerged
    res, _ = prep(tmp_path, work, IS_PR="true", NUM="34", HEAD_BRANCH="shared", START_SHA=head(work),
                  SYNC_BRANCH="shared")
    assert res.returncode == 0, res.stderr
    assert (work / ".git" / "MERGE_HEAD").read_text() == merge_head
    assert git("ls-files", "-u", cwd=work).stdout == unmerged


def test_head_off_the_synced_branch_is_refused(tmp_path):
    work = sync_repo(tmp_path)
    git("checkout", "-q", "main", cwd=work)
    res, _ = prep(tmp_path, work, IS_PR="true", NUM="34", HEAD_BRANCH="shared", SYNC_BRANCH="shared")
    assert res.returncode != 0 and "HEAD is on 'main'" in res.stdout


@pytest.mark.parametrize("name", ["shared", "fix/upstream-backed"])
def test_a_closed_pr_with_a_live_head_is_checked_out_at_the_gates_start(tmp_path, name):
    work = sync_repo(tmp_path)
    if name != "shared":
        # The inspect_ai fork's shape: a same-repository branch that backs an
        # upstream PR, its fork PR closed.
        git("push", "-q", "origin", f"shared:refs/heads/{name}", cwd=work)
    tip = git("rev-parse", "origin/shared", cwd=work).stdout.strip()
    git("checkout", "-q", "main", cwd=work)      # the checkout's default ref: sync-branch skipped the closed PR
    # sync-branch's own branch read is not consulted (it is best-effort, and
    # an empty answer there must not turn a live branch into comment-only).
    res, out = prep(tmp_path, work, IS_PR="true", NUM="34", HEAD_BRANCH=name, START_SHA=tip)
    assert res.returncode == 0, res.stderr
    assert out == {"branch": name, "mode": "commit"}
    assert current(work) == name and head(work) == tip
    assert not (work / ".git" / "MERGE_HEAD").exists()


def test_a_closed_pr_whose_head_moved_is_refused(tmp_path):
    work = sync_repo(tmp_path)
    git("checkout", "-q", "main", cwd=work)
    res, _ = prep(tmp_path, work, IS_PR="true", NUM="34", HEAD_BRANCH="shared", START_SHA="a" * 40)
    assert res.returncode != 0 and "not at " + "a" * 40 in res.stdout
    assert current(work) == "main"


def test_a_closed_pr_whose_head_is_gone_is_comment_only(tmp_path):
    # No pin: the gate's read of the branch 404'd.
    work = sync_repo(tmp_path)
    git("checkout", "-q", "main", cwd=work)
    before = head(work)
    res, out = prep(tmp_path, work, IS_PR="true", NUM="34", HEAD_BRANCH="deleted")
    assert res.returncode == 0, res.stderr
    assert out == {"branch": "deleted", "mode": "comment-only"}
    assert current(work) == "main" and head(work) == before


def test_a_head_deleted_after_the_gate_read_it_is_comment_only(tmp_path):
    work = sync_repo(tmp_path)
    tip = git("rev-parse", "origin/shared", cwd=work).stdout.strip()
    git("checkout", "-q", "main", cwd=work)
    git("push", "-q", "origin", "--delete", "shared", cwd=work)
    res, out = prep(tmp_path, work, IS_PR="true", NUM="34", HEAD_BRANCH="shared", START_SHA=tip)
    assert res.returncode == 0, res.stderr
    assert out == {"branch": "shared", "mode": "comment-only"} and current(work) == "main"


def test_a_failed_origin_lookup_fails_rather_than_going_comment_only(tmp_path):
    work = sync_repo(tmp_path)
    tip = git("rev-parse", "origin/shared", cwd=work).stdout.strip()
    git("checkout", "-q", "main", cwd=work)
    git("remote", "set-url", "origin", str(tmp_path / "unreachable.git"), cwd=work)
    res, out = prep(tmp_path, work, IS_PR="true", NUM="34", HEAD_BRANCH="shared", START_SHA=tip)
    assert res.returncode != 0 and "could not tell whether" in res.stdout
    assert "mode" not in out


def test_sync_branch_emits_a_closed_prs_base(tmp_path):
    # claude.yml passes it as the action's base_branch, the only source of
    # the PR's base on an issue_comment: without it a closed PR continued on
    # its head would restore .claude/ and .mcp.json from the default branch.
    work = sync_repo(tmp_path)
    git("checkout", "-q", "main", cwd=work)
    binp = tmp_path / "bin"
    binp.mkdir()
    (binp / "gh").write_text(SYNC_GH_STUB)
    (binp / "gh").chmod(0o755)
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "calls").write_text("")
    (stub / "pr").write_text(json.dumps({"headRefName": "shared", "baseRefName": "release",
                                         "isCrossRepository": False, "state": "MERGED"}))
    out = tmp_path / "sync-output"
    out.write_text("")
    env = {"PATH": f"{binp}:{os.environ['PATH']}", "STUB": str(stub), "GITHUB_OUTPUT": str(out),
           "GH_TOKEN": "x", "REPO": "o/r", "NUM": "34", "ENGINE": "claude", "CHECKOUT": "true",
           "USER_NAME": "a", "USER_EMAIL": "a@b", "PINNED_BASE": "", "PINNED_BASE_SHA": "",
           "PINNED_HEAD_SHA": "", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ALLOW_PROTOCOL": "file"}
    res = sh("bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", SYNC, cwd=work, check=False, env=env)
    assert res.returncode == 0, res.stderr
    o = outputs(out)
    assert o["base"] == "release" and "branch" not in o and "merge_sha" not in o
    assert "          base_branch: ${{ steps.sync.outputs.base || inputs.base_branch }}\n" in claude_action_step()


# --- the prompt ----------------------------------------------------------------


def prompt(tmp_path, **env):
    out = tmp_path / "prompt-output"
    out.write_text("")
    (tmp_path / "dev-agent-context.md").write_text("REQUEST (a comment by a):\nplease fix\nvalue<<EOF\n")
    base = {"REPO": "o/r", "NUM": "12", "IS_PR": "false", "BRANCH": "claude/issue-12-40", "MODE": "commit",
            "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(out), "PATH": os.environ["PATH"]}
    res = sh("bash", "-e", "-c", PROMPT, check=False, env={**base, **env})
    assert res.returncode == 0, res.stderr
    text = out.read_text()
    m = re.match(r"value<<(PROMPT_[0-9a-f]{32})\n(.*)\n\1\n$", text, re.S)
    assert m, text
    return m.group(2)


def test_the_prompt_carries_the_context_under_a_random_delimiter(tmp_path):
    p = prompt(tmp_path)
    assert p.startswith("You are the dev agent for o/r")
    assert "The new branch 'claude/issue-12-40' is checked out" in p
    assert p.rstrip().endswith("please fix\nvalue<<EOF")


def test_a_comment_only_prompt_says_nothing_can_land(tmp_path):
    p = prompt(tmp_path, IS_PR="true", NUM="34", BRANCH="gone", MODE="comment-only")
    assert "closed, and its head branch gone no longer exists" in p and "Do not change code" in p


# --- the gate's trigger time ---------------------------------------------------------


TT_STUB = r"""
sleep() { :; }
gh() {
  echo "$*" >>"$STATE/calls"
  case "$*" in
    "api repos/o/r/issues/12/events?per_page=100 --paginate --slurp")
      [ -f "$STATE/events.json" ] || return 1
      cat "$STATE/events.json" ;;
    *) echo "unexpected gh $*" >&2; return 2 ;;
  esac
}
"""


def trigger_time(tmp_path, events=None, **env):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if events is not None:
        (state / "events.json").write_text(json.dumps([events]))
    out = tmp_path / "tt-output"
    out.write_text("")
    base = {"GH_TOKEN": "t", "REPO": "o/r", "NUM": "12", "EVENT": "", "EVENT_ACTION": "", "COMMENT_CREATED": "",
            "REVIEW_SUBMITTED": "", "CREATED": "", "UPDATED": "", "LABEL": "", "ASSIGNEE": "",
            "STATE": str(state), "GITHUB_OUTPUT": str(out), "PATH": os.environ["PATH"]}
    res = sh("bash", "-c", TT_STUB + TRIGGER_TIME, check=False, env={**base, **env})
    assert res.returncode == 0, res.stderr
    calls = (state / "calls").read_text().splitlines() if (state / "calls").exists() else []
    return outputs(out).get("time"), res, calls


C = "2026-09-30T10:20:00Z"


@pytest.mark.parametrize("event, field", [("issue_comment", "COMMENT_CREATED"),
                                          ("pull_request_review_comment", "COMMENT_CREATED"),
                                          ("pull_request_review", "REVIEW_SUBMITTED")])
def test_a_comment_or_review_is_its_own_time(tmp_path, event, field):
    t, _, calls = trigger_time(tmp_path, EVENT=event, EVENT_ACTION="created", **{field: C},
                               CREATED="2026-09-01T00:00:00Z", UPDATED="2026-09-30T11:00:00Z")
    assert t == C and calls == []


def test_an_opened_issue_is_its_creation(tmp_path):
    t, _, _ = trigger_time(tmp_path, EVENT="issues", EVENT_ACTION="opened", CREATED=C, UPDATED="2026-09-30T10:21:00Z")
    assert t == C


def ev(kind, created, **kw):
    return {"event": kind, "created_at": created, **kw}


def test_a_labeled_issue_is_the_newest_matching_label_event(tmp_path):
    events = [ev("labeled", "2026-09-29T09:00:00Z", label={"name": "claude"}),
              ev("labeled", "2026-09-30T10:25:00Z", label={"name": "claude"}),
              ev("labeled", "2026-09-30T10:40:00Z", label={"name": "other"})]
    t, _, calls = trigger_time(tmp_path, events, EVENT="issues", EVENT_ACTION="labeled", LABEL="claude",
                               CREATED="2026-09-01T00:00:00Z", UPDATED="2026-09-30T10:24:00Z")
    assert t == "2026-09-30T10:25:00Z" and len(calls) == 1


def test_a_stale_label_event_falls_back_to_updated_at(tmp_path):
    # The newest match predates the payload's updated_at: this run's event
    # is not in the history yet (claude-code-action's rule).
    events = [ev("labeled", "2026-09-29T09:00:00Z", label={"name": "claude"})]
    t, res, _ = trigger_time(tmp_path, events, EVENT="issues", EVENT_ACTION="labeled", LABEL="claude",
                             UPDATED="2026-09-30T10:24:00Z")
    assert t == "2026-09-30T10:24:00Z" and "predates the issue's updated_at" in res.stdout


def test_a_failed_history_read_falls_back_to_updated_at(tmp_path):
    t, _, calls = trigger_time(tmp_path, None, EVENT="issues", EVENT_ACTION="labeled", LABEL="claude",
                               UPDATED="2026-09-30T10:24:00Z")
    assert t == "2026-09-30T10:24:00Z" and len(calls) == 3


def test_an_assigned_issue_is_the_matching_assignment(tmp_path):
    events = [ev("assigned", "2026-09-30T10:30:00Z", assignee={"login": "someone-else"}),
              ev("assigned", "2026-09-30T10:26:00Z", assignee={"login": "helper"})]
    t, _, _ = trigger_time(tmp_path, events, EVENT="issues", EVENT_ACTION="assigned", ASSIGNEE="helper",
                           UPDATED="2026-09-30T10:24:00Z")
    assert t == "2026-09-30T10:26:00Z"


def test_a_malformed_time_is_dropped(tmp_path):
    t, res, _ = trigger_time(tmp_path, EVENT="issue_comment", COMMENT_CREATED="yesterday")
    assert t == "" and "no trigger time" in res.stdout


# --- the status comment -----------------------------------------------------------


GH_POST_STUB = r"""
sleep() { :; }
gh() {
  printf '%s\n' "$*" >>"$STATE/calls"
  for a in "$@"; do case "$a" in body=*) printf '%s' "${a#body=}" >"$STATE/body" ;; esac; done
  case "$SCENARIO" in fail) echo '{"message":"Server Error"}'; return 1 ;; esac
  case "$*" in *"--jq .id"*) echo 987654 ;; esac
}
"""


def status(tmp_path, scenario=""):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    out = tmp_path / "status-output"
    out.write_text("")
    env = {"GH_TOKEN": "t", "REPO": "o/r", "NUM": "12", "RUN_URL": "https://github.com/o/r/actions/runs/5",
           "STATE": str(state), "SCENARIO": scenario, "GITHUB_OUTPUT": str(out), "PATH": os.environ["PATH"]}
    res = sh("bash", "-c", GH_POST_STUB + STATUS, check=False, env=env)
    assert res.returncode == 0, res.stderr
    return outputs(out)["id"], (state / "body").read_text(), (state / "calls").read_text().splitlines()


def test_the_gate_posts_a_fixed_status_comment(tmp_path):
    cid, body, calls = status(tmp_path)
    assert cid == "987654"
    assert body == "🤖 **The dev agent is working on this** — [run](https://github.com/o/r/actions/runs/5)\n\n<!-- dev-agent-status -->"
    assert calls[0].startswith("api repos/o/r/issues/12/comments ")


def test_a_failed_status_post_leaves_no_id(tmp_path):
    cid, _, calls = status(tmp_path, "fail")
    assert cid == "" and sum(c.startswith("api ") for c in calls) == 2


FINISH_STUB = r"""
gh() {
  printf '%s\n' "$*" >>"$STATE/calls"
  for a in "$@"; do case "$a" in body=*) printf '%s' "${a#body=}" >"$STATE/body" ;; esac; done
}
"""


def finish(tmp_path, **env):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    base = {"GH_TOKEN": "t", "REPO": "o/r", "SERVER_URL": "https://github.com", "COMMENT_ID": "987654",
            "REQUESTER": "maintainer", "RUN_URL": "https://github.com/o/r/actions/runs/5", "IS_PR": "false",
            "LAND_OUTCOME": "success", "PR_NUMBER": "", "PUSHED_BRANCH": "", "AGENT_RESULT": "success",
            "STATE": str(state), "PATH": os.environ["PATH"]}
    res = sh("bash", "-c", FINISH_STUB + FINISH, check=False, env={**base, **env})
    assert res.returncode == 0, res.stderr
    calls = (state / "calls").read_text().splitlines()
    assert calls[0].startswith("api -X PATCH repos/o/r/issues/comments/987654 ")
    body = (state / "body").read_text()
    first, line, marker = body.split("\n\n")
    assert first == "**Claude finished @maintainer's task** — [run](https://github.com/o/r/actions/runs/5)"
    assert marker == "<!-- dev-agent-status -->"
    return line


@pytest.mark.parametrize("env, want", [
    ({"IS_PR": "true", "PR_NUMBER": "34", "PUSHED_BRANCH": "feature"}, "Pushed the changes to #34."),
    ({"PR_NUMBER": "35", "PUSHED_BRANCH": "claude/issue-12-40"}, "Opened #35 from branch `claude/issue-12-40`."),
    ({"PUSHED_BRANCH": "claude/issue-12-40"},
     "Pushed branch [`claude/issue-12-40`](https://github.com/o/r/tree/claude/issue-12-40)."),
    ({}, "No code changes."),
    ({"LAND_OUTCOME": "failure"}, "The run did not finish cleanly; see the note below."),
    ({"LAND_OUTCOME": "failure", "IS_PR": "true", "PR_NUMBER": "34", "PUSHED_BRANCH": "feature"},
     "Pushed the changes to #34. The run did not finish cleanly; see the note below."),
    ({"AGENT_RESULT": "cancelled", "LAND_OUTCOME": "skipped"}, "The agent job was cancelled; nothing was landed."),
])
def test_the_land_job_finishes_the_status_comment(tmp_path, env, want):
    assert finish(tmp_path, **env) == want


def test_the_finishing_step_reads_no_agent_text():
    land = job_block(CLAUDE.read_text(), "land")
    step = land[land.index("      - name: Finish the status comment\n"):]
    assert step.index("\n        if: always()") > 0
    exprs = set(re.findall(r"\$\{\{ ([^}]*) \}\}", step))
    assert exprs == {
        "steps.mint.outputs.token || github.token", "github.repository", "github.server_url",
        "needs.gate.outputs.status_comment_id", "github.actor", "github.run_id", "needs.gate.outputs.is_pr",
        "steps.land.outcome", "steps.land.outputs.pr_number", "steps.land.outputs.pushed_branch",
    }
    # Last step of the land job.
    assert land.rstrip().endswith('echo "::warning::could not update the status comment $COMMENT_ID."\n          fi')


# --- the workflow's shape ----------------------------------------------------------


def claude_action_step():
    agent = job_block(CLAUDE.read_text(), "agent")
    return next(s for s in agent.split("\n      - ") if "anthropics/claude-code-action@v1" in s)


def test_the_action_runs_agent_mode_on_the_job_token():
    s = claude_action_step()
    assert "          github_token: ${{ github.token }}\n" in s
    assert "          prompt: ${{ steps.prompt.outputs.value }}\n" in s
    assert "          base_branch: ${{ steps.sync.outputs.base || inputs.base_branch }}\n" in s
    for gone in ("trigger_phrase:", "label_trigger:", "additional_permissions:"):
        assert gone not in s


def test_the_claude_job_prepares_branch_and_context_before_the_agent_user():
    agent = job_block(CLAUDE.read_text(), "agent")
    order = ["id: sync\n", "id: prepbranch\n", "id: context\n", "id: agentuser\n", "id: launcher\n", "id: prompt\n",
             "id: claude\n", "id: agentreclaim\n"]
    at = [agent.index("        " + o) for o in order]
    assert at == sorted(at)
    assert "          context-dir: ${{ runner.temp }}/agent-context\n" in agent
    assert "steps.claude.outputs.branch_name" not in agent


def test_no_job_can_mint_a_claude_app_token():
    # Every claude-code-action step passes the job token, so the action never
    # runs the OIDC exchange; the codex jobs hold no id-token permission.
    for name in REUSABLE:
        for job, block in jobs((WORKFLOWS / name).read_text()).items():
            for s in steps(block):
                if "anthropics/claude-code-action@v1" in s:
                    assert "          github_token: ${{ github.token }}\n" in s, (name, job)


def test_the_gate_outputs_the_trigger_time_and_the_status_comment():
    gate = job_block(CLAUDE.read_text(), "gate")
    assert "      trigger_time: ${{ steps.triggertime.outputs.time }}\n" in gate
    assert "      status_comment_id: ${{ steps.status.outputs.id }}\n" in gate
    # After the 👀 and with the machine account's token (the job token when
    # the caller has no app secrets).
    assert gate.index("name: Acknowledge trigger (eyes)") < gate.index("name: Post the status comment")
    status_step = gate[gate.index("name: Post the status comment"):]
    assert "          GH_TOKEN: ${{ steps.mint.outputs.token || github.token }}\n" in status_step[:600]
