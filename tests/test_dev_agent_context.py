"""The `dev-agent-context` composite's script (.github/scripts/dev_agent_context.py;
design/untrusted-agent-job.md → claude.yml: agent mode → Context prompt), run
in-process against a stub `gh` and fixture event payloads:

- issue runs carry the issue's comments posted before the run, oldest first,
  machine control comments dropped, bounded to the newest 30 and 40,000
  characters with an "N earlier comments omitted" line — for a comment that
  points at an earlier one ("implement the second option above"), a
  `labeled` trigger after a discussion and an `assigned` trigger;
- nothing created or edited at or after the trigger time reaches the file: a
  later comment, a later edit, a later review; the title and body come from
  the payload even when the live body was edited since;
- PR runs carry the review summaries and the pr-feedback-context file;
- a failed read fails after three attempts;
- images: only GitHub's signed attachment host is fetched, redirects are
  refused, the count and size caps hold, and a failed download leaves the
  link;
- the composite passes the time and the images directory through, and both
  engines' prompt steps in claude.yml read the file.
"""

import http.server
import importlib.util
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_helpers import lift_run, sh  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "dev_agent_context.py"
ACTION = ROOT / ".github" / "actions" / "dev-agent-context" / "action.yml"
CLAUDE_YML = ROOT / ".github" / "workflows" / "claude.yml"

spec = importlib.util.spec_from_file_location("dev_agent_context", SCRIPT)
dac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dac)

T = "2026-09-30T10:20:00Z"
# The stub answers from files in $STUB named after the endpoint; a missing
# file, or FAIL_ALL, is a failed read. Every call is logged.
GH_STUB = r"""#!/usr/bin/env bash
echo "$*" >>"$STUB/calls"
[ -z "${FAIL_ALL:-}" ] || { echo "HTTP 502" >&2; exit 1; }
case "$*" in
  *graphql*) f=reviews.json ;;
  *"issues/7/comments"*) f=comments.json ;;
  *"pulls/7/comments"*) f=review-comments.json ;;
  *"pulls/7/reviews"*) f=reviews-rest.json ;;
  *"issues/7"*) f=issue.json ;;
  *) echo "unexpected gh $*" >&2; exit 2 ;;
esac
[ -f "$STUB/$f" ] || { echo "no $f" >&2; exit 1; }
cat "$STUB/$f"
"""


def c(n, who, body, *, edited=None, kind="User"):
    created = f"2026-09-30T10:{n:02d}:00Z"
    return {"id": n, "user": {"login": who, "type": kind}, "created_at": created, "updated_at": edited or created, "body": body}


@pytest.fixture
def env(tmp_path, monkeypatch):
    stub = tmp_path / "stub"
    (stub / "bin").mkdir(parents=True)
    (stub / "bin" / "gh").write_text(GH_STUB)
    (stub / "bin" / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub / 'bin'}:{os.environ['PATH']}")
    monkeypatch.setenv("STUB", str(stub))
    monkeypatch.setattr(dac.time, "sleep", lambda s: None)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "event.json"))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "")
    return {"tmp": tmp_path, "stub": stub}


def write(env, name, data):
    (env["stub"] / name).write_text(json.dumps(data))


def run(env, event_name, event, *, trigger_time=T, feedback="", images_dir=""):
    (env["tmp"] / "event.json").write_text(json.dumps(event))
    os.environ["GITHUB_EVENT_NAME"] = event_name  # restored by the fixture's monkeypatch
    out = env["tmp"] / "context.md"
    rc = dac.main(["--repo", "o/r", "--out", str(out), "--trigger-time", trigger_time,
                   "--feedback", feedback, "--images-dir", images_dir])
    return rc, (out.read_text() if out.exists() else "")


def calls(env):
    f = env["stub"] / "calls"
    return f.read_text().splitlines() if f.exists() else []


ISSUE = {"number": 7, "title": "Add a flag", "body": "Payload body: option one, option two.", "user": {"login": "author"}}


def comment_event(body="@claude implement the second option above", n=20):
    return {"action": "created", "issue": ISSUE, "comment": c(n, "maintainer", body), "sender": {"login": "maintainer"}}


# --- issue runs ------------------------------------------------------------------


