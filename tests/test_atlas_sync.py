"""Tests for .github/scripts/atlas_sync.py — the author checks behind two of
its writes.

Security scan 2026-09-04, findings 4121985 and 4121986: the hourly sync
believed comment text (retrigger_stale_handbacks) and issue-body text
(companion_pr) from anyone. Finding 4628443 (2026-09-21): it then believed
any author whose payload carried an OWNER/MEMBER/COLLABORATOR
`author_association`, which GitHub reports for org members and invited
collaborators at ANY repository permission, read and triage included.
Findings 4773874 and 4773875 (2026-09-30): the companion-loop reflection
believed any ts-mono PR and comment, and the stale-field check believed a
machine-account comment's text (see those sections). Every
`gh` call goes through the module's `gh()` wrapper, so the tests stand in a
fake for it and route each call by the arguments. Run with `python3 -m
pytest` from the repo root.
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
        self.repos = []  # the `repo=` each call named (gh_env's token key)

    def route(self, pred, response):
        self.routes.append((pred, response))

    def __call__(self, *args, repo=None):
        self.calls.append(args)
        self.repos.append(repo)
        for pred, resp in self.routes:
            if pred(args):
                if isinstance(resp, Exception):
                    raise resp
                return resp if isinstance(resp, str) else json.dumps(resp)
        raise AssertionError(f"unrouted gh call: {args}")

    def matching(self, pred):
        return [c for c in self.calls if pred(c)]

    def repos_of(self, pred):
        """The `repo=` named by every call matching pred, in order."""
        return [r for c, r in zip(self.calls, self.repos) if pred(c)]


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
    "login, expected",
    [
        (MARVIN, True),
        (MARVIN_BOT, True),  # by name: an App's permission reads `none`
        ("github-actions[bot]", False),  # never
        ("", False),
    ],
)
def test_trusted_author_decides_the_named_logins_without_a_lookup(gh, login, expected):
    assert atlas.trusted_author(login, FORK) is expected
    assert gh.calls == []


def test_trusted_author_takes_no_association_argument():
    # Finding 4628443: the payload's `author_association` used to short-circuit
    # the lookup, yet MEMBER and COLLABORATOR carry no repository permission.
    # The helper now has no way to be told one, so no caller can trust it.
    import inspect

    assert list(inspect.signature(atlas.trusted_author).parameters) == ["login", "repo"]
    assert not hasattr(atlas, "TRUSTED_ASSOCIATIONS")


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
    assert atlas.trusted_author("someone", FORK) is expected
    assert len(gh.matching(is_permission_lookup)) == 1


def test_trusted_author_refuses_a_triage_collaborator(gh):
    # GitHub answers `permission: read` with the finer role in `role_name`
    permission(gh, "someone", "read", "triage")
    assert atlas.trusted_author("someone", FORK) is False


def test_trusted_author_fails_closed_on_a_broken_lookup(gh):
    gh.route(has("/collaborators/"), RuntimeError("gh api ...: HTTP 502"))
    assert atlas.trusted_author("someone", FORK) is False


def test_trusted_author_caches_the_lookup_per_login_and_repo(gh):
    permission(gh, "someone", "write")
    assert atlas.trusted_author("someone", FORK) is True
    assert atlas.trusted_author("someone", FORK) is True
    assert len(gh.matching(is_permission_lookup)) == 1
    assert atlas.trusted_author("someone", TS_MONO) is True
    assert len(gh.matching(is_permission_lookup)) == 2


# --- the ts-mono token (Phase 2: one read-only token per repository read) ---
#
# The sync's GH_TOKEN is minted for the fork alone; every ts-mono call names
# `repo=TS_MONO` so gh() substitutes GH_TOKEN_TS_MONO, and a call for the
# fork leaves the environment alone. These run the REAL gh() against a
# recorded subprocess.run, since the FakeGH fixture replaces gh() whole.


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw.get("env")))
        import subprocess

        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")


@pytest.fixture
def run(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(atlas.subprocess, "run", rec)
    monkeypatch.setattr(atlas, "actions", [])
    monkeypatch.setattr(atlas, "_permission_cache", {}, raising=False)
    monkeypatch.setenv("GH_TOKEN", "fork-token")
    return rec


def test_a_ts_mono_call_runs_under_the_ts_mono_token(run, monkeypatch):
    monkeypatch.setenv(atlas.TS_MONO_TOKEN_VAR, "ts-mono-token")
    atlas.gh_json("api", f"repos/{TS_MONO}/collaborators/epatey/permission", repo=TS_MONO)
    ((argv, env),) = run.calls
    assert argv[:2] == ["gh", "api"]
    assert env["GH_TOKEN"] == "ts-mono-token"
    assert env[atlas.TS_MONO_TOKEN_VAR] == "ts-mono-token"  # the rest of the environment is kept


@pytest.mark.parametrize("repo", [FORK, atlas.UPSTREAM, None])
def test_every_other_call_keeps_the_fork_token(run, monkeypatch, repo):
    monkeypatch.setenv(atlas.TS_MONO_TOKEN_VAR, "ts-mono-token")
    atlas.gh_json("api", f"repos/{repo or FORK}/issues/1", repo=repo)
    ((_, env),) = run.calls
    assert env is None  # the process environment: GH_TOKEN is the fork token


def test_the_permission_lookup_names_the_repo_it_reads(run, monkeypatch):
    monkeypatch.setenv(atlas.TS_MONO_TOKEN_VAR, "ts-mono-token")
    run_impl = run

    def answer(argv, **kw):
        run_impl.calls.append((argv, kw.get("env")))
        import subprocess

        return subprocess.CompletedProcess(argv, 0, stdout='{"permission": "write"}', stderr="")

    monkeypatch.setattr(atlas.subprocess, "run", answer)
    assert atlas.trusted_author("epatey", TS_MONO) is True
    assert atlas.trusted_author("epatey", FORK) is True
    (ts_mono, fork) = run.calls
    assert f"repos/{TS_MONO}/collaborators/epatey/permission" in ts_mono[0]
    assert ts_mono[1]["GH_TOKEN"] == "ts-mono-token"
    assert f"repos/{FORK}/collaborators/epatey/permission" in fork[0]
    assert fork[1] is None


def test_a_ts_mono_call_fails_closed_without_the_ts_mono_token(run, monkeypatch):
    monkeypatch.delenv(atlas.TS_MONO_TOKEN_VAR, raising=False)
    with pytest.raises(RuntimeError, match=atlas.TS_MONO_TOKEN_VAR):
        atlas.gh("api", f"repos/{TS_MONO}/pulls/9", repo=TS_MONO)
    # trusted_author's lookup fails closed: not trusted, and gh never ran.
    assert atlas.trusted_author("epatey", TS_MONO) is False
    assert run.calls == []
    # An empty value is "absent" too — never a fall-through to the fork token.
    monkeypatch.setenv(atlas.TS_MONO_TOKEN_VAR, "")
    with pytest.raises(RuntimeError):
        atlas.gh("pr", "list", "--repo", TS_MONO, repo=TS_MONO)
    assert run.calls == []


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
    assert not atlas.trusted_author(login, FORK)
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


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_a_write_access_colleagues_handback_is_revived_only_after_a_lookup(gh, association):
    # Finding 4628443: the association used to skip the lookup. A colleague
    # with write access is still revived — by the lookup, not the payload.
    permission(gh, "colleague", "write")
    auto_pr(gh, [comment("@review", login="colleague", association=association)])
    atlas.retrigger_stale_handbacks()
    assert len(revivals(gh)) == 1
    assert len(gh.matching(is_permission_lookup)) == 1


@pytest.mark.parametrize("association", ["MEMBER", "COLLABORATOR"])
@pytest.mark.parametrize("perm, role", [("read", "read"), ("read", "triage"), ("none", "none")])
def test_does_not_revive_a_sub_write_members_trigger(gh, association, perm, role):
    # An org member without write on the fork, or a collaborator invited at
    # read/triage: the reviewer gate refused this `@review` (write access
    # looked up, no ack), so re-issuing it as the machine account would be
    # the privilege step the gate denied. The payload's association is the
    # attacker's only asset here and it decides nothing.
    permission(gh, "colleague", perm, role)
    auto_pr(gh, [comment("@review", login="colleague", association=association)])
    atlas.retrigger_stale_handbacks()
    assert revivals(gh) == []
    assert len(gh.matching(is_permission_lookup)) == 1
    assert any("by colleague ignored" in a for a in atlas.actions)


def test_does_not_revive_a_sub_write_members_forged_verdict(gh):
    permission(gh, "colleague", "read", "triage")
    auto_pr(gh, [comment(VERDICT, login="colleague", association="MEMBER")])
    atlas.retrigger_stale_handbacks()
    assert revivals(gh) == []


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


def opinion(login, state, sha=COMP_HEAD, typename="User"):
    # As GraphQL renders a review author: a Bot's login comes BARE.
    return {"state": state, "commit": {"oid": sha}, "author": {"login": login, "__typename": typename}}


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
    # the author's login rides the body fetch: one issue read, asking for it
    # and not for the association, which decides nothing (finding 4628443)
    (fetch,) = gh.matching(is_issue_fetch)
    assert ".user.login" in fetch[3] and ".author_association" not in fetch[3]
    assert gh.repos_of(is_url_query) == [TS_MONO]  # the URL's PR is read under the ts-mono token


def test_a_write_access_colleagues_opt_out_is_honoured_after_a_lookup(gh):
    permission(gh, "colleague", "write")
    anchor(gh, "Companion PR: none", login="colleague", association="MEMBER")
    assert atlas.companion_pr(ISSUE, HEAD) is None
    assert len(gh.matching(is_permission_lookup)) == 1
    assert gh.matching(is_discovery_query) == []


@pytest.mark.parametrize("association", ["MEMBER", "COLLABORATOR"])
@pytest.mark.parametrize("perm, role", [("read", "read"), ("read", "triage")])
def test_a_sub_write_members_opt_out_is_ignored(gh, association, perm, role):
    # Finding 4628443: a board issue's author with a MEMBER/COLLABORATOR
    # association but no write access could disable the companion hold
    permission(gh, "colleague", perm, role)
    anchor(gh, "Companion PR: none", login="colleague", association=association)
    assert atlas.companion_pr(ISSUE, HEAD)["number"] == 9  # the convention decided
    assert len(gh.matching(is_permission_lookup)) == 1
    assert any(
        "issue author colleague is not a trusted author" in a for a in atlas.actions
    )


def test_a_sub_write_members_url_line_is_ignored(gh):
    permission(gh, "colleague", "read", "triage")
    anchor(gh, f"Companion PR: {TS_MONO_URL}", login="colleague", association="MEMBER")
    assert atlas.companion_pr(ISSUE, HEAD)["number"] == 9
    assert gh.matching(is_url_query) == []


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
    # Every ts-mono read — the companion discovery and the reviewer's
    # permission — names ts-mono (gh() then runs it under the ts-mono
    # token); the anchor issue fetch on the fork names nothing.
    assert gh.repos_of(is_permission_lookup) == [TS_MONO]
    assert gh.repos_of(is_discovery_query) == [TS_MONO]
    assert gh.repos_of(is_issue_fetch) == [None]


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
            and "author{login __typename}" in query[3]
        )


def gate_with_url(gh, comp):
    """Anchor #42 naming `comp` by its `Companion PR:` URL line."""
    gh.route(
        is_issue_fetch,
        {"body": f"Companion PR: {TS_MONO_URL}", "login": MARVIN, "association": "MEMBER"},
    )
    gh.route(is_url_query, {"data": {"repository": {"pullRequest": comp}}})
    return atlas.companion_blocks_merge(ISSUE, {"headRefName": HEAD})


