"""Tests for the trust rule in skills/checkout/checkout.sh and skills/promote/promote.sh.

Both scripts resolve "the issue's PR" from linked-PR chips that any GitHub
account can create (a `Fixes #N` PR from a personal fork into the org fork),
so before any PR text is read they apply one rule: the head repository must
be the org fork itself AND the author must be in TRUSTED_LOGINS or hold
write access there. These tests run the scripts against a stub `gh` that
answers from fixtures and logs every call: refusals must exit non-zero
naming the candidate and the reason before any write, the agent's own PR
must still resolve, and promote must refuse ambiguity without `--pr` and
resolve with it (agents #32) or fall back to closing refs (#33). Acceptance
is exercised through `--dry-run` only.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKOUT = ROOT / "skills" / "checkout" / "checkout.sh"
PROMOTE = ROOT / "skills" / "promote" / "promote.sh"

FORK = "meridianlabs-ai/inspect_ai"
UPSTREAM = "UKGovernmentBEIS/inspect_ai"
MARVIN = "i-am-marvin"
N = 42  # the issue under test

# Every `gh` call the scripts make up to (and, in --dry-run, past) the pick.
# Unknown calls fail loudly so a new network call cannot pass unnoticed.
GH_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB/calls"
args="$*"
case "$args" in
  "api graphql "*) cat "$STUB/graphql.json" ;;
  api\ repos/*/collaborators/*/permission*)
    login=$(sed -E 's#^api repos/[^/]+/[^/]+/collaborators/([^/]+)/permission.*#\1#' <<<"$args")
    perm=$(grep -E "^$login=" "$STUB/perms" 2>/dev/null | head -1 | cut -d= -f2)
    if [ "$perm" = "FAIL" ]; then echo "gh: HTTP 500" >&2; exit 1; fi
    echo "${perm:-read}" ;;
  "pr list --repo meridianlabs-ai/inspect_ai --state open "*)
    if [ -f "$STUB/prlist_fail" ]; then echo "gh: HTTP 500" >&2; exit 1; fi
    cat "$STUB/open_prs.json" 2>/dev/null || echo '[]' ;;
  "pr list --repo meridianlabs-ai/ts-mono "*) ;;
  "pr view "*)
    n=$(awk '{print $3}' <<<"$args"); repo=$(sed -E 's#.*--repo ([^ ]+).*#\1#' <<<"$args")
    f="$STUB/pr_view_${repo//\//_}_$n.json"
    if [ -f "$f" ]; then cat "$f"; else echo "gh: no such PR $repo#$n" >&2; exit 1; fi ;;
  api\ repos/meridianlabs-ai/inspect_ai/branches/*)
    # The branch tip: from $STUB/branches (branch=sha), else the fixtures'
    # default of sha-<branch>, which the PR builders below also use.
    b=$(sed -E 's#^api repos/meridianlabs-ai/inspect_ai/branches/([^ ]+).*#\1#' <<<"$args")
    sha=$(grep -F "$b=" "$STUB/branches" 2>/dev/null | head -1 | sed 's/^[^=]*=//')
    echo "${sha:-sha-$b}" ;;
  "api --paginate "*comments*)
    # Run the caller's own --jq over the comment fixture for that PR/issue.
    n=$(sed -E 's#.*/issues/([0-9]+)/comments.*#\1#' <<<"$args"); expr=""
    while [ $# -gt 0 ]; do [ "$1" = "--jq" ] && expr=$2; shift; done
    f="$STUB/comments_$n.json"; [ -f "$f" ] || f=/dev/null
    { cat "$f"; [ "$f" = /dev/null ] && echo '[]'; } | jq -r "$expr"
    # A later page failing after the first returned rows: gh has already
    # streamed page one and exits non-zero.
    if [ -f "$STUB/comments_fail_$n" ]; then echo "gh: HTTP 502 fetching page 2" >&2; exit 1; fi ;;
  "pr checks "*) ;;
  "api repos/UKGovernmentBEIS/inspect_ai/commits/main "*) echo "0123abcd" ;;
  *) echo "stub gh: unexpected call: $args" >&2; exit 97 ;;
esac
"""


# The machine account's Phase 2 GitHub App login as GraphQL renders it: a Bot's
# login comes bare (`meridian-marvin`, not `meridian-marvin[bot]`), with the
# type alongside; `gh --json author` renders the same App as `app/meridian-marvin`.
BOT_AUTHOR = {"login": "meridian-marvin", "__typename": "Bot"}


def _author(a):
    """An author field: a login string, a ready-made author object, or None (deleted)."""
    return None if a is None else ({"login": a} if isinstance(a, str) else a)


def chip(number, *, state="OPEN", author=MARVIN, head_repo=FORK, repo=FORK, branch=None,
         base="main", title=None, body="", head_sha=None):
    return {
        "number": number, "state": state, "isDraft": False,
        "title": title or f"PR {number}", "body": body,
        "headRefName": branch or f"claude/issue-{N}-2026-{number}", "baseRefName": base,
        "headRefOid": head_sha or f"sha-{branch or f'claude/issue-{N}-2026-{number}'}",
        "author": _author(author),
        "repository": {"nameWithOwner": repo},
        "headRepository": {"nameWithOwner": head_repo} if head_repo else None,
    }


def issue(chips=(), *, author="someone", labels=(), body="", title="an issue"):
    return {"data": {"repository": {"issue": {
        "id": "I_x", "title": title, "state": "OPEN", "body": body,
        "author": _author(author),
        "labels": {"nodes": [{"name": l} for l in labels]},
        "closedByPullRequestsReferences": {"nodes": list(chips)},
        "projectItems": {"nodes": []},
    }}}}


def open_pr(number, *, author=MARVIN, head_repo=FORK, branch, body="", title=None, head_sha=None):
    """A PR in `gh pr list/view --json` shape (head repo split into owner + name)."""
    owner, name = head_repo.split("/")
    return {
        "number": number, "state": "OPEN", "isDraft": False, "title": title or f"PR {number}",
        "body": body, "headRefName": branch, "headRefOid": head_sha or f"sha-{branch}",
        "author": _author(author),
        "headRepository": {"id": "R_1", "name": name},
        "headRepositoryOwner": {"id": "O_1", "login": owner},
    }


