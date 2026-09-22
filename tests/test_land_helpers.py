"""Tests for the `land` composite's shell helpers and its git contract.

The composite's bash cannot run here as a unit, but two things it relies on
can: the de-fang/retry helpers in .github/actions/land/lib.sh, and the git
sequence the fetch/push steps execute (bundle the agent's commits above the
start SHA in one repo; in an EMPTY bare repo fetch the start SHA by SHA from
origin, verify and unbundle, check the tip and ancestry, refuse a moved
branch, refuse a bundle that changes files under .github/workflows/, push
without --force). The sequence here mirrors the steps line for line, so a
git behaviour change (or an edit to the steps that this does not follow)
shows up as a failing test rather than a red land job on every caller.
"""

import json
import re
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / ".github" / "actions" / "land" / "lib.sh"
EMIT = ROOT / ".github" / "actions" / "emit-landing" / "action.yml"
LAND = ROOT / ".github" / "actions" / "land" / "action.yml"


def sh(*cmd, cwd=None, check=True, env=None):
    e = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    if env:
        e.update(env)
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True, env=e)


def git(*args, cwd, check=True):
    return sh("git", *args, cwd=cwd, check=check)


def bash_lib(snippet: str, cwd=None) -> subprocess.CompletedProcess:
    return sh("bash", "-c", f". '{LIB}'\n{snippet}", cwd=cwd, check=False)


# The sentence every agent prompt's LANDING paragraph (and every codex
# CONSTRAINTS line) carries, up to the engine-specific "where to say so"
# clause: the land composite's `workflows` step refuses a bundle touching
# .github/workflows/, and the agent must hear that before spending its run.
WORKFLOW_FILES_RULE = (
    "Do NOT create or edit files under .github/workflows/: the workflow cannot push them "
    "(the machine account has no Workflows permission) and the landing refuses the whole bundle, "
    "so every commit of the run is lost; if the task needs a workflow change, stop and say so"
)


def step_block(text: str, step_id: str, indent: int = 6) -> str:
    """The text of one workflow/composite step: from its `id:` line to the next
    step's `- ` at INDENT spaces (6 for a job's steps, 4 for a composite's)."""
    lines = text.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.strip() == f"id: {step_id}" or line.strip() == f"- id: {step_id}")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith(" " * indent + "- ")), len(lines))
    return "".join(lines[start:end])


def job_block(text: str, job: str) -> str:
    """The text of one top-level job of a workflow: from its `  <job>:` line
    to the next top-level job (or the end). The four reusable workflows run
    each engine in its own job since 2026-09-22 (findings 4628446 and
    4629153), so a step lifted by id must be lifted from the right job."""
    body = text[text.index("\njobs:\n") + len("\njobs:\n"):]
    starts = [(m.start(), m.group(1)) for m in re.finditer(r"^  ([a-z_-]+):$", body, re.M)]
    for i, (s, name) in enumerate(starts):
        if name == job:
            return body[s:(starts[i + 1][0] if i + 1 < len(starts) else len(body))]
    raise KeyError(job)


def lift_run(text: str, anchor: str) -> str:
    """A step's bash, lifted from workflow TEXT (a job block, usually): the
    block scalar under the first `run: |` after the anchor line is every
    following line indented past the `run:` key, up to the first that is
    not — test_review_fix_gate.lift_step over text instead of a path."""
    lines = text.splitlines()
    start = lines.index(anchor)
    run_at = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run_at]) - len(lines[run_at].lstrip()) + 2
    body = []
    for line in lines[run_at + 1:]:
        if line.strip() == "":
            body.append("")
        elif line.startswith(" " * indent):
            body.append(line[indent:])
        else:
            break
    return "\n".join(body) + "\n"


# --- lib.sh -----------------------------------------------------------------


def test_defang_breaks_triggers_and_markers_case_insensitively(tmp_path):
    src = tmp_path / "in.md"
    src.write_text(
        "Please @review this and @Claude too; @AUTO.\n"
        "<!-- claude-review-verdict --> <!-- Claude-Review-Summary --> claude-review-comment claude-review-nudge\n"
        "<!-- AUTO-HANDOFF --> auto-converged auto-review-rounds auto-review-head auto-fix-attempts\n"
        "🤖 engine: codex · Engine: Codex\n"
    )
    dst = tmp_path / "out.md"
    r = bash_lib(f"defang '{src}' '{dst}'")
    assert r.returncode == 0, r.stderr
    out = dst.read_text()
    for live in ("@review", "@Claude", "@AUTO", "claude-review-", "AUTO-HANDOFF", "auto-converged",
                 "auto-review-rounds", "auto-review-head", "auto-fix-attempts"):
        assert live.lower() not in out.lower(), out
    assert "`review`" in out and "`Claude`" in out and "`AUTO`" in out
    # The replacement text is literal (lowercase); the captured suffix keeps its case.
    assert "claude-review verdict" in out and "auto HANDOFF" in out
    # The codex reviewer's footer SURVIVES: claude-review.yml lands the codex review
    # through this composite, and the footer is pr-feedback-context's anchor for the
    # next codex fix round. Callers that must not pose as a review sed it themselves.
    assert "🤖 engine: codex · Engine: Codex" in out


def test_defang_caps_oversized_bodies(tmp_path):
    src = tmp_path / "in.md"
    src.write_text("x" * 70000)
    dst = tmp_path / "out.md"
    assert bash_lib(f"defang '{src}' '{dst}'").returncode == 0
    out = dst.read_text()
    assert len(out) < 65536
    assert out.endswith("_[truncated: the body exceeded the comment size cap]_\n")


def test_defang_str_strips_newlines_and_triggers():
    r = bash_lib("defang_str 'Fix @auto loop\nclaude-review-summary'")
    assert r.returncode == 0
    assert r.stdout == "Fix `auto` loopclaude-review summary"


def test_retry_returns_last_status_and_keeps_stdout_clean():
    # sleep is skipped after the final attempt; with n=1 there is no sleep at all.
    r = bash_lib("retry 1 what false; echo rc=$?")
    assert r.stdout.strip() == "rc=1"
    r = bash_lib("out=$(retry 1 what echo hello); echo \"[$out]\"")
    assert r.stdout.strip() == "[hello]"


def test_retry_read_prints_only_the_successful_attempts_stdout(tmp_path):
    # A failed `gh api` prints its JSON error body to stdout even with --jq;
    # plain `retry` would stream it into the capture ahead of the real value.
    flag = tmp_path / "flaked"
    cmd = "\n".join([
        "sleep() { :; }",
        f"flaky() {{ if [ ! -f '{flag}' ]; then touch '{flag}'; echo '{{\"message\": \"Server Error\"}}'; return 1; fi; echo 0; }}",
        'out=$(retry_read 3 what flaky); echo "rc=$? out=[$out]"',
    ])
    r = bash_lib(cmd)
    assert r.stdout.strip() == "rc=0 out=[0]"
    assert "retrying" in r.stderr
    r = bash_lib("sleep() { :; }; out=$(retry_read 2 what sh -c 'echo junk; exit 3'); echo \"rc=$? out=[$out]\"")
    assert r.stdout.strip() == "rc=3 out=[]"


# A stub `gh` for open_or_adopt_pr: `pr list` answers with the JSON the real
# command would (`--json` fields), listing the same-repo PR once it exists
# ($STATE/pr, head owner `o` — the owner of the `o/r` the tests pass) and,
# when $STATE/fork names one, a FORK PR whose head branch merely shares the
# name (listed FIRST, as the API may order it). It refuses a `pr list` that
# does not request the ownership fields, since the helper's filter is only
# as good as what it asked for. `pr create` performs the write, and when
# LOSE_FIRST is set its FIRST call exits 1 after writing (a timeout / 5xx
# after the server accepted the PR). Calls are logged to $STATE/calls.
# `sleep` is neutralised so the retry backoff does not slow the suite.
GH_STUB = r"""
sleep() { :; }
gh() {
  echo "$1 $2" >>"$STATE/calls"
  case "$1 $2" in
    "pr list")
      for f in isCrossRepository headRepositoryOwner headRefName; do
        case " $* " in *"$f"*) ;; *) echo "stub: pr list did not request $f" >&2; return 2 ;; esac
      done
      sep=""
      echo "["
      if [ -f "$STATE/fork" ]; then
        n=$(cat "$STATE/fork")
        printf '{"number":%s,"url":"https://x/pull/%s","isCrossRepository":true,"headRepositoryOwner":{"login":"someone"},"headRefName":"feat"}' "$n" "$n"
        sep=","
      fi
      if [ -f "$STATE/pr" ]; then
        n=$(cat "$STATE/pr")
        printf '%s{"number":%s,"url":"https://x/pull/%s","isCrossRepository":false,"headRepositoryOwner":{"login":"o"},"headRefName":"feat"}' "$sep" "$n" "$n"
      fi
      echo "]" ;;
    "pr create")
      [ -f "$STATE/pr" ] || echo 42 >"$STATE/pr"
      if [ -n "${LOSE_FIRST:-}" ] && [ ! -f "$STATE/lost" ]; then touch "$STATE/lost"; echo "gh: timeout" >&2; return 1; fi
      echo "https://x/pull/$(cat "$STATE/pr")" ;;
    *) echo "unexpected gh $*" >&2; return 2 ;;
  esac
}
"""


def open_or_adopt(tmp_path, *, lose_first=False, existing=None, fork=None):
    state = tmp_path / "state"
    state.mkdir()
    if existing is not None:
        (state / "pr").write_text(str(existing))
    if fork is not None:
        (state / "fork").write_text(str(fork))
    body = tmp_path / "body.md"
    body.write_text("body\n")
    env = {"STATE": str(state), "LOSE_FIRST": "1" if lose_first else ""}
    r = sh(
        "bash", "-c",
        f". '{LIB}'\n{GH_STUB}\nresult=$(retry 3 what open_or_adopt_pr o/r feat main T '{body}') || exit 9\n"
        "read -r how number url <<<\"$result\"; echo \"$how|$number|$url\"",
        check=False, env=env,
    )
    calls = (state / "calls").read_text().splitlines() if (state / "calls").exists() else []
    return r, calls


