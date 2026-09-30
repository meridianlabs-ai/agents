"""Tests for skills/post-upstream-review/gather.sh and post.sh.

Claude Security 4773885: the skill relayed "the latest review-findings
comment" on a public proxy issue by recency, so anyone's findings-shaped
comment could be posted upstream under the maintainer's name. gather.sh
chooses only a comment by the machine account or a write-access
collaborator, ignores every other one (for the relay and for the
double-relay check), and stops naming the author when the newest
findings-shaped comment is untrusted. Claude Security 4773877: the posting
template pasted review text into a double-quoted `gh api -f body=...`;
post.sh sends a file with `gh api --input`, after a field check, the
trigger-phrase rewrite, the disclosure footer and a reference check by a
stand-in for GitHub's renderer. Both run against a stub `gh` that answers
from fixtures and logs every call. Run with `python3 -m pytest` from the
repo root.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATHER = ROOT / "skills" / "post-upstream-review" / "gather.sh"
POST = ROOT / "skills" / "post-upstream-review" / "post.sh"

FORK = "meridianlabs-ai/inspect_ai"
UPSTREAM = "UKGovernmentBEIS/inspect_ai"
N, M = 900, 5360
HEAD = "a" * 40
ME = "ransomr"
FOOTER = "*This review was AI-generated from findings a maintainer chose to relay; the maintainer did not review its wording.*"
OLD_FOOTER = "*This review was AI-generated, and reviewed by a maintainer before posting.*"
FINDINGS = "Changes needed.\n\n- **blocking** `src/x.py:12`: the loop never ends.\n"

GH_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB/calls"
args="$*"
jqexpr() { while [ $# -gt 0 ]; do [ "$1" = "--jq" ] && { printf '%s' "$2"; return; }; shift; done; }
case "$args" in
  "api --paginate repos/meridianlabs-ai/inspect_ai/issues/"*"/comments"*)
    [ -f "$STUB/comments_fail" ] && { echo "gh: HTTP 502" >&2; exit 1; }
    jq -r "$(jqexpr "$@")" "$STUB/comments.json" ;;
  "api repos/meridianlabs-ai/inspect_ai/issues/"[0-9]*) cat "$STUB/issue.json" ;;
  "api repos/UKGovernmentBEIS/inspect_ai/pulls/"[0-9]*)
    case "$args" in
      *"/reviews -X POST --input "*)
        f=$(sed -E 's#.*--input ([^ ]+).*#\1#' <<<"$args"); cp "$f" "$STUB/posted.json"
        echo "https://github.com/UKGovernmentBEIS/inspect_ai/pull/5360#pullrequestreview-77" ;;
      *) cat "$STUB/pr.json" ;;
    esac ;;
  "api --paginate repos/UKGovernmentBEIS/inspect_ai/pulls/"*"/reviews"*)
    jq -r "$(jqexpr "$@")" "$STUB/reviews.json" ;;
  api\ repos/*/collaborators/*/permission*)
    login=$(sed -E 's#^api repos/[^/]+/[^/]+/collaborators/([^/]+)/permission.*#\1#' <<<"$args")
    perm=$(grep -E "^$login=" "$STUB/perms" 2>/dev/null | head -1 | cut -d= -f2)
    echo "${perm:-read}" ;;
  "api user --jq .login") echo "ransomr" ;;
  "issue comment "*)
    [ -f "$STUB/comment_fail" ] && { echo "gh: HTTP 502" >&2; exit 1; }
    f=$(sed -E 's#.*--body-file ([^ ]+).*#\1#' <<<"$args"); cp "$f" "$STUB/audit.md" ;;
  "api graphql -f query=query"*) echo "PVTI_item" ;;
  "api graphql -f query=mutation"*) ;;
  "api markdown --input -")
    cat >"$STUB/md_req.json"
    jq -c . "$STUB/md_req.json" >>"$STUB/md_in.jsonl"
    [ -f "$STUB/markdown_fail" ] && { echo "gh: HTTP 502" >&2; exit 1; }
    python3 - "$STUB/md_req.json" <<'PY'
import json, re, sys
req = json.load(open(sys.argv[1]))
text, ctx = req["text"], req["context"]
text = re.sub(r"^```.*?^```", "", text, flags=re.S | re.M)
text = re.sub(r"`[^`]*`", "", text)
for m in re.finditer(r"(?<![\w/&])#(\d+)\b|\b([\w.-]+/[\w.-]+)#(\d+)\b"
                     r"|https://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)", text):
    repo, num = (ctx, m[1]) if m[1] else (m[2], m[3]) if m[2] else (m[4], m[5])
    print(f'<a class="issue-link" data-url="https://github.com/{repo}/issues/{num}">#{num}</a>')
PY
    ;;
  *) echo "stub gh: unexpected call: $args" >&2; exit 97 ;;
esac
"""

