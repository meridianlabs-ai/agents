"""Tests for skills/merge-approved-prs/approval_at_head.py — the merge queue's
approval-to-head binding (Claude Security finding 4122327).

The decision function runs on canned API payloads; the command line runs end
to end against a stub `gh` on PATH that serves those payloads and logs every
call, so the request shape (GETs only, `--paginate` on the lists) is pinned
as well as the verdicts. The skill's approval-bound checkout blocks (promotion
and External) are lifted from SKILL.md and run against local repos, and the
merge / re-approval commands are checked for the head pin. Run with
`python3 -m pytest` from the repo root.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "merge-approved-prs" / "approval_at_head.py"
SKILL = ROOT / "skills" / "merge-approved-prs" / "SKILL.md"

spec = importlib.util.spec_from_file_location("approval_at_head", SCRIPT)
aah = importlib.util.module_from_spec(spec)
sys.modules["approval_at_head"] = aah
spec.loader.exec_module(aah)

REPO = "UKGovernmentBEIS/inspect_ai"
PR_PATH = f"repos/{REPO}/pulls/42"
SHA1 = "a" * 40
SHA2 = "b" * 40
SHA3 = "c" * 40
T_COMMIT = "2026-09-10T09:00:00Z"  # the reviewed commits' committer date
T_REVIEW = "2026-09-10T12:00:00Z"
T_LATER = "2026-09-11T08:00:00Z"
T_LATER2 = "2026-09-11T09:00:00Z"
MAINTAINERS = {"dragonstyle", "ransomr"}

_ids = iter(range(1000, 10_000))


def review(login, state, sha, at):
    return {"id": next(_ids), "user": {"login": login}, "state": state, "commit_id": sha, "submitted_at": at}


def commit(sha, date):
    return {"sha": sha, "commit": {"committer": {"date": date}, "author": {"date": date}}}


def pr(head):
    return {"number": 42, "state": "open", "head": {"sha": head}}


def trust(login):
    return login in MAINTAINERS


def run_check(head, reviews, commits=None, is_trusted=trust):
    if commits is None:
        commits = [commit(head, T_COMMIT)]
    return aah.check(pr(head), reviews, commits, is_trusted, REPO)


# --- the decision -----------------------------------------------------------


def test_approved_at_head_passes():
    v = run_check(SHA1, [review("dragonstyle", "APPROVED", SHA1, T_REVIEW)])
    assert v.ok
    assert v.message == f"approved {SHA1} by dragonstyle at {T_REVIEW}"


def test_approved_then_pushed_skips_naming_both_shas_and_the_pushed_commits():
    commits = [commit(SHA1, T_COMMIT), commit(SHA2, T_LATER), commit(SHA3, T_LATER2)]
    v = run_check(SHA3, [review("dragonstyle", "APPROVED", SHA1, T_REVIEW)], commits)
    assert not v.ok
    assert v.message == f"approval is for {SHA1}, head is {SHA3}; 2 commits pushed after {T_REVIEW}"


def test_head_moved_with_no_later_dated_commit_still_skips():
    # A rebase or a backdated commit: the head is not the approved SHA even
    # though no PR commit is dated after the approval. Dates never rescue it.
    v = run_check(SHA2, [review("dragonstyle", "APPROVED", SHA1, T_REVIEW)], [commit(SHA2, T_COMMIT)])
    assert not v.ok
    assert v.message == (
        f"approval is for {SHA1}, head is {SHA2}; head moved after {T_REVIEW} "
        "(no PR commit is dated later: rewritten history)"
    )


def test_no_reviews_skips():
    v = run_check(SHA1, [])
    assert not v.ok
    assert v.message == f"no approval for head {SHA1}"


def test_approval_then_changes_requested_by_same_reviewer_skips():
    reviews = [
        review("dragonstyle", "APPROVED", SHA1, T_REVIEW),
        review("dragonstyle", "CHANGES_REQUESTED", SHA1, T_LATER),
    ]
    v = run_check(SHA1, reviews)
    assert not v.ok
    assert v.message == (
        f"no approval for head {SHA1}: dragonstyle approved it at {T_REVIEW} "
        f"but their latest review is CHANGES_REQUESTED at {T_LATER}"
    )


def test_dismissed_review_is_not_an_approval():
    # Dismissing an approval rewrites that review's state to DISMISSED.
    v = run_check(SHA1, [review("dragonstyle", "DISMISSED", SHA1, T_REVIEW)])
    assert not v.ok
    assert v.message == f"no approval for head {SHA1}: latest verdict is DISMISSED by dragonstyle at {T_REVIEW}"


def test_comment_after_approval_keeps_it_standing():
    reviews = [review("dragonstyle", "APPROVED", SHA1, T_REVIEW), review("dragonstyle", "COMMENTED", SHA1, T_LATER)]
    assert run_check(SHA1, reviews).ok


def test_comment_after_changes_requested_does_not_clear_it():
    # The head is back at the SHA the reviewer once approved, but their
    # standing verdict is changes requested; a later COMMENTED review does
    # not lift it (GitHub's own semantics).
    reviews = [
        review("dragonstyle", "APPROVED", SHA1, T_COMMIT),
        review("dragonstyle", "CHANGES_REQUESTED", SHA2, T_REVIEW),
        review("dragonstyle", "COMMENTED", SHA2, T_LATER),
    ]
    v = run_check(SHA1, reviews)
    assert not v.ok
    assert "latest review is CHANGES_REQUESTED" in v.message


def test_other_reviewer_in_good_standing_carries_the_approval():
    reviews = [
        review("dragonstyle", "APPROVED", SHA1, T_COMMIT),
        review("dragonstyle", "CHANGES_REQUESTED", SHA1, T_REVIEW),
        review("ransomr", "APPROVED", SHA1, T_LATER),
    ]
    v = run_check(SHA1, reviews)
    assert v.ok
    assert v.message == f"approved {SHA1} by ransomr at {T_LATER}"


def test_untrusted_approver_at_head_skips():
    # The sock-puppet case: a maintainer approved the benign head, the
    # contributor pushed, and a second account "approved" the new head while
    # the stale approval keeps reviewDecision at APPROVED.
    reviews = [review("dragonstyle", "APPROVED", SHA1, T_REVIEW), review("sockpuppet", "APPROVED", SHA2, T_LATER)]
    v = run_check(SHA2, reviews, [commit(SHA1, T_COMMIT), commit(SHA2, T_LATER)])
    assert not v.ok
    assert (
        v.message == f"no approval for head {SHA2}: approval by sockpuppet does not count (no write access on {REPO})"
    )


def test_head_back_at_approved_sha_passes_despite_activity_in_between():
    # Force-pushed away and back to the identical SHA: same content, passes.
    reviews = [review("dragonstyle", "APPROVED", SHA1, T_REVIEW), review("someone", "COMMENTED", SHA2, T_LATER)]
    assert run_check(SHA1, reviews).ok


def test_commit_dated_after_its_own_approval_passes():
    # Committer clocks run ahead (UKGovernmentBEIS/inspect_ai#5360): the SHA
    # is the binding, dates are diagnostic only.
    v = run_check(SHA1, [review("dragonstyle", "APPROVED", SHA1, T_REVIEW)], [commit(SHA1, T_LATER)])
    assert v.ok


def test_mismatch_names_the_latest_standing_approval():
    reviews = [review("ransomr", "APPROVED", SHA1, T_COMMIT), review("dragonstyle", "APPROVED", SHA2, T_REVIEW)]
    v = run_check(SHA3, reviews, [commit(SHA3, T_LATER)])
    assert v.message == f"approval is for {SHA2}, head is {SHA3}; 1 commit pushed after {T_REVIEW}"


def test_pending_reviews_are_ignored():
    reviews = [{"id": 1, "user": {"login": "dragonstyle"}, "state": "PENDING", "commit_id": SHA1, "submitted_at": None}]
    v = run_check(SHA1, reviews)
    assert not v.ok
    assert v.message == f"no approval for head {SHA1}"


def test_review_without_a_user_never_counts():
    v = run_check(SHA1, [{"id": 1, "user": None, "state": "APPROVED", "commit_id": SHA1, "submitted_at": T_REVIEW}])
    assert not v.ok


# --- trust ------------------------------------------------------------------


def test_trust_lookup_accepts_write_or_admin_and_caches_per_login():
    perms = {
        "dragonstyle": {"permission": "write", "role_name": "maintain"},
        "jjallaire": {"permission": "admin", "role_name": "admin"},
        "reader": {"permission": "read", "role_name": "read"},
        "github-actions[bot]": {"permission": "none", "role_name": ""},
    }
    calls = []

    def api(path, **kwargs):
        calls.append(path)
        return perms[path.split("/")[-2]]

    is_trusted = aah.make_trust_check(REPO, api)
    assert is_trusted("dragonstyle") and is_trusted("jjallaire")
    assert not is_trusted("reader") and not is_trusted("github-actions[bot]")
    assert is_trusted("DragonStyle")  # cached, logins are case-insensitive
    assert calls == [f"repos/{REPO}/collaborators/{login}/permission" for login in perms]


def test_trust_lookup_failure_is_untrusted():
    def api(path, **kwargs):
        raise aah.GhError("gh api: HTTP 404")

    assert not aah.make_trust_check(REPO, api)("ghost")


def test_trust_lookup_rejects_a_malformed_login_without_a_request():
    calls = []

    def api(path, **kwargs):
        calls.append(path)
        return {"permission": "admin"}

    assert not aah.make_trust_check(REPO, api)("evil/../permission?x=")
    assert calls == []


def test_trusted_logins_skip_the_lookup(monkeypatch):
    monkeypatch.setattr(aah, "TRUSTED_LOGINS", frozenset({"i-am-marvin"}))

    def api(path, **kwargs):
        raise AssertionError("no lookup expected for a trusted login")

    assert aah.make_trust_check(REPO, api)("I-Am-Marvin")


# --- parsing ----------------------------------------------------------------


def test_parse_json_stream_joins_paginated_arrays_and_passes_single_values():
    assert aah.parse_json_stream('[{"a": 1}][{"a": 2}]\n[{"a": 3}]\n') == [{"a": 1}, {"a": 2}, {"a": 3}]
    assert aah.parse_json_stream('{"x": 1}\n') == {"x": 1}
    with pytest.raises(aah.GhError):
        aah.parse_json_stream("")
    with pytest.raises(aah.GhError):
        aah.parse_json_stream('{"x": 1}{"y": 2}')


def test_parse_target_accepts_a_pr_url_or_a_repo_and_number():
    assert aah.parse_target([f"https://github.com/{REPO}/pull/5388"]) == (REPO, 5388)
    assert aah.parse_target([f"https://github.com/{REPO}/pull/5388/files#diff-1"]) == (REPO, 5388)
    assert aah.parse_target([REPO, "5388"]) == (REPO, 5388)
    for bad in ([], ["5388"], [f"https://github.com/{REPO}/issues/5388"], [REPO, "x"], ["a/b/c", "1"]):
        with pytest.raises(ValueError):
            aah.parse_target(bad)


# --- the command line, against a stub gh ------------------------------------

FAKE_GH_PY = r"""
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
fixtures = json.load(open(os.environ["FAKE_GH_FIXTURES"]))
path = args[1] if len(args) > 1 and args[0] == "api" else None
if path not in fixtures:
    sys.stderr.write("gh: Not Found (HTTP 404)\n")
    sys.exit(1)
