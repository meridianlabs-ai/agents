"""Tests for skills/lib, the helpers the local maintainer skills share.

skills/THREAT_MODEL.md states the rules; these pin the one implementation
of each: the trusted-author check (TRUSTED_LOGINS by name, else a
write-access lookup that fails closed; no other App is trusted, and nothing
not shaped like a login reaches the request path), the External-proxy test,
the git pins for an outsider's tree (hooks, fsmonitor, every filter driver
the clone or the worktree defines, submodule recursion), and the
outbound-text guards (trigger-phrase rewrite, the rendered-reference parse,
the plain-text commit-message scan and the bare-reference rewrite the
agent workflows run). The bash helpers run under a stub `gh`
that logs every call. A last check keeps the skills on the shared copies.
Run with `python3 -m pytest` from the repo root.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "skills" / "lib"
COMMON = LIB / "common.sh"

spec = importlib.util.spec_from_file_location("outbound", LIB / "outbound.py")
outbound = importlib.util.module_from_spec(spec)
sys.modules["outbound"] = outbound
spec.loader.exec_module(outbound)

GH_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB/calls"
case "$*" in
  api\ repos/*/collaborators/*/permission*)
    key=$(sed -E 's#^api repos/([^/]+/[^/]+)/collaborators/([^/ ]+)/permission.*#\1:\2#' <<<"$*")
    perm=$(grep -F "$key=" "$STUB/perms" 2>/dev/null | head -1 | cut -d= -f2)
    [ "$perm" = FAIL ] && { echo "gh: HTTP 500" >&2; exit 1; }
    echo "${perm:-read}" ;;
  *) echo "stub gh: unexpected call: $*" >&2; exit 97 ;;
esac
"""
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


class Lib:
    def __init__(self, tmp_path, perms=()):
        self.dir = tmp_path / "stub"
        self.dir.mkdir()
        gh = tmp_path / "bin" / "gh"
        gh.parent.mkdir()
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        (self.dir / "perms").write_text("".join(f"{k}={v}\n" for k, v in perms))
        self.env = {**os.environ, **GIT_ENV, "PATH": f"{gh.parent}:{os.environ['PATH']}", "STUB": str(self.dir)}

    def sh(self, script, cwd=None, **env):
        return subprocess.run(["bash", "-c", f'set -euo pipefail; . "{COMMON}"; {script}'], cwd=cwd, text=True,
                              capture_output=True, env={**self.env, **env})

    def calls(self):
        p = self.dir / "calls"
        return p.read_text().splitlines() if p.exists() else []


def trusted(lib, repo, login):
    return lib.sh(f'trusted_login "$R" "$L" && echo yes || echo no', R=repo, L=login).stdout.strip() == "yes"


# --- trusted_login / check_pr / genuine_proxy / proxy_upstream_pr ------------


@pytest.mark.parametrize("login", ["i-am-marvin", "I-Am-Marvin", "meridian-marvin[bot]"])
def test_trusted_logins_are_trusted_by_name_without_a_lookup(tmp_path, login):
    lib = Lib(tmp_path)
    assert trusted(lib, "o/r", login)
    assert lib.calls() == []


@pytest.mark.parametrize("perm,ok", [("admin", True), ("maintain", True), ("write", True),
                                     ("triage", False), ("read", False), ("none", False), ("FAIL", False)])
def test_other_logins_need_write_access_and_a_failed_lookup_is_untrusted(tmp_path, perm, ok):
    lib = Lib(tmp_path, perms=[("o/r:colleague", perm)])
    assert trusted(lib, "o/r", "colleague") is ok
    assert lib.calls() == ["api repos/o/r/collaborators/colleague/permission --jq .permission"]


@pytest.mark.parametrize("login", ["github-actions[bot]", "claude[bot]", "", "a/b", "../x", "-x",
                                   "x y", "x$(touch pwned)"])
def test_apps_and_values_not_shaped_like_a_login_are_never_looked_up(tmp_path, login):
    lib = Lib(tmp_path, perms=[(f"o/r:{login}", "write")])
    assert trusted(lib, "o/r", login) is False
    assert lib.calls() == []
    assert not (tmp_path / "pwned").exists()


def test_a_user_named_after_the_apps_slug_is_an_ordinary_user(tmp_path):
    lib = Lib(tmp_path)
    assert trusted(lib, "o/r", "meridian-marvin") is False
    assert lib.calls() == ["api repos/o/r/collaborators/meridian-marvin/permission --jq .permission"]


