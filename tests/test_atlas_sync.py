"""Tests for .github/scripts/atlas_sync.py — the author checks behind two of
its writes.

Security scan 2026-09-04, findings 4121985 and 4121986: the hourly sync
believed comment text (retrigger_stale_handbacks) and issue-body text
(companion_pr) from anyone. Every `gh` call goes through the module's `gh()`
wrapper, so the tests stand in a fake for it and route each call by the
arguments. Run with `python3 -m pytest` from the repo root.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "atlas_sync.py"

spec = importlib.util.spec_from_file_location("atlas_sync", SCRIPT)
atlas = importlib.util.module_from_spec(spec)
sys.modules["atlas_sync"] = atlas
spec.loader.exec_module(atlas)

FORK = atlas.FORK
TS_MONO = atlas.TS_MONO
MARVIN = atlas.MACHINE_ACCOUNT
MARVIN_BOT = atlas.MACHINE_BOT  # the machine account's Phase 2 GitHub App login
REVIEWER_BOT = "claude[bot]"
OLD = "2020-01-01T00:00:00Z"  # far older than STALE_MINUTES
VERDICT = "🔎 Review complete. <!-- claude-review-summary --><!-- claude-review-verdict:suggestions -->"


class FakeGH:
    """Stand-in for atlas.gh: the first route whose predicate matches answers.

    A response is a JSON-able value (dumped), a str (returned verbatim) or
    an exception (raised, like a failed `gh`). Unrouted calls fail the test.
    """

    def __init__(self):
        self.calls = []
        self.routes = []

    def route(self, pred, response):
        self.routes.append((pred, response))

    def __call__(self, *args):
        self.calls.append(args)
        for pred, resp in self.routes:
            if pred(args):
                if isinstance(resp, Exception):
                    raise resp
                return resp if isinstance(resp, str) else json.dumps(resp)
        raise AssertionError(f"unrouted gh call: {args}")

    def matching(self, pred):
        return [c for c in self.calls if pred(c)]


def has(sub):
    return lambda args: any(sub in a for a in args)


def is_permission_lookup(args):
    return args[0] == "api" and "/collaborators/" in args[1]


def is_revival(args):
    return args[0] == "api" and args[1].endswith("/comments") and args[2] == "-f"


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGH()
    monkeypatch.setattr(atlas, "gh", fake)
    monkeypatch.setattr(atlas, "actions", [])
    monkeypatch.setattr(atlas, "_permission_cache", {}, raising=False)
    return fake


def permission(gh, login, perm, role=None):
    gh.route(
        has(f"/collaborators/{login}/permission"),
        {"permission": perm, "role_name": role or perm},
    )


# ------------------------------------------------------------ trusted_author


@pytest.mark.parametrize(
    "login, association, expected",
    [
        (MARVIN, None, True),
        (MARVIN_BOT, None, True),  # by name: an App's permission reads `none`
        (MARVIN_BOT, "NONE", True),
        ("colleague", "OWNER", True),
        ("colleague", "MEMBER", True),
        ("colleague", "COLLABORATOR", True),
        ("github-actions[bot]", "MEMBER", False),  # never, whatever the payload says
        ("", "OWNER", False),
    ],
)
def test_trusted_author_decides_without_a_lookup(gh, login, association, expected):
    assert atlas.trusted_author(login, FORK, association) is expected
    assert gh.calls == []


@pytest.mark.parametrize(
    "perm, role, expected",
    [
        ("admin", "admin", True),
        ("write", "write", True),
        ("write", "maintain", True),  # maintain surfaces as role_name
        ("read", "read", False),  # every account on a public repo
        ("none", "none", False),  # bots (the endpoint answers 200)
    ],
)
def test_trusted_author_looks_up_write_access(gh, perm, role, expected):
    permission(gh, "someone", perm, role)
    assert atlas.trusted_author("someone", FORK, "CONTRIBUTOR") is expected
    assert len(gh.matching(is_permission_lookup)) == 1


def test_trusted_author_fails_closed_on_a_broken_lookup(gh):
    gh.route(has("/collaborators/"), RuntimeError("gh api ...: HTTP 502"))
    assert atlas.trusted_author("someone", FORK, "NONE") is False


def test_trusted_author_caches_the_lookup_per_login_and_repo(gh):
    permission(gh, "someone", "write")
    assert atlas.trusted_author("someone", FORK) is True
    assert atlas.trusted_author("someone", FORK, "NONE") is True
    assert len(gh.matching(is_permission_lookup)) == 1
    assert atlas.trusted_author("someone", TS_MONO) is True
    assert len(gh.matching(is_permission_lookup)) == 2


# ------------------------------------------------- retrigger_stale_handbacks


def comment(body, login=MARVIN, association="MEMBER", cid=1, created=OLD):
    return {
        "id": cid,
        "body": body,
        "created_at": created,
        "user": {"login": login},
        "author_association": association,
    }


def outsider(body, cid=2):
    return comment(body, login="drive-by", association="NONE", cid=cid)


def auto_pr(gh, comments):
    """One open auto-labeled fork PR #7: no eyes ack, no live run."""
    gh.route(lambda a: a[:2] == ("pr", "list"), [{"number": 7, "title": "Fix it"}])
    gh.route(has(f"repos/{FORK}/issues/7/comments?per_page=100"), [comments])
    gh.route(has("/reactions"), "0")
    gh.route(has("actions/runs?status="), "[]")
    gh.route(is_revival, "")