def test_open_or_adopt_pr_creates_when_none_exists(tmp_path):
    r, calls = open_or_adopt(tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "opened|42|https://x/pull/42"
    assert calls == ["pr list", "pr create"]


def test_open_or_adopt_pr_adopts_an_agent_opened_pr(tmp_path):
    r, calls = open_or_adopt(tmp_path, existing=7)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "adopted|7|https://x/pull/7"
    assert "pr create" not in calls


def test_open_or_adopt_pr_adopts_after_a_lost_create_response(tmp_path):
    # Attempt 1 created the PR but its response was lost; attempt 2 must
    # find and adopt it rather than fail on "a pull request already exists".
    r, calls = open_or_adopt(tmp_path, lose_first=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "adopted|42|https://x/pull/42"
    assert calls.count("pr create") == 1
    assert "retrying" in r.stderr


def test_open_or_adopt_pr_skips_a_fork_pr_with_the_same_head_name(tmp_path):
    # `gh pr list --head` matches on the branch NAME alone, so a fork PR whose
    # head is also called `feat` is listed. It is not ours to adopt: the
    # landing must say so and open a PR for the branch it just pushed.
    r, calls = open_or_adopt(tmp_path, fork=5)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "opened|42|https://x/pull/42"
    assert calls == ["pr list", "pr create"]
    assert "not adopting PR #5" in r.stderr and "someone:feat" in r.stderr, r.stderr


def test_open_or_adopt_pr_adopts_the_same_repo_pr_not_the_fork_listed_first(tmp_path):
    # Both exist; the fork PR comes first in the listing. Ownership, not
    # position, decides.
    r, calls = open_or_adopt(tmp_path, existing=7, fork=5)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "adopted|7|https://x/pull/7"
    assert "pr create" not in calls
    assert r.stderr == ""


# A stub `gh api repos/o/r/branches/<b>` for remote_branch_exists: `exists`
# answers 200, `missing` answers gh's 404 line, `flaky` fails with a 502 on
# its first call and 200 afterwards (the transient blip `retry` must absorb).
BRANCH_STUB = r"""
sleep() { :; }
gh() {
  echo "$*" >>"$STATE/calls"
  case "$2" in
    repos/o/r/branches/exists) return 0 ;;
    repos/o/r/branches/missing) echo "gh: Branch not found (HTTP 404)" >&2; return 1 ;;
    repos/o/r/branches/flaky)
      if [ ! -f "$STATE/flaked" ]; then touch "$STATE/flaked"; echo "gh: Server Error (HTTP 502)" >&2; return 1; fi
      return 0 ;;
    *) echo "unexpected gh $*" >&2; return 2 ;;
  esac
}
"""


def branch_exists(tmp_path, branch, attempts=3):
    state = tmp_path / "state"
    state.mkdir()
    r = sh(
        "bash", "-c",
        f". '{LIB}'\n{BRANCH_STUB}\nout=$(retry {attempts} what remote_branch_exists o/r {branch}); echo \"rc=$? out=$out\"",
        check=False, env={"STATE": str(state)},
    )
    calls = (state / "calls").read_text().splitlines() if (state / "calls").exists() else []
    return r, calls


def test_remote_branch_exists_yes(tmp_path):
    r, calls = branch_exists(tmp_path, "exists")
    assert r.stdout.strip() == "rc=0 out=yes"
    assert len(calls) == 1


def test_remote_branch_exists_treats_404_as_a_definite_no(tmp_path):
    # A 404 is an answer, not a failure: no retry, `no` on stdout, exit 0.
    r, calls = branch_exists(tmp_path, "missing")
    assert r.stdout.strip() == "rc=0 out=no"
    assert len(calls) == 1
    assert "retrying" not in r.stderr


def test_remote_branch_exists_retries_a_transient_error(tmp_path):
    r, calls = branch_exists(tmp_path, "flaky")
    assert r.stdout.strip() == "rc=0 out=yes"
    assert len(calls) == 2
    assert "HTTP 502" in r.stderr and "retrying" in r.stderr


def test_remote_branch_exists_fails_when_the_error_persists(tmp_path):
    # One attempt only: the 502 is not absorbed, and the caller can tell it
    # from a missing branch (non-zero exit, nothing on stdout).
    r, calls = branch_exists(tmp_path, "flaky", attempts=1)
    assert r.stdout.strip() == "rc=1 out="
    assert len(calls) == 1


# A stub `gh` for atlas_todo: the issue's node id, the board add (an item
# id), the current Status (from $STATE/status) and the status write, which
# is logged so the tests see whether Todo was written.
ATLAS_STUB = r"""
gh() {
  echo "$*" >>"$STATE/calls"
  case "$*" in
    "api repos/"*) echo "I_kwDONODE" ;;
    *addProjectV2ItemById*) echo "PVTI_ITEM" ;;
    *fieldValueByName*)
      if [ -n "${FLAKY:-}" ] && [ ! -f "$STATE/flaked" ]; then touch "$STATE/flaked"; echo '{"errors":[{"message":"Something went wrong"}]}'; return 1; fi
      cat "$STATE/status" 2>/dev/null; echo ;;
    *updateProjectV2ItemFieldValue*) echo "status-write" >>"$STATE/writes" ;;
    *) echo "unexpected gh $*" >&2; return 2 ;;
  esac
}
"""


def atlas(tmp_path, status, *, flaky=False):
    state = tmp_path / "state"
    state.mkdir()
    if status is not None:
        (state / "status").write_text(status)
    env = {"STATE": str(state), **({"FLAKY": "1"} if flaky else {})}
    r = sh("bash", "-c", f". '{LIB}'\nsleep() {{ :; }}\n{ATLAS_STUB}\natlas_todo o/r 7", check=False, env=env)
    writes = (state / "writes").read_text().splitlines() if (state / "writes").exists() else []
    return r, writes


@pytest.mark.parametrize("status", [None, "", "Done"])
def test_atlas_todo_sets_todo_when_status_is_unset_or_done(tmp_path, status):
    r, writes = atlas(tmp_path, status)
    assert r.returncode == 0, r.stderr
    assert writes == ["status-write"]
    assert "Status=Todo" in r.stdout


def test_atlas_todo_writes_todo_after_a_recovered_status_read(tmp_path):
    # The first read fails with a JSON error body on stdout; the retry's
    # "Done" is the value that decides — not the two concatenated.
    r, writes = atlas(tmp_path, "Done", flaky=True)
    assert r.returncode == 0, r.stderr
    assert writes == ["status-write"]
    assert "Status=Todo (was 'Done')" in r.stdout
    assert "retrying" in r.stderr


def test_atlas_todo_leaves_a_status_a_human_set(tmp_path):
    r, writes = atlas(tmp_path, "In progress")
    assert r.returncode == 0, r.stderr
    assert writes == []
    assert "left as is" in r.stdout


def test_atlas_todo_fails_when_the_board_add_fails(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    stub = ATLAS_STUB.replace('*addProjectV2ItemById*) echo "PVTI_ITEM" ;;', '*addProjectV2ItemById*) return 1 ;;')
    r = sh("bash", "-c", f". '{LIB}'\n{stub}\natlas_todo o/r 7", check=False, env={"STATE": str(state)})
    assert r.returncode == 1


def test_atlas_todo_skips_the_status_write_when_the_read_fails(tmp_path):
    # A failed read is not "unset": writing Todo on it would overwrite a
    # status a human chose. The read is retried; when it still fails the
    # add stands (exit 0), the write is skipped and a warning says so.
    state = tmp_path / "state"
    state.mkdir()
    (state / "status").write_text("In progress")
    stub = ATLAS_STUB.replace('      cat "$STATE/status" 2>/dev/null; echo ;;', '      echo \'{"errors":[]}\'; return 1 ;;')
    r = sh("bash", "-c", f". '{LIB}'\nsleep() {{ :; }}\n{stub}\natlas_todo o/r 7", check=False, env={"STATE": str(state)})
    assert r.returncode == 0, r.stderr
    calls = (state / "calls").read_text().splitlines()
    assert sum("fieldValueByName" in c for c in calls) == 3
    assert not (state / "writes").exists()
    assert "Status could not be read; not set to Todo" in r.stdout


# A stub `curl` for slack_post_file: records the request body and answers
# from $STATE/response.
SLACK_STUB = r"""
curl() {
  cat >"$STATE/body"
  echo "$*" >"$STATE/curl-args"
  cat "$STATE/response"
}
"""


def slack(tmp_path, response, *, thread="1726000000.123456", text="hello <@U1> *bold*\n@review me\n", token="xoxb-test"):
    state = tmp_path / "state"
    state.mkdir()
    (state / "response").write_text(response)
    f = tmp_path / "slack.txt"
    f.write_text(text)
    r = sh("bash", "-c", f". '{LIB}'\n{SLACK_STUB}\nslack_post_file C0123456789 '{thread}' '{f}'",
           check=False, env={"STATE": str(state), "SLACK_TOKEN": token})
    body = json.loads((state / "body").read_text()) if (state / "body").exists() else None
    args = (state / "curl-args").read_text() if (state / "curl-args").exists() else ""
    return r, body, args


def test_slack_post_file_posts_the_text_as_data_in_the_thread(tmp_path):
    r, body, args = slack(tmp_path, '{"ok": true, "ts": "1.2"}')
    assert r.returncode == 0, r.stderr
    assert body == {"channel": "C0123456789", "thread_ts": "1726000000.123456", "text": "hello <@U1> *bold*\n@review me\n"}
    assert "Bearer xoxb-test" in args and "chat.postMessage" in args


def test_slack_post_file_posts_at_the_channel_root_without_a_thread(tmp_path):
    r, body, _ = slack(tmp_path, '{"ok": true}', thread="")
    assert r.returncode == 0, r.stderr
    assert "thread_ts" not in body


def test_slack_post_file_fails_on_a_refused_post(tmp_path):
    r, _, _ = slack(tmp_path, '{"ok": false, "error": "channel_not_found"}')
    assert r.returncode == 1
    assert "channel_not_found" in r.stderr


def test_slack_post_file_refuses_an_empty_token(tmp_path):
    r, body, _ = slack(tmp_path, '{"ok": true}', token="")
    assert r.returncode == 1
    assert body is None  # no request was made


def test_slack_post_file_caps_the_text(tmp_path):
    r, body, _ = slack(tmp_path, '{"ok": true}', text="x" * 50000)
    assert r.returncode == 0, r.stderr
    assert len(body["text"]) == 39000


def test_slack_post_file_appends_the_tail_after_the_cap(tmp_path):
    # The land job's own `Tracking issue:` lines ride in a tail file that
    # is appended whole: the cap cuts the agent's text, never the links.
    state = tmp_path / "state"
    state.mkdir()
    (state / "response").write_text('{"ok": true}')
    (tmp_path / "slack.txt").write_text("x" * 50000)
    tail = "\nTracking issue: https://github.com/o/r/issues/7\n"
    (tmp_path / "tail.txt").write_text(tail)
    r = sh("bash", "-c", f". '{LIB}'\n{SLACK_STUB}\nslack_post_file C0123456789 '' '{tmp_path}/slack.txt' '{tmp_path}/tail.txt'",
           check=False, env={"STATE": str(state), "SLACK_TOKEN": "xoxb-test"})
    assert r.returncode == 0, r.stderr
    body = json.loads((state / "body").read_text())
    assert len(body["text"]) == 39000
    assert body["text"].endswith(tail)


# --- the Post step ---------------------------------------------------------
#
# The `post` step's bash, lifted from the action (see emit_landing_script for
# the extraction) and run against a manifest with `gh` and `curl` stubbed:
# the issues loop's assign / reopen / Atlas decisions and the Slack post are
# where a wrong branch writes to someone else's issue or board item.


def step_script(step_id: str) -> str:
    """The bash under `run: |` of one of the land composite's steps."""
    lines = LAND.read_text().splitlines()
    start = lines.index(f"    - id: {step_id}")
    run_at = next(i for i in range(start, len(lines)) if lines[i] == "      run: |")
    body = []
    for line in lines[run_at + 1:]:
        if line.strip() == "":
            body.append("")
        elif line.startswith("        "):
            body.append(line[8:])
        else:
            break
    return "\n".join(body) + "\n"


def post_script() -> str:
    return step_script("post")


# `gh` answers from $SCENARIO: the issue's assignee count (`0`, `1`, or a
# failed lookup), whether `issue edit` / `issue reopen` succeed, the PR's
# head SHA (or a failed lookup: `head-fails`) and whether an inline review
# comment anchors (`inline-422`: never; `inline-flaky`: after one 500;
# `inline-500`: never, no 422) or a top-level comment posts
# (`comment-fails`, alone or as `inline-422-comment-fails`). `curl` records the Slack request. Every call is logged;
# the Atlas status write is logged separately so a test can assert the board
# was not touched, and every body file handed to `-F body=@…` is appended to
# $STATE/bodies (separated by `===` lines) so a test can read what posted.
POST_STUB = r"""
sleep() { :; }
record_body() { for a in "$@"; do case "$a" in body=@*) cat "${a#body=@}" >>"$STATE/bodies"; printf '\n===\n' >>"$STATE/bodies" ;; esac; done; }
gh() {
  echo "$*" >>"$STATE/calls"
  case "$*" in
    "pr view "*"--json headRefOid"*)
      case "$SCENARIO" in
        head-fails) echo '{"message": "Server Error"}'; return 1 ;;
        *) echo "cccccccccccccccccccccccccccccccccccccccc" ;;
      esac ;;
    "api repos/"*"/pulls/"*"/comments "*)
      record_body "$@"
      case "$SCENARIO" in
        inline-422*) echo '{"message": "Validation Failed"}'; echo "gh: Validation Failed (HTTP 422)" >&2; return 1 ;;
        inline-500) echo "gh: Server Error (HTTP 500)" >&2; return 1 ;;
        inline-flaky) if [ ! -f "$STATE/flaked" ]; then touch "$STATE/flaked"; echo "gh: Server Error (HTTP 500)" >&2; return 1; fi ;;
      esac
      return 0 ;;
    "api repos/"*"/issues/"*"/comments "*)
      record_body "$@"
      case "$SCENARIO" in *comment-fails) return 1 ;; esac
      return 0 ;;
    "api repos/"*"--jq .node_id") echo "I_kwDONODE" ;;
    "api repos/"*"--jq .assignees | length")
      case "$SCENARIO" in
        lookup-fails) echo '{"message": "Server Error"}'; return 1 ;;
        lookup-flaky) if [ ! -f "$STATE/flaked" ]; then touch "$STATE/flaked"; echo '{"message": "Server Error"}'; return 1; fi; echo 0 ;;
        owned) echo 1 ;;
        *) echo 0 ;;
      esac ;;
    "issue create "*) echo "https://github.com/o/r/issues/7" ;;
    "issue edit "*) [ "$SCENARIO" != edit-fails ] ;;
    "issue reopen "*) [ "$SCENARIO" != reopen-fails ] ;;
    *addProjectV2ItemById*) echo "PVTI_ITEM" ;;
    *fieldValueByName*) echo "Done" ;;
    *updateProjectV2ItemFieldValue*) echo "status-write" >>"$STATE/writes" ;;
    *) echo "unexpected gh $*" >&2; return 2 ;;
  esac
}
curl() {
  cat >"$STATE/slack-request"
  echo '{"ok": true}'
}
"""


def run_post(tmp_path, manifest: dict, files: dict, *, scenario="", slack_env=None, pr_number="", refused=False):
    state = tmp_path / "state"
    state.mkdir()
    landing = tmp_path / "landing"
    landing.mkdir()
    for name, text in files.items():
        (landing / name).write_text(text)
    (landing / "manifest.json").write_text(json.dumps(manifest))
    out = tmp_path / "out.txt"
    out.write_text("")
    env = {
        "STATE": str(state), "SCENARIO": scenario, "LIB": str(LIB), "DIR": str(landing),
        "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(out), "REPO": "o/r", "PR_NUMBER": pr_number,
        "SLACK_TOKEN": "", "SLACK_CHANNEL": "", "SLACK_THREAD_TS": "",
        "REFUSED": "1" if refused else "",
        **(slack_env or {}),
    }
    # `-eo pipefail`, as GitHub invokes `shell: bash` (`bash --noprofile
    # --norc -eo pipefail`): the step's own `set -uo pipefail` adds -u and
    # leaves -e on, so a failing bare command aborts the real step too.
    r = sh("bash", "-eo", "pipefail", "-c", POST_STUB + post_script(), check=False, env=env)
    calls = (state / "calls").read_text().splitlines() if (state / "calls").exists() else []
    writes = (state / "writes").read_text().splitlines() if (state / "writes").exists() else []
    failed = post_outputs(tmp_path).get("failed", "")
    return r, calls, writes, failed


def post_outputs(tmp_path) -> dict:
    """The Post step's $GITHUB_OUTPUT lines (`failed=…`, `posted_comments=…`)."""
    return dict(line.split("=", 1) for line in (tmp_path / "out.txt").read_text().splitlines() if "=" in line)


def posted_bodies(tmp_path) -> list:
    """Every body the stub `gh` was handed, in posting order."""
    f = tmp_path / "state" / "bodies"
    return [b for b in f.read_text().split("\n===\n") if b] if f.exists() else []


def comment_on_manifest(**issue):
    return {"schema": 1, "repo": "o/r", "run_id": 123, "branch": "b", "start_sha": "a" * 40, "head_sha": "a" * 40,
            "has_bundle": False,
            "issues": [{"repo": "o/r", "title": "t", "body_file": "i.md", "comment_on": 9, **issue}]}


def test_post_assigns_an_unowned_issue_and_puts_it_on_atlas(tmp_path):
    r, calls, writes, failed = run_post(tmp_path, comment_on_manifest(assignees=["ransomr"]), {"i.md": "body\n"})
    assert r.returncode == 0, r.stderr
    assert "issue edit 9 --repo o/r --add-assignee ransomr" in calls
    assert writes == ["status-write"]
    assert failed == ""


def test_post_assigns_after_a_recovered_assignee_lookup(tmp_path):
    # `gh api` prints its error body to STDOUT on a failed attempt; the
    # retried lookup must yield the successful attempt's `0` alone, or the
    # assignment is skipped as "already owned" with nothing recorded.
    r, calls, writes, failed = run_post(tmp_path, comment_on_manifest(assignees=["ransomr"]), {"i.md": "body\n"}, scenario="lookup-flaky")
    assert r.returncode == 0, r.stderr
    assert sum(c.endswith("--jq .assignees | length") for c in calls) == 2
    assert "issue edit 9 --repo o/r --add-assignee ransomr" in calls
    assert writes == ["status-write"]
    assert failed == ""


def test_post_leaves_an_owned_issue_and_its_board_item_alone(tmp_path):
    # Nothing was adopted: no assignment over a human's ownership, and no
    # Atlas write on the strength of a request that was declined.
    r, calls, writes, failed = run_post(tmp_path, comment_on_manifest(assignees=["ransomr"]), {"i.md": "body\n"}, scenario="owned")
    assert r.returncode == 0, r.stderr
    assert not any(c.startswith("issue edit") for c in calls)
    assert not any("addProjectV2ItemById" in c for c in calls)
    assert writes == []
    assert failed == ""
    assert "already has 1 assignee(s)" in r.stdout


def test_post_records_a_failed_assignee_lookup(tmp_path):
    # Ownership unknown is not ownership taken: the lookup is retried, then
    # the assignment is a recorded failure (`failed` output) and nothing is
    # written to the issue or the board.
    r, calls, writes, failed = run_post(tmp_path, comment_on_manifest(assignees=["ransomr"]), {"i.md": "body\n"}, scenario="lookup-fails")
    assert r.returncode == 0, r.stderr
    assert sum(c.endswith("--jq .assignees | length") for c in calls) == 3
    assert not any(c.startswith("issue edit") for c in calls)
    assert writes == []
    assert "could not read o/r#9's assignees; not assigned" in failed
    assert "already has" not in r.stdout


@pytest.mark.parametrize("scenario,issue,message", [
    ("edit-fails", {"assignees": ["ransomr"]}, "assign of o/r#9 failed"),
    ("reopen-fails", {"reopen": True}, "reopen of o/r#9 failed"),
])
def test_post_does_not_board_an_issue_whose_adoption_failed(tmp_path, scenario, issue, message):
    r, calls, writes, failed = run_post(tmp_path, comment_on_manifest(**issue), {"i.md": "body\n"}, scenario=scenario)
    assert r.returncode == 0, r.stderr
    assert not any("addProjectV2ItemById" in c for c in calls)
    assert writes == []
    assert message in failed


def test_post_reopens_and_boards_an_issue(tmp_path):
    r, calls, writes, failed = run_post(tmp_path, comment_on_manifest(reopen=True), {"i.md": "body\n"})
    assert r.returncode == 0, r.stderr
    assert "issue reopen 9 --repo o/r" in calls
    assert writes == ["status-write"]
    assert failed == ""


def test_post_slack_tracking_links_survive_the_cap(tmp_path):
    # A message at the cap plus an issue created in the same run: every
    # created URL is in the post, inside the cap; the agent's text is cut.
    m = {"schema": 1, "repo": "o/r", "run_id": 123, "branch": "b", "start_sha": "a" * 40, "head_sha": "a" * 40,
         "has_bundle": False,
         "issues": [{"repo": "o/r", "title": "t", "body_file": "i.md", "assignees": ["ransomr"]}],
         "slack": {"text_file": "slack.txt"}}
    r, calls, writes, failed = run_post(
        tmp_path, m, {"i.md": "body\n", "slack.txt": "x" * 39000},
        slack_env={"SLACK_TOKEN": "xoxb-test", "SLACK_CHANNEL": "C0123456789", "SLACK_THREAD_TS": "1726000000.123456"},
    )
    assert r.returncode == 0, r.stderr
    assert "issue create --repo o/r --title t --body-file" in " ".join(calls) and "--assignee ransomr" in " ".join(calls)
    assert writes == ["status-write"]
    assert failed == ""
    req = json.loads((tmp_path / "state" / "slack-request").read_text())
    assert req["channel"] == "C0123456789" and req["thread_ts"] == "1726000000.123456"
    assert len(req["text"]) <= 39000
    assert req["text"].endswith("\nTracking issue: https://github.com/o/r/issues/7\n")


# --- the reviewer's landing (issue #114): the review marker and the inline
# review comments. The Claude reviewer posts nothing itself any more; its
# summary lands through comments[] flagged `review`, its line-level findings
# through review_comments[], its verdict through review_verdict.


def review_landing(**overrides):
    m = {"schema": 1, "repo": "o/r", "run_id": 123, "branch": "b", "start_sha": "a" * 40, "head_sha": "a" * 40,
         "has_bundle": False, "pr_number": 5,
         "comments": [{"number": 5, "body_file": "review.md", "review": True}],
         "review_comments": [
             {"path": "src/a.py", "line": 3, "side": "RIGHT", "body_file": "rc0.md"},
             {"path": "docs/x.md", "line": 10, "body_file": "rc1.md"},
         ],
         "review_verdict": "suggestions"}
    m.update(overrides)
    return m


REVIEW_FILES = {"review.md": "Summary; please @review again. <!-- claude-review-comment -->\n",
                "rc0.md": "Off by one; see @auto.\n", "rc1.md": "Typo.\n"}


def test_post_appends_the_review_marker_after_the_defang(tmp_path):
    # The marker is land's own text: the copy in the agent's body is split by
    # the de-fang like any loop marker, and exactly one live marker follows,
    # so pr-feedback-context anchors on the landed review and the stubs skip
    # it — and a comments[] entry without the flag gets none.
    r, calls, writes, failed = run_post(tmp_path, review_landing(review_comments=[]), REVIEW_FILES, pr_number="5")
    assert r.returncode == 0, r.stderr
    assert failed == ""
    (body,) = posted_bodies(tmp_path)
    assert body.count("<!-- claude-review-comment -->") == 1
    assert body.endswith("\n<!-- claude-review-comment -->\n")
    assert "`review`" in body and "claude-review comment" in body
    assert post_outputs(tmp_path)["posted_comments"] == "1"
    (tmp_path / "plain").mkdir()
    r, calls, writes, failed = run_post(tmp_path / "plain", review_landing(
        review_comments=[], comments=[{"number": 5, "body_file": "review.md"}]), REVIEW_FILES, pr_number="5")
    (body,) = posted_bodies(tmp_path / "plain")
    assert "<!-- claude-review-comment -->" not in body


def test_post_anchors_inline_review_comments_to_the_prs_head(tmp_path):
    r, calls, writes, failed = run_post(tmp_path, review_landing(), REVIEW_FILES, pr_number="5")
    assert r.returncode == 0, r.stderr
    assert failed == ""
    inline = [c for c in calls if c.startswith("api repos/o/r/pulls/5/comments ")]
    assert len(inline) == 2
    assert "-f commit_id=" + "c" * 40 + " -f path=src/a.py -F line=3 -f side=RIGHT --silent" in inline[0]
    # `side` defaults to RIGHT; every body is de-fanged.
    assert "-f path=docs/x.md -F line=10 -f side=RIGHT" in inline[1]
    bodies = posted_bodies(tmp_path)
    assert bodies[1] == "Off by one; see `auto`.\n" and bodies[2] == "Typo.\n"
    # No follow-up comment: everything anchored. Summary + 2 inline = 3 posts.
    assert len(bodies) == 3
    assert "posted 2 of 2 inline review comment(s)" in r.stdout


def test_post_folds_unanchorable_inline_comments_into_one_follow_up(tmp_path):
    # 422 is final (no retry — one attempt each), the findings go into ONE
    # top-level comment, and nothing is recorded as failed: the verdict must
    # not be withheld over an anchor.
    r, calls, writes, failed = run_post(tmp_path, review_landing(), REVIEW_FILES, pr_number="5", scenario="inline-422")
    assert r.returncode == 0, r.stderr
    assert failed == ""
    assert sum(c.startswith("api repos/o/r/pulls/5/comments ") for c in calls) == 2
    bodies = posted_bodies(tmp_path)
    follow_up = bodies[-1]
    assert follow_up.startswith("Inline review comments that could not be anchored")
    assert "**src/a.py** line 3 (RIGHT)" in follow_up and "Off by one; see `auto`." in follow_up
    assert "**docs/x.md** line 10 (RIGHT)" in follow_up and "Typo." in follow_up
    assert calls[-1].startswith("api repos/o/r/issues/5/comments ")


def test_post_retries_a_transient_inline_comment_failure(tmp_path):
    r, calls, writes, failed = run_post(tmp_path, review_landing(), REVIEW_FILES, pr_number="5", scenario="inline-flaky")
    assert r.returncode == 0, r.stderr
    assert failed == ""
    assert sum(c.startswith("api repos/o/r/pulls/5/comments ") for c in calls) == 3
    assert not any(c.startswith("api repos/o/r/issues/5/comments ") and "unanchored" in c for c in calls)
    assert len(posted_bodies(tmp_path)) == 4  # summary, failed attempt, retry, second inline


def test_post_folds_every_inline_comment_when_the_head_lookup_fails(tmp_path):
    # No commit to anchor to: nothing is tried inline, all go to the follow-up.
    r, calls, writes, failed = run_post(tmp_path, review_landing(), REVIEW_FILES, pr_number="5", scenario="head-fails")
    assert r.returncode == 0, r.stderr
    assert failed == ""
    assert not any(c.startswith("api repos/o/r/pulls/") for c in calls)
    assert sum(c.startswith("pr view 5 ") for c in calls) == 3  # retried, then given up
    assert "could not read #5's head SHA" in r.stdout
    assert posted_bodies(tmp_path)[-1].startswith("Inline review comments that could not be anchored")


def test_post_records_a_lost_follow_up_comment(tmp_path):
    # The one inline-comment failure that IS a lost post: the follow-up
    # comment itself failing after the 422s. (The summary shares the
    # endpoint and fails too; both are named, and the verdict is withheld.)
    r, calls, writes, failed = run_post(tmp_path, review_landing(), REVIEW_FILES, pr_number="5",
                                        scenario="inline-422-comment-fails")
    assert r.returncode == 0, r.stderr
    assert "comment on #5 failed after 5 attempts" in failed
    assert "follow-up comment (part 1) with unanchored inline review comment(s) on #5 failed after 5 attempts" in failed


def test_post_on_a_refused_bundle_posts_only_the_comments(tmp_path):
    # The `workflows` step refused the bundle: the Post step still runs (its
    # `if` admits that one failure) and posts the manifest's comments[] —
    # they name their own issue/PR and do not depend on the push — but
    # nothing that does: no reply, no thread resolution, no follow-up
    # issue, no Slack post. Nothing is recorded as failed: the refusal is
    # Report's to explain.
    manifest = {
        "schema": 1, "repo": "o/r", "run_id": 123, "branch": "b", "start_sha": "a" * 40, "head_sha": "b" * 40,
        "has_bundle": True, "pr_number": 5,
        "comments": [{"number": 9, "body_file": "c.md"}],
        "replies": [{"review_comment_id": 11, "body_file": "r.md"}],
        "resolve_threads": ["PRRT_x"],
        "issues": [{"repo": "o/r", "title": "t", "body_file": "i.md"}],
        "slack": {"text_file": "s.md"},
    }
    files = {"c.md": "the task needs a workflow change; a maintainer must make it\n", "r.md": "reply\n", "i.md": "issue\n", "s.md": "slack\n"}
    r, calls, writes, failed = run_post(tmp_path, manifest, files, pr_number="5", refused=True,
                                        slack_env={"SLACK_TOKEN": "xoxb", "SLACK_CHANNEL": "C1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert failed == ""
    assert post_outputs(tmp_path)["posted_comments"] == "1"
    assert calls == ["api repos/o/r/issues/9/comments -F body=@" + str(tmp_path / "comment-0.md") + " --silent"]
    assert posted_bodies(tmp_path) == [files["c.md"]]
    assert writes == []
    assert not (tmp_path / "state" / "slack-request").exists()
    assert "posting nothing that depends on the push" in r.stdout


def test_post_step_runs_on_the_refusal_path_only_for_the_comments():
    text = LAND.read_text()
    block = step_block(text, "post", indent=4)
    assert block.splitlines()[1].strip() == "if: always() && (steps.pr.outcome == 'success' || steps.workflows.outcome == 'failure')"
    assert "REFUSED: ${{ steps.workflows.outcome == 'failure' && '1' || '' }}" in block
    # The verdict, hand-back, hand-off and stage steps stay skipped: none
    # admits a failed `workflows` step.
    for step in ("verdict", "handback", "handoff", "stage"):
        assert "steps.workflows" not in step_block(text, step, indent=4), step


def big_finding(i: int) -> str:
    return f"FINDING_{i}_BEGIN\n" + "x" * 30000 + f"\nFINDING_{i}_END\n"


def test_post_splits_unanchored_findings_into_bounded_comments(tmp_path):
    # Review round 1 of #116: three findings that each fit a comment were
    # concatenated into one and defang's 60,000 cap dropped the end of the
    # second and all of the third while `failed` stayed empty. Now a chunk is
    # posted before it would pass 56,000 bytes, so every finding arrives
    # whole, in as many comments as it takes, and nothing is recorded as lost.
    comments = [{"path": f"src/{i}.py", "line": 3, "body_file": f"rc{i}.md"} for i in range(3)]
    files = {"review.md": "Findings are inline.\n", **{f"rc{i}.md": big_finding(i) for i in range(3)}}
    r, calls, writes, failed = run_post(tmp_path, review_landing(review_comments=comments), files, pr_number="5", scenario="inline-422")
    assert r.returncode == 0, r.stderr
    assert failed == ""
    bodies = posted_bodies(tmp_path)
    follow_ups = [b for b in bodies if b.startswith("Inline review comments that could not be anchored")]
    assert len(follow_ups) == 3  # ~30 KB each: one per chunk
    assert follow_ups[1].startswith("Inline review comments that could not be anchored to the diff (the line is outside the PR's diff hunks, or the post failed) (continued):")
    joined = "".join(follow_ups)
    for i in range(3):
        assert f"FINDING_{i}_BEGIN" in joined and f"FINDING_{i}_END" in joined, i
    assert all(len(b.encode()) < 60000 and "truncated:" not in b for b in follow_ups)
    # The bodies are ordered: summary, three 422 attempts, three chunks.
    assert sum(c.startswith("api repos/o/r/issues/5/comments ") for c in calls) == 4


def test_post_splits_a_single_oversized_unanchored_finding(tmp_path):
    # Review round 2 of #116: a 999-byte path plus a body just under the
    # Claude prep cap is one entry over the chunk budget; measured whole it
    # was posted whole and defang's cap cut its tail with `failed` empty.
    # Now the entry is split at line boundaries into pieces that each fill
    # a chunk, and every byte arrives.
    path = "/".join(["a" * 199] * 5)
    body = "x" * 58950 + "\nTAIL_FINDING\n"
    r, calls, writes, failed = run_post(tmp_path, review_landing(review_comments=[{"path": path, "line": 3, "body_file": "rc0.md"}]),
                                        {"review.md": "Review\n", "rc0.md": body}, pr_number="5", scenario="inline-422")
    assert r.returncode == 0, r.stderr
    assert failed == ""
    follow_ups = [b for b in posted_bodies(tmp_path) if b.startswith("Inline review comments that could not be anchored")]
    assert len(follow_ups) == 2
    joined = "".join(follow_ups)
    assert "TAIL_FINDING" in joined and "truncated:" not in joined
    assert joined.count("x") == 58950 and f"**{path}** line 3 (RIGHT)" in joined
    assert all(len(b.encode()) < 60000 for b in follow_ups)


def test_post_measures_unanchored_entries_after_the_defang(tmp_path):
    # Review round 2 of #116: paths carrying a trigger token grow when the
    # chunk is de-fanged (`@auto` → `` `auto` ``), so a chunk measured on
    # raw bytes overflowed the cap and the last finding was cut. Entries
    # are de-fanged before they are measured, so all fifty arrive.
    path = "/".join(["@auto" * 39] * 5)
    comments = [{"path": path, "line": 3, "body_file": f"rc{i}.md"} for i in range(50)]
    files = {"review.md": "Review\n", **{f"rc{i}.md": f"FINDING_{i}\n" for i in range(50)}}
    r, calls, writes, failed = run_post(tmp_path, review_landing(review_comments=comments), files, pr_number="5", scenario="inline-422")
    assert r.returncode == 0, r.stderr
    assert failed == ""
    follow_ups = [b for b in posted_bodies(tmp_path) if b.startswith("Inline review comments that could not be anchored")]
    joined = "".join(follow_ups)
    assert sum(f"FINDING_{i}\n" in joined for i in range(50)) == 50
    assert "truncated:" not in joined and "@auto" not in joined and "`auto`" in joined
    assert len(follow_ups) >= 2 and all(len(b.encode()) < 60000 for b in follow_ups)


def fallback_content(follow_ups: list) -> str:
    """The follow-up comments' entries, headers dropped and newlines removed, so a
    token the byte cut split across two comments counts once."""
    return "".join(b.split(":\n", 1)[1] for b in follow_ups).replace("\n", "")


def test_post_folds_a_single_line_longer_than_the_budget(tmp_path):
    # Review round 3 of #116: folding at the budget made a budget + 1 piece
    # (the newline), which re-split and removed its own input — under
    # Actions' `bash -e` the step died before posting any fallback. A line
    # longer than the budget is now folded at budget - 1, every piece fits,
    # and every byte arrives across as many comments as it takes.
    body = "z" * 59900 + "\nTAIL_FINDING\n"
    r, calls, writes, failed = run_post(tmp_path, review_landing(review_comments=[{"path": "src/a.py", "line": 3, "body_file": "rc0.md"}]),
                                        {"review.md": "Review\n", "rc0.md": body}, pr_number="5", scenario="inline-422")
    assert r.returncode == 0, r.stderr
    assert failed == "" and "No such file" not in r.stderr
    follow_ups = [b for b in posted_bodies(tmp_path) if b.startswith("Inline review comments that could not be anchored")]
    assert len(follow_ups) == 2
    joined = "".join(follow_ups)
    assert fallback_content(follow_ups).count("z") == 59900 and "TAIL_FINDING" in joined and "truncated:" not in joined
    assert all(len(b.encode()) < 60000 for b in follow_ups)


def test_post_fallback_carries_everything_the_body_cap_kept(tmp_path):
    # The reviewer's fixture: `@AUTO` × 11,780 is under the prep cap but
    # grows past defang's 60,000 cap in land (case-insensitive: `AUTO` →
    # `` `AUTO` ``), so the body is cut to exactly 10,000 tokens plus the
    # truncation note BEFORE it reaches the fallback. Everything the cap
    # kept — 10,000 tokens and the note, one 60,000-byte line — must reach
    # the PR: two comments, nothing missing, nothing recorded as failed.
    body = "@AUTO" * 11780 + "\nTAIL_FINDING\n"
    r, calls, writes, failed = run_post(tmp_path, review_landing(review_comments=[{"path": "src/a.py", "line": 3, "body_file": "rc0.md"}]),
                                        {"review.md": "Review\n", "rc0.md": body}, pr_number="5", scenario="inline-422")
    assert r.returncode == 0, r.stderr
    assert failed == ""
    follow_ups = [b for b in posted_bodies(tmp_path) if b.startswith("Inline review comments that could not be anchored")]
    assert len(follow_ups) == 2
    joined = "".join(follow_ups)
    assert fallback_content(follow_ups).count("AUTO") == 10000 and "@AUTO" not in joined
    assert "_[truncated: the body exceeded the comment size cap]_" in joined  # the per-body cap's own note, carried whole
    assert all(len(b.encode()) < 60000 for b in follow_ups)


@pytest.mark.parametrize("char,count,lead", [("é", 29450, ""), ("中", 19633, "x"), ("😀", 14725, ""), ("😀", 14725, "xy")])
def test_post_cuts_a_long_line_at_utf8_character_boundaries(tmp_path, char, count, lead):
    # Review round 4 of #116: a cut at an arbitrary byte split a multi-byte
    # character, and gh serialised the stray bytes as U+FFFD instead of
    # refusing them — silent corruption. The cut now backs off to a
    # character boundary: every posted chunk is valid UTF-8 and every
    # character arrives, for two-, three- and four-byte characters, at
    # offsets that put the cut point at each position inside a character.
    body = lead + char * count + "\nTAIL_FINDING\n"
    assert len(body.encode()) < 59000  # under the prep cap: nothing is truncated before the fallback
    path = "/".join(["a" * 199] * 5)
    r, calls, writes, failed = run_post(tmp_path, review_landing(review_comments=[{"path": path, "line": 3, "body_file": "rc0.md"}]),
                                        {"review.md": "Review\n", "rc0.md": body}, pr_number="5", scenario="inline-422")
    assert r.returncode == 0, r.stderr
    assert failed == ""
    # Every posted chunk file is valid UTF-8 (posted_bodies decodes strictly too).
    for p in sorted(tmp_path.glob("unanchored-*-post.md")):
        p.read_bytes().decode("utf-8")
    follow_ups = [b for b in posted_bodies(tmp_path) if b.startswith("Inline review comments that could not be anchored")]
    assert len(follow_ups) == 2
    content = fallback_content(follow_ups)
    assert content.count(char) == count and "\ufffd" not in content and "TAIL_FINDING" in content
    assert all(len(b.encode()) < 60000 for b in follow_ups)


def test_post_cuts_at_a_character_boundary_in_a_partly_filled_chunk(tmp_path):
    # The open chunk already holds a finding, so the room left — and the
    # cut point inside the long line — is offset by that entry's length. The
    # body is just under land's own 60,000 cap (the validator admits 64 KiB;
    # only the Claude prep step caps at 59,000), so the entry is over the
    # chunk budget on its own and is split, its heading completing the open
    # chunk.
    body = "é" * 29990 + "\nTAIL_FINDING\n"
    comments = [{"path": "src/first.py", "line": 1, "body_file": "rc0.md"}, {"path": "src/second.py", "line": 3, "body_file": "rc1.md"}]
    r, calls, writes, failed = run_post(tmp_path, review_landing(review_comments=comments),
                                        {"review.md": "Review\n", "rc0.md": "short finding\n", "rc1.md": body}, pr_number="5", scenario="inline-422")
    assert r.returncode == 0, r.stderr
    assert failed == ""
    for p in sorted(tmp_path.glob("unanchored-*-post.md")):
        p.read_bytes().decode("utf-8")
    follow_ups = [b for b in posted_bodies(tmp_path) if b.startswith("Inline review comments that could not be anchored")]
    assert len(follow_ups) == 2 and "short finding" in follow_ups[0] and "src/second.py" in follow_ups[0]
    content = fallback_content(follow_ups)
    assert content.count("é") == 29990 and "\ufffd" not in content and "TAIL_FINDING" in content


def test_post_packs_small_unanchored_findings_into_one_comment(tmp_path):
    # Two findings under the chunk bound share one comment (the round-1
    # shape); a third that would pass the bound opens a second.
    comments = [{"path": f"src/{i}.py", "line": 3, "body_file": f"rc{i}.md"} for i in range(3)]
    files = {"review.md": "s\n", "rc0.md": "a" * 20000, "rc1.md": "b" * 20000, "rc2.md": "c" * 20000}
    r, calls, writes, failed = run_post(tmp_path, review_landing(review_comments=comments), files, pr_number="5", scenario="inline-422")
    assert failed == ""
    follow_ups = [b for b in posted_bodies(tmp_path) if b.startswith("Inline review comments that could not be anchored")]
    assert len(follow_ups) == 2
    assert "src/0.py" in follow_ups[0] and "src/1.py" in follow_ups[0] and "src/2.py" in follow_ups[1]


def test_post_records_every_lost_follow_up_chunk(tmp_path):
    comments = [{"path": f"src/{i}.py", "line": 3, "body_file": f"rc{i}.md"} for i in range(2)]
    files = {"review.md": "s\n", "rc0.md": big_finding(0), "rc1.md": big_finding(1)}
    r, calls, writes, failed = run_post(tmp_path, review_landing(review_comments=comments), files, pr_number="5",
                                        scenario="inline-422-comment-fails")
    assert "follow-up comment (part 1) with unanchored inline review comment(s) on #5 failed after 5 attempts" in failed
    assert "follow-up comment (part 2) with unanchored inline review comment(s) on #5 failed after 5 attempts" in failed


# post_review_comment_file on its own: retry policy per status.
RC_STUB = r"""
sleep() { :; }
gh() {
  echo "$*" >>"$STATE/calls"
  case "$SCENARIO" in
    422) echo '{"message": "Validation Failed"}'; echo "gh: Validation Failed (HTTP 422)" >&2; return 1 ;;
    500) echo "gh: Server Error (HTTP 500)" >&2; return 1 ;;
    flaky) if [ ! -f "$STATE/flaked" ]; then touch "$STATE/flaked"; echo "gh: Server Error (HTTP 500)" >&2; return 1; fi ;;
  esac
  return 0
}
"""


@pytest.mark.parametrize("scenario,rc,attempts", [("", 0, 1), ("422", 1, 1), ("flaky", 0, 2), ("500", 1, 3)])
def test_post_review_comment_file_retry_policy(tmp_path, scenario, rc, attempts):
    state = tmp_path / "state"
    state.mkdir()
    body = tmp_path / "b.md"
    body.write_text("finding\n")
    r = sh("bash", "-c", RC_STUB + f". '{LIB}'\npost_review_comment_file o/r 5 {'c' * 40} 'src/a b.py' 7 LEFT '{body}'; echo rc=$?",
           check=False, env={"STATE": str(state), "SCENARIO": scenario})
    assert r.stdout.strip().endswith(f"rc={rc}"), r.stdout + r.stderr
    calls = (state / "calls").read_text().splitlines()
    assert len(calls) == attempts
    assert calls[0] == f"api repos/o/r/pulls/5/comments -F body=@{body} -f commit_id={'c' * 40} -f path=src/a b.py -F line=7 -f side=LEFT --silent"


def test_land_outputs_what_the_reviewer_landed():
    # claude-review.yml's landed-review check reads these instead of counting
    # the agent's comments (it posts none): the verdict comment is the review
    # in pr mode, the proxy-issue comment in external mode.
    text = LAND.read_text()
    assert "posted_comments:\n" in text and "value: ${{ steps.post.outputs.posted_comments }}" in text
    assert "value: ${{ steps.verdict.outcome == 'success' && steps.plan.outputs.verdict != '' && '1' || '' }}" in text



@pytest.mark.parametrize(
    "failed,pushed,expected",
    [
        ("validate", "",
         "The landing was refused before any write: the agent's commits were **not** pushed and nothing was posted."),
        ("download", "",
         "The landing was refused before any write: the agent's commits were **not** pushed and nothing was posted."),
        ("push", "", "The agent's commits were **not** pushed."),
        ("fetch", "", "The agent's commits were **not** pushed."),
        # `workflows` failed with no file list: the listing itself failed.
        ("workflows", "",
         "The landing could not check whether the agent's commits change workflow files (listing the bundle's paths failed, see the run log), "
         "so the bundle was refused unchecked. The commits were **not** pushed and are lost with the runner: there is no branch to look for."),
        ("post (comment on #79 failed after 5 attempts; issue create in o/r failed)", "1",
         "The agent's commits were pushed; only what follows the push is affected."),
        ("handback", "1",
         "The agent's commits were pushed; only what follows the push is affected. Post the re-review request by hand."),
        ("pr, stage", "1",
         "The agent's commits were pushed; only what follows the push is affected. Move the Atlas stage by hand."),
        ("handoff", "", "Post the hand-off by hand."),
        # The reviewer's land job: nothing is ever pushed, so a failed
        # verdict post owes exactly the verdict.
        ("verdict", "", "Post the review verdict by hand."),
        ("pr", "", ""),
    ],
)
def test_landing_failure_hint(failed, pushed, expected):
    r = bash_lib(f"landing_failure_hint '{failed}' '{pushed}'")
    assert r.returncode == 0, r.stderr
    assert r.stdout == expected
    # The note is posted un-de-fanged: it must never carry a live trigger.
    assert "@" not in r.stdout


PUSHED_PREFIX = "The agent's commits were pushed; only what follows the push is affected."


@pytest.mark.parametrize(
    "failed,pushed,withheld,expected",
    [
        # The PR step failed after the push, so the planned stage step was
        # skipped (not failed): the move is owed all the same.
        ("pr", "1", "stage", f"{PUSHED_PREFIX} Move the Atlas stage by hand."),
        # ... and so are a planned hand-back and hand-off; each is named.
        ("pr", "1", "handback, stage",
         f"{PUSHED_PREFIX} Post the re-review request by hand. Move the Atlas stage by hand."),
        ("pr", "1", "handoff", f"{PUSHED_PREFIX} Post the hand-off by hand."),
        ("pr", "1", "handback, handoff, stage",
         f"{PUSHED_PREFIX} Post the re-review request by hand. Post the hand-off by hand. Move the Atlas stage by hand."),
        # A failed hand-back and a withheld stage coincide: both are named.
        ("handback", "1", "stage",
         f"{PUSHED_PREFIX} Post the re-review request by hand. Move the Atlas stage by hand."),
        # Failed AND withheld never both apply to one step, but the line must
        # not double if they did.
        ("stage", "1", "stage", f"{PUSHED_PREFIX} Move the Atlas stage by hand."),
        # Nothing withheld (the common case): unchanged.
        ("pr", "1", "", PUSHED_PREFIX),
        # The reviewer's codex path: the Post step lost the review body, so
        # the verdict was withheld (a verdict over a missing review would
        # spend an @auto round on nothing) — owed, and named.
        ("post (comment on #456 failed after 5 attempts)", "", "verdict", "Post the review verdict by hand."),
    ],
)
def test_landing_failure_hint_names_withheld_steps(failed, pushed, withheld, expected):
    r = bash_lib(f"landing_failure_hint '{failed}' '{pushed}' '{withheld}'")
    assert r.returncode == 0, r.stderr
    assert r.stdout == expected
    assert "@" not in r.stdout


WORKFLOWS_HINT = (
    "The agent's commits change workflow files ({files}), which the machine account may not push (it has no Workflows permission); "
    "changes under `.github/workflows/` are made from a maintainer's machine. "
    "The commits were **not** pushed and are lost with the runner: there is no branch to look for."
)


@pytest.mark.parametrize(
    "files,rendered",
    [
        ("`.github/workflows/x.yml`", "`.github/workflows/x.yml`"),
        ("`.github/workflows/a.yml`, `.github/workflows/b.yml` and 3 more",
         "`.github/workflows/a.yml`, `.github/workflows/b.yml` and 3 more"),
        # The paths are the agent's: a live trigger in one is broken like any
        # agent text, since the report posts from a write-access account.
        ("`.github/workflows/@review.yml`", "`.github/workflows/`review`.yml`"),
    ],
)
def test_landing_failure_hint_names_the_refused_workflow_files(files, rendered):
    r = bash_lib(f"landing_failure_hint 'workflows' '' '' '{files}'")
    assert r.returncode == 0, r.stderr
    assert r.stdout == WORKFLOWS_HINT.format(files=rendered)
    assert "@" not in r.stdout


def test_verdict_step_is_withheld_when_a_comment_was_lost():
    # The verdict (claude-review.yml's codex path) is the only marker-bearing
    # body besides the hand-back, and it must never post over a review body
    # the Post step failed to land: gated on `failed` being empty, and posted
    # before the hand-back so a workflow that used both would order them.
    text = LAND.read_text()
    block = text[text.index("    - id: verdict\n"):]
    cond = block.splitlines()[1].strip()
    assert cond == "if: steps.plan.outputs.verdict != '' && steps.post.outputs.failed == ''"
    assert text.index("    - id: post\n") < text.index("    - id: verdict\n") < text.index("    - id: handback\n")
    # Both fixed bodies are the exact lines the review prompt and the @auto
    # loop's gates key on.
    assert "'🔎 Review complete — no outstanding suggestions. <!-- claude-review-summary --><!-- claude-review-verdict:clean -->'" in block
    assert "'🔎 Review complete. <!-- claude-review-summary --><!-- claude-review-verdict:suggestions -->'" in block


def test_verdict_bodies_would_not_survive_the_defang(tmp_path):
    # Why review_verdict exists: a verdict sent through comments[] would have
    # its markers split by defang, and the loop would never see it.
    src = tmp_path / "v.md"
    src.write_text("🔎 Review complete. <!-- claude-review-summary --><!-- claude-review-verdict:suggestions -->\n")
    dst = tmp_path / "out.md"
    r = bash_lib(f"defang '{src}' '{dst}'")
    assert r.returncode == 0, r.stderr
    assert "claude-review-summary" not in dst.read_text()


def test_pr_and_handback_steps_run_only_after_the_validator_passed():
    # `refuse-pr` (and every other refusal) is enforced by the validate step:
    # a violation fails it, and the steps that would open/adopt/label a PR or
    # post the `@review` hand-back carry no `always()`, so they are skipped
    # like everything after a failed step. The flag reaches the validator
    # from the input, quoted through env like `refuse-bundle`.
    text = LAND.read_text()
    assert text.index("    - id: validate\n") < text.index("    - id: plan\n") < text.index("    - id: pr\n") < text.index("    - id: handback\n")
    validate = step_block(text, "validate", indent=4)
    assert "REFUSE_PR: ${{ inputs.refuse-pr == 'true' && '1' || '' }}" in validate
    assert "${REFUSE_PR:+--refuse-pr}" in validate
    pr = step_block(text, "pr", indent=4)
    assert pr.splitlines()[1].strip() == "shell: bash"          # no `if:` at all: runs on success only
    assert "always()" not in pr
    handback = step_block(text, "handback", indent=4)
    assert handback.splitlines()[1].strip() == "if: steps.plan.outputs.handback == 'true'"
    assert "always()" not in handback


def test_workflows_step_gates_the_push_and_reaches_the_report():
    # The refusal runs after the bundle is verified and before the push, in
    # the same empty bare repo; the push has no always(), so a refusal skips
    # it (and the PR, hand-back and stage after it) exactly as a failed
    # fetch does. Report lists the step's outcome and hands its file list to
    # the hint, which is where the plain sentence comes from.
    text = LAND.read_text()
    assert text.index("    - id: fetch\n") < text.index("    - id: workflows\n") < text.index("    - id: push\n")
    block = step_block(text, "workflows", indent=4)
    assert block.splitlines()[1].strip() == "if: steps.plan.outputs.has_bundle == 'true' && steps.fetch.outputs.already == ''"
    assert "GIT_TOKEN:" not in block  # local diff only: no token in its env
    # The base tip it compares against comes from the fetch step (read
    # token), for the branch trusted context names: the PR's base from the
    # lookup step (the pinned pr-number), else the default branch — never
    # the manifest's pr.base.
    assert "BASE_SHA: ${{ steps.fetch.outputs.base_sha }}" in block
    fetch = step_block(text, "fetch", indent=4)
    assert "BASE: ${{ steps.lookup.outputs.base || inputs.default-branch || steps.lookup.outputs.default_branch }}" in fetch
    assert '"refs/heads/$BASE:refs/land/base"' in fetch and 'echo "base_sha=$base_sha"' in fetch
    lookup = step_block(text, "lookup", indent=4)
    assert lookup.splitlines()[1].strip() == "if: inputs.pr-number != '' || inputs.default-branch == ''"
    assert "--json headRefName,baseRefName" in lookup
    push = step_block(text, "push", indent=4)
    assert push.splitlines()[1].strip() == "if: steps.plan.outputs.has_bundle == 'true'"
    assert "always()" not in push
    report = step_block(text, "report", indent=4)
    assert "workflows=${{ steps.workflows.outcome }}" in report
    assert "WORKFLOW_FILES: ${{ steps.workflows.outputs.files }}" in report
    assert 'landing_failure_hint "$failed" "$PUSHED" "$withheld" "$WORKFLOW_FILES"' in report


def test_stage_and_handoff_steps_run_after_a_failed_hand_back():
    # Neither the stage move nor the hand-off may be withheld by a failed
    # hand-back (that strands the board at Agent with the PR head moved);
    # both are gated on the PR step instead, which succeeding implies the
    # push did. Report's "never ran" list relies on the same gate: a planned
    # step is owed-but-skipped exactly when the PR step failed.
    text = LAND.read_text()
    for step, planned in (("stage", "steps.plan.outputs.stage != ''"), ("handoff", "steps.plan.outputs.handoff == 'true'")):
        block = text[text.index(f"    - id: {step}\n"):]
        cond = block.splitlines()[1].strip()
        assert cond == f"if: always() && steps.pr.outcome == 'success' && {planned}", step


# --- the git contract ------------------------------------------------------


@pytest.fixture
def repos(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    landing = tmp_path / "landing"
    landing.mkdir()
    git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    git("init", "-q", str(work), cwd=tmp_path)
    git("config", "user.email", "a@b", cwd=work)
    git("config", "user.name", "a", cwd=work)
    (work / "f").write_text("1\n")
    git("add", "f", cwd=work)
    git("commit", "-qm", "base", cwd=work)
    git("branch", "-M", "feature", cwd=work)
    git("push", "-q", str(origin), "feature", "feature:main", cwd=work)
    start = git("rev-parse", "HEAD", cwd=work).stdout.strip()
    (work / "f").write_text("2\n")
    git("commit", "-qam", "agent change", cwd=work)
    (work / "f").write_text("3\n")
    git("commit", "-qam", "agent change 2", cwd=work)
    head = git("rev-parse", "HEAD", cwd=work).stdout.strip()
    return {"origin": origin, "work": work, "landing": landing, "start": start, "head": head, "tmp": tmp_path}


def emit(r):
    """What emit-landing does with the agent's repo."""
    work, landing = r["work"], r["landing"]
    git("merge-base", "--is-ancestor", r["start"], r["head"], cwd=work)
    # `<start>..HEAD`, not `<start>..<sha>`: bundle create records REFS, and a
    # bare SHA on the positive side is refused (nothing to name).
    git("bundle", "create", "--quiet", str(landing / "commits.bundle"), f"{r['start']}..HEAD", cwd=work)
    git("bundle", "verify", "--quiet", str(landing / "commits.bundle"), cwd=work)


def land_fetch(r):
    """What the land composite's fetch step does, in an empty bare repo."""
    repo = r["tmp"] / "landing-repo"
    git("init", "-q", "--bare", str(repo), cwd=r["tmp"])
    git("fetch", "--quiet", "--no-tags", str(r["origin"]), r["start"], cwd=repo)
    git("bundle", "verify", "--quiet", str(r["landing"] / "commits.bundle"), cwd=repo)
    git("fetch", "--quiet", "--no-tags", str(r["landing"] / "commits.bundle"), "HEAD", cwd=repo)
    assert git("rev-parse", "FETCH_HEAD", cwd=repo).stdout.strip() == r["head"]
    git("merge-base", "--is-ancestor", r["start"], r["head"], cwd=repo)
    # The base branch's tip, for the workflows step (refs/land/base).
    git("fetch", "--quiet", "--no-tags", str(r["origin"]), "refs/heads/main:refs/land/base", cwd=repo)
    return repo


def base_sha(repo) -> str:
    return git("rev-parse", "refs/land/base", cwd=repo).stdout.strip()


def remote_tip(r):
    out = git("ls-remote", "--quiet", "--heads", str(r["origin"]), "refs/heads/feature", cwd=r["tmp"]).stdout
    return out.split("\t")[0] if out else ""


def tip_is_ancestor(repo, tip, head) -> bool:
    if git("cat-file", "-e", f"{tip}^{{commit}}", cwd=repo, check=False).returncode != 0:
        return False
    return git("merge-base", "--is-ancestor", tip, head, cwd=repo, check=False).returncode == 0


def test_bundle_lands_exactly_head_sha(repos):
    r = repos
    emit(r)
    repo = land_fetch(r)
    tip = remote_tip(r)
    assert tip == r["start"]
    assert tip_is_ancestor(repo, tip, r["head"])
    git("push", "--quiet", str(r["origin"]), f"{r['head']}:refs/heads/feature", cwd=repo)
    assert remote_tip(r) == r["head"]


def test_new_branch_is_created_by_the_push(repos):
    r = repos
    emit(r)
    repo = land_fetch(r)
    out = git("ls-remote", "--heads", str(r["origin"]), "refs/heads/new-branch", cwd=repo).stdout
    assert out == ""
    git("push", "--quiet", str(r["origin"]), f"{r['head']}:refs/heads/new-branch", cwd=repo)
    assert git("ls-remote", "--heads", str(r["origin"]), "refs/heads/new-branch", cwd=repo).stdout.startswith(r["head"])


def test_moved_branch_is_refused(repos):
    r = repos
    emit(r)
    repo = land_fetch(r)
    # Someone else pushed to the branch during the run.
    work = r["work"]
    git("checkout", "-q", "-b", "other", r["start"], cwd=work)
    (work / "g").write_text("x\n")
    git("add", "g", cwd=work)
    git("commit", "-qm", "human push", cwd=work)
    git("push", "-q", str(r["origin"]), "other:feature", cwd=work)
    tip = remote_tip(r)
    assert tip != r["start"]
    assert not tip_is_ancestor(repo, tip, r["head"])
    # And a plain (non-force) push is rejected non-fast-forward as the backstop.
    assert git("push", "--quiet", str(r["origin"]), f"{r['head']}:refs/heads/feature", cwd=repo, check=False).returncode != 0


def test_partially_pushed_bundle_is_still_landable(repos):
    r = repos
    emit(r)
    repo = land_fetch(r)
    # The agent already pushed its first commit itself: the tip is a bundle
    # commit, an ancestor of head — fine.
    first = git("rev-parse", f"{r['head']}~1", cwd=r["work"]).stdout.strip()
    git("push", "-q", str(r["origin"]), f"{first}:refs/heads/feature", cwd=r["work"])
    tip = remote_tip(r)
    assert tip == first
    assert tip_is_ancestor(repo, tip, r["head"])


def commit_path(r, path, text="x\n"):
    """One more agent commit on the work repo, touching PATH; head moves."""
    f = r["work"] / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    git("add", str(f), cwd=r["work"])
    git("commit", "-qm", f"touch {path}", cwd=r["work"])
    r["head"] = git("rev-parse", "HEAD", cwd=r["work"]).stdout.strip()


def run_workflows_step(r, repo, stub="", base=None):
    """The land composite's `workflows` step, lifted and run in the bare repo
    `land_fetch` filled, as GitHub runs it (`bash -eo pipefail`); STUB is
    shell prepended to it (a failing `git`, say). BASE is the fetch step's
    base_sha output: the fetched base tip unless a test says otherwise."""
    out = r["tmp"] / "workflows-out.txt"
    out.write_text("")
    env = {"WORK": str(repo), "START_SHA": r["start"], "HEAD_SHA": r["head"], "GITHUB_OUTPUT": str(out),
           "RUNNER_TEMP": str(r["tmp"]), "BASE_SHA": base_sha(repo) if base is None else base}
    res = sh("bash", "-eo", "pipefail", "-c", stub + step_script("workflows"), check=False, env=env)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    return res, outputs


@pytest.mark.parametrize("path", [".github/workflows/x.yml", ".github/workflows/nested/x.yml"])
def test_bundle_touching_a_workflow_file_is_refused_before_the_push(repos, path):
    r = repos
    commit_path(r, path)
    emit(r)
    repo = land_fetch(r)
    res, outputs = run_workflows_step(r, repo)
    assert res.returncode != 0
    assert outputs["files"] == f"`{path}`"
    assert (f"::error::land: the agent's commits change workflow files (`{path}`), which the machine account "
            "may not push (it has no Workflows permission); refusing the bundle — changes under .github/workflows/ "
            "are made from a maintainer's machine.") in res.stdout
    # Nothing was pushed: the branch is where the agent started.
    assert remote_tip(r) == r["start"]
    # And the report the requester reads names the file and the loss.
    hint = bash_lib(f"landing_failure_hint 'workflows' '' '' '{outputs['files']}'")
    assert hint.returncode == 0, hint.stderr
    assert hint.stdout == WORKFLOWS_HINT.format(files=f"`{path}`")


def test_bundle_deleting_a_workflow_file_is_refused(repos):
    # A deletion (or a move out of the directory, which --no-renames lists
    # as one) is refused like an edit: GitHub refuses that push too.
    r = repos
    commit_path(r, ".github/workflows/old.yml")
    # Re-base the run on a start that already has the file — on the base
    # branch too, so the deletion is the agent's — then delete it.
    r["start"] = r["head"]
    git("push", "-q", str(r["origin"]), "HEAD:feature", "HEAD:main", cwd=r["work"])
    git("mv", ".github/workflows/old.yml", "elsewhere.yml", cwd=r["work"])
    git("commit", "-qm", "move the workflow out", cwd=r["work"])
    r["head"] = git("rev-parse", "HEAD", cwd=r["work"]).stdout.strip()
    emit(r)
    repo = land_fetch(r)
    res, outputs = run_workflows_step(r, repo)
    assert res.returncode != 0
    assert outputs["files"] == "`.github/workflows/old.yml`"


def test_failed_path_listing_refuses_the_bundle_unchecked(repos):
    # A diff that fails (a corrupt object, a resource limit) must not read as
    # "no workflow files": the step captures the listing and fails on a
    # non-zero status, sets no `files` (so Report says the check did not
    # run, not that the agent changed workflow files), and the push is
    # never reached.
    r = repos
    emit(r)
    repo = land_fetch(r)
    res, outputs = run_workflows_step(r, repo, stub='git() { echo "fatal: simulated diff read failure" >&2; return 128; }\n')
    assert res.returncode != 0
    assert "files" not in outputs
    assert "::error::land: could not list the paths the bundle changes (git diff failed, see above); refusing the bundle unchecked." in res.stdout
    assert "no workflow files" not in res.stdout
    assert remote_tip(r) == r["start"]
    hint = bash_lib("landing_failure_hint 'workflows' '' '' ''")
    assert hint.stdout.startswith("The landing could not check whether the agent's commits change workflow files")


def advance_base(r, path=".github/workflows/ci.yml", text="on: push\n"):
    """A maintainer changes a workflow file on the base branch (origin/main)
    after the agent's start; returns the new base tip."""
    work = r["work"]
    git("fetch", "-q", str(r["origin"]), "main", cwd=work)
    git("checkout", "-q", "-b", f"base-{path.replace('/', '-')}-{text.count(chr(10))}", "FETCH_HEAD", cwd=work)
    f = work / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    git("add", str(f), cwd=work)
    git("commit", "-qm", f"maintainer: {path}", cwd=work)
    tip = git("rev-parse", "HEAD", cwd=work).stdout.strip()
    git("push", "-q", str(r["origin"]), "HEAD:main", cwd=work)
    git("checkout", "-q", "feature", cwd=work)
    return tip


def merge_base_into_feature(r):
    """What sync-branch does before the agent starts: `git merge origin/main`
    on the branch, recorded after start_sha, so the merge is in the bundle."""
    git("fetch", "-q", str(r["origin"]), "main", cwd=r["work"])
    git("merge", "-q", "--no-edit", "FETCH_HEAD", cwd=r["work"])
    r["head"] = git("rev-parse", "HEAD", cwd=r["work"]).stdout.strip()


@pytest.mark.parametrize("how", ["merge", "fast-forward"])
def test_workflow_change_brought_in_by_the_base_merge_lands(repos, how):
    # The runner merged origin/main — where a maintainer changed a workflow
    # file — into the branch; the agent touched no workflow file. The
    # change is on origin already and not the agent's: the bundle lands.
    r = repos
    advance_base(r)
    if how == "merge":
        merge_base_into_feature(r)
    else:
        # The branch had nothing of its own yet, so the sync fast-forwarded
        # and the agent worked on top: base commits in the bundle, no merge.
        git("fetch", "-q", str(r["origin"]), "main", cwd=r["work"])
        git("reset", "-q", "--hard", "FETCH_HEAD", cwd=r["work"])
        commit_path(r, "src/agent.py")
    emit(r)
    repo = land_fetch(r)
    res, outputs = run_workflows_step(r, repo)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "files" not in outputs
    assert f"1 workflow file(s) in {r['start']}..{r['head']} changed only by the base merge (matching origin's base at {base_sha(repo)}); not the agent's." in res.stdout
    git("push", "--quiet", str(r["origin"]), f"{r['head']}:refs/heads/feature", cwd=repo)
    assert remote_tip(r) == r["head"]


def test_agent_workflow_change_on_top_of_a_base_merge_is_refused_and_named_alone(repos):
    r = repos
    advance_base(r)
    merge_base_into_feature(r)
    commit_path(r, ".github/workflows/agent.yml")
    emit(r)
    repo = land_fetch(r)
    res, outputs = run_workflows_step(r, repo)
    assert res.returncode != 0
    # Only the agent's file: the base merge's ci.yml is not named.
    assert outputs["files"] == "`.github/workflows/agent.yml`"


def test_agent_edit_of_a_base_merged_workflow_file_is_refused(repos):
    # The blob at head differs from both start (absent) and the base: the
    # agent's edit, however small.
    r = repos
    advance_base(r)
    merge_base_into_feature(r)
    commit_path(r, ".github/workflows/ci.yml", "on: push\n# tweaked by the agent\n")
    emit(r)
    repo = land_fetch(r)
    res, outputs = run_workflows_step(r, repo)
    assert res.returncode != 0
    assert outputs["files"] == "`.github/workflows/ci.yml`"


def test_base_that_moved_after_the_merge_still_exempts_the_merged_version(repos):
    # The merge brought ci.yml at version 1; a maintainer then changed it
    # again on main before the landing. Head's blob is not the base tip's,
    # but it is the merged base's — the merge base of head and the tip — so
    # it is the base's, not the agent's.
    r = repos
    advance_base(r)
    merge_base_into_feature(r)
    advance_base(r, text="on: push\n# v2\n")
    emit(r)
    repo = land_fetch(r)
    res, outputs = run_workflows_step(r, repo)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "files" not in outputs
    git("push", "--quiet", str(r["origin"]), f"{r['head']}:refs/heads/feature", cwd=repo)


def test_fast_forwarded_base_that_moved_before_landing_still_lands(repos):
    # Codex round 3 (a): the sync fast-forwarded the branch onto base v1
    # (no merge commit), the agent changed source only, and main moved to
    # v2 before the landing. Head's ci.yml is v1: the merge base of head
    # and the tip is the v1 commit, so the file is the base's, not the
    # agent's.
    r = repos
    advance_base(r)
    git("fetch", "-q", str(r["origin"]), "main", cwd=r["work"])
    git("reset", "-q", "--hard", "FETCH_HEAD", cwd=r["work"])
    commit_path(r, "src/agent.py")
    advance_base(r, text="on: push\n# v2\n")
    emit(r)
    repo = land_fetch(r)
    res, outputs = run_workflows_step(r, repo)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "files" not in outputs
    git("push", "--quiet", str(r["origin"]), f"{r['head']}:refs/heads/feature", cwd=repo)


def test_reverting_a_workflow_to_an_older_base_version_is_refused(repos):
    # Codex round 3 (b): the branch already sits at the base tip (no runner
    # merge); the agent reverts ci.yml to an older base version and names
    # that older base commit as a second parent of its commit. The merge
    # base of head and the tip is the tip itself (an ancestor of head), so
    # the old version is nobody's base and the change is the agent's.
    r = repos
    work = r["work"]
    commit_path(r, ".github/workflows/ci.yml", "on: push\n# old\n")
    old = r["head"]
    git("push", "-q", str(r["origin"]), "HEAD:main", cwd=work)
    commit_path(r, ".github/workflows/ci.yml", "on: push\n# current\n")
    r["start"] = r["head"]
    git("push", "-q", str(r["origin"]), "HEAD:main", "HEAD:feature", cwd=work)
    commit_path(r, ".github/workflows/ci.yml", "on: push\n# old\n")
    tree = git("rev-parse", "HEAD^{tree}", cwd=work).stdout.strip()
    forged = git("commit-tree", tree, "-p", r["head"], "-p", old, "-m", "crafted merge of an ancestor", cwd=work).stdout.strip()
    git("reset", "-q", "--hard", forged, cwd=work)
    r["head"] = forged
    emit(r)
    repo = land_fetch(r)
    assert base_sha(repo) == r["start"]
    res, outputs = run_workflows_step(r, repo)
    assert res.returncode != 0
    assert outputs["files"] == "`.github/workflows/ci.yml`"


@pytest.mark.parametrize("sub", ["ls-tree", "merge-base"])
def test_failed_comparison_read_refuses_the_bundle_unchecked(repos, sub):
    # Codex round 3: after the diff succeeded, a failed object read during
    # the comparison must not read as "absent at both" (round 3's blob()
    # swallowed every error and exempted an agent-added file that way).
    r = repos
    commit_path(r, ".github/workflows/agent.yml")
    emit(r)
    repo = land_fetch(r)
    stub = f'git() {{ if [ "$1" = {sub} ]; then echo "fatal: simulated object read failure" >&2; return 128; fi; command git "$@"; }}\n'
    res, outputs = run_workflows_step(r, repo, stub=stub)
    assert res.returncode != 0
    assert "files" not in outputs
    assert "refusing the bundle unchecked." in res.stdout
    assert "changed only by the base merge" not in res.stdout
    assert remote_tip(r) == r["start"]


def test_crafted_merge_parent_does_not_exempt_a_workflow_file(repos):
    # The agent commits a workflow file on a side branch of its own and
    # merges it in: the file's blob matches the merge's second parent, but
    # that parent is not on origin's base — the agent made it, and it is no
    # merge base of head and the tip — so the bundle is refused.
    r = repos
    work = r["work"]
    git("checkout", "-q", "-b", "side", r["start"], cwd=work)
    commit_path(r, ".github/workflows/evil.yml", "on: pull_request_target\n")
    git("checkout", "-q", "feature", cwd=work)
    git("merge", "-q", "--no-edit", "side", cwd=work)
    r["head"] = git("rev-parse", "HEAD", cwd=work).stdout.strip()
    emit(r)
    repo = land_fetch(r)
    res, outputs = run_workflows_step(r, repo)
    assert res.returncode != 0
    assert outputs["files"] == "`.github/workflows/evil.yml`"


def test_without_a_base_tip_nothing_is_exempt(repos):
    # The fetch step could not fetch the base (a warning there): the check
    # falls back to refusing every workflow change, the base merge's too.
    r = repos
    advance_base(r)
    merge_base_into_feature(r)
    emit(r)
    repo = land_fetch(r)
    res, outputs = run_workflows_step(r, repo, base="")
    assert res.returncode != 0
    assert outputs["files"] == "`.github/workflows/ci.yml`"


def test_workflow_file_list_is_capped(repos):
    r = repos
    for i in range(23):
        commit_path(r, f".github/workflows/w{i:02d}.yml")
    emit(r)
    repo = land_fetch(r)
    res, outputs = run_workflows_step(r, repo)
    assert res.returncode != 0
    names = ", ".join(f"`.github/workflows/w{i:02d}.yml`" for i in range(20))
    assert outputs["files"] == f"{names} and 3 more"


@pytest.mark.parametrize("path", [".github/other.yml", ".github/workflows-notes/x.yml", "src/.github/workflows/x.yml", "workflows/x.yml"])
def test_bundle_without_workflow_files_passes_and_the_push_proceeds(repos, path):
    r = repos
    commit_path(r, path)
    emit(r)
    repo = land_fetch(r)
    res, outputs = run_workflows_step(r, repo)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "files" not in outputs
    assert f"land: no workflow files in {r['start']}..{r['head']}." in res.stdout
    # The push step follows as before.
    git("push", "--quiet", str(r["origin"]), f"{r['head']}:refs/heads/feature", cwd=repo)
    assert remote_tip(r) == r["head"]


def test_tampered_bundle_tip_is_detected(repos):
    r = repos
    emit(r)
    # The manifest claims a head_sha the bundle does not deliver.
    fake = dict(r, head="0" * 40)
    repo = r["tmp"] / "landing-repo"
    git("init", "-q", "--bare", str(repo), cwd=r["tmp"])
    git("fetch", "--quiet", "--no-tags", str(r["origin"]), r["start"], cwd=repo)
    git("fetch", "--quiet", "--no-tags", str(r["landing"] / "commits.bundle"), "HEAD", cwd=repo)
    assert git("rev-parse", "FETCH_HEAD", cwd=repo).stdout.strip() != fake["head"]


def test_bundle_from_rewritten_history_is_refused_at_emit(repos):
    r = repos
    work = r["work"]
    # The agent reset and rewrote: HEAD no longer descends from start.
    git("checkout", "-q", "--orphan", "rewrite", cwd=work)
    git("commit", "-qam", "rewritten", cwd=work)
    head = git("rev-parse", "HEAD", cwd=work).stdout.strip()
    assert git("merge-base", "--is-ancestor", r["start"], head, cwd=work, check=False).returncode != 0


def test_start_sha_unknown_to_origin_is_refused(repos):
    r = repos
    emit(r)
    repo = r["tmp"] / "landing-repo"
    git("init", "-q", "--bare", str(repo), cwd=r["tmp"])
    assert git("fetch", "--quiet", "--no-tags", str(r["origin"]), "1" * 40, cwd=repo, check=False).returncode != 0


# --- the composites reference no secret ------------------------------------


def test_emit_landing_references_no_secret():
    text = EMIT.read_text()
    assert "secrets." not in text
    assert "GIT_TOKEN" not in text  # not even a token input: nothing to leak


def emit_landing_script() -> str:
    """The `write` step's bash, lifted from the action so it runs here.

    Text extraction rather than a YAML parser (PyYAML is not a test
    dependency): the block scalar under `run: |` is every following line
    indented by at least its 8 spaces, up to the first that is not.
    """
    lines = EMIT.read_text().splitlines()
    start = lines.index("    - id: write")
    run_at = next(i for i in range(start, len(lines)) if lines[i] == "      run: |")
    body = []
    for line in lines[run_at + 1:]:
        if line.strip() == "":
            body.append("")
        elif line.startswith("        "):
            body.append(line[8:])
        else:
            break
    return "\n".join(body) + "\n"


def run_emit_landing(tmp_path, *, cwd, read_only, start_sha, extra=None,
                     branch="claude/issue-81-review", pr_number="456", issue_number=""):
    landing = tmp_path / "landing"
    out = tmp_path / "out.txt"
    out.write_text("")
    env = {
        "START_SHA": start_sha,
        "BRANCH": branch,
        "PR_NUMBER": pr_number,
        "ISSUE_NUMBER": issue_number,
        "EXTRA": str(extra) if extra else "",
        "DIR": str(landing),
        "READ_ONLY": "true" if read_only else "false",
        "REPO": "meridianlabs-ai/agents",
        "RUN_ID": "123",
        "GITHUB_OUTPUT": str(out),
        # A git that cannot run: the read-only path must never need it (a
        # caller that made the `nobin` directory of symlinked tools opts in).
        "PATH": str(tmp_path / "nobin") if read_only and (tmp_path / "nobin").is_dir() else os.environ["PATH"],
    }
    r = sh("bash", "-c", emit_landing_script(), cwd=cwd, check=False, env=env)
    return r, landing, out.read_text()


def test_emit_landing_read_only_runs_no_git_and_never_bundles(repos):
    # claude-review.yml's shape: HEAD moved (the agent checked out the base
    # branch, say) but the run is read-only — no bundle, head_sha is the
    # start SHA, and no git binary was needed at all.
    r = repos
    nobin = r["tmp"] / "nobin"
    nobin.mkdir()
    for tool in ("bash", "jq", "echo", "mkdir", "rm", "cat"):
        p = sh("bash", "-c", f"command -v {tool}").stdout.strip()
        if p and p.startswith("/"):
            (nobin / tool).symlink_to(p)
    res, landing, output = run_emit_landing(r["tmp"], cwd=r["work"], read_only=True, start_sha=r["start"])
    assert res.returncode == 0, res.stderr
    import json

    m = json.loads((landing / "manifest.json").read_text())
    assert m["has_bundle"] is False
    assert m["head_sha"] == r["start"] == m["start_sha"]
    assert "error" not in m
    assert not (landing / "commits.bundle").exists()
    assert "wrote=true" in output
    assert "read-only run; no bundle, no git" in res.stdout


def test_emit_landing_default_path_still_bundles(repos):
    r = repos
    res, landing, output = run_emit_landing(r["tmp"], cwd=r["work"], read_only=False, start_sha=r["start"])
    assert res.returncode == 0, res.stderr
    import json

    m = json.loads((landing / "manifest.json").read_text())
    assert m["has_bundle"] is True
    assert m["head_sha"] == r["head"]
    assert (landing / "commits.bundle").is_file()


def test_land_tokens_are_step_scoped():
    text = LAND.read_text()
    assert "secrets." not in text
    assert "persist-credentials" not in text and "actions/checkout" not in text
    # Every credential-helper block reads the one variable name (AGENTS.md).
    assert text.count("GIT_CONFIG_VALUE_1: '!f() { echo username=x-access-token; echo \"password=$GIT_TOKEN\"; }; f'") == 2


def test_emit_landing_lost_bundle_drops_every_landed_work_claim(repos):
    # HEAD moved but is not a descendant of the start SHA (the agent rewrote
    # history), so no bundle can be written and nothing lands. The workflow
    # composed its manifest-extra before this step from its own "HEAD moved"
    # test, so every field that CLAIMS the work landed must go with the
    # bundle: the hand-back (a bare `@review` over lost work), the stage
    # move, the thread resolutions (a thread is settled by code that lands)
    # and the completion hand-off (review round 1 of #83 reproduced the last
    # two outliving a failed bundle). `comments` and `error` stay: the
    # summary and the report are how the loss reaches the PR.
    import json

    r = repos
    work = r["work"]
    git("checkout", "-q", "--orphan", "rewrite", cwd=work)
    git("commit", "-qam", "rewritten", cwd=work)
    extra = r["tmp"] / "extra.json"
    (r["landing"] / "c.md").write_text("summary\n")
    (r["landing"] / "h.md").write_text("done\n")
    extra.write_text(json.dumps({
        "handback": True,
        "stage": "Review",
        "resolve_threads": ["PRRT_a"],
        "handoff_body_file": "h.md",
        "comments": [{"number": 456, "body_file": "c.md"}],
        "error": {"message": "the agent said so", "fail_run": True},
    }))
    res, landing, output = run_emit_landing(r["tmp"], cwd=work, read_only=False, start_sha=r["start"], extra=extra)
    assert res.returncode == 0, res.stderr
    m = json.loads((landing / "manifest.json").read_text())
    assert m["has_bundle"] is False and m["head_sha"] == r["start"]
    assert not (landing / "commits.bundle").exists()
    for gone in ("handback", "stage", "resolve_threads", "handoff_body_file"):
        assert gone not in m, m
    assert m["comments"] == [{"number": 456, "body_file": "c.md"}]
    assert m["error"]["fail_run"] is True
    assert m["error"]["message"].startswith("the agent said so")
    assert "dropped with the bundle" in m["error"]["message"]
    assert "wrote=true" in output


def test_emit_landing_keeps_a_no_change_handoff(repos):
    # HEAD never moved: a no-change round's hand-off (and its replies) are
    # owed by the round, not by a commit, and must survive untouched.
    import json

    r = repos
    work = r["work"]
    git("reset", "-q", "--hard", r["start"], cwd=work)
    extra = r["tmp"] / "extra.json"
    (r["landing"] / "h.md").write_text("declined everything\n")
    extra.write_text(json.dumps({"handoff_body_file": "h.md", "stage": "Review",
                                 "replies": [{"review_comment_id": 7, "body_file": "h.md"}]}))
    res, landing, _ = run_emit_landing(r["tmp"], cwd=work, read_only=False, start_sha=r["start"], extra=extra)
    assert res.returncode == 0, res.stderr
    m = json.loads((landing / "manifest.json").read_text())
    assert m["has_bundle"] is False
    assert m["handoff_body_file"] == "h.md" and m["stage"] == "Review"
    assert m["replies"] == [{"review_comment_id": 7, "body_file": "h.md"}]
    assert "error" not in m