body = fixtures[path]
if isinstance(body, dict) and "__sequence__" in body:
    seen = sum(1 for line in open(os.environ["FAKE_GH_LOG"]) if json.loads(line)[1:2] == [path])
    body = body["__sequence__"][min(seen - 1, len(body["__sequence__"]) - 1)]
if isinstance(body, list) and "--paginate" in args and len(body) > 1:
    # The older gh shape: one array per page, back to back.
    sys.stdout.write(json.dumps(body[:1]) + json.dumps(body[1:]))
else:
    sys.stdout.write(json.dumps(body) + "\n")
"""


def fixtures_for(head, reviews, commits, perms):
    fx = {PR_PATH: pr(head), f"{PR_PATH}/reviews": reviews, f"{PR_PATH}/commits": commits}
    for login, perm in perms.items():
        fx[f"repos/{REPO}/collaborators/{login}/permission"] = perm
    return fx


def run_cli(tmp_path, args, fixtures):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "fake_gh.py").write_text(FAKE_GH_PY)
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fake_gh.py"}" "$@"\n')
    gh.chmod(0o755)
    fx = tmp_path / "fixtures.json"
    fx.write_text(json.dumps(fixtures))
    log = tmp_path / "gh.log"
    log.write_text("")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_FIXTURES": str(fx),
        "FAKE_GH_LOG": str(log),
    }
    r = subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True, env=env, check=False)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return r, calls


def test_cli_passes_with_reads_only(tmp_path):
    reviews = [review("dragonstyle", "APPROVED", SHA1, T_REVIEW), review("dragonstyle", "COMMENTED", SHA1, T_LATER)]
    perms = {"dragonstyle": {"permission": "write", "role_name": "maintain"}}
    r, calls = run_cli(
        tmp_path, [f"https://github.com/{REPO}/pull/42"], fixtures_for(SHA1, reviews, [commit(SHA1, T_COMMIT)], perms)
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout == f"approved {SHA1} by dragonstyle at {T_REVIEW}\n"
    assert [c[1] for c in calls] == [
        PR_PATH,
        f"{PR_PATH}/reviews",
        f"{PR_PATH}/commits",
        PR_PATH,  # re-read: the head must not have moved while the lists were fetched
        f"repos/{REPO}/collaborators/dragonstyle/permission",
    ]
    for c in calls:
        assert c[0] == "api"
        # GETs only: no -X/--method, no -f/-F/--input, nothing but --paginate.
        assert all(a == "--paginate" for a in c[2:]), c
    assert "--paginate" in calls[1] and "--paginate" in calls[2] and "--paginate" not in calls[0]


def test_cli_skips_a_pushed_head_with_exit_1_and_no_lookup(tmp_path):
    fx = fixtures_for(
        SHA2, [review("dragonstyle", "APPROVED", SHA1, T_REVIEW)], [commit(SHA1, T_COMMIT), commit(SHA2, T_LATER)], {}
    )
    r, calls = run_cli(tmp_path, [REPO, "42"], fx)
    assert r.returncode == 1
    assert r.stdout == f"approval is for {SHA1}, head is {SHA2}; 1 commit pushed after {T_REVIEW}\n"
    assert not any("/collaborators/" in c[1] for c in calls)


def test_cli_permission_404_is_untrusted(tmp_path):
    fx = fixtures_for(SHA1, [review("nobody", "APPROVED", SHA1, T_REVIEW)], [commit(SHA1, T_COMMIT)], {})
    r, calls = run_cli(tmp_path, [REPO, "42"], fx)
    assert r.returncode == 1
    assert r.stdout == f"no approval for head {SHA1}: approval by nobody does not count (no write access on {REPO})\n"
    assert calls[-1][1] == f"repos/{REPO}/collaborators/nobody/permission"


def test_cli_gh_failure_is_exit_2(tmp_path):
    r, _ = run_cli(tmp_path, [REPO, "42"], {})
    assert r.returncode == 2
    assert r.stdout == ""
    assert "HTTP 404" in r.stderr


def test_cli_usage_error_is_exit_2_without_a_request(tmp_path):
    r, calls = run_cli(tmp_path, ["nonsense"], {})
    assert r.returncode == 2
    assert calls == []
    assert "usage" in r.stderr


def test_cli_head_moved_during_the_check_is_exit_1_without_a_lookup(tmp_path):
    # Approved at SHA1 when the check started; the contributor pushed SHA2
    # while the reviews/commits were being read. The stale "approved SHA1"
    # must not be printed.
    perms = {"dragonstyle": {"permission": "write", "role_name": "maintain"}}
    fx = fixtures_for(SHA1, [review("dragonstyle", "APPROVED", SHA1, T_REVIEW)], [commit(SHA1, T_COMMIT)], perms)
    fx[PR_PATH] = {"__sequence__": [pr(SHA1), pr(SHA2)]}
    r, calls = run_cli(tmp_path, [REPO, "42"], fx)
    assert r.returncode == 1
    assert r.stdout == f"head moved during the check: was {SHA1}, now {SHA2}\n"
    assert [c[1] for c in calls] == [PR_PATH, f"{PR_PATH}/reviews", f"{PR_PATH}/commits", PR_PATH]


# --- the skill's checkout block, against local repos -------------------------

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "queue",
    "GIT_AUTHOR_EMAIL": "queue@example.com",
    "GIT_COMMITTER_NAME": "queue",
    "GIT_COMMITTER_EMAIL": "queue@example.com",
}


def sh(*cmd, cwd, env=None, check=True):
    return subprocess.run(
        cmd, cwd=cwd, text=True, capture_output=True, env={**os.environ, **GIT_ENV, **(env or {})}, check=check
    )


def git(*args, cwd, check=True):
    return sh("git", *args, cwd=cwd, check=check)


def commit_file(repo, name, text, message):
    (repo / name).write_text(text)
    git("add", name, cwd=repo)
    git("commit", "-q", "-m", message, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def skill_block(after_heading):
    """The first ```bash block after `after_heading` in SKILL.md."""
    text = SKILL.read_text()
    start = text.index(after_heading)
    opened = text.index("```bash\n", start) + len("```bash\n")
    return text[opened : text.index("```", opened)]