def revivals(gh):
    return gh.matching(is_revival)


def test_the_trusted_set_is_the_machine_account_under_both_logins():
    assert atlas.TRUSTED_LOGINS == frozenset({"i-am-marvin", "meridian-marvin[bot]"})
    assert atlas.NEVER_TRUSTED == frozenset({"github-actions[bot]"})


@pytest.mark.parametrize("login", ["foo[bot]", REVIEWER_BOT])
def test_other_apps_fall_through_to_the_lookup_and_are_refused_on_none(gh, login):
    permission(gh, login, "none")
    assert not atlas.trusted_author(login, FORK, "NONE")
    assert len(gh.matching(is_permission_lookup)) == 1


@pytest.mark.parametrize("login, association", [(MARVIN, "MEMBER"), (MARVIN_BOT, "NONE")])
def test_revives_a_stale_machine_account_handback(gh, login, association):
    # Under either login: the App's comments carry no MEMBER association and
    # its permission cannot be looked up, so the revival is by name.
    auto_pr(gh, [comment("@review", login=login, association=association)])
    atlas.retrigger_stale_handbacks()
    (post,) = revivals(gh)
    assert post[3].startswith("body=@review — re-triggered by the Atlas sync")
    assert gh.matching(is_permission_lookup) == []
    assert any("review re-triggered" in a for a in atlas.actions)


def test_does_not_revive_a_reviewer_bot_verdict(gh):
    # On the fork the suggestions verdict is authored by the reviewer's GitHub
    # App, whose collaborator permission is `none`: it is not in TRUSTED_LOGINS
    # (decision: Ransom, 2026-09-15), so a stale verdict is left alone — only
    # the machine account's own hand-backs are revived.
    permission(gh, REVIEWER_BOT, "none")
    auto_pr(gh, [comment(VERDICT, login=REVIEWER_BOT, association="NONE")])
    atlas.retrigger_stale_handbacks()
    assert revivals(gh) == []
    assert any(f"by {REVIEWER_BOT} ignored" in a for a in atlas.actions)


def test_does_not_revive_another_apps_handback(gh):
    permission(gh, "foo[bot]", "none")
    auto_pr(gh, [comment("@review", login="foo[bot]", association="NONE")])
    atlas.retrigger_stale_handbacks()
    assert revivals(gh) == []


def test_revives_for_a_write_access_author_found_by_lookup(gh):
    permission(gh, "colleague", "write")
    auto_pr(gh, [comment("@review", login="colleague", association="CONTRIBUTOR")])
    atlas.retrigger_stale_handbacks()
    assert len(revivals(gh)) == 1
    assert len(gh.matching(is_permission_lookup)) == 1


def test_ignores_an_outsider_review_trigger(gh):
    permission(gh, "drive-by", "read")
    auto_pr(gh, [outsider("@review")])
    atlas.retrigger_stale_handbacks()
    assert revivals(gh) == []
    assert any("by drive-by ignored" in a for a in atlas.actions)


def test_ignores_an_outsider_forged_verdict(gh):
    permission(gh, "drive-by", "read")
    auto_pr(gh, [outsider("looks fine to me " + VERDICT)])
    atlas.retrigger_stale_handbacks()
    assert revivals(gh) == []
    assert any("by drive-by ignored" in a for a in atlas.actions)


