"""The `pr-feedback-context` composite's review anchor
(.github/actions/pr-feedback-context/action.yml), its step lifted and run
against a stub `gh`: a review round is anchored only on a marker-bearing
comment by the machine account's two logins (i-am-marvin and
meridian-marvin[bot], which the reviewer's land job posts every review as).
No other Bot anchors one, claude[bot] included
(design/untrusted-agent-job.md → Stop trusting claude[bot]); with no anchor
the section falls back to the last 8 comments.
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


def feedback(tmp_path, comments) -> str:
    stub = tmp_path / "stub"
    (stub / "bin").mkdir(parents=True)
    (stub / "bin" / "gh").write_text(GH_STUB)
    (stub / "bin" / "gh").chmod(0o755)
    (stub / "comments.json").write_text(json.dumps([comments]))  # one --slurp page
    (stub / "threads.json").write_text(json.dumps(THREADS))
    out = tmp_path / "out.md"
    env = {"PATH": f"{stub / 'bin'}:{os.environ['PATH']}", "STUB": str(stub), "RUNNER_TEMP": str(tmp_path),
           "GH_TOKEN": "t", "REPO": "o/r", "PR": "7", "OUT": str(out)}
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


def test_the_codex_footer_anchors_only_by_the_machine_account(tmp_path):
    comments = thread("human", "User")
    comments[10] = comment(20, "claude[bot]", "Review\n\n🤖 engine: codex", "Bot")
    assert "\nh6\n" not in feedback(tmp_path, comments)
    comments[10] = comment(20, "meridian-marvin[bot]", "Review\n\n🤖 engine: codex", "Bot")
    assert "\nh6\n" in feedback(tmp_path / "b", comments)