@pytest.fixture
def queue_repos(tmp_path):
    """`origin` (upstream main), `meridian` (the fork, PR branch at A), and the queue
    worktree, detached at origin/main."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", "-b", "main", cwd=seed)
    commit_file(seed, "README", "base\n", "base")
    origin, meridian = tmp_path / "origin.git", tmp_path / "meridian.git"
    for bare in (origin, meridian):
        git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    git("remote", "add", "origin", str(origin), cwd=seed)
    git("remote", "add", "meridian", str(meridian), cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)
    git("checkout", "-q", "-b", "feature", cwd=seed)
    approved = commit_file(seed, "feature.txt", "A\n", "A: the reviewed commit")
    git("push", "-q", "meridian", "feature", cwd=seed)
    git("checkout", "-q", "main", cwd=seed)
    commit_file(seed, "main.txt", "main moved on\n", "main moves")
    git("push", "-q", "origin", "main", cwd=seed)
    work = tmp_path / "work"
    sh("git", "clone", "-q", str(origin), str(work), cwd=tmp_path)
    git("remote", "add", "meridian", str(meridian), cwd=work)
    git("checkout", "-q", "--detach", "origin/main", cwd=work)
    return {"seed": seed, "work": work, "approved": approved}


def push_after_approval(seed, text):
    """The contributor pushes another commit on the PR branch; returns its SHA."""
    git("checkout", "-q", "feature", cwd=seed)
    sha = commit_file(seed, "feature.txt", text, "B: pushed after the approval")
    git("push", "-q", "meridian", "feature", cwd=seed)
    return sha


def run_checkout_block(work, approved, branch="feature"):
    return sh(
        "bash",
        "-e",
        "-c",
        skill_block("## 2. Per PR, in order"),
        cwd=work,
        env={"BRANCH": branch, "APPROVED": approved},
        check=False,
    )


def test_skill_checkout_block_checks_out_the_approved_commit_and_merges_main(queue_repos):
    q = queue_repos
    r = run_checkout_block(q["work"], q["approved"])
    assert r.returncode == 0, r.stderr
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=q["work"]).stdout.strip() == "feature"
    head = git("rev-parse", "HEAD", cwd=q["work"]).stdout.strip()
    assert (
        git("rev-parse", "HEAD^1", cwd=q["work"]).stdout.strip() == q["approved"]
    )  # the merge sits on the approved commit
    assert git("merge-base", "--is-ancestor", "origin/main", head, cwd=q["work"]).returncode == 0


def test_skill_checkout_block_refuses_a_head_that_moved_after_the_check(queue_repos):
    q = queue_repos
    moved = push_after_approval(q["seed"], "B\n")
    before = git("rev-parse", "HEAD", cwd=q["work"]).stdout.strip()
    r = run_checkout_block(q["work"], q["approved"])
    assert r.returncode != 0
    # Stopped at the verification: nothing checked out, no branch created, no merge.
    assert git("rev-parse", "HEAD", cwd=q["work"]).stdout.strip() == before
    assert git("branch", "--list", "feature", cwd=q["work"]).stdout.strip() == ""
    assert not (q["work"] / "feature.txt").exists()
    assert git("rev-parse", "meridian/feature", cwd=q["work"]).stdout.strip() == moved  # fetched, seen, refused


def test_skill_checkout_block_accepts_a_tip_forced_back_to_the_approved_sha(queue_repos):
    q = queue_repos
    push_after_approval(q["seed"], "B\n")
    git("push", "-q", "-f", "meridian", f"{q['approved']}:refs/heads/feature", cwd=q["seed"])
    r = run_checkout_block(q["work"], q["approved"])
    assert r.returncode == 0, r.stderr
    assert git("rev-parse", "HEAD^1", cwd=q["work"]).stdout.strip() == q["approved"]


# --- the External checkout block: fetch, refuse, then check out the approved SHA ---


@pytest.fixture
def external_repos(tmp_path):
    """`origin` (upstream, serving refs/pull/42/head), the contributor's `fork` (branch
    feature at A) and the queue clone, whose hooks path points into the tree."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", "-b", "main", cwd=seed)
    commit_file(seed, "README", "base\n", "base")
    origin, fork = tmp_path / "origin.git", tmp_path / "fork.git"
    for bare in (origin, fork):
        git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    git("remote", "add", "origin", str(origin), cwd=seed)
    git("remote", "add", "fork", str(fork), cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)
    git("checkout", "-q", "-b", "feature", cwd=seed)
    approved = commit_file(seed, "feature.txt", "A\n", "A: the reviewed commit")
    git("push", "-q", "fork", "feature", cwd=seed)
    git("push", "-q", "origin", "feature:refs/pull/42/head", cwd=seed)  # what GitHub serves for the PR head
    git("checkout", "-q", "main", cwd=seed)
    commit_file(seed, "main.txt", "main moved on\n", "main moves")
    git("push", "-q", "origin", "main", cwd=seed)
    work = tmp_path / "work"
    sh("git", "clone", "-q", str(origin), str(work), cwd=tmp_path)
    git("config", "core.hooksPath", ".githooks", cwd=work)  # the reviewer's scenario: hooks resolved inside the tree
    git("checkout", "-q", "--detach", "origin/main", cwd=work)
    return {"seed": seed, "work": work, "fork": fork, "approved": approved, "marker": tmp_path / "hook-ran"}