class Stub:
    def __init__(self, tmp_path, issue_json, *, perms=(), open_prs=(), pr_views=(), comments=None,
                 branches=(), prlist_fail=False, comments_fail=()):
        self.dir = tmp_path / "stub"
        self.dir.mkdir(parents=True)
        gh = tmp_path / "bin" / "gh"
        gh.parent.mkdir(parents=True)
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        (self.dir / "graphql.json").write_text(json.dumps(issue_json))
        (self.dir / "perms").write_text("".join(f"{k}={v}\n" for k, v in perms))
        (self.dir / "open_prs.json").write_text(json.dumps(list(open_prs)))
        for repo, pr in pr_views:
            (self.dir / f"pr_view_{repo.replace('/', '_')}_{pr['number']}.json").write_text(json.dumps(pr))
        for number, items in (comments or {}).items():
            (self.dir / f"comments_{number}.json").write_text(
                json.dumps([{"user": {"login": login}, "body": body} for login, body in items]))
        (self.dir / "branches").write_text("".join(f"{b}={sha}\n" for b, sha in branches))
        if prlist_fail:
            (self.dir / "prlist_fail").touch()
        for number in comments_fail:
            (self.dir / f"comments_fail_{number}").touch()
        # checkout.sh resolves the issue's repo from the clone's remotes and
        # guards on a clean tree; an empty repo with the fork as origin is both.
        # The URL is non-routable so no test can reach the network even when a
        # script wrongly proceeds to fetch.
        self.clone = tmp_path / "clone"
        self.clone.mkdir(parents=True)
        self.git("init", "-q")
        self.git("remote", "add", "origin", f"https://127.0.0.1:9/{FORK}.git")
        self.env = {
            **os.environ,
            "PATH": f"{gh.parent}:{os.environ['PATH']}",
            "STUB": str(self.dir),
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
        }

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.clone, check=True, text=True, capture_output=True,
                              env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})

    def run(self, script, *args):
        return subprocess.run(["bash", str(script), *args], cwd=self.clone, text=True,
                              capture_output=True, env=self.env)

    def calls(self):
        p = self.dir / "calls"
        return p.read_text().splitlines() if p.exists() else []


# --- checkout.sh ------------------------------------------------------------


def test_checkout_refuses_personal_fork_chip_and_body_line_without_writing(tmp_path):
    # The scanner's scenario: an outsider's `Fixes #N` PR from their own fork
    # is the only chip, and the issue body points at their PR.
    s = Stub(tmp_path, issue(
        [chip(500, author="outsider", head_repo="outsider/inspect_ai", branch="main")],
        author="outsider", body=f"Upstream PR: https://github.com/outsider/inspect_ai/pull/9"))
    r = s.run(CHECKOUT, str(N))  # a real run, not --dry-run: it must stop before any write
    assert r.returncode == 3, r.stderr
    assert "NO QUALIFYING OPEN CHIP" in r.stderr
    assert "#500 OPEN" in r.stderr and "REFUSED: head repository is 'outsider/inspect_ai'" in r.stderr
    assert "body line" in r.stderr and "REFUSED: issue is not an External proxy" in r.stderr
    assert not any(c.startswith("pr checkout") for c in s.calls())
    # No branch config was written either (git config exits 1 when nothing matches).
    assert subprocess.run(["git", "config", "--get-regexp", "^branch\\."], cwd=s.clone, capture_output=True).returncode == 1