MARVIN = {"login": "i-am-marvin", "type": "User"}
APP = {"login": "meridian-marvin[bot]", "type": "Bot"}


def comment(cid, user, body, at, updated=None):
    return {
        "id": cid,
        "updated_at": updated or at,
        "html_url": f"https://github.com/{FORK}/issues/{N}#issuecomment-{cid}",
        "user": user if isinstance(user, dict) else {"login": user, "type": "User"},
        "created_at": at,
        "body": body,
    }


def review(login, at, rid=1):
    return {"user": {"login": login}, "submitted_at": at,
            "html_url": f"https://github.com/{UPSTREAM}/pull/{M}#pullrequestreview-{rid}"}


class Stub:
    def __init__(self, tmp_path, *, comments, reviews=(), perms=(), author=APP, labels=("External",),
                 body=None, state="open", head=HEAD):
        self.tmp = tmp_path
        self.dir = tmp_path / "stub"
        self.dir.mkdir(parents=True)
        gh = tmp_path / "bin" / "gh"
        gh.parent.mkdir(parents=True)
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        self.issue(author=author, labels=labels, body=body)
        self.set_comments(comments)
        (self.dir / "reviews.json").write_text(json.dumps(list(reviews)))
        (self.dir / "perms").write_text("".join(f"{k}={v}\n" for k, v in perms))
        self.set_pr(state=state, head=head)
        self.out = tmp_path / "out"
        self.env = {**os.environ, "PATH": f"{gh.parent}:{os.environ['PATH']}", "STUB": str(self.dir)}

    def issue(self, *, author, labels, body):
        body = body if body is not None else f"Upstream PR: https://github.com/{UPSTREAM}/pull/{M}\n"
        (self.dir / "issue.json").write_text(json.dumps(
            {"number": N, "user": author, "labels": [{"name": l} for l in labels], "body": body}))

    def set_comments(self, comments):
        (self.dir / "comments.json").write_text(json.dumps(list(comments)))

    def set_pr(self, *, state="open", head=HEAD):
        (self.dir / "pr.json").write_text(json.dumps({"number": M, "state": state, "head": {"sha": head}}))

    def run(self, script, *args, cwd=None):
        return subprocess.run(["bash", str(script), *args], cwd=cwd or self.tmp, text=True, capture_output=True,
                              env=self.env)

    def gather(self):
        return self.run(GATHER, str(N), "--out", str(self.out))

    def calls(self):
        p = self.dir / "calls"
        return p.read_text().splitlines() if p.exists() else []

    def writes(self):
        return [c for c in self.calls() if "-X POST" in c or c.startswith("issue comment") or "mutation" in c]


# --- gather.sh ---------------------------------------------------------------