def test_an_issue_comment_run_carries_the_discussion_before_it(env):
    write(env, "comments.json", [[
        c(1, "author", "Two options: (1) a flag, (2) an env var."),
        c(2, "meridian-marvin[bot]", "🤖 **The dev agent is working on this** — [run](u)\n\n<!-- dev-agent-status -->", kind="Bot"),
        c(3, "maintainer", "@claude"),
        c(4, "i-am-marvin", "<!-- claude-review-summary -->\nverdict", kind="User"),
        c(5, "reviewer", "I prefer the second option."),
        c(6, "meridian-marvin[bot]", "**Claude finished @x's task**", kind="Bot"),
        c(20, "maintainer", "@claude implement the second option above"),  # the trigger itself
        c(21, "author", "written after the run started"),
        c(9, "author", "edited after the run started", edited="2026-09-30T10:30:00Z"),
    ]])
    rc, text = run(env, "issue_comment", comment_event())
    assert rc == 0
    assert text.startswith("REQUEST (a comment by maintainer at 2026-09-30T10:20:00Z on issue #7):\n@claude implement the second option above\n")
    assert "ISSUE #7: Add a flag\nPayload body: option one, option two.\n" in text
    hist = text[text.index("ISSUE DISCUSSION"):]
    assert hist.index("Two options") < hist.index("I prefer the second option")
    for gone in ("dev agent is working", "\n@claude\n", "verdict", "Claude finished", "written after", "edited after",
                 "implement the second option above"):
        assert gone not in hist, gone
    assert "omitted" not in hist
    # No live read of the issue itself: the payload's title and body are used.
    assert not any(" repos/o/r/issues/7" in f" {x}" and "comments" not in x for x in calls(env))


def test_the_history_is_bounded_with_a_note(env):
    write(env, "comments.json", [[c(i, "u", f"comment {i}") for i in range(1, 19)]])
    # 18 comments: all kept.
    _, text = run(env, "issue_comment", comment_event(n=59))
    assert "omitted" not in text and "comment 1\n" in text
    # 45 comments before the trigger: the newest 30, with the note.
    many = [{**c(0, "u", f"comment {i}"), "created_at": f"2026-09-30T09:{i:02d}:00Z", "updated_at": f"2026-09-30T09:{i:02d}:00Z"}
            for i in range(45)]
    write(env, "comments.json", [many])
    _, text = run(env, "issue_comment", comment_event())
    assert "(15 earlier comments omitted)" in text
    assert "comment 14\n" not in text and "comment 15\n" in text and "comment 44\n" in text
    # Long comments: the character bound drops the oldest ones too.
    big = [{**c(0, "u", f"big {i} " + "y" * 3000), "created_at": f"2026-09-30T09:{i:02d}:00Z",
            "updated_at": f"2026-09-30T09:{i:02d}:00Z"} for i in range(20)]
    write(env, "comments.json", [big])
    _, text = run(env, "issue_comment", comment_event())
    hist = text[text.index("ISSUE DISCUSSION"):]
    assert len(hist) < 41_000 and "big 19 " in hist and "big 0 " not in hist
    assert "earlier comments omitted)" in hist


def test_a_labeled_trigger_after_a_discussion(env):
    write(env, "comments.json", [[c(1, "author", "Maybe option two?"), c(2, "maintainer", "Yes, option two.")]])
    event = {"action": "labeled", "issue": ISSUE, "label": {"name": "claude"}, "sender": {"login": "maintainer"}}
    rc, text = run(env, "issues", event)
    assert rc == 0
    assert text.startswith("REQUEST: the issue below (labelled `claude` by maintainer); its title and body are the task.\n")
    assert "Maybe option two?" in text and "Yes, option two." in text


def test_an_assigned_trigger(env):
    write(env, "comments.json", [[c(1, "author", "context comment")]])
    event = {"action": "assigned", "issue": ISSUE, "assignee": {"login": "claude-helper"}, "sender": {"login": "maintainer"}}
    _, text = run(env, "issues", event)
    assert "REQUEST: the issue below (assigned to claude-helper by maintainer)" in text
    assert "context comment" in text


def test_a_failed_read_fails_after_three_attempts(env, monkeypatch, capsys):
    monkeypatch.setenv("FAIL_ALL", "1")
    rc, text = run(env, "issue_comment", comment_event())
    assert rc == 1 and text == ""
    assert sum("issues/7/comments" in x for x in calls(env)) == 3
    assert "failed after 3 attempts" in capsys.readouterr().out


def test_a_malformed_trigger_time_is_refused(env):
    assert run(env, "issue_comment", comment_event(), trigger_time="10:20")[0] == 1


# --- PR runs ---------------------------------------------------------------------


PR_ISSUE = {**ISSUE, "title": "PR title", "body": "PR payload body", "pull_request": {"url": "u"}}


def reviews(*nodes):
    return {"data": {"repository": {"pullRequest": {"reviews": {"nodes": list(nodes)}}}}}


