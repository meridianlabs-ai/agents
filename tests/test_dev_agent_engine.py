"""Tests for claude.yml's `Detect engine` step: who applied an issue's `auto`
label decides whether it is the run's opt-in (Claude Security finding
4628438). Applying a label needs only triage permission, and a triage
account's `auto` — refused as a kickoff by the trig step, but left on the
issue — used to turn the next write-access human's plain `@claude` into an
autonomous run: the PR labelled `auto` by the machine account, whose label
the loop gates trust by login. Lifted from the workflow the way
test_dev_agent_trig.py lifts the trig step, run against a stub `gh`.
"""

import json
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
LAND = ROOT / ".github" / "actions" / "land" / "action.yml"
TRUSTED_LOGINS = workflow_env(WORKFLOW, "TRUSTED_LOGINS")

GH_STUB = r"""
sleep() { :; }
gh() {
  case "$*" in
    "api repos/o/r/issues/12/labels --paginate --jq .[].name") cat "$STATE/labels" ;;
    "api repos/o/r/issues/12/timeline?per_page=100 --paginate")
      echo timeline >>"$STATE/reads"
      [ -f "$STATE/timeline.json" ] || { echo '{"message":"Server Error"}'; return 1; }
      cat "$STATE/timeline.json" ;;
    "api repos/o/r/collaborators/"*"/permission --jq .permission")
      local login=${2#repos/o/r/collaborators/}; login=${login%/permission}
      echo "$login" >>"$STATE/lookups"
      local p; p=$(awk -v l="$login" '$1==l {print $2}' "$STATE/perms")
      [ -n "$p" ] || { echo '{"message":"Not Found"}'; return 1; }
      echo "$p" ;;
    "pr view 12 --repo o/r --json headRefName --jq .headRefName") echo feature ;;
    *) echo "unexpected gh call: $*" >&2; return 1 ;;
  esac
}
"""


def labeled(login, name="auto"):
    return {"event": "labeled", "label": {"name": name}, "actor": {"login": login}}