def test_graphql_login_restores_the_rest_suffix_for_bots_only():
    assert atlas.graphql_login({"login": "meridian-marvin", "__typename": "Bot"}) == MARVIN_BOT
    assert atlas.graphql_login({"login": "meridian-marvin[bot]", "__typename": "Bot"}) == MARVIN_BOT
    assert atlas.graphql_login({"login": "meridian-marvin", "__typename": "User"}) == "meridian-marvin"
    assert atlas.graphql_login({"login": MARVIN}) == MARVIN
    assert atlas.graphql_login(None) == "" and atlas.graphql_login({}) == ""


@pytest.mark.parametrize("gate", [gate_with, gate_with_url])
@pytest.mark.parametrize("login, typename", [(MARVIN, "User"), ("meridian-marvin", "Bot")])
def test_companion_approved_at_head_by_the_machine_account_clears_the_hold_on_both_discovery_paths(
    gh, gate, login, typename
):
    # The machine account under either login, as GraphQL renders each (the
    # App's bare), found by the branch convention or by the URL line: trusted
    # by name, so no lookup — the endpoint would answer `none` for the App.
    comp = companion(9, latestOpinionatedReviews={"nodes": [opinion(login, "APPROVED", typename=typename)]})
    assert gate(gh, comp) is False
    assert gh.matching(is_permission_lookup) == []