def test_checkout_prefers_trusted_chip_even_when_outsider_pr_number_is_higher(tmp_path):
    # Before the rule the highest-numbered open same-repo chip won, so an
    # outsider PR opened after the agent's outranked it.
    s = Stub(tmp_path, issue([
        chip(400, author=MARVIN),
        chip(500, author="outsider", head_repo="outsider/inspect_ai", branch="claude/issue-42-fake"),
    ]))
    r = s.run(CHECKOUT, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert f"DECISION — check out {FORK}#400 via open same-repo chip" in r.stdout
    assert "#500 OPEN" in r.stdout and "REFUSED" in r.stdout
    assert not any(c.startswith("pr checkout") for c in s.calls())


@pytest.mark.parametrize("perm, accepted", [("admin", True), ("maintain", True), ("write", True),
                                            ("read", False), ("none", False), ("FAIL", False)])
def test_checkout_author_trust_comes_from_the_permission_lookup(tmp_path, perm, accepted):
    # A collaborator's own fork branch qualifies; `read` (what a public repo
    # answers for everyone) does not, and a failed lookup fails closed.
    s = Stub(tmp_path, issue([chip(400, author="colleague")]), perms=[("colleague", perm)])
    r = s.run(CHECKOUT, str(N), "--dry-run")
    if accepted:
        assert r.returncode == 0 and f"check out {FORK}#400" in r.stdout, r.stderr
    else:
        assert r.returncode == 3 and "author 'colleague' is not in TRUSTED_LOGINS" in r.stderr, r.stdout
    assert sum("collaborators/colleague/permission" in c for c in s.calls()) == 1


def test_checkout_permission_lookup_is_cached_per_login(tmp_path):
    s = Stub(tmp_path, issue([chip(400, author="colleague"), chip(401, author="colleague")]),
             perms=[("colleague", "write")])
    r = s.run(CHECKOUT, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert sum("collaborators/colleague/permission" in c for c in s.calls()) == 1
    assert f"check out {FORK}#401" in r.stdout  # highest number among the qualifying


def test_checkout_trusted_login_needs_no_lookup_and_deleted_author_is_refused(tmp_path):
    s = Stub(tmp_path, issue([chip(400, author=MARVIN), chip(401, author=None)]))
    r = s.run(CHECKOUT, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert f"check out {FORK}#400" in r.stdout
    assert "#401 OPEN" in r.stdout and "author 'unknown' is not in TRUSTED_LOGINS" in r.stdout
    assert not any("/permission" in c for c in s.calls())


def test_checkout_trusts_the_apps_login_by_name_from_graphqls_bare_bot_login(tmp_path):
    # Phase 2: the dev agent's PRs are authored by the machine account's App
    # login. GraphQL renders it bare with `__typename: Bot`; the script
    # normalises it to the REST form TRUSTED_LOGINS names, so no lookup runs
    # (the collaborators endpoint answers `none` for an App).
    s = Stub(tmp_path, issue([chip(400, author=BOT_AUTHOR)]))
    r = s.run(CHECKOUT, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert f"check out {FORK}#400" in r.stdout
    assert not any("/permission" in c for c in s.calls())


def test_checkout_refuses_another_app_and_a_user_who_took_the_apps_slug(tmp_path):
    # Another App is looked up under its REST login and refused (the stub
    # answers `read`); a User's login is never rewritten, so a User named
    # after the App's slug is not the App and gets an ordinary lookup.
    s = Stub(tmp_path, issue([chip(400, author={"login": "foo", "__typename": "Bot"}),
                              chip(401, author={"login": "meridian-marvin", "__typename": "User"})]))
    r = s.run(CHECKOUT, str(N), "--dry-run")
    assert r.returncode == 3, r.stdout
    assert "author 'foo[bot]' is not in TRUSTED_LOGINS" in r.stderr
    assert "author 'meridian-marvin' is not in TRUSTED_LOGINS" in r.stderr
    assert sum("collaborators/foo[bot]/permission" in c for c in s.calls()) == 1
    assert sum("collaborators/meridian-marvin/permission" in c for c in s.calls()) == 1


def test_checkout_external_proxy_written_by_the_apps_login_is_genuine(tmp_path):
    # The sync files External proxies as the machine account: under Phase 2
    # that is the App login, bare in GraphQL.
    theirs = chip(5001, repo=UPSTREAM, head_repo="outsider/inspect_ai", author="outsider", branch="fix")
    s = Stub(tmp_path, issue([theirs], author=BOT_AUTHOR, labels=["External"]))
    r = s.run(CHECKOUT, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert f"check out {UPSTREAM}#5001 via open cross-repo chip" in r.stdout
    assert "qualifies (External proxy" in r.stdout


def test_checkout_cross_repo_chip_qualifies_as_a_promotion_only_with_fork_head(tmp_path):
    ours = chip(5000, repo=UPSTREAM, head_repo=FORK, author="ransomr", branch="claude/issue-42-x")
    s = Stub(tmp_path, issue([ours]), perms=[("ransomr", "admin")])
    r = s.run(CHECKOUT, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert f"check out {UPSTREAM}#5000 via open cross-repo chip" in r.stdout and "[cross-repo]" in r.stdout

    theirs = chip(5001, repo=UPSTREAM, head_repo="outsider/inspect_ai", author="outsider", branch="fix")
    s2 = Stub(tmp_path / "b", issue([theirs]))
    r2 = s2.run(CHECKOUT, str(N), "--dry-run")
    assert r2.returncode == 3
    assert "#5001 OPEN" in r2.stderr and "issue is not an External proxy" in r2.stderr


def test_checkout_external_proxy_admits_the_contributors_upstream_pr(tmp_path):
    # A genuine proxy: written by a trusted login, labelled External. Its
    # single open upstream chip is the contributor's PR from a personal fork.
    theirs = chip(5001, repo=UPSTREAM, head_repo="outsider/inspect_ai", author="outsider", branch="fix")
    s = Stub(tmp_path, issue([theirs], author=MARVIN, labels=["External"]))
    r = s.run(CHECKOUT, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert f"check out {UPSTREAM}#5001 via open cross-repo chip" in r.stdout
    assert "qualifies (External proxy" in r.stdout


def test_checkout_external_label_alone_does_not_make_a_proxy(tmp_path):
    # Anyone can file an issue; only a trusted author's External issue is a proxy.
    theirs = chip(5001, repo=UPSTREAM, head_repo="outsider/inspect_ai", author="outsider", branch="fix")
    s = Stub(tmp_path, issue([theirs], author="outsider", labels=["External"],
                             body=f"Upstream PR: https://github.com/{UPSTREAM}/pull/5001"))
    r = s.run(CHECKOUT, str(N), "--dry-run")
    assert r.returncode == 3
    assert "issue is not an External proxy (author=outsider labels=External)" in r.stderr
    assert not any(c.startswith("pr view") for c in s.calls())


def test_checkout_body_line_fallback_on_a_genuine_proxy(tmp_path):
    up = {"number": 5336, "state": "OPEN", "headRefName": "their-fix", "baseRefName": "main"}
    s = Stub(tmp_path, issue([], author=MARVIN, labels=["External"],
                             body=f"Mirror.\n\nUpstream PR: https://github.com/{UPSTREAM}/pull/5336\n"),
             pr_views=[(UPSTREAM, up)])
    r = s.run(CHECKOUT, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert f"check out {UPSTREAM}#5336 via proxy body's Upstream PR line" in r.stdout
    assert "resolved via the proxy body" in r.stderr


def test_checkout_body_line_must_point_under_upstream(tmp_path):
    s = Stub(tmp_path, issue([], author=MARVIN, labels=["External"],
                             body="Upstream PR: https://github.com/outsider/inspect_ai/pull/9"))
    r = s.run(CHECKOUT, str(N), "--dry-run")
    assert r.returncode == 3
    assert f"REFUSED: not under {UPSTREAM}" in r.stderr
    assert not any(c.startswith("pr view") for c in s.calls())


def test_checkout_body_line_ignored_on_an_ordinary_issue(tmp_path):
    s = Stub(tmp_path, issue([], author="outsider",
                             body=f"Upstream PR: https://github.com/{UPSTREAM}/pull/5336"))
    r = s.run(CHECKOUT, str(N))
    assert r.returncode == 3
    assert "REFUSED: issue is not an External proxy (author=outsider labels=none)" in r.stderr
    assert not any(c.startswith("pr view") or c.startswith("pr checkout") for c in s.calls())


# --- promote.sh -------------------------------------------------------------


def promote_calls_wrote_nothing(calls):
    return not any(" -X POST" in c or c.startswith("pr close") or c.startswith("pr comment") for c in calls)


def test_promote_refuses_outsider_chip_and_falls_back_to_the_agents_fixes_ref(tmp_path):
    # The scanner's scenario: the org PR bases on main (inert Fixes ref, no
    # chip); the outsider's default-branch PR is the only chip.
    outsider = chip(500, author="outsider", head_repo="outsider/inspect_ai", branch="claude/issue-42-x",
                    base="meridian", title="Evil", body=f"Fixes #{N}\n@everyone")
    ours = open_pr(401, branch="claude/issue-42-20260901", body=f"Summary.\n\nFixes #{N}\n")
    s = Stub(tmp_path, issue([outsider]), open_prs=[
        open_pr(500, author="outsider", head_repo="outsider/inspect_ai", branch="claude/issue-42-x",
                body=f"Fixes #{N}"), ours])
    r = s.run(PROMOTE, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "RESOLVED: fork PR #401 (OPEN) via open fork PR matched by closing ref or branch convention" in r.stdout
    assert "#500 OPEN" in r.stdout and "REFUSED: head repository is 'outsider/inspect_ai'" in r.stdout
    assert 'head=claude/issue-42-20260901 -f head_repo=meridianlabs-ai/inspect_ai -f title="PR 401"' in r.stdout
    assert "Evil" not in r.stdout.split("RESOLVED", 1)[1]
    assert promote_calls_wrote_nothing(s.calls())


def test_promote_refuses_when_no_candidate_qualifies_before_any_write(tmp_path):
    outsider = chip(500, author="outsider", head_repo="outsider/inspect_ai", branch="claude/issue-42-x")
    s = Stub(tmp_path, issue([outsider]), open_prs=[
        open_pr(500, author="outsider", head_repo="outsider/inspect_ai", branch="claude/issue-42-x",
                body=f"Fixes #{N}")])
    r = s.run(PROMOTE, str(N))  # a real run: it must stop before any write
    assert r.returncode == 3, r.stdout
    assert "NO QUALIFYING FORK PR for issue #42" in r.stderr
    assert "Looked for:" in r.stderr and "gh pr list --repo meridianlabs-ai/inspect_ai --state open" in r.stderr
    assert r.stderr.count("#500 OPEN") == 1  # judged once as a chip, not again as an open PR
    assert promote_calls_wrote_nothing(s.calls())
    assert not any(c.startswith("api repos/UKGovernmentBEIS") for c in s.calls())


def test_promote_fixes_ref_and_branch_name_are_read_only_after_the_rule(tmp_path):
    # An outsider PR that is NOT a chip but carries both fallback signals
    # (closing ref and agent branch name) must be refused, never matched.
    s = Stub(tmp_path, issue([]), open_prs=[
        open_pr(500, author="outsider", head_repo="outsider/inspect_ai", branch=f"claude/issue-{N}-x",
                body=f"Fixes meridianlabs-ai/inspect_ai#{N}")])
    r = s.run(PROMOTE, str(N), "--dry-run")
    assert r.returncode == 3
    assert "#500 OPEN" in r.stderr and "REFUSED: head repository is 'outsider/inspect_ai'" in r.stderr
    assert "qualifies" not in r.stderr


def test_promote_ambiguous_open_chips_refuse_without_pr_and_resolve_with_it(tmp_path):
    # agents #32: issue 308 had two open fork PRs; promote took the first.
    s = Stub(tmp_path, issue([chip(309, branch="claude/issue-42-a"), chip(345, branch="claude/issue-42-b")]))
    r = s.run(PROMOTE, str(N))
    assert r.returncode == 6, r.stdout
    assert "AMBIGUOUS: more than one open fork-PR chip qualifies for issue #42" in r.stderr
    assert "#309 OPEN" in r.stderr and "#345 OPEN" in r.stderr and "--pr <number>" in r.stderr
    assert promote_calls_wrote_nothing(s.calls())

    r2 = s.run(PROMOTE, str(N), "--dry-run", "--pr", "345")
    assert r2.returncode == 0, r2.stderr
    assert "RESOLVED: fork PR #345 (OPEN) via --pr 345" in r2.stdout
    assert "head=claude/issue-42-b" in r2.stdout
    assert "WARN" not in r2.stderr


def test_promote_pinned_pr_must_still_pass_the_rule(tmp_path):
    outsider = chip(500, author="outsider", head_repo="outsider/inspect_ai", branch="claude/issue-42-x")
    s = Stub(tmp_path, issue([chip(400), outsider]),
             pr_views=[(FORK, open_pr(777, author="outsider", head_repo="outsider/inspect_ai", branch="x"))])
    r = s.run(PROMOTE, str(N), "--pr", "500")  # a chip
    assert r.returncode == 3 and "REFUSED --pr 500: head repository is 'outsider/inspect_ai'" in r.stderr
    r = s.run(PROMOTE, str(N), "--pr", "777")  # not a chip: looked up on the fork
    assert r.returncode == 3 and "REFUSED --pr 777: head repository is 'outsider/inspect_ai'" in r.stderr
    r = s.run(PROMOTE, str(N), "--pr", "778")  # no such PR
    assert r.returncode == 3 and "--pr 778 is not a PR on meridianlabs-ai/inspect_ai" in r.stderr
    assert promote_calls_wrote_nothing(s.calls())


def test_promote_pinned_pr_without_a_link_to_the_issue_warns(tmp_path):
    s = Stub(tmp_path, issue([]), pr_views=[(FORK, open_pr(600, branch="human-named", body="no ref"))])
    r = s.run(PROMOTE, str(N), "--dry-run", "--pr", "600")
    assert r.returncode == 0, r.stderr
    assert "RESOLVED: fork PR #600 (OPEN) via --pr 600" in r.stdout
    assert "WARN: --pr 600 is not linked to issue #42" in r.stderr


def test_promote_pr_flag_validation(tmp_path):
    s = Stub(tmp_path, issue([]))
    assert s.run(PROMOTE, str(N), "--pr").returncode == 1
    assert s.run(PROMOTE, str(N), "--pr", "abc").returncode == 1
    assert s.run(PROMOTE, "--dry-run").returncode == 1
    assert s.calls() == []


def test_promote_no_chip_falls_back_to_branch_convention(tmp_path):
    # agents #33: the fork PR is linked only by an inert ref or its branch name.
    s = Stub(tmp_path, issue([]), open_prs=[
        open_pr(401, branch=f"claude/issue-{N}-20260901", body="no closing ref"),
        open_pr(402, branch="claude/issue-7-x", body="Fixes #7"),
        open_pr(403, branch=f"claude/issue-{N}0-x", body=f"Fixes #{N}0"),  # #420, not #42
    ])
    r = s.run(PROMOTE, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "RESOLVED: fork PR #401 (OPEN) via open fork PR matched by closing ref or branch convention" in r.stdout
    assert "#402 OPEN" in r.stdout and "no reference to issue #42" in r.stdout
    assert "#403 OPEN" in r.stdout


def test_promote_trusts_the_apps_login_in_graphql_chips_and_in_the_gh_json_fallback(tmp_path):
    # A chip (GraphQL: bare Bot login) and a fallback-listed PR (`gh --json
    # author`: `app/<slug>`) authored by the machine account's App login both
    # qualify by name, with no lookup; another App's fallback PR is refused.
    s = Stub(tmp_path, issue([chip(400, author=BOT_AUTHOR, branch=f"claude/issue-{N}-a")]))
    r = s.run(PROMOTE, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "RESOLVED: fork PR #400 (OPEN)" in r.stdout
    assert not any("/permission" in c for c in s.calls())
    s2 = Stub(tmp_path / "b", issue([]), open_prs=[
        open_pr(401, author="app/meridian-marvin", branch=f"claude/issue-{N}-x", body="")])
    r2 = s2.run(PROMOTE, str(N), "--dry-run")
    assert r2.returncode == 0, r2.stderr
    assert "RESOLVED: fork PR #401 (OPEN) via open fork PR matched by closing ref or branch convention" in r2.stdout
    assert not any("/permission" in c for c in s2.calls())
    s3 = Stub(tmp_path / "c", issue([]), open_prs=[open_pr(401, author="app/foo", branch=f"claude/issue-{N}-x", body="")])
    r3 = s3.run(PROMOTE, str(N), "--dry-run")
    assert r3.returncode == 3, r3.stdout + r3.stderr
    assert "author 'foo[bot]' is not in TRUSTED_LOGINS" in r3.stderr


def test_promote_no_chip_accepts_the_bare_issue_branch_convention_and_qualified_ref(tmp_path):
    s = Stub(tmp_path, issue([]), open_prs=[open_pr(401, branch=f"issue-{N}-fix", body="")])
    r = s.run(PROMOTE, str(N), "--dry-run")
    assert r.returncode == 0 and "RESOLVED: fork PR #401" in r.stdout, r.stderr
    s2 = Stub(tmp_path / "b", issue([]), open_prs=[open_pr(401, branch="topic", body=f"Closes meridianlabs-ai/inspect_ai#{N}")])
    r2 = s2.run(PROMOTE, str(N), "--dry-run")
    assert r2.returncode == 0 and "RESOLVED: fork PR #401" in r2.stdout, r2.stderr


def test_promote_no_chip_and_no_match_fails_loudly(tmp_path):
    s = Stub(tmp_path, issue([]), open_prs=[open_pr(402, branch="claude/issue-7-x", body="Fixes #7")])
    r = s.run(PROMOTE, str(N))
    assert r.returncode == 3
    assert "NO QUALIFYING FORK PR for issue #42" in r.stderr
    assert f"Fixes|Closes|Resolves #{N}" in r.stderr and f"claude/issue-{N}-*" in r.stderr
    assert "#402 OPEN" in r.stderr and "no reference to issue #42" in r.stderr
    assert promote_calls_wrote_nothing(s.calls())


def test_promote_no_chip_and_two_matches_is_ambiguous(tmp_path):
    s = Stub(tmp_path, issue([]), open_prs=[
        open_pr(401, branch=f"claude/issue-{N}-a", body=""),
        open_pr(405, branch="topic", body=f"Resolves #{N}"),
    ])
    r = s.run(PROMOTE, str(N))
    assert r.returncode == 6
    assert "AMBIGUOUS: more than one open fork PR matched by closing ref or branch convention" in r.stderr
    assert "#401 OPEN" in r.stderr and "#405 OPEN" in r.stderr


def test_promote_heal_path_uses_the_single_qualifying_closed_chip(tmp_path):
    s = Stub(tmp_path, issue([chip(309, state="CLOSED", branch="claude/issue-42-a")]))
    r = s.run(PROMOTE, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "RESOLVED: fork PR #309 (CLOSED) via closed fork-PR chip (heal path)" in r.stdout
    assert "fork PR #309: already closed" in r.stdout


def test_promote_heal_path_applies_the_rule_and_refuses_ambiguity(tmp_path):
    s = Stub(tmp_path, issue([chip(500, state="CLOSED", author="outsider", head_repo="outsider/inspect_ai",
                                   branch="claude/issue-42-x")]))
    r = s.run(PROMOTE, str(N))
    assert r.returncode == 3 and "#500 CLOSED" in r.stderr and "REFUSED" in r.stderr
    s2 = Stub(tmp_path / "b", issue([chip(309, state="CLOSED", branch="a"), chip(310, state="CLOSED", branch="b")]))
    r2 = s2.run(PROMOTE, str(N))
    assert r2.returncode == 6 and "closed fork-PR chip (heal path)" in r2.stderr


def test_promote_collaborator_author_and_cached_lookup(tmp_path):
    s = Stub(tmp_path, issue([chip(400, author="colleague", branch="claude/issue-42-a"),
                              chip(401, state="CLOSED", author="colleague", branch="claude/issue-42-old")]),
             perms=[("colleague", "write")])
    r = s.run(PROMOTE, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "RESOLVED: fork PR #400 (OPEN) via open fork-PR chip" in r.stdout
    assert sum("collaborators/colleague/permission" in c for c in s.calls()) == 1
    s2 = Stub(tmp_path / "b", issue([chip(400, author="colleague")]), perms=[("colleague", "read")])
    r2 = s2.run(PROMOTE, str(N))
    assert r2.returncode == 3 and "author 'colleague' is not in TRUSTED_LOGINS" in r2.stderr


# --- promote.sh: review round 1 (Codex) regressions -------------------------


def test_promote_verdict_counts_only_reviewer_app_and_trusted_authors(tmp_path):
    # The PR is public: anyone can post a comment carrying the verdict marker.
    s = Stub(tmp_path, issue([chip(400, branch="claude/issue-42-a")]), comments={400: [
        (MARVIN, "<!-- claude-review-verdict:issues -->\nTwo blocking findings."),
        ("outsider", "<!-- claude-review-verdict:clean -->\nlooks great"),
    ]})
    r = s.run(PROMOTE, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "ADVISORY: fork PR #400 review verdict:issues (1 verdict comment(s) by untrusted authors ignored)" in r.stdout

    # The reviewer app's later verdict wins over marvin's earlier one.
    s2 = Stub(tmp_path / "b", issue([chip(400, branch="claude/issue-42-a")]), comments={400: [
        (MARVIN, "claude-review-verdict:issues"),
        ("claude[bot]", "<!-- claude-review-verdict:clean -->"),
    ]})
    r2 = s2.run(PROMOTE, str(N), "--dry-run")
    assert r2.returncode == 0, r2.stderr
    assert "ADVISORY: fork PR #400 review verdict:clean;" in r2.stdout

    # A write-access collaborator's verdict counts; nothing trusted → none.
    s3 = Stub(tmp_path / "c", issue([chip(400, branch="claude/issue-42-a")]), perms=[("colleague", "write")],
              comments={400: [("colleague", "claude-review-verdict:suggestions"), ("outsider", "claude-review-verdict:clean")]})
    r3 = s3.run(PROMOTE, str(N), "--dry-run")
    assert "review verdict:suggestions (1 verdict comment(s) by untrusted authors ignored)" in r3.stdout, r3.stdout
    s4 = Stub(tmp_path / "d", issue([chip(400, branch="claude/issue-42-a")]),
              comments={400: [("outsider", "claude-review-verdict:clean")]})
    r4 = s4.run(PROMOTE, str(N), "--dry-run")
    assert "review verdict:none (1 verdict comment(s) by untrusted authors ignored)" in r4.stdout, r4.stdout


@pytest.mark.parametrize("branch", ["main", "meridian"])
def test_promote_refuses_protected_branches_as_promotion_heads(tmp_path, branch):
    # The create path merges upstream main INTO the head branch; a trusted
    # author's PR from main/meridian must never reach it, pinned or not.
    s = Stub(tmp_path, issue([chip(400, branch=branch)]))
    for args in ([str(N)], [str(N), "--pr", "400"]):
        r = s.run(PROMOTE, *args)
        assert r.returncode == 5, r.stdout
        assert f"REFUSED: fork PR #400's head is the protected branch {branch}" in r.stderr
    assert not any("/merges" in c or " -X POST" in c for c in s.calls())
    assert not any(c.startswith("api repos/UKGovernmentBEIS") for c in s.calls())


def test_promote_refuses_when_the_fork_branch_moved_past_the_prs_head(tmp_path):
    s = Stub(tmp_path, issue([chip(400, branch="claude/issue-42-a", head_sha="aaa")]),
             branches=[("claude/issue-42-a", "bbb")])
    r = s.run(PROMOTE, str(N))
    assert r.returncode == 5, r.stdout
    assert "ABORT: fork PR #400's head is aaa but meridianlabs-ai/inspect_ai:claude/issue-42-a is at bbb" in r.stderr
    assert not any("/merges" in c or " -X POST" in c for c in s.calls())
    # Same for a pinned PR looked up on the fork, and for a closed PR that
    # has no upstream PR to adopt (the create path would run).
    s2 = Stub(tmp_path / "b", issue([]), pr_views=[(FORK, open_pr(600, branch="topic", head_sha="aaa", body=f"Fixes #{N}"))],
              branches=[("topic", "bbb")])
    r2 = s2.run(PROMOTE, str(N), "--pr", "600")
    assert r2.returncode == 5 and "the branch moved since the PR was read" in r2.stderr
    s3 = Stub(tmp_path / "c", issue([chip(309, state="CLOSED", branch="claude/issue-42-a", head_sha="aaa")]),
              branches=[("claude/issue-42-a", "bbb")])
    r3 = s3.run(PROMOTE, str(N))
    assert r3.returncode == 5 and "the branch moved since the PR was read" in r3.stderr


def test_promote_heal_tolerates_a_moved_branch_only_when_adopting_the_upstream_pr(tmp_path):
    # After promotion the fork PR is closed and upstream reviewers push to the
    # branch; healing the bookkeeping must still work — nothing touches the branch.
    fork_pr = chip(309, state="CLOSED", branch="claude/issue-42-a", head_sha="aaa")
    up_pr = chip(5001, state="OPEN", repo=UPSTREAM, head_repo=FORK, author="ransomr", branch="claude/issue-42-a")
    s = Stub(tmp_path, issue([fork_pr, up_pr]), branches=[("claude/issue-42-a", "bbb")], perms=[("ransomr", "admin")])
    r = s.run(PROMOTE, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "ADOPTED existing upstream PR #5001" in r.stdout
    assert "note: claude/issue-42-a is at bbb, past closed fork PR #309's head aaa" in r.stderr
    assert "/merges" not in r.stdout


def test_promote_fallback_lookup_failure_aborts_instead_of_healing_a_closed_chip(tmp_path):
    # A failed `gh pr list` used to read as "no open PRs" and fall through to
    # the closed chip, re-promoting an old branch.
    s = Stub(tmp_path, issue([chip(309, state="CLOSED", branch="claude/issue-42-a")]), prlist_fail=True)
    r = s.run(PROMOTE, str(N))
    assert r.returncode == 5, r.stdout
    assert "ABORT: could not list the open fork PRs" in r.stderr
    assert "RESOLVED" not in r.stdout
    assert not any(" -X POST" in c or c.startswith("api repos/UKGovernmentBEIS") for c in s.calls())


def test_promote_fallback_refuses_a_truncated_listing(tmp_path):
    # A listing as long as the limit may hide a second match past the page;
    # uniqueness cannot be established, so neither the single visible match
    # nor the closed chip is taken.
    many = [open_pr(1000 + i, branch=f"claude/issue-7-{i}", body="Fixes #7") for i in range(499)]
    many.append(open_pr(2000, branch=f"claude/issue-{N}-a", body=""))  # exactly LIST_LIMIT rows, one match
    s = Stub(tmp_path, issue([chip(309, state="CLOSED", branch="claude/issue-42-old")]), open_prs=many)
    r = s.run(PROMOTE, str(N))
    assert r.returncode == 5, r.stdout
    assert "ABORT: meridianlabs-ai/inspect_ai has 500 or more open PRs — the listing is truncated" in r.stderr
    assert "RESOLVED" not in r.stdout
    # One short of the limit is complete: the single match resolves.
    s2 = Stub(tmp_path / "b", issue([]), open_prs=many[1:])
    r2 = s2.run(PROMOTE, str(N), "--dry-run")
    assert r2.returncode == 0 and "RESOLVED: fork PR #2000" in r2.stdout, r2.stderr


def test_promote_verdict_is_unavailable_when_the_comment_lookup_fails_part_way(tmp_path):
    # Page one carried a trusted `clean`; page two (which might hold a newer
    # blocking verdict) failed. Partial rows must not leave `clean` standing.
    s = Stub(tmp_path, issue([chip(400, branch="claude/issue-42-a")]),
             comments={400: [(MARVIN, "<!-- claude-review-verdict:clean -->")]}, comments_fail=[400])
    r = s.run(PROMOTE, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    advisory = [l for l in r.stdout.splitlines() if l.startswith("ADVISORY:")][0]
    assert "review verdict:unavailable (comment lookup failed);" in advisory
    assert "clean" not in advisory
    assert "WARN: could not read fork PR #400's comments" in r.stderr


# --- promote.sh: the upstream PR body (findings 4629156 and 4629152) ---------
#
# The body promote.sh publishes upstream under the operator's name has two
# outsider-writable inputs: the fork ISSUE's `Upstream issue:` line (which
# adds a bare `Fixes #<up>` that closes an upstream issue on merge) and the
# fork PR body's bare `#M` refs (written for the fork's tracker, they rebind
# to upstream's once republished on a PR based on upstream main). The line
# is honoured only as /import's header — the body's first line — from an
# author passing the trust rule; every bare ref is qualified to the fork; and
# the body is printed as it will be published so the operator can see it.

UP_N = 2615  # the upstream issue an import header names
IMPORT_HEADER = f"Upstream issue: https://github.com/{UPSTREAM}/issues/{UP_N}"


def import_body(header=IMPORT_HEADER, snapshot="Outsider's upstream text.\n"):
    """A fork issue body as import.sh writes it: header, `---` rule, snapshot."""
    return f"{header}\n\nImported from upstream so the agents can work it here.\n\n---\n\n{snapshot}"


def published_body(stdout):
    """The upstream PR body promote.sh printed (each line prefixed `  | `)."""
    lines = stdout.splitlines()
    start = lines.index("upstream PR body (as published):") + 1
    out = []
    for line in lines[start:]:
        if not line.startswith("  | "):
            break
        out.append(line[4:])
    return "\n".join(out)


def advisory(stdout):
    return [l for l in stdout.splitlines() if l.startswith("ADVISORY:")][0]


def test_promote_honours_the_import_header_from_a_trusted_issue_author(tmp_path):
    # A genuine import: the machine-written header from a trusted importer.
    # The snapshot below the `---` rule is the upstream author's text and may
    # itself carry the line — that copy names #999 and must not be believed.
    s = Stub(tmp_path, issue([chip(400, branch="claude/issue-42-a")], author=MARVIN,
                             body=import_body(snapshot=f"Upstream issue: https://github.com/{UPSTREAM}/issues/999\n")))
    r = s.run(PROMOTE, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert f"upstream issue: #{UP_N}" in advisory(r.stdout)
    body = published_body(r.stdout)
    assert body.splitlines()[:2] == [f"Fixes #{UP_N}", f"Fixes meridianlabs-ai/inspect_ai#{N}"]
    assert "#999" not in body
    assert "Upstream issue:" not in r.stderr
    # A write-access importer qualifies too, through the same lookup the PR rule uses.
    s2 = Stub(tmp_path / "b", issue([chip(400, branch="claude/issue-42-a")], author="colleague",
                                    body=import_body()), perms=[("colleague", "write")])
    r2 = s2.run(PROMOTE, str(N), "--dry-run")
    assert r2.returncode == 0, r2.stderr
    assert published_body(r2.stdout).splitlines()[0] == f"Fixes #{UP_N}"
    assert sum("collaborators/colleague/permission" in c for c in s2.calls()) == 1


@pytest.mark.parametrize("perm", ["read", "none", "FAIL"])
def test_promote_ignores_the_upstream_issue_header_from_an_untrusted_issue_author(tmp_path, perm):
    # The scanner's scenario: any GitHub account files a fork issue whose body
    # opens with a forged import header naming an upstream issue of their
    # choosing. The agent's own PR qualifies; the header does not. A failed
    # permission lookup fails closed.
    s = Stub(tmp_path, issue([chip(400, branch="claude/issue-42-a")], author="outsider", body=import_body()),
             perms=[("outsider", perm)])
    r = s.run(PROMOTE, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "note: issue #42's 'Upstream issue:' header ignored — issue author 'outsider' is not in TRUSTED_LOGINS" in r.stderr
    assert "upstream issue: none" in advisory(r.stdout)
    body = published_body(r.stdout)
    assert f"#{UP_N}" not in body
    assert not re.search(r"(?<![\w/])#\d+", body), body  # no bare ref at all
    assert body.splitlines()[0] == f"Fixes meridianlabs-ai/inspect_ai#{N}"
    assert sum("collaborators/outsider/permission" in c for c in s.calls()) == 1


NOT_FIRST = "note: issue #42's 'Upstream issue:' line ignored — it is not the body's first line"
NO_RULE = "note: issue #42's 'Upstream issue:' header ignored — no `---` rule follows it"


@pytest.mark.parametrize("body, note", [
    # a trusted author's issue carrying the line only below the `---` rule
    (import_body(header="Mirror of an upstream report.", snapshot=f"{IMPORT_HEADER}\n"), NOT_FIRST),
    # hidden in an HTML comment GitHub renders invisibly
    (f"<!-- {IMPORT_HEADER} -->\nA plausible bug report.", NOT_FIRST),
    # not at the start of its line
    (f"See {IMPORT_HEADER}\n\n---\n\nsnapshot", NOT_FIRST),
    # preceded by prose, so not the header
    (f"Please look at this.\n{IMPORT_HEADER}\n", NOT_FIRST),
    # the literal first line is blank: the header is the second line
    ("\n" + import_body(), NOT_FIRST),
    # a URL under another repository
    (import_body(header=f"Upstream issue: https://github.com/outsider/inspect_ai/issues/{UP_N}"), NOT_FIRST),
    # the header without /import's `---` rule: a hand-written body
    (f"{IMPORT_HEADER}\nA hand-written report.", NO_RULE),
    # a rule-like line that is not a rule
    (f"{IMPORT_HEADER}\n\n--- snapshot ---\n\ntext", NO_RULE),
], ids=["below-rule", "html-comment", "mid-line", "second-line", "blank-first-line", "other-repo",
        "no-rule", "rule-with-text"])
def test_promote_ignores_an_upstream_issue_line_that_is_not_the_import_header(tmp_path, body, note):
    # Even from a trusted author (marvin here), only /import's shape counts:
    # the header as the literal first line, a `---` rule below it.
    s = Stub(tmp_path, issue([chip(400, branch="claude/issue-42-a")], author=MARVIN, body=body))
    r = s.run(PROMOTE, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert note in r.stderr
    assert "upstream issue: none" in advisory(r.stdout)
    assert f"#{UP_N}" not in published_body(r.stdout)
    assert not any("/permission" in c for c in s.calls())  # nothing to look up: the shape failed first


def test_promote_issue_without_the_line_needs_no_author_lookup(tmp_path):
    s = Stub(tmp_path, issue([chip(400, branch="claude/issue-42-a")], author="outsider", body="A bug report."))
    r = s.run(PROMOTE, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "upstream issue: none" in advisory(r.stdout)
    assert "Upstream issue:" not in r.stderr
    assert not any("/permission" in c for c in s.calls())


def test_promote_qualifies_every_bare_ref_in_the_upstream_body(tmp_path):
    # The scanner's body: closing keywords in other cases and for other
    # issues, a bare mention, and refs that are already qualified (never
    # double-qualified) — including an upstream ref the agent copied from an
    # import snapshot and an HTML entity.
    fork_body = (f"Summary.\n\nCloses #7\nfixes #{N}\nsee #8, UKGovernmentBEIS/inspect_ai#5, "
                 f"meridianlabs-ai/inspect_ai#9 and &#123; in step #1.\n")
    s = Stub(tmp_path, issue([chip(400, branch="claude/issue-42-a", body=fork_body)]))
    r = s.run(PROMOTE, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    body = published_body(r.stdout)
    assert not re.search(r"(?<![\w/&])#\d+", body), body  # no bare ref survives
    assert "Closes meridianlabs-ai/inspect_ai#7" in body
    assert f"fixes meridianlabs-ai/inspect_ai#{N}" in body
    assert "see meridianlabs-ai/inspect_ai#8, UKGovernmentBEIS/inspect_ai#5, meridianlabs-ai/inspect_ai#9 and &#123; in step meridianlabs-ai/inspect_ai#1." in body
    # `fixes #N` counts as the closing ref to this issue: no second one is prepended.
    assert body.count(f"meridianlabs-ai/inspect_ai#{N}") == 1
    assert not body.startswith("Fixes")
    assert '-f body=<the body printed above>' in r.stdout
    assert promote_calls_wrote_nothing(s.calls())


def test_promote_leaves_qualified_refs_link_destinations_and_urls_alone(tmp_path):
    # Review round 1: a `#M` glued to a qualified repository name (whatever
    # its last character), an HTML entity, a Markdown link destination or a
    # URL fragment is not a bare issue reference and must survive verbatim,
    # while bare refs next to punctuation are still qualified.
    keep = ("[Reproduction](#1-reproduction) [fragment](https://example.test/?q=#123) "
            "other/repo-#7 other/repo.#8 other/repo_x#9 repo1#10 UKGovernmentBEIS/inspect_ai#11 &#123; "
            "https://github.com/x/y/pull/5#issuecomment-6 <https://e.test/#15> https://e.test/a#16")
    bare = "(#21) **#22** |#23| a,#24 \"#25\" [#26]"
    fork_body = f"Summary.\n\n{keep}\n{bare}\n#27 at the start of a line\nCloses\t#28\n"
    s = Stub(tmp_path, issue([chip(400, branch="claude/issue-42-a", body=fork_body)]))
    r = s.run(PROMOTE, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    body = published_body(r.stdout)
    assert body.splitlines()[0] == f"Fixes meridianlabs-ai/inspect_ai#{N}"
    assert keep in body  # every kept token verbatim, none double-qualified
    for m in range(21, 29):
        assert f"meridianlabs-ai/inspect_ai#{m}" in body, (m, body)
    assert "Closes\tmeridianlabs-ai/inspect_ai#28" in body
    assert not re.search(r"(?<![\w/&.=-])#\d+", body.replace("](#1-", "")), body  # no bare ref left


@pytest.mark.parametrize("ref, prepended", [
    (f"Fixed #{N}", False), (f"RESOLVED #{N}", False), (f"closed #{N}", False), (f"Close #{N}", False),
    (f"Resolves meridianlabs-ai/inspect_ai#{N}", False),
    (f"see #{N}", True),          # a mention is not a closing ref
    (f"Fixes #{N}0", True),       # #420 is another issue
    ("no ref at all", True),
], ids=["Fixed", "RESOLVED", "closed", "Close", "qualified", "mention", "other-issue", "none"])
def test_promote_prepends_the_fixes_ref_only_when_no_closing_keyword_variant_names_the_issue(tmp_path, ref, prepended):
    s = Stub(tmp_path, issue([chip(400, branch="claude/issue-42-a", body=f"Summary.\n\n{ref}\n")]))
    r = s.run(PROMOTE, str(N), "--dry-run")
    assert r.returncode == 0, r.stderr
    body = published_body(r.stdout)
    assert body.startswith(f"Fixes meridianlabs-ai/inspect_ai#{N}\n\n") is prepended, body
    assert not re.search(r"(?<![\w/&])#\d+", body), body


def test_promote_real_run_prints_the_published_body_before_creating_anything(tmp_path):
    # Not --dry-run: the body is logged before the branch merge and the PR
    # creation, so the operator sees every closing reference at creation.
    # The stub knows no merges endpoint, so the run aborts there — after the
    # body was printed and before anything upstream was written.
    s = Stub(tmp_path, issue([chip(400, branch="claude/issue-42-a", body="Closes #7\n")], author="outsider",
                             body=import_body()))
    r = s.run(PROMOTE, str(N))
    assert r.returncode == 5, r.stdout + r.stderr
    assert "ABORT: could not merge upstream main" in r.stderr
    assert f"upstream issue: none" in advisory(r.stdout)
    body = published_body(r.stdout)
    assert body == f"Fixes meridianlabs-ai/inspect_ai#{N}\n\nCloses meridianlabs-ai/inspect_ai#7"
    assert f"#{UP_N}" not in body
    assert not any(c.startswith("api repos/UKGovernmentBEIS/inspect_ai/pulls") for c in s.calls())