def test_the_lookup_is_cached_per_repository_and_login(tmp_path):
    lib = Lib(tmp_path, perms=[("o/a:colleague", "write"), ("o/b:colleague", "read")])
    r = lib.sh('for i in 1 2; do trusted_login o/a colleague && echo a-ok; trusted_login o/b Colleague || echo b-no; done')
    assert r.stdout.split() == ["a-ok", "b-no", "a-ok", "b-no"]
    assert len(lib.calls()) == 2


def test_check_pr_reads_the_head_repository_first_then_the_author(tmp_path):
    lib = Lib(tmp_path, perms=[("o/r:colleague", "write")])
    def reason(pr):
        return lib.sh('check_pr o/r "$PR"; printf "%s" "$REASON"', PR=json.dumps(pr)).stdout
    assert reason({"headRepository": {"nameWithOwner": "o/r"}, "author": {"login": "colleague"}}) == ""
    assert reason({"headRepository": {"nameWithOwner": "o/r"}, "author": {"login": "meridian-marvin", "__typename": "Bot"}}) == ""
    assert reason({"headRepository": {"nameWithOwner": "o/r"}, "author": {"login": "app/meridian-marvin"}}) == ""
    assert "head repository is 'x/r', not o/r" in reason({"headRepository": {"nameWithOwner": "x/r"}, "author": {"login": "colleague"}})
    assert "head repository is 'unknown'" in reason({"headRepository": None, "author": {"login": "colleague"}})
    assert "author 'stranger' is not in TRUSTED_LOGINS" in reason({"headRepository": {"nameWithOwner": "o/r"}, "author": {"login": "stranger"}})
    assert "author 'unknown'" in reason({"headRepository": {"nameWithOwner": "o/r"}, "author": None})


@pytest.mark.parametrize("author,labels,ok", [
    ("i-am-marvin", "bug,External", True),
    ("meridian-marvin[bot]", "External", True),
    ("i-am-marvin", "bug", False),
    ("outsider", "External", False),
    ("colleague", "External", False),  # write access does not make a proxy
    ("", "External", False),
])
def test_genuine_proxy_is_the_machine_account_and_the_label(tmp_path, author, labels, ok):
    lib = Lib(tmp_path, perms=[("meridianlabs-ai/inspect_ai:colleague", "write")])
    r = lib.sh('genuine_proxy "$A" "$L" && echo yes || echo no', A=author, L=labels)
    assert (r.stdout.strip() == "yes") is ok
    assert lib.calls() == []


@pytest.mark.parametrize("body,line,num", [
    ("x\nUpstream PR: https://github.com/UKGovernmentBEIS/inspect_ai/pull/5360\n", True, "5360"),
    ("upstream pr:  https://github.com/UKGovernmentBEIS/inspect_ai/pull/7", True, "7"),
    ("Upstream PR: https://github.com/outsider/inspect_ai/pull/9", True, ""),
    ("Upstream PR: https://github.com/UKGovernmentBEIS/inspect_ai/pull/9/files", True, "9"),
    ("Upstream PR: https://github.com/UKGovernmentBEIS/inspect_ai_fork/pull/9", True, ""),
    ("no line here", False, ""),
])
def test_proxy_upstream_pr_accepts_only_a_pr_of_the_named_repository(tmp_path, body, line, num):
    lib = Lib(tmp_path)
    r = lib.sh('proxy_upstream_pr "$B" UKGovernmentBEIS/inspect_ai; printf "%s|%s" "$UP_LINE" "$UP_NUM"', B=body)
    got_line, got_num = r.stdout.split("|")
    assert bool(got_line) is line and got_num == num


# --- pin_git_config ---------------------------------------------------------


def git(*args, cwd, env=None, check=True):
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True,
                          env={**os.environ, **GIT_ENV, **(env or {})}, check=check)