def test_an_outsider_comment_ends_the_search_above_a_genuine_handback(gh):
    permission(gh, "drive-by", "read")
    auto_pr(gh, [comment("@review", cid=1), outsider("@review", cid=2)])
    atlas.retrigger_stale_handbacks()
    assert revivals(gh) == []
    # the older machine-account hand-back was never examined
    assert gh.matching(has("/reactions")) == []
    assert gh.matching(has("actions/runs")) == []


def test_skips_the_machine_account_counters_to_the_handback(gh):
    auto_pr(
        gh,
        [
            comment("@review", cid=1),
            comment("<!-- auto-review-rounds --> 🤖 auto review rounds: 1", cid=2),
        ],
    )
    atlas.retrigger_stale_handbacks()
    assert len(revivals(gh)) == 1


def test_an_outsider_counter_shaped_comment_is_not_skipped(gh):
    permission(gh, "drive-by", "read")
    auto_pr(gh, [comment("@review", cid=1), outsider("<!-- auto-review-rounds -->")])
    atlas.retrigger_stale_handbacks()
    assert revivals(gh) == []
    assert gh.matching(has("/reactions")) == []


def test_leaves_a_pr_whose_newest_comment_is_not_a_signal(gh):
    auto_pr(gh, [comment("@review", cid=1), comment("thanks, merging by hand", cid=2)])
    atlas.retrigger_stale_handbacks()
    assert revivals(gh) == []


# --------------------------------------------------------------- companion_pr

ISSUE = 42
HEAD = "claude/issue-42-20260915-1200"
TS_MONO_URL = f"https://github.com/{TS_MONO}/pull/7"


def is_issue_fetch(args):
    return args[0] == "api" and args[1] == f"repos/{FORK}/issues/{ISSUE}"


def is_url_query(args):
    return args[:2] == ("api", "graphql") and "pullRequest(number:" in args[3]


def is_discovery_query(args):
    return args[:2] == ("api", "graphql") and "pullRequests(headRefName:" in args[3]


COMP_HEAD = "c" * 40
COMP_OLD = "b" * 40


def companion(number, **fields):
    d = {
        "number": number,
        "state": "OPEN",
        "merged": False,
        "reviewDecision": None,
        "headRefOid": COMP_HEAD,
        "latestOpinionatedReviews": {"nodes": []},
    }
    d.update(fields)
    return d


def opinion(login, state, sha=COMP_HEAD):
    return {"state": state, "commit": {"oid": sha}, "author": {"login": login}}


def anchor(gh, body, login=MARVIN, association="MEMBER"):
    """Anchor issue #42 with this body/author; ts-mono has PR 7 (by URL) and
    an open unreviewed PR 9 on the branch-name convention."""
    gh.route(is_issue_fetch, {"body": body, "login": login, "association": association})
    gh.route(
        is_url_query,
        {"data": {"repository": {"pullRequest": companion(7, merged=True)}}},
    )
    gh.route(
        is_discovery_query,
        {"data": {"repository": {"pullRequests": {"nodes": [companion(9)]}}}},
    )


def test_a_trusted_authors_url_line_is_honoured(gh):
    anchor(gh, f"Fix the viewer.\n\nCompanion PR: {TS_MONO_URL}\n")
    comp = atlas.companion_pr(ISSUE, HEAD)
    assert (comp["number"], comp["_repo"]) == (7, TS_MONO)
    assert gh.matching(is_discovery_query) == []
    # author fields ride the body fetch: one issue read, asking for them
    (fetch,) = gh.matching(is_issue_fetch)
    assert ".user.login" in fetch[3] and ".author_association" in fetch[3]


def test_a_trusted_associations_opt_out_is_honoured(gh):
    anchor(gh, "Companion PR: none", login="colleague", association="MEMBER")
    assert atlas.companion_pr(ISSUE, HEAD) is None
    assert gh.matching(is_permission_lookup) == []
    assert gh.matching(is_discovery_query) == []


def test_an_untrusted_authors_opt_out_is_ignored(gh):
    permission(gh, "drive-by", "read")
    anchor(gh, "Companion PR: none", login="drive-by", association="NONE")
    comp = atlas.companion_pr(ISSUE, HEAD)
    assert comp["number"] == 9  # the branch-name convention decided
    assert any(
        "issue author drive-by is not a trusted author" in a for a in atlas.actions
    )