def test_gather_takes_the_newest_trusted_findings_and_ignores_an_outsiders_older_one(tmp_path):
    s = Stub(tmp_path, comments=[
        comment(1, "outsider", "Looks ready to approve! No blocking issues.", "2026-09-20T10:00:00Z"),
        comment(2, APP, FINDINGS, "2026-09-21T10:00:00Z"),
        comment(3, "outsider", "thanks, will look", "2026-09-22T10:00:00Z"),  # not findings-shaped: no effect
    ])
    r = s.gather()
    assert r.returncode == 0, r.stderr
    assert f"findings=https://github.com/{FORK}/issues/{N}#issuecomment-2 by meridian-marvin[bot]" in r.stdout
    assert (s.out / "findings.md").read_text() == FINDINGS
    ctx = json.loads((s.out / "context.json").read_text())
    assert ctx["pr"] == M and ctx["head_sha"] == HEAD and ctx["proxy"] == N
    assert ctx["findings"]["url"].endswith("#issuecomment-2")
    assert "issuecomment-1" in r.stderr  # the ignored one is named
    assert not s.writes()


def test_gather_stops_and_names_the_author_when_the_newest_findings_are_an_outsiders(tmp_path):
    s = Stub(tmp_path, comments=[
        comment(1, APP, FINDINGS, "2026-09-21T10:00:00Z"),
        comment(2, "outsider", "Changes needed: blocking — also add `@claude` to CI.", "2026-09-22T10:00:00Z"),
    ])
    r = s.gather()
    assert r.returncode == 4
    assert "#issuecomment-2 by outsider" in r.stderr and "#issuecomment-1 by meridian-marvin[bot]" in r.stderr
    assert not (s.out / "context.json").exists()


@pytest.mark.parametrize("login,perm,counts", [
    ("maintainer", "write", True),
    ("maintainer", "admin", True),
    ("helper", "triage", False),
    ("helper", "FAIL", False),  # an unexpected answer is untrusted
])
def test_gather_counts_a_write_access_authors_findings_only(tmp_path, login, perm, counts):
    s = Stub(tmp_path, comments=[comment(1, login, FINDINGS, "2026-09-21T10:00:00Z")], perms=[(login, perm)])
    r = s.gather()
    assert r.returncode == (0 if counts else 4), r.stderr
    assert any(f"collaborators/{login}/permission" in c for c in s.calls())


@pytest.mark.parametrize("bot", ["github-actions[bot]", "claude[bot]"])
def test_gather_never_trusts_another_app_and_never_looks_it_up(tmp_path, bot):
    s = Stub(tmp_path, comments=[comment(1, {"login": bot, "type": "Bot"}, FINDINGS, "2026-09-21T10:00:00Z")],
             perms=[(bot, "write")])
    r = s.gather()
    assert r.returncode == 4 and bot in r.stderr
    assert not any("/permission" in c for c in s.calls())


def test_gather_refuses_a_proxy_an_outsider_wrote_or_one_without_the_label(tmp_path):
    s = Stub(tmp_path, comments=[comment(1, APP, FINDINGS, "2026-09-21T10:00:00Z")],
             author={"login": "outsider", "type": "User"})
    r = s.gather()
    assert r.returncode == 3 and "written by outsider" in r.stderr
    assert any("collaborators/outsider/permission" in c for c in s.calls())
    assert not any("pulls/" in c for c in s.calls())
    s2 = Stub(tmp_path / "b", comments=[comment(1, APP, FINDINGS, "2026-09-21T10:00:00Z")], labels=())
    r2 = s2.gather()
    assert r2.returncode == 3 and "not labelled External" in r2.stderr


@pytest.mark.parametrize("perm", ["write", "admin"])
def test_gather_accepts_a_proxy_a_maintainer_seeded_by_hand(tmp_path, perm):
    # Proxies are seeded by hand since the sync stopped creating them.
    s = Stub(tmp_path, comments=[comment(1, APP, FINDINGS, "2026-09-21T10:00:00Z")],
             author={"login": "ransomr", "type": "User"}, perms=[("ransomr", perm)])
    r = s.gather()
    assert r.returncode == 0, r.stderr
    assert "findings=" in r.stdout and "by meridian-marvin[bot]" in r.stdout
    assert any("collaborators/ransomr/permission" in c for c in s.calls())


