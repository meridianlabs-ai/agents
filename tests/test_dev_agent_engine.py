"""Tests for claude.yml's `Detect engine` step: who applied an issue's `auto`
label decides whether it is the run's opt-in (Claude Security finding
4628438). Applying a label needs only triage permission, and a triage
account's `auto` — refused as a kickoff by the trig step, but left on the
issue — used to turn the next write-access human's plain `@claude` into an
autonomous run: the PR labelled `auto` by the machine account, whose label
the loop gates trust by login. A decided refusal also removes the label
and says so on the issue (issue #141). Lifted from the workflow the way
test_dev_agent_trig.py lifts the trig step, run against a stub `gh`.
"""

import itertools
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
      # `gh api --paginate` prints each page's array as it arrives; a page
      # that fails at the transport layer leaves ONLY the earlier pages on
      # stdout (its diagnostic goes to stderr) and exits non-zero — valid
      # JSON that parses cleanly, which is what made B1 (review round 1).
      # An HTTP error body, by contrast, lands on stdout and breaks the
      # parse by itself, so the partial fixture must not append one (review
      # round 2, T1: the old code passed with that fixture).
      [ ! -f "$STATE/timeline-partial.json" ] || { cat "$STATE/timeline-partial.json"; echo 'error connecting to api.github.com' >&2; return 1; }
      [ -f "$STATE/timeline.json" ] || { echo '{"message":"Server Error"}'; return 1; }
      cat "$STATE/timeline.json" ;;
    "api repos/o/r/collaborators/"*"/permission --jq .permission")
      local login=${2#repos/o/r/collaborators/}; login=${login%/permission}
      echo "$login" >>"$STATE/lookups"
      # The issue changing while the lookup is in flight (review round 1
      # of #141): the next timeline read sees the new state, or fails.
      [ ! -f "$STATE/timeline-after-lookup.json" ] || mv "$STATE/timeline-after-lookup.json" "$STATE/timeline.json"
      [ ! -f "$STATE/timeline-gone-after-lookup" ] || rm -f "$STATE/timeline.json"
      [ ! -f "$STATE/perm-fails-once" ] || { rm "$STATE/perm-fails-once"; echo '{"message":"Server Error"}'; return 1; }
      local p; p=$(awk -v l="$login" '$1==l {print $2}' "$STATE/perms")
      [ -n "$p" ] || { echo '{"message":"Not Found"}'; return 1; }
      echo "$p" ;;
    "pr view 12 --repo o/r --json headRefName --jq .headRefName") echo feature ;;
    "api -X DELETE repos/o/r/issues/12/labels/auto --silent")
      echo unlabel >>"$STATE/writes"
      [ ! -f "$STATE/unlabel-fails" ] || { echo '{"message":"Forbidden"}'; return 1; } ;;
    "issue comment 12 --repo o/r --body "*)
      echo comment >>"$STATE/writes"
      printf '%s' "$7" >"$STATE/comment" ;;
    *) echo "unexpected gh call: $*" >&2; return 1 ;;
  esac
}
"""


_event_ids = itertools.count(1)


def labeled(login, name="auto"):
    return {"event": "labeled", "id": next(_event_ids), "label": {"name": name}, "actor": {"login": login}}


def unlabeled(login, name="auto"):
    return {"event": "unlabeled", "id": next(_event_ids), "label": {"name": name}, "actor": {"login": login}}


def pages(*events_per_page):
    """What `gh api --paginate` prints without --slurp: one JSON array per
    page, concatenated."""
    return "".join(json.dumps(list(p)) + "\n" for p in events_per_page)


def run_engine(tmp_path, *, labels=("auto",), timeline=(), perms=None, phrase="@claude", is_pr=False,
               timeline_fails=False, timeline_text=None, partial_text=None, labeled_auto=False,
               unlabel_fails=False, timeline_after_lookup=None, timeline_gone_after_lookup=False,
               perm_fails_once=False):
    state = fresh_state(tmp_path)
    if unlabel_fails:
        (state / "unlabel-fails").write_text("")
    if timeline_after_lookup is not None:
        (state / "timeline-after-lookup.json").write_text(pages(timeline_after_lookup))
    if timeline_gone_after_lookup:
        (state / "timeline-gone-after-lookup").write_text("")
    if perm_fails_once:
        (state / "perm-fails-once").write_text("")
    (state / "labels").write_text("".join(f"{x}\n" for x in labels))
    if partial_text is not None:
        (state / "timeline-partial.json").write_text(partial_text)
    elif not timeline_fails:
        (state / "timeline.json").write_text(pages(timeline) if timeline_text is None else timeline_text)
    (state / "perms").write_text("".join(f"{k} {v}\n" for k, v in (perms or {}).items()))
    out = tmp_path / "out"
    out.write_text("")
    env = {
        "GITHUB_OUTPUT": str(out), "STATE": str(state), "REPO": "o/r", "NUM": "12", "PHRASE": phrase,
        "IS_PR": "true" if is_pr else "false", "HEAD_REF": "", "TRUSTED_LOGINS": TRUSTED_LOGINS,
        "LABELED_AUTO": "true" if labeled_auto else "false",
    }
    r = sh(*STEP_BASH, GH_STUB + lift_step(WORKFLOW, "        id: engine"), check=False, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    o = outputs(out)
    o["pr_labels"] = json.loads(o["pr_labels"])
    return r, o, state


def timeline_reads(state):
    f = state / "reads"
    return f.read_text().split() if f.exists() else []


def writes(state):
    """The refused-label cleanup's writes, in order: `unlabel`, `comment`."""
    f = state / "writes"
    return f.read_text().split() if f.exists() else []


