"""Tests for skills/merge-approved-prs/external.sh — the merge queue's External path.

Claude Security 4773883 and 4773882: the External flow ran checkout, merge,
commit, status, diff and push on a contributor's tree with the clone's
inherited hooks, fsmonitor and filter drivers live, and `git checkout -B
"$BRANCH"` plus `branch.$BRANCH.*` writes, with BRANCH the contributor's
headRefName, reset and rewired a branch of the shared clone. These tests
run the script for real in a linked worktree of a local clone whose
configuration points hooks, an fsmonitor and a required filter driver at
relative paths the contributor's tree supplies (a plain checkout of that
tree runs them all), against a local upstream serving `refs/pull/42/head`
and a local contributor fork reached through `url.<path>.insteadOf`. `gh`
is a stub that answers the one `gh pr view` the push makes. Run with
`python3 -m pytest` from the repo root.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXTERNAL = ROOT / "skills" / "merge-approved-prs" / "external.sh"
FORK_URL = "https://github.com/contrib/inspect_ai.git"

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}
GH_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$STUB/calls"
case "$*" in
  "pr view 42 --repo UKGovernmentBEIS/inspect_ai "*) cat "$STUB/pr.json" ;;
  *) echo "stub gh: unexpected call: $*" >&2; exit 97 ;;
esac
"""
CHANGELOG = "## Unreleased\n\n## v0.3.100 (01 September 2026)\n\n- Old entry\n"


def sh(*cmd, cwd, env=None, check=True):
    return subprocess.run(
        cmd, cwd=cwd, text=True, capture_output=True, env={**os.environ, **GIT_ENV, **(env or {})}, check=check
    )


def git(*args, cwd, check=True):
    return sh("git", *args, cwd=cwd, check=check)


def write(repo, name, text, mode=None):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode:
        path.chmod(mode)


def commit(repo, message):
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", message, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def hostile_tree(seed, marker):
    """The contributor's commit: hooks, an fsmonitor, filter scripts and the attributes selecting them."""
    run = f'#!/bin/sh\necho "$0" >>"{marker}"\n'
    for hook in ("post-checkout", "pre-commit", "commit-msg", "post-merge", "pre-push", "post-commit"):
        write(seed, f".githooks/{hook}", run, 0o755)
    write(seed, "fsmon.sh", run, 0o755)
    write(seed, "smudge.sh", f'#!/bin/sh\necho smudge >>"{marker}"\ncat\n', 0o755)
    write(seed, "clean.sh", f'#!/bin/sh\necho clean >>"{marker}"\ncat\n', 0o755)
    write(seed, ".gitattributes", "*.txt filter=evil\n")
    write(seed, "data.txt", "contributor data\n")