def test_companion_approved_by_a_user_named_after_the_apps_slug_is_looked_up_and_holds(gh):
    # Only the type restores the suffix: a User who registered `meridian-marvin`
    # is not the App and gets an ordinary lookup under its own login.
    permission(gh, "meridian-marvin", "read")
    comp = companion(9, latestOpinionatedReviews={"nodes": [opinion("meridian-marvin", "APPROVED", typename="User")]})
    assert gate_with(gh, comp) is True
    (lookup,) = gh.matching(is_permission_lookup)
    assert lookup[1] == f"repos/{TS_MONO}/collaborators/meridian-marvin/permission"


def test_companion_approved_by_another_app_is_looked_up_under_its_rest_login_and_holds(gh):
    permission(gh, "foo[bot]", "none")
    comp = companion(9, latestOpinionatedReviews={"nodes": [opinion("foo", "APPROVED", typename="Bot")]})
    assert gate_with(gh, comp) is True
    (lookup,) = gh.matching(is_permission_lookup)
    assert lookup[1] == f"repos/{TS_MONO}/collaborators/foo[bot]/permission"


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
#
# Finding 4773875: the sync's own recovery reopen was recognised by a
# machine-account comment starting with reopen_marker, but the land composite
# posts agent-written text under that login and the fork reviewer's land job
# can reopen an issue (`issues[].reopen`). It is recognised now by the
# ReopenedEvent's actor, a marker comment tagged with that event and its own
# comment id, and the machine account as that comment's last editor: land
# posts comments but never edits one.


REOPEN_URL = "https://github.com/UKGovernmentBEIS/inspect_ai/pull/1"
MERGED = {"mergedAt": "2026-09-01T00:00:00Z"}
REOPEN_TS = "2026-09-02T00:00:00Z"
SYNC_EVENT = "REE_sync"
HUMAN_EVENT = "REE_human"
CID = 5001