def test_an_untrusted_authors_url_line_is_ignored(gh):
    permission(gh, "drive-by", "read")
    anchor(gh, f"Companion PR: {TS_MONO_URL}", login="drive-by", association="NONE")
    assert atlas.companion_pr(ISSUE, HEAD)["number"] == 9
    assert gh.matching(is_url_query) == []


def test_a_url_outside_ts_mono_is_ignored(gh):
    anchor(gh, "Companion PR: https://github.com/someone-else/ts-mono/pull/7")
    assert atlas.companion_pr(ISSUE, HEAD)["number"] == 9
    assert gh.matching(is_url_query) == []
    assert any(f"not a {TS_MONO} PR URL" in a for a in atlas.actions)


def test_an_untrusted_author_without_a_line_costs_no_lookup(gh):
    anchor(gh, "Just a bug report.", login="drive-by", association="NONE")
    assert atlas.companion_pr(ISSUE, HEAD)["number"] == 9
    assert gh.matching(is_permission_lookup) == []


def test_merge_gate_still_holds_on_the_real_companion_despite_an_outsiders_opt_out(gh):
    permission(gh, "drive-by", "read")
    anchor(gh, "Companion PR: none", login="drive-by", association="NONE")
    assert atlas.companion_blocks_merge(ISSUE, {"headRefName": HEAD}) is True
    assert any("waiting on companion" in a for a in atlas.actions)


def test_merge_gate_honours_a_trusted_opt_out(gh):
    anchor(gh, "Companion PR: none")
    assert atlas.companion_blocks_merge(ISSUE, {"headRefName": HEAD}) is False


# ---------------------------------------------------------- companion_approved
# The companion gate binds the approval to the companion's current head by a
# trusted reviewer (finding 4121986, criterion 2), as approval_at_head.py does
# for the upstream PR and companion_mergeable.py does again in the queue.


def gate_with(gh, comp):
    """Anchor #42 with no directive; the branch-name convention finds `comp`."""
    gh.route(
        is_issue_fetch,
        {"body": "Fix the viewer.", "login": MARVIN, "association": "MEMBER"},
    )
    gh.route(
        is_discovery_query,
        {"data": {"repository": {"pullRequests": {"nodes": [comp]}}}},
    )
    return atlas.companion_blocks_merge(ISSUE, {"headRefName": HEAD})


def test_companion_approved_at_head_by_a_write_access_reviewer_clears_the_hold(gh):
    permission(gh, "epatey", "write", "maintain")
    comp = companion(
        9, latestOpinionatedReviews={"nodes": [opinion("epatey", "APPROVED")]}
    )
    assert gate_with(gh, comp) is False
    (lookup,) = gh.matching(is_permission_lookup)
    assert lookup[1] == f"repos/{TS_MONO}/collaborators/epatey/permission"


def test_companion_approval_on_an_older_head_holds(gh):
    permission(gh, "epatey", "write")
    comp = companion(
        9, latestOpinionatedReviews={"nodes": [opinion("epatey", "APPROVED", COMP_OLD)]}
    )
    assert gate_with(gh, comp) is True
    assert any("no trusted approval of its head" in a for a in atlas.actions)


def test_companion_review_decision_approved_alone_holds(gh):
    # PR-level and sticky: without a review naming the head it proves nothing.
    comp = companion(
        9,
        reviewDecision="APPROVED",
        latestOpinionatedReviews={"nodes": [opinion("epatey", "APPROVED", COMP_OLD)]},
    )
    permission(gh, "epatey", "write")
    assert gate_with(gh, comp) is True


def test_companion_approved_at_head_by_an_outsider_holds(gh):
    permission(gh, "drive-by", "read")
    comp = companion(
        9, latestOpinionatedReviews={"nodes": [opinion("drive-by", "APPROVED")]}
    )
    assert gate_with(gh, comp) is True


def test_companion_with_a_standing_changes_request_holds_despite_an_approval_at_head(
    gh,
):
    permission(gh, "epatey", "write")
    nodes = [opinion("epatey", "APPROVED"), opinion("ransomr", "CHANGES_REQUESTED")]
    assert (
        gate_with(gh, companion(9, latestOpinionatedReviews={"nodes": nodes})) is True
    )