def review(n, who, body, state="COMMENTED", edited=None):
    return {"author": {"login": who}, "body": body, "state": state,
            "submittedAt": f"2026-09-30T10:{n:02d}:00Z", "lastEditedAt": edited}


def test_a_pr_run_carries_reviews_before_the_trigger_and_the_thread(env):
    write(env, "reviews.json", reviews(review(1, "human", "Please rename x."), review(2, "human", ""),
                                       review(25, "human", "submitted after the run started"),
                                       review(3, "human", "edited later", edited="2026-09-30T10:40:00Z")))
    fb = env["tmp"] / "feedback.md"
    fb.write_text("RECENT PR COMMENTS (newest last; the review feedback is here):\n--- a at t:\nfix it\nREVIEW THREADS (...):\n")
    rc, text = run(env, "issue_comment", {**comment_event("@claude address the review"), "issue": PR_ISSUE}, feedback=str(fb))
    assert rc == 0
    assert "on pull request #7" in text and "PULL REQUEST #7: PR title\nPR payload body\n" in text
    assert "Please rename x." in text and "submitted after" not in text and "edited later" not in text
    assert text.index("PULL REQUEST REVIEWS") < text.index("RECENT PR COMMENTS") < text.index("REVIEW THREADS")
    assert "ISSUE DISCUSSION" not in text


def test_a_review_comment_trigger_names_its_line(env):
    write(env, "reviews.json", reviews())
    comment = {**c(20, "maintainer", "@claude fix this"), "path": "src/a.py", "line": 12, "diff_hunk": "@@ -1 +1 @@\n-a\n+b"}
    event = {"action": "created", "pull_request": {"number": 7, "title": "t", "body": "b"}, "comment": comment}
    _, text = run(env, "pull_request_review_comment", event)
    assert "inline review comment on src/a.py line 12, on this diff hunk:\n@@ -1 +1 @@" in text


def test_a_review_trigger(env):
    write(env, "reviews.json", reviews())
    event = {"action": "submitted", "pull_request": {"number": 7, "title": "t", "body": "b"},
             "review": {"user": {"login": "maintainer"}, "state": "changes_requested", "submitted_at": T, "body": "@claude do it"}}
    _, text = run(env, "pull_request_review", event)
    assert text.startswith("REQUEST (a review by maintainer, changes_requested, at 2026-09-30T10:20:00Z on pull request #7):\n@claude do it\n")


# --- images ------------------------------------------------------------------------


G1 = "11111111-2222-3333-4444-555555555555"
G2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100


def signed(guid):
    return f"https://private-user-images.githubusercontent.com/123/456-{guid}.png?jwt=abc"


class FakeOpener:
    def __init__(self, data):
        self.data = data
        self.opened = []

    def open(self, req, timeout):
        self.opened.append(req.full_url)
        data = self.data(req.full_url) if callable(self.data) else self.data
        if isinstance(data, Exception):
            raise data

        class Resp:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def read(s, n):
                return data[:n]
        return Resp()


def image_fixture(env, html_urls):
    write(env, "issue.json", {"body_html": " ".join(f'<img src="{u}">' for u in html_urls)})
    write(env, "comments.json", [[]])


def test_images_are_fetched_from_the_signed_host_and_named(env, tmp_path):
    image_fixture(env, [signed(G1), signed(G2).replace("private-user-images.githubusercontent.com", "evil.example")])
    text = f"see ![a](https://github.com/user-attachments/assets/{G1}) and <img src=\"https://github.com/user-attachments/assets/{G2}\">"
    opener = FakeOpener(PNG)
    out, saved = dac.fetch_images(text, "o/r", 7, False, str(tmp_path / "img"), opener)
    assert saved == 1 and opener.opened == [signed(G1)]
    assert f"![a]({tmp_path / 'img' / 'image-1.png'})" in out
    assert f"https://github.com/user-attachments/assets/{G2}" in out  # no signed URL on the host: the link stays
    assert (tmp_path / "img" / "image-1.png").read_bytes() == PNG


def test_a_failed_or_wrong_download_leaves_the_link(env, tmp_path):
    image_fixture(env, [signed(G1), signed(G2)])
    text = f"![a](https://github.com/user-attachments/assets/{G1}) ![b](https://github.com/user-attachments/assets/{G2})"
    opener = FakeOpener(lambda u: OSError("boom") if G1 in u else b"<html>not an image</html>")
    out, saved = dac.fetch_images(text, "o/r", 7, False, str(tmp_path / "img"), opener)
    assert saved == 0 and out == text