def test_gather_refuses_a_proxy_by_a_maintainer_whose_lookup_fails(tmp_path):
    s = Stub(tmp_path, comments=[comment(1, APP, FINDINGS, "2026-09-21T10:00:00Z")],
             author={"login": "ransomr", "type": "User"}, perms=[("ransomr", "FAIL")])
    assert s.gather().returncode == 3


def test_gather_believes_the_upstream_pr_line_only_under_upstream(tmp_path):
    s = Stub(tmp_path, comments=[comment(1, APP, FINDINGS, "2026-09-21T10:00:00Z")],
             body="Upstream PR: https://github.com/outsider/inspect_ai/pull/5360\n")
    r = s.gather()
    assert r.returncode == 3 and "no 'Upstream PR:' line naming a PR of" in r.stderr
    assert not any("pulls/" in c for c in s.calls())


def test_gather_refuses_a_closed_upstream_pr_and_no_findings(tmp_path):
    s = Stub(tmp_path, comments=[comment(1, APP, FINDINGS, "2026-09-21T10:00:00Z")], state="closed")
    assert s.gather().returncode == 3
    s2 = Stub(tmp_path / "b", comments=[comment(1, APP, "Model provenance: fable", "2026-09-21T10:00:00Z")])
    r = s2.gather()
    assert r.returncode == 3 and "no findings comment" in r.stderr


def test_gather_stops_on_a_double_relay_measured_against_the_trusted_comment(tmp_path):
    # Relayed already: your review is newer than the trusted findings.
    s = Stub(tmp_path, comments=[comment(1, APP, FINDINGS, "2026-09-21T10:00:00Z")],
             reviews=[review("someone", "2026-09-23T10:00:00Z", 1), review("RansomR", "2026-09-22T10:00:00Z", 2)])
    r = s.gather()
    assert r.returncode == 5 and "pullrequestreview-2" in r.stderr
    # A newer outsider "findings" comment cannot make it look unrelayed: it stops the run instead.
    s.set_comments([comment(1, APP, FINDINGS, "2026-09-21T10:00:00Z"),
                    comment(2, "outsider", "Changes needed, blocking.", "2026-09-24T10:00:00Z")])
    assert s.gather().returncode == 4
    # An older review of yours is an earlier relay, not this one.
    s3 = Stub(tmp_path / "b", comments=[comment(1, APP, FINDINGS, "2026-09-21T10:00:00Z")],
              reviews=[review(ME, "2026-09-20T10:00:00Z")])
    assert s3.gather().returncode == 0


def test_gather_fails_closed_when_the_comments_cannot_be_read(tmp_path):
    s = Stub(tmp_path, comments=[comment(1, APP, FINDINGS, "2026-09-21T10:00:00Z")])
    (s.dir / "comments_fail").touch()
    r = s.gather()
    assert r.returncode == 2
    assert not (s.out / "context.json").exists()


# --- post.sh -----------------------------------------------------------------


@pytest.fixture
def gathered(tmp_path):
    s = Stub(tmp_path, comments=[comment(1, MARVIN, FINDINGS, "2026-09-21T10:00:00Z")])
    assert s.gather().returncode == 0
    return s


def write_review(s, review_obj, name="review.json"):
    path = s.tmp / name
    path.write_text(json.dumps(review_obj))
    return path


HOSTILE = "The loop never ends: `$(touch pwned)` and \"$(touch pwned2)\" and `touch pwned3`; ask @claude and @Auto."