def reopen_event(event_id, actor, typename="User", ts=REOPEN_TS):
    return {"data": {"repository": {"issue": {"timelineItems": {"nodes": [
        {"id": event_id, "createdAt": ts, "actor": {"login": actor, "__typename": typename}}]}}}}}


NO_REOPEN = {"data": {"repository": {"issue": {"timelineItems": {"nodes": []}}}}}
SYNC_REOPEN = reopen_event(SYNC_EVENT, "meridian-marvin", "Bot")


def marker(event_id=None, cid=CID, rest="parked"):
    body = atlas.reopen_marker(REOPEN_URL) + rest
    return body + "\n\n" + atlas.reopen_tag(event_id, cid) if event_id else body


def by(login, body, cid=CID, created="2026-09-02T00:00:01Z"):
    return {"id": cid, "node_id": f"IC_{cid}", "body": body, "created_at": created, "user": {"login": login}}


def is_edit_query(args):
    return args[:2] == ("api", "graphql") and "lastEditedAt" in args[3]


def edited(editor="meridian-marvin", typename="Bot", cid=CID):
    """The comment-edit read: last edited by `editor` (None: never edited)."""
    node = {"databaseId": cid, "lastEditedAt": None, "editor": None}
    if editor:
        node.update(lastEditedAt="2026-09-02T00:00:02Z", editor={"login": editor, "__typename": typename})
    return {"data": {"node": node}}


def reopened_issue(gh, event, comments, edit=None):
    """Issue #9 reopened after its PR merged by `event`, then these comments;
    `edit` answers the comment-edit read (default: edited by the App)."""
    gh.route(is_edit_query, edit or edited())
    gh.route(has("timelineItems"), event)
    gh.route(has(f"repos/{FORK}/issues/9/comments?per_page=100"), [comments])


@pytest.mark.parametrize(
    "actor, typename, commenter",
    [
        ("meridian-marvin", "Bot", MARVIN_BOT),  # GraphQL renders the App bare
        (MARVIN, "User", MARVIN),
    ],
)
def test_the_syncs_own_tagged_and_edited_reopen_keeps_the_field(gh, actor, typename, commenter):
    reopened_issue(gh, reopen_event(SYNC_EVENT, actor, typename), [by(commenter, marker(SYNC_EVENT))])
    assert atlas.field_is_stale(9, MERGED, REOPEN_URL) is False
    (q,) = gh.matching(is_edit_query)
    assert "id=IC_5001" in q


def test_a_landing_reopen_then_a_correctly_tagged_landing_comment_does_not_keep_the_field(gh):
    # Review round 1, B1: land reopens a closed issue as the machine account;
    # a later landing reads that public event and posts the marker tagged
    # with it and its own id. Every text and author check passes, but land
    # only posts: the comment was never edited.
    reopened_issue(gh, SYNC_REOPEN, [by(MARVIN_BOT, marker(SYNC_EVENT))], edit=edited(None))
    assert atlas.field_is_stale(9, MERGED, REOPEN_URL) is True


@pytest.mark.parametrize(
    "edit",
    [
        edited("ransomr", "User"),  # a human edited it: not the sync's edit
        edited("claude", "Bot"),  # another App
        edited(cid=9999),  # the node read names another comment
        {"data": {"node": None}},
    ],
)
def test_a_tagged_comment_not_last_edited_by_the_machine_account_does_not_keep_the_field(gh, edit):
    reopened_issue(gh, SYNC_REOPEN, [by(MARVIN_BOT, marker(SYNC_EVENT))], edit=edit)
    assert atlas.field_is_stale(9, MERGED, REOPEN_URL) is True


def test_a_failed_edit_read_is_neither_answer(gh):
    # Review round 2, B1: "not the sync's" would retire a genuine recovery for
    # good; "not stale" would let a terminal PR close an unverified issue.
    reopened_issue(gh, SYNC_REOPEN, [by(MARVIN_BOT, marker(SYNC_EVENT))], edit=RuntimeError("graphql: boom"))
    with pytest.raises(atlas.OriginUnverified, match="comment 5001"):
        atlas.field_is_stale(9, MERGED, REOPEN_URL)


@pytest.fixture
def writes(monkeypatch):
    """Every board and issue write sync_item can make, recorded."""
    log = []
    for name in ("clear_field", "set_single_select", "close_issue", "comment"):
        monkeypatch.setattr(atlas, name, lambda *a, _n=name: log.append((_n, *a)))
    monkeypatch.setattr(atlas, "set_stage", lambda *a: log.append(("set_stage", *a)) or True)
    monkeypatch.setattr(atlas, "auto_closed_by_pr", lambda issue: True)
    return log