@pytest.fixture
def hostile_clone(tmp_path):
    """A clone whose config runs relative commands, and a commit whose tree supplies them."""
    marker = tmp_path / "ran"
    repo = tmp_path / "clone"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "README").write_text("base\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "base", cwd=repo)
    base = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    run = f'#!/bin/sh\necho "$0" >>"{marker}"\ncat\n'
    for rel in (".githooks/post-checkout", "fsmon.sh", "smudge.sh", "wt-smudge.sh"):
        p = repo / rel
        p.parent.mkdir(exist_ok=True)
        p.write_text(run)
        p.chmod(0o755)
    (repo / ".gitattributes").write_text("*.txt filter=evil\n*.dat filter=wt\n")
    (repo / "a.txt").write_text("a\n")
    (repo / "b.dat").write_text("b\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "theirs", cwd=repo)
    theirs = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    git("checkout", "-q", base, cwd=repo)
    for key, value in (("core.hooksPath", ".githooks"), ("core.fsmonitor", "./fsmon.sh"),
                       ("filter.evil.smudge", "./smudge.sh"), ("filter.evil.required", "true"),
                       ("submodule.recurse", "true")):
        git("config", key, value, cwd=repo)
    # A driver only the linked worktree's own config defines.
    wt = tmp_path / "wt"
    git("worktree", "add", "-q", "--detach", "--no-checkout", str(wt), base, cwd=repo)
    git("config", "extensions.worktreeConfig", "true", cwd=repo)
    git("config", "--worktree", "filter.wt.smudge", "./wt-smudge.sh", cwd=wt)
    return {"repo": repo, "wt": wt, "theirs": theirs, "marker": marker}


def test_pins_keep_the_trees_hooks_fsmonitor_and_filters_inert(tmp_path, hostile_clone):
    h = hostile_clone
    lib = Lib(tmp_path)
    hooks = tmp_path / "nohooks"
    hooks.mkdir()
    r = lib.sh(f'pin_git_config "{hooks}" "{h["wt"]}"; git -C "{h["wt"]}" checkout -q --detach {h["theirs"]}; '
               f'git -C "{h["wt"]}" status --porcelain; env | grep ^GIT_CONFIG_ | sort', cwd=h["repo"])
    assert r.returncode == 0, r.stderr
    assert not h["marker"].exists()
    pins = dict(line.split("=", 1) for line in r.stdout.splitlines() if line.startswith("GIT_CONFIG_"))
    keys = {pins[k] for k in pins if k.startswith("GIT_CONFIG_KEY_")}
    assert {"core.hooksPath", "core.fsmonitor", "submodule.recurse", "fetch.recurseSubmodules",
            "filter.evil.smudge", "filter.evil.required", "filter.wt.smudge"} <= keys
    # Potent: the same checkout unpinned runs them.
    git("checkout", "-q", "--detach", h["theirs"], cwd=h["wt"], check=False)
    assert h["marker"].exists()


def test_pins_miss_a_worktree_only_driver_unless_the_worktree_is_named(tmp_path, hostile_clone):
    h = hostile_clone
    lib = Lib(tmp_path)
    hooks = tmp_path / "nohooks"
    hooks.mkdir()
    r = lib.sh(f'pin_git_config "{hooks}"; env | grep ^GIT_CONFIG_VALUE_ || true; env | grep ^GIT_CONFIG_KEY_', cwd=h["repo"])
    assert "filter.wt.smudge" not in r.stdout and "filter.evil.smudge" in r.stdout


def test_pins_replace_any_earlier_pins(tmp_path, hostile_clone):
    h = hostile_clone
    lib = Lib(tmp_path)
    r = lib.sh(f'pin_git_config /x; pin_git_config /y "{h["wt"]}"; echo "$GIT_CONFIG_COUNT"; '
               f'git config core.hooksPath', cwd=h["repo"])
    count, hooks_path = r.stdout.split()
    assert hooks_path == "/y" and int(count) == 4 + 4 * 2  # four fixed pins, four per driver (evil, wt)


# --- rendered_refs ------------------------------------------------------------


def test_rendered_refs_lists_the_repositorys_links_other_than_the_allowed(tmp_path):
    lib = Lib(tmp_path)
    html = ('<a class="issue-link" data-url="https://github.com/UKGovernmentBEIS/inspect_ai/issues/12">#12</a>'
            '<a href="https://github.com/UKGovernmentBEIS/inspect_ai/pull/5360">x</a>'
            '<a data-url="https://github.com/ukgovernmentbeis/inspect_ai/issues/3">#3</a>'
            '<a data-url="https://github.com/meridianlabs-ai/inspect_ai/issues/7">#7</a>'
            '<a data-url="https://github.com/UKGovernmentBEIS/inspect_ai/issues/12">#12</a>')
    r = lib.sh('rendered_refs "$H" UKGovernmentBEIS/inspect_ai 5360', H=html)
    assert r.returncode == 0 and r.stdout == "#3 #12 "
    assert lib.sh('rendered_refs "<p>none</p>" UKGovernmentBEIS/inspect_ai').stdout == ""


# --- outbound.py ----------------------------------------------------------------


def test_defang_backticks_trigger_phrases_and_splits_markers():
    s = "@Claude do it, @auto, @review, @i-am-marvin <!-- claude-review-verdict --> auto-review-rounds"
    out = outbound.defang(s)
    assert "@" not in out.replace("@review", "")
    assert "`Claude`" in out and "`i-am-marvin`" in out
    assert "claude-review verdict" in out and "auto review-rounds" in out
    assert outbound.defang("plain text, email a@b.com") == "plain text, email a@b.com"


def test_defang_review_rewrites_only_the_bodies():
    review = {"body": "@claude", "event": "COMMENT", "comments": [{"path": "@auto.py", "line": 1, "body": "@auto"}]}
    out = outbound.defang_review(review)
    assert out == {"body": "`claude`", "event": "COMMENT", "comments": [{"path": "@auto.py", "line": 1, "body": "`auto`"}]}


@pytest.mark.parametrize("text,refs", [
    ("Fixes #12", [12]),
    ("(#7) and GH-8 and gh-9", [7, 8, 9]),
    ("UKGovernmentBEIS/inspect_ai#10 and ukgovernmentbeis/INSPECT_AI#11", [10, 11]),
    ("https://github.com/UKGovernmentBEIS/inspect_ai/pull/13 and /issues/14",  [13]),
    ("https://github.com/UKGovernmentBEIS/inspect_ai/issues/14", [14]),
    ("`#15` in code still counts: commit messages are not Markdown", [15]),
    ("meridianlabs-ai/inspect_ai#16, other/repo#17, abc#18, &#19; x/#20", []),
    ("https://github.com/UKGovernmentBEIS/inspect_ai_x/issues/21", []),
    ("Fixes #2615 and #2615", []),  # allowed below
])
def test_text_refs_finds_every_reference_that_resolves_in_the_repository(text, refs):
    assert outbound.text_refs(text, "UKGovernmentBEIS/inspect_ai", allow=[2615]) == refs


def row(total, sha, message):
    return json.dumps([total, sha, message])


def test_commit_refs_reports_each_offending_commit_and_requires_a_complete_listing():
    lines = [row(3, "a" * 40, "Clean\n\nbody"), row(3, "b" * 40, "Fix it\n\nFixes #12, see #13"),
             row(3, "c" * 40, "From meridianlabs-ai/inspect_ai#4")]
    offenders, error = outbound.commit_refs(lines, "UKGovernmentBEIS/inspect_ai")
    assert error is None and offenders == [f"{'b' * 40} (Fix it): #12 #13"]
    assert outbound.commit_refs(lines[:2], "UKGovernmentBEIS/inspect_ai")[1] == "listed 2 of 3 commits"
    assert outbound.commit_refs([row(1, "a" * 40, "x"), row(2, "b" * 40, "y")], "o/r")[1] == "total_commits changed between pages"
    assert "not JSON" in outbound.commit_refs(["{"], "o/r")[1]
    assert "unexpected entry" in outbound.commit_refs([json.dumps({"sha": "a"})], "o/r")[1]
    assert outbound.commit_refs([], "o/r") == ([], None)


@pytest.mark.parametrize("stdin,rc", [
    (row(1, "a" * 40, "Clean") + "\n", 0),
    (row(1, "a" * 40, "Fixes #1") + "\n", 1),
    (row(2, "a" * 40, "Clean") + "\n", 2),
    ("garbage\n", 2),
])
def test_commit_refs_command_line_exit_codes(stdin, rc):
    r = subprocess.run([sys.executable, str(LIB / "outbound.py"), "commit-refs", "--repo", "UKGovernmentBEIS/inspect_ai"],
                       input=stdin, text=True, capture_output=True)
    assert r.returncode == rc, r.stderr


FORK = "meridianlabs-ai/inspect_ai"
UPSTREAM = "UKGovernmentBEIS/inspect_ai"


@pytest.mark.parametrize("text,qualified", [
    ("Fixes #12", f"Fixes {FORK}#12"),
    ("(#7) and GH-8 and gh-9", f"({FORK}#7) and {FORK}#8 and {FORK}#9"),
    ("`#15` in code too", f"`{FORK}#15` in code too"),
    ("Closes #1, #2\n\nsee #3.", f"Closes {FORK}#1, {FORK}#2\n\nsee {FORK}#3."),
    # Qualified, linked or not a reference: left as written.
    ("meridianlabs-ai/inspect_ai#16, UKGovernmentBEIS/inspect_ai#5627, abc#18, &#19; x/#20, #12abc",
     "meridianlabs-ai/inspect_ai#16, UKGovernmentBEIS/inspect_ai#5627, abc#18, &#19; x/#20, #12abc"),
    ("https://github.com/o/r/pull/13 and page#14", "https://github.com/o/r/pull/13 and page#14"),
])
def test_qualify_refs_writes_each_bare_reference_against_the_repository(text, qualified):
    assert outbound.qualify_refs(text, FORK) == qualified
    # Idempotent, and the same items in the repository it was written in,
    # none of them in another repository's tracker.
    assert outbound.qualify_refs(qualified, FORK) == qualified
    assert outbound.text_refs(qualified, FORK) == outbound.text_refs(text, FORK)
    assert outbound.text_refs(qualified, UPSTREAM) == outbound.text_refs(text, UPSTREAM, allow=outbound.text_refs(text, FORK))


def qrepo(tmp_path):
    """A repository whose `origin/main` has an upstream-style squash subject,
    and a branch `work` cut below it (the run's start)."""
    work = tmp_path / "work"
    work.mkdir()

    def g(*args, env=None):
        e = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", **(env or {})}
        return subprocess.run(["git", *args], cwd=work, env=e, check=True, text=True, capture_output=True).stdout.strip()

    g("init", "-q", "-b", "work")
    g("config", "user.email", "agent@example.com")
    g("config", "user.name", "agent")
    (work / "f").write_text("0\n")
    g("add", "f")
    g("commit", "-q", "-m", "start")
    start = g("rev-parse", "HEAD")
    g("checkout", "-q", "-b", "upstream")
    (work / "u").write_text("u\n")
    g("add", "u")
    g("commit", "-q", "-m", "Upstream fix (#5627)")
    g("update-ref", "refs/remotes/origin/main", "HEAD")
    g("checkout", "-q", "work")
    g("branch", "-D", "-q", "upstream")
    return work, g, start


def commit(work, g, name, message, date="1700000000 +0000"):
    (work / name).write_text(message)
    g("add", name)
    env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    g("commit", "-q", "-m", message, env=env)
    return g("rev-parse", "HEAD")


def qualify(work, start, repo=FORK):
    return subprocess.run([sys.executable, str(LIB / "outbound.py"), "qualify-commits", "--repo", repo, "--start", start],
                          cwd=work, text=True, capture_output=True, check=False,
                          env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})


def test_qualify_commits_rewrites_the_runs_own_commits_and_leaves_merged_ones(tmp_path):
    work, g, start = qrepo(tmp_path)
    upstream = g("rev-parse", "origin/main")
    first = commit(work, g, "a", "Fix the parser\n\nFixes #12, see GH-3.")
    # A base merge (the runner's sync, or the agent's own): the merged
    # commit's `(#5627)` is upstream's and stays; the merge commit is the
    # run's and names nothing.
    g("merge", "-q", "--no-edit", "origin/main")
    merge = g("rev-parse", "HEAD")
    tip = commit(work, g, "b", "Follow up on #12")
    before = {c: g("log", "-1", "--format=%T%n%an <%ae> %ad%n%cn <%ce> %cd", c) for c in (first, merge, tip)}
    r = qualify(work, start)
    assert r.returncode == 0, r.stderr
    assert len(r.stdout.splitlines()) == 3  # the merge is rewritten for its new parent
    assert g("log", "--format=%B", "-1", "HEAD~2") == f"Fix the parser\n\nFixes {FORK}#12, see {FORK}#3."
    assert g("log", "--format=%s", "-1", "HEAD") == f"Follow up on {FORK}#12"
    assert "#" not in g("log", "--format=%B", "-1", "HEAD~1")
    # The merged commit and the start are the same objects; trees, authors,
    # committers and dates are kept, and the work tree is untouched.
    assert g("rev-parse", "HEAD~1^2") == upstream
    assert g("log", "--format=%s", "-1", upstream) == "Upstream fix (#5627)"
    assert g("rev-parse", "HEAD~3") == start
    after = [g("log", "-1", "--format=%T%n%an <%ae> %ad%n%cn <%ce> %cd", c) for c in ("HEAD~2", "HEAD~1", "HEAD")]
    assert after == [before[first], before[merge], before[tip]]
    assert g("status", "--porcelain") == ""
    # Deterministic: the same commits give the same SHAs; a second run is a no-op.
    rewritten = g("rev-parse", "HEAD")
    g("update-ref", "HEAD", tip)
    assert qualify(work, start).returncode == 0 and g("rev-parse", "HEAD") == rewritten
    r = qualify(work, start)
    assert r.returncode == 0 and r.stdout == "" and g("rev-parse", "HEAD") == rewritten


def test_qualify_commits_stops_at_a_commit_a_remote_ref_reaches(tmp_path):
    # A fast-forward onto the base puts the base's commits on HEAD's
    # first-parent chain: they keep their SHAs, and only the run's commit
    # above them is rewritten.
    work, g, start = qrepo(tmp_path)
    g("merge", "-q", "--ff-only", "origin/main")
    upstream = g("rev-parse", "HEAD")
    commit(work, g, "a", "Revert part of #5627")
    r = qualify(work, start)
    assert r.returncode == 0, r.stderr
    assert g("rev-parse", "HEAD~1") == upstream
    assert g("log", "--format=%s", "-1", "HEAD") == f"Revert part of {FORK}#5627"
    assert g("log", "--format=%s", "-1", "HEAD~1") == "Upstream fix (#5627)"


def test_qualify_commits_moves_nothing_without_a_bare_reference(tmp_path):
    work, g, start = qrepo(tmp_path)
    tip = commit(work, g, "a", f"Fix it\n\nFixes {FORK}#12")
    r = qualify(work, start)
    assert r.returncode == 0 and r.stdout == "" and g("rev-parse", "HEAD") == tip
    # Nothing above the start: nothing to do either.
    g("reset", "-q", "--hard", start)
    r = qualify(work, start)
    assert r.returncode == 0 and r.stdout == "" and g("rev-parse", "HEAD") == start


def own_bare(g, start, repo=FORK):
    """The run's commits (above start, off the remotes) that still carry a bare reference."""
    own = g("rev-list", "HEAD", "--not", start, "--remotes").split()
    return [c for c in own if outbound.qualify_refs(g("log", "-1", "--format=%B", c), repo) != g("log", "-1", "--format=%B", c)]


def test_qualify_commits_rewrites_a_merged_side_branch(tmp_path):
    # A side branch from the start, committed on and merged back --no-ff:
    # its commit is the run's too and is qualified, the merge mapped onto it.
    work, g, start = qrepo(tmp_path)
    g("checkout", "-q", "-b", "side")
    side = commit(work, g, "s", "Fixes #12 on the side")
    g("checkout", "-q", "work")
    commit(work, g, "a", "Main line")
    g("merge", "-q", "--no-ff", "--no-edit", "side")
    r = qualify(work, start)
    assert r.returncode == 0, r.stderr
    assert g("log", "-1", "--format=%s", "HEAD^2") == f"Fixes {FORK}#12 on the side"
    assert own_bare(g, start) == []
    assert subprocess.run(["git", "merge-base", "--is-ancestor", side, "HEAD"], cwd=work, check=False).returncode == 1
    # SHAs only: no message text reaches the workflow log.
    assert r.stdout and all(re.fullmatch(r"[0-9a-f]{40} -> [0-9a-f]{40}", line) for line in r.stdout.splitlines())


def test_qualify_commits_leaves_no_original_reachable_through_a_side_branch(tmp_path):
    # A side branch forked AFTER a bare-reference commit on the main line:
    # rewriting only the main line would leave the original reachable through
    # the merge's second parent, next to its rewrite.
    work, g, start = qrepo(tmp_path)
    first = commit(work, g, "a", "Fixes #12")
    g("checkout", "-q", "-b", "side")
    commit(work, g, "s", "Side work")
    g("checkout", "-q", "work")
    commit(work, g, "b", "More")
    g("merge", "-q", "--no-ff", "--no-edit", "side")
    r = qualify(work, start)
    assert r.returncode == 0, r.stderr
    assert own_bare(g, start) == []
    assert subprocess.run(["git", "merge-base", "--is-ancestor", first, "HEAD"], cwd=work, check=False).returncode == 1
    log = g("log", "--format=%s", f"{start}..HEAD").splitlines()
    assert log.count(f"Fixes {FORK}#12") == 1 and "Fixes #12" not in log


def signed_tag(tmp_path, g, name, rev):
    """A signed tag (an ephemeral SSH key): merging one records a `mergetag`
    header, which an unsigned annotated tag does not."""
    key = tmp_path / f"{name}-key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    g("-c", "gpg.format=ssh", "-c", f"user.signingkey={key}", "tag", "-s", "-m", f"tag {name}", name, rev)


def merge_tag(g, name):
    g("merge", "-q", "--no-ff", "--no-edit", name)
    assert "\nmergetag object " in g("cat-file", "commit", "HEAD")


needs_ssh_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="no ssh-keygen to sign a tag")