def comment(state):
    return (state / "comment").read_text()


def test_a_write_access_humans_issue_label_is_the_opt_in(tmp_path):
    # The preserved route: a maintainer labelled the issue `auto`, and a
    # later `@claude` (or the `auto` label event itself) runs autonomously —
    # the PR the land job opens carries `auto` and owes the hand-back.
    _, o, state = run_engine(tmp_path, labels=["auto", "engine:codex"], timeline=[labeled("alice")],
                             perms={"alice": "write"})
    assert o["auto"] == "true" and o["pr_labels"] == ["auto", "engine:codex"] and o["engine"] == "codex"
    assert timeline_reads(state) == ["timeline"] and lookups(state) == ["alice"]
    assert writes(state) == []


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
    # Issue #141: the refused label comes off, and the issue says why —
    # after a second timeline read shows it is still the label judged.
    assert writes(state) == ["unlabel", "comment"] and timeline_reads(state) == ["timeline", "timeline"]
    assert "last applied by `mallory` (an account without write access)" in comment(state)
    assert "The label has been removed" in comment(state)


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
    assert writes(state) == ["unlabel", "comment"]
    assert f"last applied by `{login}` (a bot or the machine account)" in comment(state)


@pytest.mark.parametrize("bot", ["github-actions[bot]", "foo[bot]"])
def test_another_apps_issue_label_is_not_an_opt_in(tmp_path, bot):
    _, o, state = run_engine(tmp_path, timeline=[labeled(bot)])
    assert o["auto"] == "false" and o["pr_labels"] == [] and lookups(state) == []
    assert writes(state) == ["unlabel", "comment"]


def test_an_unreadable_labeler_fails_closed(tmp_path):
    # No labeled event in the timeline (a shape drift, a label applied by a
    # path the timeline does not record), or a timeline read that fails:
    # not an opt-in.
    # Neither is a decided refusal, so the label (which may be a
    # maintainer's) stays and nothing is posted.
    r, o, state = run_engine(tmp_path, timeline=[])
    assert o["auto"] == "false" and o["pr_labels"] == []
    assert "who applied it could not be read from its timeline" in r.stdout
    assert writes(state) == []
    _, o, state = run_engine(tmp_path, timeline_fails=True)
    assert o["auto"] == "false" and o["pr_labels"] == []
    assert timeline_reads(state) == ["timeline", "timeline"] and lookups(state) == []
    assert writes(state) == []


def test_a_timeline_read_that_fails_after_a_good_page_is_not_used(tmp_path):
    # Review round 1 (B1): `gh api --paginate` prints each page as it
    # arrives, so a transport failure on page two leaves page one's valid
    # JSON on stdout (nothing else: the transport error is on stderr).
    # Parsing that would name the last labeler of the pages that arrived —
    # a writer whose label a triage account removed and re-applied on the
    # page that never came — and the round 1 code did (`auto=true` with this
    # fixture). The response is used only after a successful exit: the
    # failed read is retried once and the run stays one-shot, with no
    # permission lookup at all.
    r, o, state = run_engine(tmp_path, partial_text=pages([labeled("alice")]), perms={"alice": "write"})
    assert o["auto"] == "false" and o["pr_labels"] == []
    assert timeline_reads(state) == ["timeline", "timeline"] and lookups(state) == []
    assert "who applied it could not be read from its timeline" in r.stdout
    assert writes(state) == []


def test_the_last_labeler_across_pages_wins(tmp_path):
    # A complete multi-page read, in the shape `--paginate` prints (an
    # array per page): the newest event is on the last page.
    two_pages = pages([labeled("alice")], [{"event": "unlabeled", "label": {"name": "auto"}, "actor": {"login": "mallory"}},
                                           labeled("mallory")])
    _, o, state = run_engine(tmp_path, timeline_text=two_pages, perms={"alice": "write", "mallory": "read"})
    assert o["auto"] == "false" and lookups(state) == ["mallory"]
    two_pages = pages([labeled("mallory")], [labeled("alice")])
    _, o, state = run_engine(tmp_path, timeline_text=two_pages, perms={"alice": "write", "mallory": "read"})
    assert o["auto"] == "true" and lookups(state) == ["alice"] and timeline_reads(state) == ["timeline"]