def recovered_row(gh, monkeypatch, edit, *, open_, pr_state, external=False, stage="Review"):
    """Issue #9 carrying a genuine, tagged sync recovery (reopen event and
    marker by the machine account), with the comment-edit read answering
    `edit`; its upstream PR is `pr_state`. Runs sync_item on the row."""
    reopened_issue(gh, SYNC_REOPEN, [by(MARVIN_BOT, marker(SYNC_EVENT))], edit=edit)
    gh.route(lambda a: a[:3] == ("api", "-X", "PATCH"), "{}")
    merged = pr_state == "MERGED"
    monkeypatch.setattr(atlas, "upstream_pr", lambda url: {
        "_ref": ("UKGovernmentBEIS", "inspect_ai", 1), "merged": merged,
        "state": "MERGED" if merged else "CLOSED", "headRefName": "",
        "mergedAt": "2026-09-01T00:00:00Z" if merged else None,
        "closedAt": "2026-09-01T00:00:00Z"})
    atlas.sync_item({"url": REOPEN_URL, "stage": stage, "item": "I", "issue": 9,
                     "open": open_, "state_reason": "COMPLETED", "external": external})


CALL_SITES = [
    # (issue open, upstream state, external proxy): the three field_is_stale callers
    pytest.param(False, "MERGED", False, id="closed-issue"),
    pytest.param(True, "MERGED", False, id="open-merged"),
    pytest.param(True, "CLOSED", False, id="open-closed-unmerged"),
    pytest.param(True, "CLOSED", True, id="open-closed-unmerged-proxy"),
]


@pytest.mark.parametrize("open_, pr_state, external", CALL_SITES)
def test_an_unreadable_origin_leaves_the_item_untouched(gh, monkeypatch, writes, open_, pr_state, external):
    with pytest.raises(atlas.OriginUnverified):
        recovered_row(gh, monkeypatch, RuntimeError("graphql: boom"),
                      open_=open_, pr_state=pr_state, external=external)
    assert writes == []
    assert gh.matching(lambda a: a[:3] == ("api", "-X", "PATCH")) == []  # no close, no reopen


@pytest.mark.parametrize("open_, pr_state, external", CALL_SITES)
def test_a_later_readable_run_keeps_the_genuine_recovery(gh, monkeypatch, writes, open_, pr_state, external):
    recovered_row(gh, monkeypatch, edited(), open_=open_, pr_state=pr_state, external=external)
    # the field is never retired (no "field cleared" comment, no clear of it)
    assert not any(w[0] == "clear_field" and w[2] == atlas.UPSTREAM_PR_FIELD for w in writes)
    assert not any(w[0] == "comment" and "Upstream PR field cleared" in w[2] for w in writes)
    if (open_, pr_state, external) == (True, "CLOSED", False):
        assert writes == []  # parked at Review for the human decision


def test_main_skips_an_unreadable_item_and_goes_on(gh, monkeypatch, writes, capsys):
    rows = [{"url": REOPEN_URL, "stage": "Review", "item": "I", "issue": 9,
             "open": True, "state_reason": None, "external": False}]
    monkeypatch.setattr(atlas, "board_items", lambda: rows)
    monkeypatch.setattr(atlas, "reflect_companion_loops", lambda: None)
    monkeypatch.setattr(atlas, "retrigger_stale_handbacks", lambda: None)
    reopened_issue(gh, SYNC_REOPEN, [by(MARVIN_BOT, marker(SYNC_EVENT))], edit=RuntimeError("graphql: boom"))
    monkeypatch.setattr(atlas, "upstream_pr", lambda url: {
        "_ref": ("UKGovernmentBEIS", "inspect_ai", 1), "merged": True, "state": "MERGED",
        "mergedAt": "2026-09-01T00:00:00Z", "closedAt": "2026-09-01T00:00:00Z", "headRefName": ""})
    assert atlas.main() == 0
    assert writes == []
    assert "#9 left unchanged this run" in capsys.readouterr().out


def test_a_machine_account_marker_after_a_human_reopen_does_not_keep_the_field(gh):
    # The finding: land posts an agent's comment carrying the marker prefix
    # (even one naming the human's public event id) as the machine account.
    reopened_issue(gh, reopen_event(HUMAN_EVENT, "ransomr"), [
        by(MARVIN_BOT, marker(), cid=1),
        by(MARVIN_BOT, marker(HUMAN_EVENT, cid=2), cid=2),
    ])
    assert atlas.field_is_stale(9, MERGED, REOPEN_URL) is True
    assert gh.matching(is_edit_query) == []  # decided by the actor, no edit read


@pytest.mark.parametrize(
    "body",
    [marker(), marker("REE_other"), marker(SYNC_EVENT, cid=4242)],  # untagged, other event, other comment
)
def test_a_machine_account_reopen_without_its_own_tag_does_not_keep_the_field(gh, body):
    reopened_issue(gh, SYNC_REOPEN, [by(MARVIN_BOT, body)])
    assert atlas.field_is_stale(9, MERGED, REOPEN_URL) is True


@pytest.mark.parametrize("login", ["drive-by", "foo[bot]"])
def test_a_tagged_marker_from_anyone_else_does_not_keep_the_field(gh, login):
    reopened_issue(gh, SYNC_REOPEN, [by(login, marker(SYNC_EVENT))])
    assert atlas.field_is_stale(9, MERGED, REOPEN_URL) is True