def contributor_pushes_a_hook(q):
    """After the approval, the contributor pushes B carrying a post-checkout hook; GitHub moves refs/pull/42/head."""
    seed = q["seed"]
    git("checkout", "-q", "feature", cwd=seed)
    hooks = seed / ".githooks"
    hooks.mkdir()
    hook = hooks / "post-checkout"
    hook.write_text(f'#!/bin/sh\ntouch "{q["marker"]}"\n')
    hook.chmod(0o755)
    git("add", ".githooks", cwd=seed)
    git("commit", "-q", "-m", "B: pushed after the approval, with a hook", cwd=seed)
    git("push", "-q", "fork", "feature", cwd=seed)
    git("push", "-q", "origin", "feature:refs/pull/42/head", cwd=seed)
    return git("rev-parse", "HEAD", cwd=seed).stdout.strip()


def run_external_block(q, branch="feature"):
    block = skill_block("- **Checkout/push**").replace("<n>", "42")
    env = {"BRANCH": branch, "APPROVED": q["approved"], "FORK_URL": str(q["fork"])}
    return sh("bash", "-e", "-c", block, cwd=q["work"], env=env, check=False)


def test_skill_external_block_checks_out_the_approved_commit_and_pushes_to_the_fork(external_repos):
    q = external_repos
    r = run_external_block(q)
    assert r.returncode == 0, r.stderr
    assert git("rev-parse", "HEAD", cwd=q["work"]).stdout.strip() == q["approved"]
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=q["work"]).stdout.strip() == "feature"
    for key in ("remote", "pushRemote"):
        assert git("config", f"branch.feature.{key}", cwd=q["work"]).stdout.strip() == str(q["fork"])
    assert git("config", "branch.feature.merge", cwd=q["work"]).stdout.strip() == "refs/heads/feature"
    # The rest of the skill's flow: merge main, then a plain push lands on the contributor's branch.
    git("merge", "-q", "--no-edit", "origin/main", cwd=q["work"])
    git("push", "-q", cwd=q["work"])
    merged = git("rev-parse", "HEAD", cwd=q["work"]).stdout.strip()
    assert git("rev-parse", "feature", cwd=q["fork"]).stdout.strip() == merged
    assert git("rev-parse", "HEAD^1", cwd=q["work"]).stdout.strip() == q["approved"]