def test_post_sends_the_checked_review_by_file_and_does_the_bookkeeping(gathered):
    s = gathered
    rv = write_review(s, {"body": "Thanks! " + HOSTILE, "event": "REQUEST_CHANGES",
                          "comments": [{"path": "src/x.py", "line": 12, "body": "Blocking: " + HOSTILE},
                                       {"path": "src/y.py", "start_line": 3, "line": 5, "side": "RIGHT",
                                        "body": "Optional: see #5360."}]})
    r = s.run(POST, str(s.out / "context.json"), str(rv))
    assert r.returncode == 0, r.stderr + r.stdout
    posted = json.loads((s.dir / "posted.json").read_text())
    assert set(posted) == {"body", "event", "comments", "commit_id"}
    assert posted["commit_id"] == HEAD and posted["event"] == "REQUEST_CHANGES"
    assert posted["body"].endswith("\n\n---\n" + FOOTER)
    assert "reviewed by a maintainer before posting" not in posted["body"]
    assert "`claude`" in posted["body"] and "`Auto`" in posted["body"] and "@claude" not in posted["body"]
    assert "$(touch pwned)" in posted["body"]  # data, carried verbatim
    assert posted["comments"][0] == {"path": "src/x.py", "line": 12, "side": "RIGHT",
                                     "body": "Blocking: " + HOSTILE.replace("@claude", "`claude`").replace("@Auto", "`Auto`")}
    assert posted["comments"][1]["start_line"] == 3 and posted["comments"][1]["start_side"] == "RIGHT"
    assert not any((s.tmp / f).exists() for f in ("pwned", "pwned2", "pwned3"))
    # Posted with --input, never -f fields.
    post_calls = [c for c in s.calls() if "-X POST" in c]
    assert len(post_calls) == 1 and "--input" in post_calls[0] and " -f " not in post_calls[0]
    audit = (s.dir / "audit.md").read_text()
    assert "pullrequestreview-77" in audit and f"issues/{N}#issuecomment-1" in audit and "(2 inline)" in audit
    assert any("mutation" in c and "o=39c05a50" in c for c in s.calls())
    assert "review as it will be posted" in r.stdout and "OK review=" in r.stdout


@pytest.mark.parametrize("ending", ["", "\n\n---\n" + FOOTER, "\n\n---\n" + OLD_FOOTER + "\n"],
                         ids=["no-footer", "new-footer", "old-footer"])
def test_post_ends_the_body_with_the_footer_once_and_drops_the_old_one(gathered, ending):
    s = gathered
    rv = write_review(s, {"body": "Thanks for the PR." + ending, "event": "COMMENT"})
    assert s.run(POST, str(s.out / "context.json"), str(rv)).returncode == 0
    body = json.loads((s.dir / "posted.json").read_text())["body"]
    assert body == "Thanks for the PR.\n\n---\n" + FOOTER


def test_the_skill_quotes_the_footer_the_script_appends():
    assert FOOTER in (POST.parent / "SKILL.md").read_text()
    assert f"FOOTER=$'---\\n{FOOTER}'" in POST.read_text()


