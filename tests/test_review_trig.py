"""Tests for claude-review.yml's `Check trigger` step on comment-triggered
reviews (Claude Security finding 4085111).

Every `@review` comment used to be admitted on a same-repo head whoever
posted it; only fork heads and External issues asked for write access. The
step is lifted out of the workflow (as test_review_fix_gate.py lifts the
review loop's steps) and run against a stub `gh`: an outsider is refused on
every head, a write-access commenter is admitted (sandboxed on a fork head),
the loops' hand-back identity (TRUSTED_LOGINS) is admitted without a lookup,
and a bot only when the caller's allowed_bots names it, same-repo heads only.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_helpers import sh  # noqa: E402
from test_review_fix_gate import fresh_state, lift_step, lookups, outputs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "claude-review.yml"
HEAD = "c" * 40
UPSTREAM = "https://github.com/up/stream/pull/12"

GH_STUB = r"""
sleep() { :; }
gh() {
  case "$*" in
    "api repos/o/r/pulls/7") cat "$STATE/pr.json" ;;
    "api repos/o/r/collaborators/"*"/permission --jq .permission")
      local login=${2#repos/o/r/collaborators/}; login=${login%/permission}
      echo "$login" >>"$STATE/lookups"
      local p; p=$(awk -v l="$login" '$1==l {print $2}' "$STATE/perms")
      [ -n "$p" ] || { echo '{"message":"Not Found"}'; return 1; }
      echo "$p" ;;
    *) echo "unexpected gh call: $*" >&2; return 1 ;;
  esac
}
"""


def run_trig(tmp_path, *, actor, perms=None, allowed_bots="", head_repo="o/r",
             is_pr=True, external=False, ibody=""):
    state = fresh_state(tmp_path)
    (state / "perms").write_text("".join(f"{k} {v}\n" for k, v in (perms or {}).items()))
    (state / "pr.json").write_text(json.dumps({
        "head": {"repo": {"full_name": head_repo}, "sha": HEAD, "ref": "feature"},
        "state": "open", "merged": False}))
    out = tmp_path / "out"
    out.write_text("")
    env = {
        "GITHUB_OUTPUT": str(out), "STATE": str(state), "EVENT": "issue_comment",
        "ACTOR": actor, "GH_REPO": "o/r", "COMMENT": "@review", "IS_PR": "true" if is_pr else "false",
        "PR_NUM": "7", "EXTERNAL": "true" if external else "false", "IBODY": ibody,
        "HEAD_REPO": "", "HEAD_REF": "", "TRUSTED_LOGINS": "i-am-marvin", "ALLOWED_BOTS": allowed_bots,
    }
    r = sh("bash", "-c", GH_STUB + lift_step(WORKFLOW, "        id: trig"), check=False, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return r, outputs(out), state


def test_outsider_is_refused_on_a_same_repo_head(tmp_path):
    r, o, state = run_trig(tmp_path, actor="nobody", perms={"nobody": "read"})
    assert o["ok"] == "false" and o["ack"] == "false"
    assert lookups(state) == ["nobody"]
    assert "::warning::" in r.stdout and "refusing" in r.stdout


def test_write_access_commenter_is_admitted_on_a_same_repo_head(tmp_path):
    _, o, _ = run_trig(tmp_path, actor="alice", perms={"alice": "write"})
    assert o["ok"] == "true" and o["ack"] == "true" and o["fork_head"] == "false"
    assert o["head_sha"] == HEAD and o["head_ref"] == "feature"


@pytest.mark.parametrize("perm", ["admin", "maintain"])
def test_admin_and_maintain_count_as_write(tmp_path, perm):
    _, o, _ = run_trig(tmp_path, actor="alice", perms={"alice": perm})
    assert o["ok"] == "true"


def test_trusted_login_is_admitted_without_a_lookup(tmp_path):
    # No `perms` entry: a lookup would fail, so admission proves none ran.
    _, o, state = run_trig(tmp_path, actor="i-am-marvin")
    assert o["ok"] == "true" and o["ack"] == "true"
    assert lookups(state) == []


def test_failed_permission_lookup_refuses(tmp_path):
    _, o, state = run_trig(tmp_path, actor="ghost")
    assert o["ok"] == "false"
    assert set(lookups(state)) == {"ghost"}


@pytest.mark.parametrize("allowed", ["claude[bot]", "claude", "Claude[bot]", "*", "other, claude[bot]"])
def test_allow_listed_bot_is_admitted_on_a_same_repo_head_without_ack(tmp_path, allowed):
    _, o, state = run_trig(tmp_path, actor="claude[bot]", allowed_bots=allowed)
    assert o["ok"] == "true" and o["ack"] == "false", allowed
    assert lookups(state) == []


@pytest.mark.parametrize("allowed", ["", "other[bot]", "github-actions"])
def test_bot_not_in_allowed_bots_is_refused(tmp_path, allowed):
    _, o, state = run_trig(tmp_path, actor="claude[bot]", allowed_bots=allowed)
    assert o["ok"] == "false", allowed
    assert lookups(state) == []


@pytest.mark.parametrize("allowed", ["*", "github-actions", "github-actions[bot]"])
def test_github_actions_bot_is_never_admitted(tmp_path, allowed):
    # Any repository's workflow run posts as it, so it identifies nobody —
    # no allow-list entry and not even the wildcard admits it.
    _, o, state = run_trig(tmp_path, actor="github-actions[bot]", allowed_bots=allowed)
    assert o["ok"] == "false", allowed
    assert lookups(state) == []


def test_allow_listed_bot_never_opens_the_fork_escape_hatch(tmp_path):
    _, o, _ = run_trig(tmp_path, actor="claude[bot]", allowed_bots="claude[bot]", head_repo="someone/r")
    assert o["ok"] == "false"


def test_fork_head_admits_only_a_trusted_commenter_sandboxed(tmp_path):
    _, o, _ = run_trig(tmp_path, actor="nobody", perms={"nobody": "read"}, head_repo="someone/r")
    assert o["ok"] == "false"
    _, o, _ = run_trig(tmp_path, actor="alice", perms={"alice": "write"}, head_repo="someone/r")
    assert o["ok"] == "true" and o["fork_head"] == "true"
    _, o, _ = run_trig(tmp_path, actor="i-am-marvin", head_repo="someone/r")
    assert o["ok"] == "true" and o["fork_head"] == "true"


def test_external_issue_requires_the_trusted_commenter(tmp_path):
    ibody = f"Upstream PR: {UPSTREAM}"
    _, o, _ = run_trig(tmp_path, actor="nobody", perms={"nobody": "read"}, is_pr=False, external=True, ibody=ibody)
    assert o["ok"] == "false"
    _, o, _ = run_trig(tmp_path, actor="alice", perms={"alice": "write"}, is_pr=False, external=True, ibody=ibody)
    assert o["ok"] == "true" and o["mode"] == "external" and o["upstream"] == UPSTREAM
    _, o, _ = run_trig(tmp_path, actor="i-am-marvin", is_pr=False, external=True, ibody=ibody)
    assert o["ok"] == "true" and o["mode"] == "external"