@needs_ssh_keygen
def test_qualify_commits_drops_a_mergetag_whose_tagged_commit_was_rewritten(tmp_path):
    work, g, start = qrepo(tmp_path)
    g("checkout", "-q", "-b", "side")
    commit(work, g, "s", "Fixes #12")
    signed_tag(tmp_path, g, "side-tag", "HEAD")
    g("checkout", "-q", "work")
    commit(work, g, "a", "Main line")
    merge_tag(g, "side-tag")
    assert qualify(work, start).returncode == 0
    assert g("log", "-1", "--format=%s", "HEAD^2") == f"Fixes {FORK}#12"
    assert "mergetag" not in g("cat-file", "commit", "HEAD")


@needs_ssh_keygen
def test_qualify_commits_drops_a_mergetag_whose_tagged_commit_moved_with_its_ancestor(tmp_path):
    # The tagged commit's own message is clean, but its parent was rewritten,
    # so its SHA changed all the same.
    work, g, start = qrepo(tmp_path)
    commit(work, g, "a", "Fixes #12")
    g("checkout", "-q", "-b", "side")
    tagged = commit(work, g, "s", "Side work")
    signed_tag(tmp_path, g, "side-tag", "HEAD")
    g("checkout", "-q", "work")
    commit(work, g, "b", "More")
    merge_tag(g, "side-tag")
    assert qualify(work, start).returncode == 0
    assert g("log", "-1", "--format=%s", "HEAD^2") == "Side work" and g("rev-parse", "HEAD^2") != tagged
    assert "mergetag" not in g("cat-file", "commit", "HEAD")