def test_companion_with_a_non_approved_decision_holds_without_a_lookup(gh):
    comp = companion(
        9,
        reviewDecision="REVIEW_REQUIRED",
        latestOpinionatedReviews={"nodes": [opinion("epatey", "APPROVED")]},
    )
    assert gate_with(gh, comp) is True
    assert gh.matching(is_permission_lookup) == []


def test_companion_without_a_head_holds(gh):
    comp = companion(
        9,
        headRefOid=None,
        latestOpinionatedReviews={"nodes": [opinion("epatey", "APPROVED")]},
    )
    assert gate_with(gh, comp) is True
    assert gh.matching(is_permission_lookup) == []


def test_companion_queries_ask_for_the_head_and_each_reviews_commit_and_author(gh):
    anchor(gh, f"Companion PR: {TS_MONO_URL}")
    atlas.companion_pr(ISSUE, HEAD)
    anchor(gh, "Fix the viewer.")
    atlas.companion_pr(ISSUE, HEAD)
    for query in (*gh.matching(is_url_query), *gh.matching(is_discovery_query)):
        assert (
            "headRefOid" in query[3]
            and "commit{oid}" in query[3]
            and "author{login}" in query[3]
        )


def imported(snapshot, header_extra=""):
    """The body skills/import/import.sh writes: a machine-readable header, a
    `---` rule, then the upstream author's body verbatim."""
    return (
        f"Upstream issue: https://github.com/{atlas.UPSTREAM}/issues/2615\n\n"
        "Imported from upstream so the agents can work it here (no upstream write\n"
        "access). Canonical discussion stays upstream; below is a snapshot of the\n"
        f"upstream body at import time.\n{header_extra}\n---\n\n{snapshot}"
    )


@pytest.mark.parametrize(
    "directive", ["Companion PR: none", f"Companion PR: {TS_MONO_URL}"]
)
@pytest.mark.parametrize(
    "login, association", [(MARVIN, "MEMBER"), ("importer", "COLLABORATOR")]
)
def test_an_imported_snapshots_directive_is_ignored(gh, directive, login, association):
    # The fork issue's author is the trusted importer, but everything below the
    # rule is the upstream (outsider) author's text.
    body = imported(f"The viewer crashes.\n\n{directive}\n\nSteps: ...", "")
    anchor(gh, body, login=login, association=association)
    assert atlas.companion_pr(ISSUE, HEAD)["number"] == 9
    assert gh.matching(is_url_query) == []
    assert atlas.companion_blocks_merge(ISSUE, {"headRefName": HEAD}) is True
    assert any("imported upstream snapshot ignored" in a for a in atlas.actions)


def test_an_importers_own_directive_above_the_rule_is_honoured(gh):
    anchor(
        gh, imported("Companion PR: none  <- upstream text", "\nCompanion PR: none\n")
    )
    assert atlas.companion_pr(ISSUE, HEAD) is None
    assert gh.matching(is_discovery_query) == []


# ------------------------------------------------------------ field_is_stale


REOPEN_URL = "https://github.com/UKGovernmentBEIS/inspect_ai/pull/1"


def reopened_issue(gh, marker_login):
    """Issue #9 reopened after its PR merged, with a reopen-marker comment by
    `marker_login` right after the reopen (None: no marker)."""
    gh.route(has("timelineItems"), {"data": {"repository": {"issue": {"timelineItems": {
        "nodes": [{"createdAt": "2026-09-02T00:00:00Z"}]}}}}})
    comments = []
    if marker_login:
        comments.append({"body": atlas.reopen_marker(REOPEN_URL) + "parked", "created_at": "2026-09-02T00:00:01Z",
                         "user": {"login": marker_login}})
    gh.route(has(f"repos/{FORK}/issues/9/comments?per_page=100"), [comments])


@pytest.mark.parametrize("login", [MARVIN, MARVIN_BOT])
def test_the_syncs_own_reopen_marker_under_either_login_keeps_the_field(gh, login):
    reopened_issue(gh, login)
    assert atlas.field_is_stale(9, {"mergedAt": "2026-09-01T00:00:00Z"}, REOPEN_URL) is False


@pytest.mark.parametrize("login", ["drive-by", "foo[bot]", None])
def test_a_reopen_without_the_syncs_own_marker_makes_the_field_stale(gh, login):
    reopened_issue(gh, login)
    assert atlas.field_is_stale(9, {"mergedAt": "2026-09-01T00:00:00Z"}, REOPEN_URL) is True
