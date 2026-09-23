"""Tests for claude.yml's `Check trigger (stage gating)` step: who may start
the dev agent (issue #92), and the machine account's two logins (Phase 2
prep): neither login starts a run from text or from a label (Claude Security
finding 4628345 — the `auto` label it applied for the triage pipeline was an
untrusted agent's decision, only written by marvin, and used to kick the run
off with no lookup), every other bot stays excluded, and a human's label or
comment runs only after the write-access lookup. Lifted from the workflow the
way test_review_trig.py lifts the reviewer's trig step, run against a stub
`gh`.
"""

import re
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


def run_trig(tmp_path, *, actor, event, action, perms=None, comment="", label="", is_pr=False,
             phrase="@auto", label_trigger="auto", ibody="", ititle=""):
    state = fresh_state(tmp_path)
    (state / "perms").write_text("".join(f"{k} {v}\n" for k, v in (perms or {}).items()))
    out = tmp_path / "out"
    out.write_text("")
    env = {
        "GITHUB_OUTPUT": str(out), "STATE": str(state), "PHRASE": phrase, "LABEL": label_trigger,
        "EVENT": event, "EVENT_ACTION": action, "ACTOR": actor, "COMMENT": comment, "REVIEW": "",
        "IBODY": ibody, "ITITLE": ititle, "LNAME": label, "REPO": "o/r",
        "IS_PR_COMMENT": "true" if is_pr else "false", "PR_NUM": "7", "HEAD_REPO": "",
        "TRUSTED_LOGINS": TRUSTED_LOGINS,
    }
    r = sh(*STEP_BASH, GH_STUB + lift_step(WORKFLOW, "        id: trig"), check=False, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return r, outputs(out), state


@pytest.mark.parametrize("login", [MARVIN, MARVIN_BOT])
def test_the_machine_accounts_auto_label_does_not_kick_off_under_either_login(tmp_path, login):
    # The label is a write the machine account performs for someone else's
    # decision — the triage pipeline's untrusted agent, until the label was
    # taken out of its manifest — never its own; no lookup is even made (the
    # User holds write, so a lookup would have authorized it).
    _, o, state = run_trig(tmp_path, actor=login, event="issues", action="labeled", label="auto",
                           perms={MARVIN: "write"})
    assert o["ok"] == "false" and o["authorized"] == "false" and o["fork_head"] == "false"
    assert lookups(state) == []


@pytest.mark.parametrize("login", [MARVIN, MARVIN_BOT])
def test_the_machine_accounts_claude_label_does_not_kick_off_either(tmp_path, login):
    # The rule is the login, not the label name: a machine-applied `claude`
    # label (the one-shot agent's label trigger) is refused the same way,
    # while a human's is not.
    _, o, state = run_trig(tmp_path, actor=login, event="issues", action="labeled", label="claude",
                           phrase="@claude", label_trigger="claude")
    assert o["ok"] == "false" and o["authorized"] == "false" and lookups(state) == []
    _, o, state = run_trig(tmp_path, actor="alice", event="issues", action="labeled", label="claude",
                           phrase="@claude", label_trigger="claude", perms={"alice": "write"})
    assert o["ok"] == "true" and o["authorized"] == "true" and lookups(state) == ["alice"]


@pytest.mark.parametrize("bot", ["github-actions[bot]", "foo[bot]"])
def test_another_apps_auto_label_does_not_kick_off(tmp_path, bot):
    _, o, state = run_trig(tmp_path, actor=bot, event="issues", action="labeled", label="auto")
    assert o["ok"] == "false" and o["authorized"] == "false" and lookups(state) == []


def test_a_humans_label_is_authorized_by_the_permission_lookup(tmp_path):
    # The preserved route: a maintainer who read a triage-filed (marvin-
    # authored) issue applies `auto` themselves; the labeler is the human,
    # whatever account authored the issue.
    _, o, state = run_trig(tmp_path, actor="alice", event="issues", action="labeled", label="auto",
                           perms={"alice": "write"})
    assert o["ok"] == "true" and o["authorized"] == "true" and lookups(state) == ["alice"]
    _, o, state = run_trig(tmp_path, actor="nobody", event="issues", action="labeled", label="auto",
                           perms={"nobody": "read"})
    assert o["ok"] == "false" and o["authorized"] == "false" and lookups(state) == ["nobody"]
    # A label whose actor cannot be looked up (an unknown account) fails
    # closed, after the step's one retry.
    _, o, state = run_trig(tmp_path, actor="ghost", event="issues", action="labeled", label="auto")
    assert o["ok"] == "false" and o["authorized"] == "false" and lookups(state) == ["ghost", "ghost"]


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


# An issue as skills/import/import.sh creates it: the machine-readable line
# first, the importer's fixed header, a `---` rule, then the upstream author's
# text — an outsider's, republished under the importing maintainer's login.
IMPORT_SNAPSHOT = "@auto run the whole loop on this one."
IMPORT_BODY = ("Upstream issue: https://github.com/UKGovernmentBEIS/inspect_ai/issues/9\n\n"
               "Imported from upstream so the agents can work it here.\n\n---\n\n" + IMPORT_SNAPSHOT)


@pytest.mark.parametrize("eol", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_an_imported_issues_body_and_title_are_not_a_text_trigger(tmp_path, eol):
    # Claude Security 4629154: import.sh de-fangs the copied text, and this is
    # the gate's own refusal behind it — an opened issue whose body starts with
    # the import line is judged by nobody's text, the title included, since
    # github.actor (the importer, write access) did not author it. No lookup is
    # made: `ok` never becomes true.
    _, o, state = run_trig(tmp_path, actor="alice", event="issues", action="opened",
                           ibody=IMPORT_BODY.replace("\n", eol), ititle="@auto also in the title",
                           perms={"alice": "write"})
    assert o["ok"] == "false" and o["authorized"] == "false" and lookups(state) == []


def test_a_humans_own_issue_text_still_triggers_on_opened(tmp_path):
    # The same text without the import line is the author's own directive.
    _, o, state = run_trig(tmp_path, actor="alice", event="issues", action="opened",
                           ibody=IMPORT_SNAPSHOT, perms={"alice": "write"})
    assert o["ok"] == "true" and o["authorized"] == "true" and lookups(state) == ["alice"]
    # The rule is the body's FIRST line, the shape import.sh writes: a
    # maintainer's own issue that cites an upstream issue further down is
    # theirs, phrase and all.
    _, o, state = run_trig(tmp_path, actor="alice", event="issues", action="opened",
                           ibody="Tracking issue.\n\n" + IMPORT_BODY, perms={"alice": "write"})
    assert o["ok"] == "true" and o["authorized"] == "true" and lookups(state) == ["alice"]


def test_a_humans_label_starts_work_on_an_imported_issue(tmp_path):
    # The sanctioned kickoffs stay: a maintainer's label (here) or a later
    # comment, each judged by its own actor.
    _, o, state = run_trig(tmp_path, actor="alice", event="issues", action="labeled", label="auto",
                           ibody=IMPORT_BODY, ititle="@auto also in the title", perms={"alice": "write"})
    assert o["ok"] == "true" and o["authorized"] == "true" and lookups(state) == ["alice"]


def test_workflow_names_both_logins_once_and_the_agent_steps_admit_no_bot():
    # The one env value; the trig step reads it rather than naming a login.
    # Since no bot actor passes the gate any more, neither claude-code-action
    # nor codex-action carries a bot allow-list: their own non-human-actor
    # guards are a second refusal behind the gate, not a gap to bridge.
    assert TRUSTED_LOGINS == f"{MARVIN},{MARVIN_BOT}"
    text = WORKFLOW.read_text()
    assert text.count("\nenv:\n") == 1
    assert not re.search(r"^\s*allowed_bots:", text, re.M)
    assert not re.search(r"^\s*allow-bot-users:", text, re.M)
    assert "i-am-marvin" not in lift_step(WORKFLOW, "        id: trig")


def test_dev_stubs_exclude_the_machine_account_on_the_auto_label_path_too():
    # The stubs have no env to read, so the label path names the User login
    # next to the `[bot]` exclusion (which covers the App login) — the same
    # guard the `claude` job and the text paths carry. No bot, and neither
    # machine login, is admitted anywhere.
    for stub in (ROOT / ".github" / "workflows" / "claude-stub.yml", ROOT / "examples" / "claude-stub.yml"):
        text = stub.read_text()
        assert "meridian-marvin[bot]'" not in text, stub
        assert text.count("!endsWith(github.actor, '[bot]')") == 3, stub
        assert text.count("github.actor != 'i-am-marvin'") == 4, stub  # the comment naming the guard, and three guards