def test_skill_external_block_never_materializes_a_moved_head_or_runs_its_hook(external_repos):
    q = external_repos
    moved = contributor_pushes_a_hook(q)
    before = git("rev-parse", "HEAD", cwd=q["work"]).stdout.strip()
    r = run_external_block(q)
    assert r.returncode != 0
    # Fetched and compared, then refused: nothing checked out, no branch, no tree, no hook run.
    assert git("rev-parse", "FETCH_HEAD", cwd=q["work"]).stdout.strip() == moved
    assert git("rev-parse", "HEAD", cwd=q["work"]).stdout.strip() == before
    assert git("branch", "--list", "feature", cwd=q["work"]).stdout.strip() == ""
    assert not (q["work"] / "feature.txt").exists() and not (q["work"] / ".githooks").exists()
    assert not q["marker"].exists()
    # The fixture is potent: what `gh pr checkout` amounts to (fetch the PR head into a
    # branch and check it out) runs B's hook before any comparison could refuse it.
    git("fetch", "-q", "origin", "refs/pull/42/head:naive", cwd=q["work"])
    git("checkout", "-q", "naive", cwd=q["work"])
    assert q["marker"].exists()


def test_skill_pins_every_upstream_merge_request_and_re_approval_to_the_pushed_commit():
    lines = SKILL.read_text().splitlines()
    merges = [line for line in lines if "gh pr merge" in line and "UKGovernmentBEIS/inspect_ai" in line]
    assert merges, "SKILL.md no longer shows the upstream merge command"
    for line in merges:
        assert '--match-head-commit "$(git rev-parse HEAD)"' in line, line
    approvals = [line for line in lines if "event=APPROVE" in line]
    assert approvals, "SKILL.md no longer shows the re-approval"
    for line in approvals:
        assert 'commit_id="$(git rev-parse HEAD)"' in line, line
    # Both checkout paths verify the head against the approved SHA.
    assert sum('= "$APPROVED"' in line for line in lines) >= 2
    # The companion merge is pinned to the head companion_mergeable.py verified (or the one we pushed).
    companions = [line for line in lines if "gh pr merge" in line and "meridianlabs-ai/ts-mono" in line]
    assert companions, "SKILL.md no longer shows the companion merge command"
    for line in companions:
        assert '--match-head-commit "$COMPANION_HEAD"' in line, line