def run_engine(tmp_path, *, labels=("auto",), timeline=(), perms=None, phrase="@claude", is_pr=False,
               timeline_fails=False):
    state = fresh_state(tmp_path)
    (state / "labels").write_text("".join(f"{x}\n" for x in labels))
    if not timeline_fails:
        # One page, as `gh api --paginate` prints it: an array per page.
        (state / "timeline.json").write_text(json.dumps(list(timeline)))
    (state / "perms").write_text("".join(f"{k} {v}\n" for k, v in (perms or {}).items()))
    out = tmp_path / "out"
    out.write_text("")
    env = {
        "GITHUB_OUTPUT": str(out), "STATE": str(state), "REPO": "o/r", "NUM": "12", "PHRASE": phrase,
        "IS_PR": "true" if is_pr else "false", "HEAD_REF": "", "TRUSTED_LOGINS": TRUSTED_LOGINS,
    }
    r = sh(*STEP_BASH, GH_STUB + lift_step(WORKFLOW, "        id: engine"), check=False, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    o = outputs(out)
    o["pr_labels"] = json.loads(o["pr_labels"])
    return r, o, state


def timeline_reads(state):
    f = state / "reads"
    return f.read_text().split() if f.exists() else []


def test_a_write_access_humans_issue_label_is_the_opt_in(tmp_path):
    # The preserved route: a maintainer labelled the issue `auto`, and a
    # later `@claude` (or the `auto` label event itself) runs autonomously —
    # the PR the land job opens carries `auto` and owes the hand-back.
    _, o, state = run_engine(tmp_path, labels=["auto", "engine:codex"], timeline=[labeled("alice")],
                             perms={"alice": "write"})
    assert o["auto"] == "true" and o["pr_labels"] == ["auto", "engine:codex"] and o["engine"] == "codex"
    assert timeline_reads(state) == ["timeline"] and lookups(state) == ["alice"]


def test_a_triage_accounts_issue_label_is_not_an_opt_in(tmp_path):
    # The finding: the label persists after the triage account's own
    # kickoff was refused; the write-access human's plain `@claude` must
    # stay a one-shot run — no `auto` in pr_labels, no hand-back — while the
    # issue's engine label is still copied.
    r, o, state = run_engine(tmp_path, labels=["auto", "engine:codex"], timeline=[labeled("mallory")],
                             perms={"mallory": "read"})
    assert o["auto"] == "false" and o["pr_labels"] == ["engine:codex"] and o["engine"] == "codex"
    assert lookups(state) == ["mallory"]
    assert "applied by mallory, who does not have write access (permission: read)" in r.stdout


def test_the_most_recent_labeler_decides(tmp_path):
    # A maintainer's earlier label that a triage account removed and
    # re-applied is the triage account's now; the reverse order is the
    # maintainer's — the same "last labeled event" rule as verify-auto-labeler.
    _, o, _ = run_engine(tmp_path, timeline=[labeled("alice"), {"event": "unlabeled", "label": {"name": "auto"},
                                                                 "actor": {"login": "mallory"}}, labeled("mallory")],
                         perms={"alice": "write", "mallory": "read"})
    assert o["auto"] == "false"
    _, o, _ = run_engine(tmp_path, timeline=[labeled("mallory"), labeled("alice")],
                         perms={"alice": "write", "mallory": "read"})
    assert o["auto"] == "true"
    # Another label's events do not count for `auto`.
    _, o, _ = run_engine(tmp_path, timeline=[labeled("alice", "engine:codex"), labeled("mallory")],
                         perms={"alice": "write", "mallory": "read"})
    assert o["auto"] == "false"


@pytest.mark.parametrize("login", [MARVIN, MARVIN_BOT])
def test_the_machine_accounts_own_issue_label_is_not_an_opt_in(tmp_path, login):
    # The trig step's rule for the label path, kept here: the machine
    # account writes what a trusted job or a human decided, so its `auto` on
    # an issue is not that decision (finding 4628345 — the triage pipeline
    # labelled issues `auto` as marvin from an untrusted agent's manifest).
    # No lookup is made (the User holds write, so one would have passed).
    r, o, state = run_engine(tmp_path, timeline=[labeled(login)], perms={MARVIN: "write"})
    assert o["auto"] == "false" and o["pr_labels"] == []
    assert lookups(state) == []
    assert f"applied by {login} (a bot or the machine account)" in r.stdout


@pytest.mark.parametrize("bot", ["github-actions[bot]", "foo[bot]"])
def test_another_apps_issue_label_is_not_an_opt_in(tmp_path, bot):
    _, o, state = run_engine(tmp_path, timeline=[labeled(bot)])
    assert o["auto"] == "false" and o["pr_labels"] == [] and lookups(state) == []


def test_an_unreadable_labeler_fails_closed(tmp_path):
    # No labeled event in the timeline (a shape drift, a label applied by a
    # path the timeline does not record), or a timeline read that fails:
    # not an opt-in.
    r, o, _ = run_engine(tmp_path, timeline=[])
    assert o["auto"] == "false" and o["pr_labels"] == []
    assert "who applied it could not be read from its timeline" in r.stdout
    _, o, _ = run_engine(tmp_path, timeline_fails=True)
    assert o["auto"] == "false" and o["pr_labels"] == []


def test_a_failed_permission_lookup_fails_closed_after_one_retry(tmp_path):
    # An account the collaborators endpoint cannot answer for (deleted, or
    # the API blipped twice): not an opt-in, like the trig step.
    r, o, state = run_engine(tmp_path, timeline=[labeled("ghost")])
    assert o["auto"] == "false" and o["pr_labels"] == []
    assert lookups(state) == ["ghost", "ghost"]
    assert "permission: lookup failed" in r.stdout


def test_an_at_auto_comment_opts_in_without_reading_the_label(tmp_path):
    # The commenter's write access passed the trig step; the mention is the
    # opt-in whether or not the item carries the label, and whoever applied
    # a label that is there is not consulted.
    _, o, state = run_engine(tmp_path, labels=[], phrase="@auto")
    assert o["auto"] == "true" and o["pr_labels"] == ["auto"]
    assert timeline_reads(state) == [] and lookups(state) == []
    _, o, state = run_engine(tmp_path, labels=["auto"], timeline=[labeled("mallory")], perms={"mallory": "read"},
                             phrase="@auto")
    assert o["auto"] == "true" and o["pr_labels"] == ["auto"]
    assert timeline_reads(state) == [] and lookups(state) == []


def test_an_unlabelled_issue_reads_no_timeline(tmp_path):
    _, o, state = run_engine(tmp_path, labels=["engine:codex"])
    assert o["auto"] == "false" and o["pr_labels"] == ["engine:codex"]
    assert timeline_reads(state) == [] and lookups(state) == []


def test_a_prs_label_is_left_to_the_loop_gates(tmp_path):
    # On a PR run the label's provenance is verify-auto-labeler's check,
    # made before any loop round runs (and the machine account's own label
    # is legitimate there); this step only reads the standing.
    _, o, state = run_engine(tmp_path, is_pr=True, timeline=[labeled("mallory")], perms={"mallory": "read"})
    assert o["auto"] == "true" and o["head_branch"] == "feature"
    assert timeline_reads(state) == [] and lookups(state) == []


def test_land_job_pins_the_pr_labels_to_the_gates_read():
    # Finding 4628441: the land job passes the gate's label read — the same
    # output the agent job's composer copies into `pr.labels` — as the
    # validator's allow-list, and the `land` composite forwards it. Text
    # checks, like test_app_token_minting's.
    text = WORKFLOW.read_text()
    land_job = text[text.index("\n  land:\n"):]
    assert re.search(r"^\s*allowed-pr-labels: \$\{\{ needs\.gate\.outputs\.pr_labels \}\}$", land_job, re.M)
    assert re.search(r"^\s*PR_LABELS: \$\{\{ needs\.gate\.outputs\.pr_labels \}\}$", text, re.M)
    land = LAND.read_text()
    assert re.search(r"^\s*ALLOWED_PR_LABELS: \$\{\{ inputs\.allowed-pr-labels \}\}$", land, re.M)
    assert '--allowed-pr-labels "$ALLOWED_PR_LABELS"' in land
    assert re.search(r"^  allowed-pr-labels:\n(?:    .*\n)*?    default: \"\*\"$", land, re.M)