def test_post_dry_run_prints_the_review_and_posts_nothing(gathered):
    s = gathered
    rv = write_review(s, {"body": "Looks close. @review please", "event": "COMMENT"})
    r = s.run(POST, str(s.out / "context.json"), str(rv), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "| Looks close. `review` please" in r.stdout and "AI-generated" in r.stdout
    assert not s.writes() and not (s.dir / "posted.json").exists()


@pytest.mark.parametrize("bad,why", [
    ({"body": "ok", "event": "APPROVE"}, "never approves"),
    ({"body": "", "event": "COMMENT"}, "non-empty"),
    ({"body": "ok", "event": "COMMENT", "commit_id": "b" * 40}, "unexpected key"),
    ({"body": "ok", "event": "COMMENT", "comments": [{"path": "a", "line": 0, "body": "x"}]}, "positive integer"),
    ({"body": "ok", "event": "COMMENT", "comments": [{"path": "a", "line": 2, "body": "x", "position": 1}]}, "unexpected inline"),
    ({"body": "ok", "event": "COMMENT", "comments": [{"path": "a", "line": 2, "start_line": 2, "body": "x"}]}, "below line"),
    ({"body": "ok", "event": "COMMENT", "comments": [{"path": "a", "line": 2, "side": "UP", "body": "x"}]}, "RIGHT or LEFT"),
    (["not", "an", "object"], "not a JSON object"),
])
def test_post_refuses_a_malformed_review_before_anything_is_sent(gathered, bad, why):
    s = gathered
    r = s.run(POST, str(s.out / "context.json"), str(write_review(s, bad)))
    assert r.returncode == 1 and why in r.stderr, r.stderr
    assert not s.writes()


def test_post_refuses_an_upstream_reference_unless_allowed(gathered):
    s = gathered
    rv = write_review(s, {"body": "Same bug as #1234 and UKGovernmentBEIS/inspect_ai#99; see `#77` in code.",
                          "event": "COMMENT",
                          "comments": [{"path": "a.py", "line": 1, "body": "Duplicates https://github.com/UKGovernmentBEIS/inspect_ai/issues/55"}]})
    r = s.run(POST, str(s.out / "context.json"), str(rv))
    assert r.returncode == 4
    assert "review body: #99 #1234" in r.stderr and "inline comment a.py:1: #55" in r.stderr
    assert "#77" not in r.stderr  # code is not a reference
    assert not s.writes()
    r = s.run(POST, str(s.out / "context.json"), str(rv), "--allow-ref", "1234", "--allow-ref", "99", "--allow-ref", "55")
    assert r.returncode == 0, r.stderr
    # Every text was rendered in upstream's context.
    reqs = [json.loads(l) for l in (s.dir / "md_in.jsonl").read_text().splitlines()]
    assert {q["context"] for q in reqs} == {UPSTREAM}


def test_post_refuses_when_the_render_fails(gathered):
    s = gathered
    (s.dir / "markdown_fail").touch()
    r = s.run(POST, str(s.out / "context.json"), str(write_review(s, {"body": "ok", "event": "COMMENT"})))
    assert r.returncode == 4 and "could not render" in r.stderr
    assert not s.writes()


def test_post_refuses_when_the_findings_or_the_head_changed_since_gather(gathered):
    s = gathered
    rv = write_review(s, {"body": "ok", "event": "COMMENT"})
    s.set_comments([comment(1, MARVIN, FINDINGS, "2026-09-21T10:00:00Z"),
                    comment(2, MARVIN, "Needs discussion: blocking.", "2026-09-25T10:00:00Z")])
    r = s.run(POST, str(s.out / "context.json"), str(rv))
    assert r.returncode == 3 and "findings.url changed" in r.stderr
    # The same comment, edited in place since gather.sh read it.
    s.set_comments([comment(1, MARVIN, FINDINGS + "- **blocking**: also X\n", "2026-09-21T10:00:00Z",
                            updated="2026-09-25T10:00:00Z")])
    r = s.run(POST, str(s.out / "context.json"), str(rv))
    assert r.returncode == 3 and "findings.updated_at changed" in r.stderr
    s.set_comments([comment(1, MARVIN, FINDINGS, "2026-09-21T10:00:00Z")])
    s.set_pr(head="c" * 40)
    r = s.run(POST, str(s.out / "context.json"), str(rv))
    assert r.returncode == 3 and "head_sha changed" in r.stderr
    s.set_pr(state="closed")
    r = s.run(POST, str(s.out / "context.json"), str(rv))
    assert r.returncode == 3 and "not open" in r.stderr
    assert not s.writes()


def test_post_a_second_time_is_refused_as_a_double_relay(gathered):
    s = gathered
    rv = write_review(s, {"body": "ok", "event": "COMMENT"})
    assert s.run(POST, str(s.out / "context.json"), str(rv)).returncode == 0
    (s.dir / "reviews.json").write_text(json.dumps([review(ME, "2026-09-26T10:00:00Z")]))
    r = s.run(POST, str(s.out / "context.json"), str(rv))
    assert r.returncode == 3 and "relayed already" in r.stderr
    assert len([c for c in s.calls() if "-X POST" in c]) == 1


def test_post_reports_bookkeeping_that_failed_after_posting(gathered):
    s = gathered
    (s.dir / "comment_fail").touch()
    r = s.run(POST, str(s.out / "context.json"), str(write_review(s, {"body": "ok", "event": "COMMENT"})))
    assert r.returncode == 6 and "audit comment" in r.stderr
    assert (s.dir / "posted.json").exists()