def test_a_tagged_marker_older_than_the_reopen_does_not_keep_the_field(gh):
    reopened_issue(gh, SYNC_REOPEN, [by(MARVIN_BOT, marker(SYNC_EVENT), created="2026-09-01T23:59:59Z")])
    assert atlas.field_is_stale(9, MERGED, REOPEN_URL) is True


def test_another_apps_reopen_is_not_the_syncs(gh):
    reopened_issue(gh, reopen_event(SYNC_EVENT, "other-app", "Bot"), [by(MARVIN_BOT, marker(SYNC_EVENT))])
    assert atlas.field_is_stale(9, MERGED, REOPEN_URL) is True


def test_a_reopen_before_the_terminal_is_not_stale(gh):
    reopened_issue(gh, reopen_event(HUMAN_EVENT, "ransomr", ts="2026-08-31T00:00:00Z"), [])
    assert atlas.field_is_stale(9, MERGED, REOPEN_URL) is False


def is_issue_patch(args):
    return args[:4] == ("api", "-X", "PATCH", f"repos/{FORK}/issues/9")


def is_comment_edit(args):
    return args[:4] == ("api", "-X", "PATCH", f"repos/{FORK}/issues/comments/{CID}")


def recovery_row(gh, monkeypatch, read_back, before=NO_REOPEN):
    """Closed non-external issue #9 at Sign-off whose upstream PR closed
    unmerged before it was panel-closed: the sync's recovery reopen. The
    timeline shows `before` until the sync's PATCH, then `read_back`.
    Returns the posted body and the edits made to it."""
    patched = lambda: bool(gh.matching(is_issue_patch))
    gh.route(lambda a: has("timelineItems")(a) and patched(), read_back)
    gh.route(has("timelineItems"), before)
    gh.route(is_issue_patch, "{}")
    gh.route(is_comment_edit, "{}")
    gh.route(lambda a: a[:2] == ("api", f"repos/{FORK}/issues/9/comments"), {"id": CID, "node_id": f"IC_{CID}"})
    monkeypatch.setattr(atlas, "upstream_pr", lambda url: {
        "_ref": ("UKGovernmentBEIS", "inspect_ai", 1), "merged": False, "state": "CLOSED",
        "closedAt": "2026-09-01T00:00:00Z", "mergedAt": None})
    monkeypatch.setattr(atlas, "auto_closed_by_pr", lambda issue: True)
    monkeypatch.setattr(atlas, "set_stage", lambda item, stage, cur: True)
    atlas.sync_item({"url": REOPEN_URL, "stage": "Sign-off", "item": "I", "issue": 9,
                     "open": False, "state_reason": "COMPLETED", "external": False})
    (post,) = gh.matching(lambda a: a[:2] == ("api", f"repos/{FORK}/issues/9/comments"))
    assert gh.calls.index(post) > gh.calls.index(gh.matching(is_issue_patch)[0])
    return post[-1].removeprefix("body="), [e[-1].removeprefix("body=") for e in gh.matching(is_comment_edit)]


def test_the_recovery_reopen_posts_then_edits_in_its_tag_and_the_next_run_keeps_the_field(gh, monkeypatch):
    body, edits = recovery_row(gh, monkeypatch, SYNC_REOPEN)
    assert body.startswith(atlas.reopen_marker(REOPEN_URL))
    assert "atlas-sync-reopen" not in body  # the post itself carries no tag
    (final,) = edits
    assert final == body + "\n\n" + atlas.reopen_tag(SYNC_EVENT, CID)
    # next hour: the same timeline, the comment as the sync left it
    later = FakeGH()
    monkeypatch.setattr(atlas, "gh", later)
    reopened_issue(later, SYNC_REOPEN, [by(MARVIN_BOT, final)])
    assert atlas.field_is_stale(9, {"closedAt": "2026-09-01T00:00:00Z"}, REOPEN_URL) is False


@pytest.mark.parametrize(
    "read_back, before",
    [
        (RuntimeError("graphql: boom"), NO_REOPEN),
        (NO_REOPEN, NO_REOPEN),
        (reopen_event(HUMAN_EVENT, "ransomr"), NO_REOPEN),
        # the PATCH was a no-op: the machine-account reopen was already there
        (SYNC_REOPEN, SYNC_REOPEN),
        # the pre-read failed: nothing to tell the new event from an old one
        (SYNC_REOPEN, RuntimeError("graphql: boom")),
    ],
)
def test_a_reopen_the_sync_cannot_prove_is_left_untagged(gh, monkeypatch, read_back, before):
    body, edits = recovery_row(gh, monkeypatch, read_back, before=before)
    assert body.startswith(atlas.reopen_marker(REOPEN_URL))
    assert edits == []


def test_a_machine_account_reopen_between_the_pre_read_and_the_patch_is_tagged_as_the_syncs(gh, monkeypatch):
    # The documented residual race: another machine-account reopen lands
    # after the pre-read, so the sync's PATCH is a no-op and the read-back
    # finds that reopen. The sync had decided to reopen the issue itself, so
    # the outcome is the one its own reopen would have had.
    _, edits = recovery_row(gh, monkeypatch, reopen_event("REE_land", "meridian-marvin", "Bot"))
    assert edits and edits[0].endswith(atlas.reopen_tag("REE_land", CID))