def test_skill_external_path_defers_to_ci_before_the_checkout():
    text = SKILL.read_text()
    external = text[text.index("## External PRs") :]
    checks = external.index("checks_at_head.py")
    checkout = external.index("- **Checkout/push**")
    assert checks < checkout, "the CI check must come before the External checkout block"
    assert '--sha "$APPROVED"' in external[checks : external.index("\n", checks)]


def companion_head_lines():
    lines = [
        line.strip() for line in SKILL.read_text().splitlines() if line.strip().startswith("COMPANION_HEAD=$(printf")
    ]
    assert len(lines) == 2, (
        "SKILL.md should derive COMPANION_HEAD before the update (step 2) and before the merge (step 3)"
    )
    return lines


@pytest.mark.parametrize("line", companion_head_lines())
def test_skill_companion_head_line_parses_both_helper_outputs(line):
    sha = "c" * 40
    for out in (
        f"approved {sha} by epatey at 2026-09-10T12:00:00Z",
        f"regenerate-only {sha}: packages/x/generated.ts by i-am-marvin",
    ):
        r = sh("bash", "-e", "-c", f'{line}\nprintf "%s" "$COMPANION_HEAD"', cwd=ROOT, env={"OUT": out})
        assert r.stdout == sha, (out, r.stdout, r.stderr)
    r = sh(
        "bash",
        "-e",
        "-c",
        f'{line}\nprintf "%s" "$COMPANION_HEAD"',
        cwd=ROOT,
        env={"OUT": f"no approval for head {sha}; not regenerate-only: empty diff"},
    )
    assert r.stdout == ""


