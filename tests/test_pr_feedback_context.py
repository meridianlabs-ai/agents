"""The `pr-feedback-context` composite's review anchor
(.github/actions/pr-feedback-context/action.yml), its step lifted and run
against a stub `gh`: a review round is anchored only on a marker-bearing
comment by the machine account's two logins (i-am-marvin and
meridian-marvin[bot], which the reviewer's land job posts every review as).
No other Bot anchors one, claude[bot] included
(design/untrusted-agent-job.md → Stop trusting claude[bot]); with no anchor
the section falls back to the last 8 comments. The dev agent's status
comment is dropped like the loops' markers, and the `trigger-time` input
(claude.yml's trigger-time snapshot, design/untrusted-agent-job.md → Context
prompt) leaves out every comment and thread comment created or edited at or
after it; without it nothing is filtered.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_helpers import lift_run, sh  # noqa: E402

ACTION = Path(__file__).resolve().parents[1] / ".github" / "actions" / "pr-feedback-context" / "action.yml"
THREADS = {"data": {"repository": {"pullRequest": {"reviewThreads": {"pageInfo": {"hasPreviousPage": False}, "nodes": []}}}}}
GH_STUB = """#!/usr/bin/env bash
case "$*" in
  *graphql*) cat "$STUB/threads.json" ;;
  *) cat "$STUB/comments.json" ;;
esac
"""


def comment(n, login, body, kind="User"):
    return {"user": {"login": login, "type": kind}, "created_at": f"2026-09-30T10:{n:02d}:00Z", "body": body}


def feedback(tmp_path, comments, threads=None, trigger_time="") -> str:
    stub = tmp_path / "stub"
    (stub / "bin").mkdir(parents=True)
    (stub / "bin" / "gh").write_text(GH_STUB)
    (stub / "bin" / "gh").chmod(0o755)
    (stub / "comments.json").write_text(json.dumps([comments]))  # one --slurp page
    (stub / "threads.json").write_text(json.dumps(threads or THREADS))
    out = tmp_path / "out.md"
    env = {"PATH": f"{stub / 'bin'}:{os.environ['PATH']}", "STUB": str(stub), "RUNNER_TEMP": str(tmp_path),
           "GH_TOKEN": "t", "REPO": "o/r", "PR": "7", "OUT": str(out), "TRIGGER_TIME": trigger_time}
    r = sh("bash", "-c", lift_run(ACTION.read_text(), "    - shell: bash"), check=False, env=env)
    assert r.returncode == 0, r.stderr
    return out.read_text()


def thread(review_author, review_type):
    # Ten human comments, the review, three more: anchored, the section keeps
    # the last 5 human comments before the review (h6..h10); unanchored, the
    # last 8 comments (h7 on).
    humans = [comment(i, "human", f"h{i}") for i in range(1, 11)]
    review = comment(20, review_author, "<!-- claude-review-comment -->\nFinding: x", review_type)
    return humans + [review] + [comment(30 + i, "human", f"h{11 + i}") for i in range(3)]


@pytest.mark.parametrize("login, kind", [("i-am-marvin", "User"), ("meridian-marvin[bot]", "Bot")])
def test_the_machine_accounts_review_anchors_the_round(tmp_path, login, kind):
    text = feedback(tmp_path, thread(login, kind))
    assert "\nh6\n" in text and "\nh5\n" not in text and "Finding: x" in text


@pytest.mark.parametrize("login", ["claude[bot]", "other-app[bot]"])
def test_no_other_bot_anchors_the_round(tmp_path, login):
    text = feedback(tmp_path, thread(login, "Bot"))
    assert "\nh6\n" not in text and "\nh7\n" in text and "Finding: x" in text


@pytest.mark.parametrize("login, kind", [("i-am-marvin", "User"), ("meridian-marvin[bot]", "Bot")])
def test_the_old_codex_footer_anchors_nothing(tmp_path, login, kind):
    # Codex reviews from before 2026-09-30 carried this footer and no marker;
    # they are no longer supported as anchors (decision: Ransom, 2026-09-30).
    comments = thread("human", "User")
    comments[10] = comment(20, login, "Review\n\n🤖 engine: codex", kind)
    text = feedback(tmp_path, comments)
    assert "\nh6\n" not in text and "\nh7\n" in text
    # The same comment with land's marker still anchors the round.
    comments[10] = comment(20, login, "Review\n\n🤖 engine: codex\n<!-- claude-review-comment -->", kind)
    assert "\nh6\n" in feedback(tmp_path / "b", comments)


def test_the_dev_agent_status_comment_is_dropped(tmp_path):
    comments = [comment(1, "human", "h1"),
                comment(2, "meridian-marvin[bot]", "🤖 **The dev agent is working on this** — [run](u)\n\n<!-- dev-agent-status -->", "Bot"),
                comment(3, "meridian-marvin[bot]", "**Claude finished @a's task** — [run](u)\n\nNo code changes.\n\n<!-- dev-agent-status -->", "Bot")]
    text = feedback(tmp_path, comments)
    assert "\nh1\n" in text and "dev agent is working" not in text and "No code changes" not in text


def tc(login, created, body, edited=None):
    return {"author": {"login": login}, "createdAt": created, "lastEditedAt": edited, "updatedAt": edited or created, "body": body}


def threads(*nodes):
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {"pageInfo": {"hasPreviousPage": False}, "nodes": [
        {"id": f"PRRT_{i}", "isResolved": False, "isOutdated": False, "path": "a.py", "line": i + 1, "originalLine": i + 1,
         "comments": {"pageInfo": {"hasNextPage": False}, "nodes": list(n)}} for i, n in enumerate(nodes)]}}}}}


T = "2026-09-30T10:20:00Z"


def snapshot_case():
    comments = [comment(1, "human", "before"),
                comment(2, "human", "edited later") | {"updated_at": "2026-09-30T10:25:00Z"},
                comment(30, "human", "after the trigger")]
    th = threads([tc("r", "2026-09-30T10:05:00Z", "old thread"), tc("r", "2026-09-30T10:21:00Z", "late reply")],
                 [tc("r", "2026-09-30T10:06:00Z", "edited thread", edited="2026-09-30T10:30:00Z")],
                 [tc("r", "2026-09-30T10:22:00Z", "new thread")])
    return comments, th


def test_the_trigger_time_drops_later_comments_and_thread_comments(tmp_path):
    comments, th = snapshot_case()
    text = feedback(tmp_path, comments, th, trigger_time=T)
    assert "\nbefore\n" in text
    for gone in ("edited later", "after the trigger", "late reply", "edited thread", "new thread", "PRRT_1", "PRRT_2"):
        assert gone not in text, gone
    assert "old thread" in text and "PRRT_0" in text


def test_without_a_trigger_time_nothing_is_filtered(tmp_path):
    # The loops' call: today's output. A trigger time after everything gives
    # the same text.
    comments, th = snapshot_case()
    text = feedback(tmp_path, comments, th)
    for kept in ("before", "edited later", "after the trigger", "late reply", "edited thread", "new thread"):
        assert kept in text, kept
    (tmp_path / "b").mkdir()
    assert feedback(tmp_path / "b", comments, th, trigger_time="2027-01-01T00:00:00Z") == text


def test_a_malformed_trigger_time_fails_the_step(tmp_path):
    with pytest.raises(AssertionError):
        feedback(tmp_path, [comment(1, "human", "h1")], trigger_time="yesterday")