def test_only_the_loops_counter_comments_are_edited_by_machine_account_code():
    """The proof above rests on land never editing a comment: the only
    comment edits in the machine account's trusted code are the loops'
    counter rewrites and claude.yml's status comment (the land job's last
    step, on the comment the gate posted), each writing a fixed body that
    can neither start with the reopen marker nor carry a reopen tag. A new
    edit path must be checked against reopen_tag before it joins this
    list."""
    import re

    edit = re.compile(r"PATCH[^\n]*issues/comments|updateIssueComment|--edit-last")
    found = {
        str(f.relative_to(ROOT))
        for f in (ROOT / ".github").rglob("*")
        if f.is_file() and f != SCRIPT and edit.search(f.read_text(errors="ignore"))
    }
    assert found == {
        ".github/workflows/claude.yml",
        ".github/workflows/claude-auto.yml",
        ".github/workflows/claude-auto-review.yml",
        ".github/actions/reset-auto-counters/action.yml",
    }
    # claude.yml's one edit: the gate's own status comment, rewritten to a
    # body that starts with the fixed "**Claude finished" line.
    claude = (ROOT / ".github" / "workflows" / "claude.yml").read_text()
    assert len(edit.findall(claude)) == 1
    assert 'gh api -X PATCH "repos/$REPO/issues/comments/$COMMENT_ID"' in claude
    assert "          COMMENT_ID: ${{ needs.gate.outputs.status_comment_id }}\n" in claude
    assert "body=$(printf '**Claude finished @%s" in claude


# ---------------------------------------------------- reflect_companion_loops
#
# Finding 4773874: the reflection believed any ts-mono PR named
# claude/issue-N-* whose branch exists on the fork, and any comment's loop
# markers on it. ts-mono is public: an outsider can comment on the real
# companion, or open a fork-of-ts-mono PR with a head named after a fork branch.

COMP = 50
COMP_BRANCH = "claude/issue-42-20260915-1200"


def ts_pr(number=COMP, branch=COMP_BRANCH, author=None, cross=False, labels=("auto",)):
    return {
        "number": number,
        "headRefName": branch,
        "labels": [{"name": n} for n in labels],
        "author": author or {"is_bot": True, "login": "app/meridian-marvin"},
        "isCrossRepository": cross,
    }


def ts_comment(body, login=MARVIN_BOT, created="2026-09-02T00:00:00Z"):
    return {"body": body, "created_at": created, "user": {"login": login}}


@pytest.fixture
def stages(monkeypatch):
    moves = []

    def record(item, stage, current):
        moves.append((item, stage, current))
        return stage != current

    monkeypatch.setattr(atlas, "set_stage", record)
    return moves


def reflection(gh, prs, comments, stage="Review"):
    """ts-mono lists `prs`; each has `comments`; every branch exists on the
    fork; fork issue #42 is Ransom's, open, on Atlas at `stage`."""
    gh.route(lambda a: a[:2] == ("pr", "list"), prs)
    gh.route(lambda a: a[0] == "api" and a[1].startswith(f"repos/{FORK}/branches/"), "{}")
    gh.route(has("projectItems"), {"data": {"repository": {"issue": {
        "state": "OPEN",
        "assignees": {"nodes": [{"login": atlas.REVIEWER}]},
        "projectItems": {"nodes": [{"id": "ITEM", "project": {"id": atlas.PROJECT_ID},
                                    "fieldValueByName": {"name": stage}}]}}}}})
    gh.route(has(f"repos/{TS_MONO}/issues/"), [comments])


def fork_branch_checks(gh):
    return gh.matching(lambda a: a[0] == "api" and a[1].startswith(f"repos/{FORK}/branches/"))


def test_a_trusted_go_marker_on_the_machine_accounts_companion_engages(gh, stages):
    reflection(gh, [ts_pr()], [ts_comment("@" + "review")])
    atlas.reflect_companion_loops()
    assert stages == [("ITEM", "Agent", "Review")]


def test_a_write_access_humans_marker_counts_after_a_lookup(gh, stages):
    permission(gh, "colleague", "write")
    reflection(gh, [ts_pr()], [ts_comment("@" + "review", login="colleague")])
    atlas.reflect_companion_loops()
    assert stages == [("ITEM", "Agent", "Review")]
    assert gh.repos_of(is_permission_lookup) == [TS_MONO]


def test_a_trusted_stop_marker_hands_back(gh, stages):
    reflection(gh, [ts_pr()], [
        ts_comment("@" + "review", created="2026-09-01T00:00:00Z"),
        ts_comment("<!-- auto-handoff -->", created="2026-09-02T00:00:00Z"),
    ], stage="Agent")
    atlas.reflect_companion_loops()
    assert stages == [("ITEM", "Review", "Agent")]