def test_a_failed_permission_lookup_fails_closed_after_one_retry(tmp_path):
    # An account the collaborators endpoint cannot answer for (deleted, or
    # the API blipped twice): not an opt-in, like the trig step.
    r, o, state = run_engine(tmp_path, timeline=[labeled("ghost")])
    assert o["auto"] == "false" and o["pr_labels"] == []
    assert lookups(state) == ["ghost", "ghost"]
    assert "permission: lookup failed" in r.stdout
    assert writes(state) == []  # not a decided refusal: the label stays


def test_a_label_reapplied_during_the_lookup_is_kept(tmp_path):
    # Review round 1 (B1): the timeline named a triage labeler, then a
    # maintainer removed and re-applied `auto` while the permission lookup
    # ran. The DELETE cannot be conditional, so the timeline is read again
    # and the newer application keeps the label: nothing is removed and no
    # note misattributes it. The run itself stays one-shot (the verdict is
    # the first read's, as before).
    first = labeled("mallory")
    newer = [first, unlabeled("alice"), labeled("alice")]
    r, o, state = run_engine(tmp_path, timeline=[first], perms={"mallory": "read", "alice": "write"},
                             timeline_after_lookup=newer)
    assert o["auto"] == "false" and lookups(state) == ["mallory"]
    assert timeline_reads(state) == ["timeline", "timeline"] and writes(state) == []
    assert "changed after it was judged" in r.stdout


def test_a_label_reapplied_during_the_lookup_retry_is_kept(tmp_path):
    # The same, with the change landing while the first permission answer
    # failed and the step slept before its retry — the widest window.
    first = labeled("mallory")
    r, o, state = run_engine(tmp_path, timeline=[first], perms={"mallory": "triage", "alice": "admin"},
                             perm_fails_once=True, timeline_after_lookup=[first, unlabeled("alice"), labeled("alice")])
    assert o["auto"] == "false" and lookups(state) == ["mallory", "mallory"]
    assert writes(state) == []
    assert "changed after it was judged" in r.stdout


def test_a_label_removed_during_the_lookup_gets_no_note(tmp_path):
    # Newest `auto` event an unlabel: the label is already gone, and a note
    # claiming a failed removal left it on the issue would be wrong.
    first = labeled("mallory")
    _, o, state = run_engine(tmp_path, timeline=[first], perms={"mallory": "read"},
                             timeline_after_lookup=[first, unlabeled("alice")])
    assert o["auto"] == "false" and writes(state) == []


def test_a_failed_second_timeline_read_writes_nothing(tmp_path):
    # Provenance that cannot be re-established is not acted on.
    r, o, state = run_engine(tmp_path, timeline=[labeled("mallory")], perms={"mallory": "read"},
                             timeline_gone_after_lookup=True)
    assert o["auto"] == "false" and writes(state) == []
    assert "could not be read again" in r.stdout


def test_a_failed_label_removal_is_said_and_does_not_fail_the_run(tmp_path):
    r, o, state = run_engine(tmp_path, timeline=[labeled("mallory")], perms={"mallory": "triage"},
                             unlabel_fails=True)
    assert o["auto"] == "false"
    assert writes(state) == ["unlabel", "comment"]
    assert "could not remove the refused 'auto' label from issue #12" in r.stdout
    assert "Removing the label failed, so it is still on the issue" in comment(state)
    assert "has been removed" not in comment(state)


def test_the_note_writes_no_bare_trigger_token(tmp_path):
    # The body is posted where a bare trigger token could start a workflow
    # (belt and braces: github-actions comments start none).
    _, _, state = run_engine(tmp_path, timeline=[labeled("mallory")], perms={"mallory": "read"})
    body = comment(state)
    for token in ("@auto", "@claude", "@review"):
        assert token not in body


def test_a_run_started_by_applying_auto_removes_nothing(tmp_path):
    # This run's own event applied `auto`, so its labeler passed the trig
    # step; a timeline that has not caught up names an earlier labeler, and
    # removing the label would undo the maintainer's opt-in. The run stays
    # one-shot, as before, but writes nothing.
    _, o, state = run_engine(tmp_path, timeline=[labeled("mallory")], perms={"mallory": "read"},
                             labeled_auto=True)
    assert o["auto"] == "false" and writes(state) == []


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
    assert timeline_reads(state) == [] and lookups(state) == [] and writes(state) == []


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
    assert timeline_reads(state) == [] and lookups(state) == [] and writes(state) == []


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
    # Issue #143: a caller that passes no list gets no labels.
    assert re.search(r"^  allowed-pr-labels:\n(?:    .*\n)*?    default: \"\[\]\"$", land, re.M)