class Queue:
    def __init__(self, tmp_path, branch):
        self.tmp = tmp_path
        self.branch = branch
        self.marker = tmp_path / "ran"
        self.seed = tmp_path / "seed"
        self.seed.mkdir()
        git("init", "-q", "-b", "main", cwd=self.seed)
        write(self.seed, "README", "base\n")
        write(self.seed, "CHANGELOG.md", CHANGELOG)
        commit(self.seed, "base")
        self.origin = tmp_path / "origin.git"
        self.fork = tmp_path / "fork.git"
        for bare in (self.origin, self.fork):
            git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
        git("remote", "add", "origin", str(self.origin), cwd=self.seed)
        git("remote", "add", "fork", str(self.fork), cwd=self.seed)
        git("push", "-q", "origin", "main", cwd=self.seed)
        # The primary clone, as Ransom has it: main plus a local branch, and
        # command-running configuration that resolves relative paths.
        self.clone = tmp_path / "clone"
        sh("git", "clone", "-q", str(self.origin), str(self.clone), cwd=tmp_path)
        git("branch", "feature", "origin/main", cwd=self.clone)
        for key, value in (
            ("core.hooksPath", ".githooks"),
            ("core.fsmonitor", "./fsmon.sh"),
            ("filter.evil.smudge", "./smudge.sh"),
            ("filter.evil.clean", "./clean.sh"),
            ("filter.evil.required", "true"),
            (f"url.{self.fork}.insteadOf", FORK_URL),
        ):
            git("config", key, value, cwd=self.clone)
        # The contributor's PR: their branch (named by the test) at the approved commit.
        git("checkout", "-q", "-b", "contrib", cwd=self.seed)
        hostile_tree(self.seed, self.marker)
        write(self.seed, "CHANGELOG.md", "## Unreleased\n\n- Contributor entry\n\n## v0.3.100 (01 September 2026)\n\n- Old entry\n")
        self.approved = commit(self.seed, "A: the reviewed commit")
        git("push", "-q", "fork", f"contrib:refs/heads/{branch}", cwd=self.seed)
        git("push", "-q", "origin", "contrib:refs/pull/42/head", cwd=self.seed)
        # Upstream main moves on.
        git("checkout", "-q", "main", cwd=self.seed)
        write(self.seed, "main.txt", "main moved on\n")
        commit(self.seed, "main moves")
        git("push", "-q", "origin", "main", cwd=self.seed)
        git("fetch", "-q", "origin", cwd=self.clone)
        # The queue's throwaway worktree, detached at origin/main.
        self.queue = tmp_path / "merge-queue"
        git("worktree", "add", "-q", "--detach", str(self.queue), "origin/main", cwd=self.clone)
        assert not self.marker.exists()
        self.stub = tmp_path / "stub"
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "gh").write_text(GH_STUB)
        (tmp_path / "bin" / "gh").chmod(0o755)
        self.stub.mkdir()
        self.pr(maintainer_can_modify=True)
        self.before = self.snapshot()

    def pr(self, *, maintainer_can_modify):
        (self.stub / "pr.json").write_text(json.dumps({
            "headRefName": self.branch,
            "headRepository": {"id": "R_1", "name": "inspect_ai"},
            "headRepositoryOwner": {"id": "O_1", "login": "contrib"},
            "maintainerCanModify": maintainer_can_modify,
        }))

    def run(self, *args):
        env = {"PATH": f"{self.tmp / 'bin'}:{os.environ['PATH']}", "STUB": str(self.stub)}
        return sh("bash", str(EXTERNAL), *args, cwd=self.queue, env=env, check=False)

    def snapshot(self):
        """The primary clone's local branches, HEAD and config: what the fix must never touch."""
        return (
            git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", cwd=self.clone).stdout,
            git("rev-parse", "HEAD", cwd=self.clone).stdout,
            (self.clone / ".git" / "config").read_bytes(),
        )

    def ran(self):
        return self.marker.read_text() if self.marker.exists() else ""

    def fork_tip(self):
        return git("rev-parse", f"refs/heads/{self.branch}", cwd=self.fork).stdout.strip()


@pytest.fixture(params=["main", "feature"], ids=["head-named-main", "head-named-after-a-local-branch"])
def queue(request, tmp_path):
    return Queue(tmp_path, request.param)


def test_the_fixture_is_potent_unpinned_git_on_the_tree_runs_the_contributors_code(tmp_path):
    # Once the tree is checked out (pinned, nothing ran), the plain git
    # commands the old block used next run the contributor's fsmonitor,
    # clean filter and hooks: the pins are what keeps them inert.
    q = Queue(tmp_path, "main")
    assert q.run("start", "42", q.approved).returncode == 0
    assert q.ran() == ""
    (q.queue / "data.txt").write_text("edited\n")
    git("status", "--porcelain", cwd=q.queue)
    git("add", "-u", cwd=q.queue)
    git("commit", "-q", "--no-edit", cwd=q.queue)
    ran = q.ran()
    assert "fsmon.sh" in ran and "clean" in ran and "pre-commit" in ran