# --- the companion merge block: re-verify on the final head, merge pinned to that SHA ---

FAKE_HELPER_PY = """
import os, sys
print(os.environ["FAKE_HELPER_OUT"])
sys.exit(int(os.environ["FAKE_HELPER_RC"]))
"""


def run_companion_merge_block(tmp_path, helper_out, helper_rc):
    """Step 3 of the ts-mono sequence, lifted from SKILL.md, with the helper and `gh` stubbed and logged."""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir(exist_ok=True)
    (skill_dir / "companion_mergeable.py").write_text(FAKE_HELPER_PY)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "gh.log"
    log.write_text("")
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\n')
    gh.chmod(0o755)
    block = skill_block("3. **Merge the companion**").replace("<skill-base-dir>", str(skill_dir)).replace("<n>", "7")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_HELPER_OUT": helper_out,
        "FAKE_HELPER_RC": str(helper_rc),
    }
    r = sh("bash", "-c", block, cwd=tmp_path, env=env, check=False)  # plain bash: the block must guard itself
    return r, log.read_text().splitlines()


def test_skill_companion_merge_block_merges_only_what_the_helper_verified(tmp_path):
    sha = "d" * 40
    r, gh_calls = run_companion_merge_block(
        tmp_path, f"regenerate-only {sha}: packages/x/generated.ts by i-am-marvin", 0
    )
    assert r.returncode == 0, r.stderr
    assert gh_calls == [f"pr merge 7 --repo meridianlabs-ai/ts-mono --squash --match-head-commit {sha}"]
    r, gh_calls = run_companion_merge_block(tmp_path, f"approved {sha} by epatey at 2026-09-10T12:00:00Z", 0)
    assert gh_calls == [f"pr merge 7 --repo meridianlabs-ai/ts-mono --squash --match-head-commit {sha}"]


def test_skill_companion_merge_block_never_merges_after_a_failed_recheck(tmp_path):
    # The update-to-merge sequence: step 2 pushed a new head of a hand-written
    # companion (or an approval was withdrawn), so the recheck fails — no merge.
    sha = "d" * 40
    r, gh_calls = run_companion_merge_block(
        tmp_path,
        f"approval is for {'c' * 40}, head is {sha}; 1 commit pushed after 2026-09-10T12:00:00Z; not regenerate-only: diff touches packages/inspect-common/src/types/index.ts, outside the generated set",
        1,
    )
    assert r.returncode != 0
    assert gh_calls == []
    assert "approval is for" in r.stdout  # the reason is echoed for the report