@needs_ssh_keygen
def test_qualify_commits_keeps_a_mergetag_whose_tagged_commit_is_unchanged(tmp_path):
    # An upstream tag merged after a bare-reference commit: the merge is
    # rewritten for its first parent, and its tag still names its second.
    work, g, start = qrepo(tmp_path)
    upstream = g("rev-parse", "origin/main")
    signed_tag(tmp_path, g, "v1", upstream)
    commit(work, g, "a", "Fixes #12")
    merge_tag(g, "v1")
    tag_header = g("cat-file", "commit", "HEAD").split("\nmergetag ", 1)[1].split("\n\n", 1)[0]
    assert qualify(work, start).returncode == 0
    merge = g("cat-file", "commit", "HEAD")
    assert g("log", "-1", "--format=%s", "HEAD~1") == f"Fixes {FORK}#12"
    assert g("rev-parse", "HEAD^2") == upstream
    assert f"\nmergetag object {upstream}\n" in merge and tag_header in merge


def test_qualify_commits_leaves_commits_fetch_head_reaches(tmp_path):
    # A base fetched without a remote-tracking ref (`git fetch <url> main`)
    # is still the base: FETCH_HEAD bounds the run's commits like a remote.
    work, g, start = qrepo(tmp_path)
    upstream = g("rev-parse", "origin/main")
    g("update-ref", "-d", "refs/remotes/origin/main")
    (work / ".git" / "FETCH_HEAD").write_text(f"{upstream}\t\tbranch 'main' of https://example.com/u\n")
    g("merge", "-q", "--ff-only", upstream)
    commit(work, g, "a", "After the merge, see #5627")
    r = qualify(work, start)
    assert r.returncode == 0, r.stderr
    assert g("rev-parse", "HEAD~1") == upstream
    assert g("log", "-1", "--format=%s", upstream) == "Upstream fix (#5627)"
    assert g("log", "-1", "--format=%s", "HEAD") == f"After the merge, see {FORK}#5627"