def test_the_full_flow_pushes_the_merge_and_leaves_the_clone_untouched(queue):
    q = queue
    r = q.run("start", "42", q.approved)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "merged origin/main cleanly" in r.stdout
    assert git("rev-parse", "HEAD", cwd=q.queue).stdout.strip() == q.approved  # not committed yet
    assert git("symbolic-ref", "-q", "HEAD", cwd=q.queue, check=False).returncode != 0  # detached
    r = q.run("commit", "--trailer", "Co-Authored-By: Claude <noreply@anthropic.com>")
    assert r.returncode == 0, r.stderr + r.stdout
    head = git("rev-parse", "HEAD", cwd=q.queue).stdout.strip()
    assert git("rev-parse", "HEAD^1", cwd=q.queue).stdout.strip() == q.approved
    assert git("merge-base", "--is-ancestor", "origin/main", head, cwd=q.queue).returncode == 0
    message = git("log", "-1", "--format=%B", cwd=q.queue).stdout
    assert message.startswith("Merge remote-tracking branch 'origin/main'")
    assert "Co-Authored-By: Claude <noreply@anthropic.com>" in message
    r = q.run("push", "42", q.approved)
    assert r.returncode == 0, r.stderr + r.stdout
    assert q.fork_tip() == head
    # Nothing of the contributor's ran, and the clone's branches, HEAD and config are as they were.
    assert q.ran() == ""
    assert q.snapshot() == q.before
    assert "contrib" not in git("remote", cwd=q.clone).stdout


def test_a_head_that_moved_after_the_approval_is_refused_before_anything_is_checked_out(queue):
    q = queue
    git("checkout", "-q", "contrib", cwd=q.seed)
    write(q.seed, "data.txt", "pushed after the approval\n")
    moved = commit(q.seed, "B: pushed after the approval")
    git("push", "-q", "origin", "contrib:refs/pull/42/head", cwd=q.seed)
    before = git("rev-parse", "HEAD", cwd=q.queue).stdout.strip()
    r = q.run("start", "42", q.approved)
    assert r.returncode == 5
    assert moved in r.stderr and q.approved in r.stderr
    assert git("rev-parse", "HEAD", cwd=q.queue).stdout.strip() == before
    assert not (q.queue / "data.txt").exists()
    assert q.ran() == ""
    assert q.snapshot() == q.before


def test_conflicts_are_listed_and_the_commit_waits_for_their_resolution(queue):
    q = queue
    name = "weird $(touch ran-subst) `touch ran-tick` 'quoted' name.py"
    git("checkout", "-q", "main", cwd=q.seed)
    write(q.seed, name, "main's version\n")
    commit(q.seed, "main: adds the file")
    git("push", "-q", "origin", "main", cwd=q.seed)
    git("checkout", "-q", "contrib", cwd=q.seed)
    write(q.seed, name, "the contributor's version\n")
    q.approved = commit(q.seed, "A2: the reviewed commit, same file")
    git("push", "-q", "-f", "origin", "contrib:refs/pull/42/head", cwd=q.seed)
    git("push", "-q", "-f", "fork", f"contrib:refs/heads/{q.branch}", cwd=q.seed)
    r = q.run("start", "42", q.approved)
    assert r.returncode == 3, r.stderr + r.stdout
    assert "CONFLICTS" in r.stdout and "main: adds the file" in r.stdout
    r = q.run("commit")
    assert r.returncode == 3 and "UNRESOLVED" in r.stderr
    (q.queue / name).write_text("resolved\n")
    r = q.run("commit")
    assert r.returncode == 0, r.stderr + r.stdout
    assert git("show", f"HEAD:{name}", cwd=q.queue).stdout == "resolved\n"
    assert not (q.queue / "ran-subst").exists() and not (q.queue / "ran-tick").exists()
    assert q.ran() == ""
    assert q.snapshot() == q.before