@pytest.mark.parametrize("body", ["@" + "review", "<!-- claude-review-verdict:suggestions -->"])
def test_an_outsiders_go_marker_on_an_auto_companion_moves_nothing(gh, stages, body):
    permission(gh, "drive-by", "read")
    reflection(gh, [ts_pr()], [ts_comment(body, login="drive-by")])
    atlas.reflect_companion_loops()
    # no counted go marker: not engaged, and the item already rests at Review
    assert [s for s in stages if s[1] != s[2]] == []


@pytest.mark.parametrize("body", ["auto-handoff", "auto-converged", "claude-review-verdict:clean"])
def test_an_outsiders_stop_marker_does_not_hand_back_an_engaged_loop(gh, stages, body):
    permission(gh, "drive-by", "none")
    reflection(gh, [ts_pr()], [
        ts_comment("@" + "review", created="2026-09-01T00:00:00Z"),
        ts_comment(body, login="drive-by", created="2026-09-02T00:00:00Z"),
    ], stage="Agent")
    atlas.reflect_companion_loops()
    assert [s for s in stages if s[1] != s[2]] == []


def test_a_sub_write_members_marker_is_not_counted(gh, stages):
    permission(gh, "triager", "triage")
    reflection(gh, [ts_pr()], [ts_comment("@" + "review", login="triager")])
    atlas.reflect_companion_loops()
    assert [s for s in stages if s[1] != s[2]] == []


def test_an_outsiders_fork_of_ts_mono_pr_is_not_a_companion(gh, stages):
    reflection(gh, [ts_pr(author={"is_bot": False, "login": "drive-by"}, cross=True)],
               [ts_comment("@" + "review")])
    atlas.reflect_companion_loops()
    assert stages == []
    assert fork_branch_checks(gh) == []
    assert gh.matching(is_permission_lookup) == []  # refused before any lookup


def test_a_cross_repository_head_is_refused_whoever_opened_it(gh, stages):
    reflection(gh, [ts_pr(cross=True)], [ts_comment("@" + "review")])
    atlas.reflect_companion_loops()
    assert stages == []
    assert fork_branch_checks(gh) == []


def test_a_pr_without_the_cross_repository_flag_fails_closed(gh, stages):
    pr = ts_pr()
    del pr["isCrossRepository"]
    reflection(gh, [pr], [ts_comment("@" + "review")])
    atlas.reflect_companion_loops()
    assert stages == []


@pytest.mark.parametrize("perm", ["read", "triage", "none"])
def test_an_untrusted_authors_same_repo_pr_is_not_a_companion(gh, stages, perm):
    permission(gh, "drive-by", perm)
    reflection(gh, [ts_pr(author={"is_bot": False, "login": "drive-by"})], [ts_comment("@" + "review")])
    atlas.reflect_companion_loops()
    assert stages == []
    assert fork_branch_checks(gh) == []


def test_another_apps_pr_is_looked_up_under_its_rest_login_and_refused(gh, stages):
    permission(gh, "dependabot[bot]", "none")
    reflection(gh, [ts_pr(author={"is_bot": True, "login": "app/dependabot"})], [ts_comment("@" + "review")])
    atlas.reflect_companion_loops()
    assert stages == []
    assert gh.matching(has("/collaborators/dependabot[bot]/permission"))


@pytest.mark.parametrize(
    "branch",
    [
        "claude/issue-42-x/../../../../repos/o/r",
        "claude/issue-42-x?ref=main",
        "claude/issue-42-x#frag",
        "claude/issue-42-x%2F..",
        "claude/issue-42-x.y",
        "claude/issue-42-",
        "claude/issue-٤٢-x",  # non-ASCII digits
        "xclaude/issue-42-x",
    ],
)
def test_a_branch_outside_the_strict_shape_reaches_no_rest_path(gh, stages, branch):
    reflection(gh, [ts_pr(branch=branch)], [ts_comment("@" + "review")])
    atlas.reflect_companion_loops()
    assert stages == []
    assert fork_branch_checks(gh) == []


@pytest.mark.parametrize(
    "branch",
    ["claude/issue-42-20260915-1200", "claude/issue-42-run-123456", "claude/issue-42-codex-17"],
)
def test_the_dev_agents_branch_names_fit_the_shape(branch):
    assert atlas.COMPANION_BRANCH_RE.fullmatch(branch).group(1) == "42"


def test_the_pr_list_asks_for_the_author_and_the_head_repository(gh, stages):
    reflection(gh, [], [])
    atlas.reflect_companion_loops()
    (call,) = gh.matching(lambda a: a[:2] == ("pr", "list"))
    fields = call[call.index("--json") + 1].split(",")
    assert {"author", "isCrossRepository"} <= set(fields)


def test_cli_login_restores_the_rest_suffix_for_bots_only():
    assert atlas.cli_login({"is_bot": True, "login": "app/meridian-marvin"}) == MARVIN_BOT
    assert atlas.cli_login({"is_bot": False, "login": "ransomr"}) == "ransomr"
    assert atlas.cli_login({"is_bot": False, "login": "meridian-marvin"}) == "meridian-marvin"
    assert atlas.cli_login(None) == ""