def test_qualify_commits_fails_closed_on_a_bad_start(tmp_path):
    work, g, _ = qrepo(tmp_path)
    tip = commit(work, g, "a", "Fixes #12")
    r = qualify(work, "f" * 40)
    assert r.returncode == 2 and "qualify-commits: git rev-list failed" in r.stderr
    assert g("rev-parse", "HEAD") == tip


def test_qualify_commits_fails_when_head_cannot_move(tmp_path):
    # A stale ref lock: the rewritten commits exist but HEAD cannot point at
    # them, so the run's commits are still bare and the command fails.
    work, g, start = qrepo(tmp_path)
    tip = commit(work, g, "a", "Fixes #12")
    (work / ".git" / "refs" / "heads" / "work.lock").write_text("")
    r = qualify(work, start)
    assert r.returncode == 2 and "git update-ref failed" in r.stderr
    assert g("rev-parse", "HEAD") == tip


def test_qualify_commits_fails_when_a_bare_reference_survives(tmp_path, monkeypatch):
    # The final check, whatever let a bare reference through: here a
    # rewrite that changes nothing.
    work, g, start = qrepo(tmp_path)
    tip = commit(work, g, "a", "Fixes #12")
    monkeypatch.chdir(work)
    monkeypatch.setattr(outbound, "_qualify_commit", lambda raw, repo, parents: raw)
    with pytest.raises(ValueError, match=f"still carry a bare reference: {tip}"):
        outbound.qualify_commits(FORK, start)