def test_a_changelog_entry_left_under_a_release_fails_the_commit_and_the_push(queue):
    q = queue
    git("checkout", "-q", "contrib", cwd=q.seed)
    write(q.seed, "CHANGELOG.md", "## Unreleased\n\n## v0.3.100 (01 September 2026)\n\n- Old entry\n- Misplaced entry\n")
    q.approved = commit(q.seed, "A2: entry under a release")
    git("push", "-q", "-f", "origin", "contrib:refs/pull/42/head", cwd=q.seed)
    git("push", "-q", "-f", "fork", f"contrib:refs/heads/{q.branch}", cwd=q.seed)
    assert q.run("start", "42", q.approved).returncode == 0
    r = q.run("commit")
    assert r.returncode == 3
    assert "## v0.3.100 (01 September 2026)\t- Misplaced entry" in r.stdout
    before = q.fork_tip()
    r = q.run("push", "42", q.approved)
    assert r.returncode == 3
    assert q.fork_tip() == before


def test_a_rejected_push_is_reported_and_never_forced(queue):
    q = queue
    assert q.run("start", "42", q.approved).returncode == 0
    assert q.run("commit").returncode == 0
    git("checkout", "-q", "contrib", cwd=q.seed)
    write(q.seed, "late.md", "late\n")
    late = commit(q.seed, "C: the contributor pushes meanwhile")
    git("push", "-q", "fork", f"contrib:refs/heads/{q.branch}", cwd=q.seed)
    r = q.run("push", "42", q.approved)
    assert r.returncode == 5 and "rejected" in r.stderr
    assert q.fork_tip() == late


def test_the_push_needs_maintainer_edits_and_a_head_built_on_the_approved_commit(queue):
    q = queue
    assert q.run("start", "42", q.approved).returncode == 0
    assert q.run("commit").returncode == 0
    q.pr(maintainer_can_modify=False)
    r = q.run("push", "42", q.approved)
    assert r.returncode == 1 and "edits by maintainers" in r.stderr
    q.pr(maintainer_can_modify=True)
    r = q.run("push", "42", "b" * 40)
    assert r.returncode == 1 and "does not descend" in r.stderr
    assert q.fork_tip() == q.approved


def test_the_primary_clone_and_a_branch_worktree_are_refused(queue):
    q = queue
    env = {"PATH": f"{q.tmp / 'bin'}:{os.environ['PATH']}", "STUB": str(q.stub)}
    r = sh("bash", str(EXTERNAL), "start", "42", q.approved, cwd=q.clone, env=env, check=False)
    assert r.returncode == 1 and "primary clone" in r.stderr
    git("checkout", "-q", "-b", "someone-elses", cwd=q.queue)
    r = q.run("start", "42", q.approved)
    assert r.returncode == 1 and "on branch" in r.stderr
    assert git("rev-parse", "HEAD", cwd=q.clone).stdout == q.before[1]


def test_a_worktree_a_promotion_left_on_its_branch_detaches_and_starts(one_queue):
    # The SKILL.md step for a queue that handled a promotion first.
    q = one_queue
    git("checkout", "-q", "-b", "claude/issue-1-promotion", "origin/main", cwd=q.queue)
    assert q.run("start", "42", q.approved).returncode == 1
    git("checkout", "-q", "--detach", "origin/main", cwd=q.queue)
    r = q.run("start", "42", q.approved)
    assert r.returncode == 0, r.stderr


def test_the_git_passthrough_is_pinned(queue):
    q = queue
    assert q.run("start", "42", q.approved).returncode == 0
    r = q.run("git", "status", "--porcelain")
    assert r.returncode == 0, r.stderr
    (q.queue / "data.txt").write_text("edited\n")
    assert q.run("git", "diff", "--stat").returncode == 0
    assert q.run("git", "add", "data.txt").returncode == 0
    assert q.ran() == ""


# --- review round 1: every marker refused before commit and push (B1), file names are literal (B2) ---