def test_the_image_caps_hold(env, tmp_path, monkeypatch):
    guids = [f"{i:08d}-2222-3333-4444-555555555555" for i in range(25)]
    image_fixture(env, [signed(g) for g in guids])
    text = " ".join(f"![i](https://github.com/user-attachments/assets/{g})" for g in guids)
    out, saved = dac.fetch_images(text, "o/r", 7, False, str(tmp_path / "a"), FakeOpener(PNG))
    assert saved == dac.MAX_IMAGES
    # Per-image and total byte caps.
    monkeypatch.setattr(dac, "MAX_IMAGE_BYTES", 150)
    monkeypatch.setattr(dac, "MAX_IMAGE_TOTAL", 250)
    big = PNG + b"0" * 100
    out, saved = dac.fetch_images(text, "o/r", 7, False, str(tmp_path / "b"),
                                  FakeOpener(lambda u: big if guids[0] in u else PNG))
    assert saved == 2  # the first is over the per-image cap; two fit the total
    assert f"assets/{guids[0]}" in out


def test_download_refuses_other_hosts_and_redirects(tmp_path):
    with pytest.raises(ValueError):
        dac.download("https://example.com/123/456-x.png", 10, FakeOpener(PNG))
    with pytest.raises(ValueError):
        dac.download("http://private-user-images.githubusercontent.com/1/2-x.png", 10, FakeOpener(PNG))

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    opener = urllib.request.build_opener(dac.NoRedirect)
    with pytest.raises(urllib.error.HTTPError, match="redirect .* refused"):
        opener.open(f"http://127.0.0.1:{srv.server_port}/x", timeout=5)
    srv.server_close()


def test_no_images_dir_downloads_nothing(env):
    body = f"![a](https://github.com/user-attachments/assets/{G1})"
    write(env, "comments.json", [[]])
    rc, text = run(env, "issue_comment", comment_event(body))
    assert rc == 0 and f"assets/{G1}" in text
    assert not any("full+json" in x for x in calls(env))


# --- the composite and its callers --------------------------------------------------


def test_the_composite_passes_the_time_and_the_images_dir(tmp_path):
    text = ACTION.read_text()
    assert "uses: meridianlabs-ai/agents/.github/actions/pr-feedback-context@main" in text
    assert "        trigger-time: ${{ inputs.trigger-time }}\n" in text
    script = lift_run(text, "    - shell: bash")
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "python3").write_text('#!/bin/bash\nprintf "%s\\n" "$@" >"$ARGS"\n')
    (stub / "python3").chmod(0o755)
    rt = tmp_path / "temp"
    (rt / "agent-context").mkdir(parents=True)
    (rt / "agent-context" / "stale.png").write_text("x")
    envv = {"PATH": f"{stub}:{os.environ['PATH']}", "RUNNER_TEMP": str(rt), "ARGS": str(tmp_path / "args"),
            "GH_TOKEN": "t", "REPO": "o/r", "TRIGGER_TIME": T, "OUT": str(rt / "ctx.md"),
            "IMAGES_DIR": str(rt / "agent-context"), "FEEDBACK": "", "SCRIPT": str(SCRIPT)}
    r = sh("bash", "-c", script, check=False, env=envv)
    assert r.returncode == 0, r.stderr
    args = (tmp_path / "args").read_text().splitlines()
    assert args[args.index("--trigger-time") + 1] == T and args[args.index("--images-dir") + 1] == str(rt / "agent-context")
    assert list((rt / "agent-context").iterdir()) == []  # re-created empty
    r = sh("bash", "-c", script, check=False, env={**envv, "IMAGES_DIR": str(tmp_path / "outside")})
    assert r.returncode != 0 and "not under" in r.stdout


def test_both_engines_build_their_prompts_from_the_context():
    text = CLAUDE_YML.read_text()
    uses = text.count("uses: meridianlabs-ai/agents/.github/actions/dev-agent-context@main")
    assert uses == 2
    assert text.count("          trigger-time: ${{ needs.gate.outputs.trigger_time }}\n") == 2
    # Only the Claude job downloads images, and binds them read-only.
    assert text.count("          images-dir: ${{ runner.temp }}/agent-context\n") == 1
    assert "          context-dir: ${{ runner.temp }}/agent-context\n" in text
    # The codex job no longer reads the PR body live.
    assert "pr view \"$NUM\" --repo \"$REPO\" \\\n                --json title,body" not in text
    assert text.count('cat "$RUNNER_TEMP/dev-agent-context.md"') == 2
