"""Tests for claude.yml's `Check trigger (stage gating)` step: who may start
the dev agent (issue #92), and the machine account's two logins (Phase 2
prep): the `auto` label it applies kicks the run off under either login with
no permission lookup (the collaborators endpoint answers `none` for a GitHub
App, so the bot is trusted by name), neither login starts a run from text,
and every other bot stays excluded. Lifted from the workflow the way
test_review_trig.py lifts the reviewer's trig step, run against a stub `gh`.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_helpers import sh  # noqa: E402
from test_review_fix_gate import MARVIN, MARVIN_BOT, STEP_BASH, fresh_state, lift_step, lookups, outputs  # noqa: E402
from test_review_fix_gate import workflow_env  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "claude.yml"
TRUSTED_LOGINS = workflow_env(WORKFLOW, "TRUSTED_LOGINS")

GH_STUB = r"""
sleep() { :; }
gh() {
  case "$*" in
    "api repos/o/r/collaborators/"*"/permission --jq .permission")
      local login=${2#repos/o/r/collaborators/}; login=${login%/permission}
      echo "$login" >>"$STATE/lookups"
      local p; p=$(awk -v l="$login" '$1==l {print $2}' "$STATE/perms")
      [ -n "$p" ] || { echo '{"message":"Not Found"}'; return 1; }
      echo "$p" ;;
    "pr view 7 --repo o/r --json isCrossRepository --jq .isCrossRepository") echo false ;;
    *) echo "unexpected gh call: $*" >&2; return 1 ;;
  esac
}
"""


def run_trig(tmp_path, *, actor, event, action, perms=None, comment="", label="", is_pr=False):
    state = fresh_state(tmp_path)
    (state / "perms").write_text("".join(f"{k} {v}\n" for k, v in (perms or {}).items()))
    out = tmp_path / "out"
    out.write_text("")
    env = {
        "GITHUB_OUTPUT": str(out), "STATE": str(state), "PHRASE": "@auto", "LABEL": "auto",
        "EVENT": event, "EVENT_ACTION": action, "ACTOR": actor, "COMMENT": comment, "REVIEW": "",
        "IBODY": "", "ITITLE": "", "LNAME": label, "REPO": "o/r",
        "IS_PR_COMMENT": "true" if is_pr else "false", "PR_NUM": "7", "HEAD_REPO": "",
        "TRUSTED_LOGINS": TRUSTED_LOGINS,
    }
    r = sh(*STEP_BASH, GH_STUB + lift_step(WORKFLOW, "        id: trig"), check=False, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return r, outputs(out), state


@pytest.mark.parametrize("login", [MARVIN, MARVIN_BOT])
def test_the_machine_accounts_auto_label_kicks_off_without_a_lookup_under_either_login(tmp_path, login):
    _, o, state = run_trig(tmp_path, actor=login, event="issues", action="labeled", label="auto")
    assert o["ok"] == "true" and o["authorized"] == "true" and o["fork_head"] == "false"
    assert lookups(state) == []


@pytest.mark.parametrize("bot", ["github-actions[bot]", "foo[bot]"])
def test_another_apps_auto_label_does_not_kick_off(tmp_path, bot):
    _, o, state = run_trig(tmp_path, actor=bot, event="issues", action="labeled", label="auto")
    assert o["ok"] == "false" and o["authorized"] == "false" and lookups(state) == []


def test_a_humans_label_is_authorized_by_the_permission_lookup(tmp_path):
    _, o, state = run_trig(tmp_path, actor="alice", event="issues", action="labeled", label="auto",
                           perms={"alice": "write"})
    assert o["ok"] == "true" and o["authorized"] == "true" and lookups(state) == ["alice"]
    _, o, state = run_trig(tmp_path, actor="nobody", event="issues", action="labeled", label="auto",
                           perms={"nobody": "read"})
    assert o["ok"] == "false" and o["authorized"] == "false" and lookups(state) == ["nobody"]


@pytest.mark.parametrize("login", [MARVIN, MARVIN_BOT, "foo[bot]"])
def test_neither_machine_login_nor_any_bot_starts_a_run_from_text(tmp_path, login):
    # The machine account's own output can quote the trigger; the label path
    # is its only kickoff, under either login.
    _, o, state = run_trig(tmp_path, actor=login, event="issue_comment", action="created",
                           comment="@auto go", is_pr=True)
    assert o["ok"] == "false" and o["authorized"] == "false" and lookups(state) == []


def test_a_humans_comment_starts_a_run_after_the_lookup(tmp_path):
    _, o, state = run_trig(tmp_path, actor="alice", event="issue_comment", action="created",
                           comment="@auto go", is_pr=True, perms={"alice": "write"})
    assert o["ok"] == "true" and o["authorized"] == "true" and o["fork_head"] == "false"
    assert lookups(state) == ["alice"]


def test_workflow_names_both_logins_once_and_carries_them_into_the_agent_steps_bot_allow_lists():
    # The one env value; the trig step reads it rather than naming a login;
    # claude-code-action's allowed_bots and codex-action's allow-bot-users
    # carry it so the label kickoff under the App login is not refused by the
    # actions' own non-human-actor guards after the gate admitted it.
    assert TRUSTED_LOGINS == f"{MARVIN},{MARVIN_BOT}"
    text = WORKFLOW.read_text()
    assert text.count("\nenv:\n") == 1
    assert "allowed_bots: ${{ env.TRUSTED_LOGINS }}" in text
    assert "allow-bot-users: ${{ env.TRUSTED_LOGINS }}" in text
    assert "i-am-marvin" not in lift_step(WORKFLOW, "        id: trig")


def test_dev_stubs_admit_the_machine_accounts_bot_login_on_the_auto_label_path_only():
    # The stubs have no env to read: the label path names the App login next
    # to the `[bot]` exclusion; the `claude` job's guard and the text paths
    # keep excluding every bot, the machine account's included.
    for stub in (ROOT / ".github" / "workflows" / "claude-stub.yml", ROOT / "examples" / "claude-stub.yml"):
        text = stub.read_text()
        assert text.count("( !endsWith(github.actor, '[bot]') || github.actor == 'meridian-marvin[bot]' )") == 1, stub
        assert text.count("!endsWith(github.actor, '[bot]')") == 3, stub
        assert text.count("github.actor != 'i-am-marvin'") == 3, stub  # the comment naming the guard, and two guards