def make_conflict(q, name, *, attrs=None, main_extra=None):
    """Main and the contributor add `name` with different text; returns the new approved SHA."""
    git("checkout", "-q", "main", cwd=q.seed)
    write(q.seed, name, "main's version\n")
    for extra, text in (main_extra or {}).items():
        write(q.seed, extra, text)
    commit(q.seed, "main: adds the file")
    git("push", "-q", "origin", "main", cwd=q.seed)
    git("checkout", "-q", "contrib", cwd=q.seed)
    write(q.seed, name, "the contributor's version\n")
    if attrs:
        write(q.seed, ".gitattributes", (q.seed / ".gitattributes").read_text() + attrs)
    q.approved = commit(q.seed, "A2: the reviewed commit, same file")
    git("push", "-q", "-f", "origin", "contrib:refs/pull/42/head", cwd=q.seed)
    git("push", "-q", "-f", "fork", f"contrib:refs/heads/{q.branch}", cwd=q.seed)
    return q.approved


@pytest.fixture
def one_queue(tmp_path):
    return Queue(tmp_path, "main")


def test_markers_of_a_configured_size_refuse_the_commit_and_the_push(one_queue):
    q = one_queue
    make_conflict(q, "conflict.py", attrs="conflict.py conflict-marker-size=12\n")
    assert q.run("start", "42", q.approved).returncode == 3
    assert "<" * 12 + " HEAD" in (q.queue / "conflict.py").read_text()  # the markers git wrote are 12 wide
    before = q.fork_tip()
    r = q.run("commit")
    assert r.returncode == 3 and "UNRESOLVED" in r.stderr and "conflict.py:" in r.stderr
    assert git("rev-parse", "HEAD", cwd=q.queue).stdout.strip() == q.approved
    assert q.run("push", "42", q.approved).returncode != 0
    assert q.fork_tip() == before


def test_markers_staged_before_the_commit_are_refused(one_queue):
    q = one_queue
    make_conflict(q, "conflict.py")
    assert q.run("start", "42", q.approved).returncode == 3
    assert q.run("git", "add", "conflict.py").returncode == 0
    r = q.run("commit")
    assert r.returncode == 3 and "conflict.py:" in r.stderr
    assert git("rev-parse", "HEAD", cwd=q.queue).stdout.strip() == q.approved


def test_a_push_refuses_markers_committed_around_the_script(one_queue):
    q = one_queue
    make_conflict(q, "conflict.py")
    assert q.run("start", "42", q.approved).returncode == 3
    assert q.run("git", "add", "conflict.py").returncode == 0
    assert q.run("git", "commit", "-q", "--no-edit").returncode == 0  # bypassing external.sh commit
    before = q.fork_tip()
    r = q.run("push", "42", q.approved)
    assert r.returncode == 3 and "conflict.py:" in r.stderr
    assert q.fork_tip() == before


def test_a_marker_line_main_carries_is_not_residue(one_queue):
    q = one_queue
    fixture = "<<<<<<< ours\nfixture\n=======\ntheirs\n>>>>>>> theirs\n"
    make_conflict(q, "conflict.py", main_extra={"tests/fixture.txt": fixture})
    assert q.run("start", "42", q.approved).returncode == 3
    (q.queue / "conflict.py").write_text("resolved\n")
    r = q.run("commit")
    assert r.returncode == 0, r.stderr + r.stdout
    assert q.run("push", "42", q.approved).returncode == 0


@pytest.mark.parametrize("name", [":(literal)conflict.py", ":(glob)**", "c*.py"])
def test_file_names_from_the_tree_are_literal_pathspecs(one_queue, name):
    q = one_queue
    # cx.py: another file main changes, which a glob `c*.py` would also match.
    make_conflict(q, name, main_extra={"cx.py": "main changes cx\n"})
    r = q.run("start", "42", q.approved)
    assert r.returncode == 3, r.stderr + r.stdout
    section = r.stdout.split("=== ", 1)[1]
    assert "main: adds the file" in section and "+main's version" in section
    assert "cx.py" not in r.stdout  # only the named path was inspected
    (q.queue / name).write_text("resolved\n")
    r = q.run("commit")
    assert r.returncode == 0, r.stderr + r.stdout
    assert git("show", f"HEAD:{name}", cwd=q.queue).stdout == "resolved\n"
    assert q.ran() == ""