def test_qualify_commit_drops_a_stale_signature_and_keeps_other_headers():
    raw = (b"tree " + b"1" * 40 + b"\nparent " + b"2" * 40 + b"\nparent " + b"3" * 40 +
           b"\nauthor a <a@x> 1 +0000\ncommitter c <c@x> 1 +0000\nencoding ISO-8859-1"
           b"\nmergetag object " + b"4" * 40 + b"\n type commit\n tag v1"
           b"\ngpgsig -----BEGIN PGP SIGNATURE-----\n \n abc\n -----END PGP SIGNATURE-----"
           b"\n\nFix #12 for caf\xe9\n")
    out = outbound._qualify_commit(raw, "o/r", {"2" * 40: "5" * 40})
    assert out == (b"tree " + b"1" * 40 + b"\nparent " + b"5" * 40 + b"\nparent " + b"3" * 40 +
                   b"\nauthor a <a@x> 1 +0000\ncommitter c <c@x> 1 +0000\nencoding ISO-8859-1"
                   b"\nmergetag object " + b"4" * 40 + b"\n type commit\n tag v1"
                   b"\n\nFix o/r#12 for caf\xe9\n")
    # A rewritten tagged parent takes its mergetag with it, continuation lines and all.
    out = outbound._qualify_commit(raw, "o/r", {"4" * 40: "6" * 40})
    assert b"mergetag" not in out and b"type commit" not in out and b"gpgsig" not in out
    assert b"\nencoding ISO-8859-1\n\nFix o/r#12" in out
    # Unchanged message and parents: the object is returned as it was, signature and all.
    assert outbound._qualify_commit(raw.replace(b"#12", b"o/r#12"), "o/r", {}) == raw.replace(b"#12", b"o/r#12")


def test_outbound_runs_on_the_macos_system_python():
    # The skills run on a maintainer's Mac, whose python3 may be 3.9.
    r = subprocess.run(["python3", str(LIB / "outbound.py"), "defang"], input="@claude", text=True, capture_output=True)
    assert r.returncode == 0 and r.stdout == "`claude`"


# --- one copy ---------------------------------------------------------------------


SCRIPTS = sorted(p for p in (ROOT / "skills").glob("*/*.sh") if p.parent != LIB)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_skills_use_the_shared_helpers_rather_than_their_own_copies(script):
    text = script.read_text()
    for defn in ("trusted_login()", "check_pr()", "pin_git_config()", "defang()", "genuine_proxy()"):
        assert defn not in text, f"{script} defines {defn}; use skills/lib/common.sh"
    assert not re.search(r"^TRUSTED_LOGINS=", text, re.M), f"{script} sets its own TRUSTED_LOGINS"
    if re.search(r"\b(trusted_login|check_pr|pin_git_config|defang|genuine_proxy|render_markdown)\b", text):
        assert '. "$(dirname "$(realpath "$0")")/../lib/common.sh"' in text or '. "$HERE/../lib/common.sh"' in text
