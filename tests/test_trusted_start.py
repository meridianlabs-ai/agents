"""The run's trusted start (Claude Security finding 4628444, criterion 2).

The land job fetches a bundle's `start_sha` from origin by SHA, and origin
serves every reachable commit that way: a fork PR's head under `refs/pull/`,
an old base commit. Until this pin the value came from the manifest — the
agent job's word — so a forged manifest could have the landing build on
history the run never began from. Now each caller's GATE reads the run's
start from the API before the agent job runs (the base tip an issue run
branches from; the PR head's live tip a PR run checks out), the agent job
pins its own start to that value before the agent starts (`claude.yml`'s
`Record base SHA`; `sync-branch`'s `head-sha`, which fails the sync before
the merge when the tip it checked out is not the gate's), and the land job
passes the same value to the validator as `start-sha`, which refuses a
manifest carrying commits under any other `start_sha` — before the fetch
step runs. So the checkout, the manifest and the push agree on one revision,
and an honest run always matches.

Tested here, against local repos and the real scripts lifted from the
actions and workflows (text extraction; PyYAML is not a test dependency):

- the validator refuses the two forged manifests the finding describes (a
  bundle built on a fork PR's head; one built on an old base commit), each
  of which the fetch step alone would have accepted — shown by fetching
  that SHA from the local origin into an empty repo, as the land job does;
  an honest run from the gate's tip validates and lands; a run that pushes
  nothing is not held to the pin (the callers' `github.sha` placeholder);
- `sync-branch`'s `head-sha`: a head that moved between the gate's read and
  the checkout (claude.yml's shape, checkout=true) or a checkout that is not
  the gate's tip (the loops' shape) fails before the merge, with HEAD and
  the tree untouched; the gate's tip merges and is recorded as `start_sha`;
- `claude.yml`'s `Record base SHA`: the gate's tip is the start whether or
  not the base advanced since; a base rewritten since the gate's read
  refuses; no gate read falls back to the local tip with a warning;
- `claude.yml`'s gate `Record run start`: which branch it reads on a PR and
  an issue run, the hex check, the retries, and that nothing fails the gate;
- the wiring: every land step that pushes passes its gate's read as
  `start-sha`, every sync-branch call the same value as `head-sha`, the
  reviewer's land step (refuse-bundle) passes none, and the land composite
  hands the input to the validator, which runs before the fetch.
"""

import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / ".github" / "scripts" / "validate_manifest.py"
LAND = ROOT / ".github" / "actions" / "land" / "action.yml"
SYNC_ACTION = ROOT / ".github" / "actions" / "sync-branch" / "action.yml"
WORKFLOWS = ROOT / ".github" / "workflows"
CLAUDE = WORKFLOWS / "claude.yml"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ci_fix_binding import GH_STUB, SHA_A, origin_tip, sync_repo  # noqa: E402
from test_ci_fix_gate import step_script  # noqa: E402
from test_land_helpers import git, job_block, run_emit_landing, sh, step_block  # noqa: E402

SYNC = step_script(SYNC_ACTION, "    - id: sync", 8)
RECORD_BASE = step_script(CLAUDE, "        id: base", 10)
RECORD_START = step_script(CLAUDE, "        id: start", 10)

BRANCH = "claude/issue-79-fix"


# --- the validator, against forged manifests over a local origin --------------


@pytest.fixture
def origin(tmp_path):
    """A local origin whose `main` is at B1 (B0 then "base advanced"), with a
    fork PR's head F — a child of B0 on no branch — reachable only under
    refs/pull/7/head, as GitHub keeps fork heads; plus a clone."""
    bare = tmp_path / "origin.git"
    work = tmp_path / "work"
    git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    git("init", "-q", "-b", "main", str(work), cwd=tmp_path)
    git("config", "user.email", "a@b", cwd=work)
    git("config", "user.name", "a", cwd=work)
    (work / "f").write_text("1\n")
    git("add", "f", cwd=work)
    git("commit", "-qm", "B0", cwd=work)
    b0 = git("rev-parse", "HEAD", cwd=work).stdout.strip()
    git("remote", "add", "origin", str(bare), cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    git("checkout", "-qb", "fork", cwd=work)
    (work / "fork.txt").write_text("from a fork\n")
    git("add", "fork.txt", cwd=work)
    git("commit", "-qm", "F: the fork PR's head", cwd=work)
    f = git("rev-parse", "HEAD", cwd=work).stdout.strip()
    git("push", "-q", "origin", "fork:refs/pull/7/head", cwd=work)
    git("checkout", "-q", "main", cwd=work)
    git("branch", "-qD", "fork", cwd=work)
    (work / "f").write_text("2\n")
    git("commit", "-qam", "B1: base advanced", cwd=work)
    b1 = git("rev-parse", "HEAD", cwd=work).stdout.strip()
    git("push", "-q", "origin", "main", cwd=work)
    assert git("ls-remote", "--heads", str(bare), cwd=tmp_path).stdout.count("\n") == 1   # main only
    return {"bare": bare, "work": work, "b0": b0, "b1": b1, "f": f, "tmp": tmp_path}


def agent_run(o, start, *, commits=1):
    """The agent job: a branch cut at START with COMMITS on it, then the
    emit-landing step over it (its real script). Returns the landing dir."""
    work = o["work"]
    git("checkout", "-qB", BRANCH, start, cwd=work)
    for i in range(commits):
        (work / "agent.txt").write_text(f"agent {i}\n")
        git("add", "agent.txt", cwd=work)
        git("commit", "-qm", f"agent change {i}", cwd=work)
    r, landing, _ = run_emit_landing(o["tmp"], cwd=work, read_only=False, start_sha=start,
                                     branch=BRANCH, pr_number="", issue_number="79")
    assert r.returncode == 0, r.stderr
    manifest = json.loads((landing / "manifest.json").read_text())
    assert manifest["start_sha"] == start
    assert manifest["has_bundle"] == (commits > 0)
    return landing


def validate(landing, *, start_sha):
    """The land composite's validate step, as the composite calls the script
    on an issue run (branch-prefix, no PR), with its `start-sha` input."""
    return sh(sys.executable, str(VALIDATOR), "--dir", str(landing), "--repo", "meridianlabs-ai/agents",
              "--run-id", "123", "--default-branch", "main", "--refused-branches", "main",
              "--allowed-issue-repos", "", "--pr-head-ref", "", "--start-sha", start_sha,
              "--event-pr-number", "", "--event-issue-number", "79", "--branch-prefix", "claude/issue-79-",
              check=False)


def fetch_alone_accepts(o, sha) -> bool:
    """What the fetch step would do with a start SHA, on its own: fetch it
    from origin by SHA into an empty bare repo."""
    repo = o["tmp"] / f"landing-repo-{sha[:7]}"
    git("init", "-q", "--bare", str(repo), cwd=o["tmp"])
    return git("fetch", "--quiet", "--no-tags", str(o["bare"]), sha, cwd=repo, check=False).returncode == 0


def test_a_bundle_built_on_a_fork_prs_head_is_refused_before_the_fetch(origin):
    o = origin
    landing = agent_run(o, o["f"])
    # The fetch step alone would have taken it: the fork head is reachable
    # on origin, on no branch.
    assert fetch_alone_accepts(o, o["f"])
    r = validate(landing, start_sha=o["b1"])
    assert r.returncode == 1
    assert f"manifest violation: manifest: start_sha {o['f']} is not the run's start the caller's trusted context recorded ({o['b1']}); refusing the bundle" in r.stdout
    assert "manifest ok." not in r.stdout


def test_a_bundle_built_on_an_old_base_commit_is_refused_before_the_fetch(origin):
    o = origin
    landing = agent_run(o, o["b0"])
    assert fetch_alone_accepts(o, o["b0"])
    r = validate(landing, start_sha=o["b1"])
    assert r.returncode == 1
    assert f"start_sha {o['b0']} is not the run's start" in r.stdout


def test_a_land_job_without_a_trusted_start_refuses_every_bundle(origin):
    o = origin
    landing = agent_run(o, o["b1"])   # even an honest one
    r = validate(landing, start_sha="")
    assert r.returncode == 1
    assert "carries commits but this land job was given no trusted start SHA (--start-sha); refusing the bundle" in r.stdout


def test_an_honest_run_from_the_gates_tip_validates_and_lands(origin):
    o = origin
    landing = agent_run(o, o["b1"], commits=2)
    r = validate(landing, start_sha=o["b1"])
    assert r.returncode == 0, r.stdout
    assert "manifest ok." in r.stdout
    # ...and the fetch step's sequence lands exactly the bundle above it.
    manifest = json.loads((landing / "manifest.json").read_text())
    repo = o["tmp"] / "landing-repo"
    git("init", "-q", "--bare", str(repo), cwd=o["tmp"])
    git("fetch", "--quiet", "--no-tags", str(o["bare"]), o["b1"], cwd=repo)
    git("bundle", "verify", "--quiet", str(landing / "commits.bundle"), cwd=repo)
    git("fetch", "--quiet", "--no-tags", str(landing / "commits.bundle"), "HEAD", cwd=repo)
    assert git("rev-parse", "FETCH_HEAD", cwd=repo).stdout.strip() == manifest["head_sha"]
    git("merge-base", "--is-ancestor", o["b1"], manifest["head_sha"], cwd=repo)
    assert git("rev-list", "--count", f"{o['b1']}..{manifest['head_sha']}", cwd=repo).stdout.strip() == "2"


def test_a_run_that_pushes_nothing_is_not_held_to_the_start(origin):
    # The callers fall back to `github.sha` as start-sha when their own
    # start step failed, so the manifest carrying that error still lands;
    # nothing is pushed, so the value is never compared.
    o = origin
    landing = agent_run(o, o["b0"], commits=0)
    for trusted in ("", o["b1"]):
        r = validate(landing, start_sha=trusted)
        assert r.returncode == 0, r.stdout


# --- sync-branch's head-sha pin --------------------------------------------------


def sync_head(tmp_path, *, checkout, pinned_head, before=None):
    """Run the lifted sync step over test_ci_fix_binding's fixture origin
    (`main` moved past the PR branch `shared`), pinned to PINNED_HEAD ("TIP"
    for origin/shared's tip as the gate would have read it). BEFORE(work)
    runs after the pin is taken and before the step — a push to origin in
    the window between the gate and the sync."""
    work = sync_repo(tmp_path)
    if pinned_head == "TIP":
        pinned_head = origin_tip(work, "shared")
    if before:
        before(work)
    if checkout == "true":
        # claude.yml's checkout lands on the default ref and holds no local
        # branch for the PR head: the sync's `git checkout <branch>` creates
        # it from origin's tip, as fetched.
        git("checkout", "-q", "main", cwd=work)
        git("branch", "-qD", "shared", cwd=work)
    binp = tmp_path / "bin"
    binp.mkdir(exist_ok=True)
    gh = binp / "gh"
    gh.write_text(GH_STUB)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    (stub / "calls").write_text("")
    (stub / "pr").write_text(json.dumps({"headRefName": "shared", "baseRefName": "main",
                                         "isCrossRepository": False, "state": "OPEN"}))
    (stub / "branch-sha").write_text(SHA_A)
    out = tmp_path / "output"
    out.write_text("")
    env = {"PATH": f"{binp}:{os.environ['PATH']}", "STUB": str(stub), "GITHUB_OUTPUT": str(out),
           "GH_TOKEN": "x", "REPO": "o/r", "NUM": "101", "ENGINE": "claude", "CHECKOUT": checkout,
           "USER_NAME": "a", "USER_EMAIL": "a@b", "PINNED_BASE": "", "PINNED_BASE_SHA": "",
           "PINNED_HEAD_SHA": pinned_head, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ALLOW_PROTOCOL": "file"}
    res = sh("bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", SYNC, cwd=work, check=False, env=env)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    return res, outputs, work, pinned_head


def push_to_shared(work):
    """Someone pushes to the PR branch on origin — from another clone, so the
    fixture checkout (the loops' shape) stays where it was."""
    other = work.parent / "other"
    git("clone", "-q", "-b", "shared", str(work.parent / "origin.git"), str(other), cwd=work.parent)
    git("config", "user.email", "h@b", cwd=other)
    git("config", "user.name", "h", cwd=other)
    (other / "h").write_text("1\n")
    git("add", "h", cwd=other)
    git("commit", "-qm", "human push after the gate", cwd=other)
    git("push", "-q", "origin", "shared", cwd=other)


def test_sync_checks_out_and_merges_the_head_the_gate_read(tmp_path):
    # claude.yml's shape: the sync fetches and checks the head out itself.
    res, out, work, pin = sync_head(tmp_path, checkout="true", pinned_head="TIP")
    assert res.returncode == 0, res.stderr
    assert out["start_sha"] == pin and out["branch"] == "shared"
    assert out["merge_sha"] and (work / "f").read_text() == "2\n"


def test_sync_refuses_a_head_that_moved_between_the_gate_and_the_checkout(tmp_path):
    # The gate read origin/shared; a push landed before the sync fetched it.
    # Refused after the checkout and before the merge: the agent never
    # starts on code the gate never saw, and the round's landing (pinned to
    # the gate's tip) could not have named this start anyway.
    res, out, work, pin = sync_head(tmp_path, checkout="true", pinned_head="TIP", before=push_to_shared)
    assert res.returncode != 0
    tip = origin_tip(work, "shared")
    assert tip != pin
    assert f"shared is at {tip}, but the caller's trusted context read {pin} as its tip; the head moved between the gate and this step" in res.stdout
    assert "start_sha" not in out and "merge_sha" not in out
    assert out["branch"] == "shared" and out["base"] == "main"       # emitted before the pin, as before
    assert git("rev-parse", "HEAD", cwd=work).stdout.strip() == tip  # checked out, nothing merged
    assert (work / "f").read_text() == "1\n" and git("status", "--porcelain", cwd=work).stdout == ""


def test_sync_requires_the_callers_checkout_to_be_the_gates_tip(tmp_path):
    # The loops' shape: the caller checked the branch out by NAME; the pin
    # is compared with that HEAD (a push since is the land job's
    # non-fast-forward refusal, not this step's).
    res, out, work, pin = sync_head(tmp_path, checkout="false", pinned_head="TIP")
    assert res.returncode == 0, res.stderr
    assert out["start_sha"] == pin and out["merge_sha"]
    res, out, work, _ = sync_head(tmp_path / "moved", checkout="false", pinned_head="e" * 40)
    assert res.returncode != 0
    head = git("rev-parse", "HEAD", cwd=work).stdout.strip()
    assert f"shared is at {head}, but the caller's trusted context read {'e' * 40} as its tip" in res.stdout
    assert "start_sha" not in out and "merge_sha" not in out
    assert (work / "f").read_text() == "1\n" and git("status", "--porcelain", cwd=work).stdout == ""


def test_sync_without_a_pinned_head_records_whatever_it_checked_out(tmp_path):
    # Unchanged behaviour for a caller that passes none.
    res, out, work, _ = sync_head(tmp_path, checkout="true", pinned_head="", before=push_to_shared)
    assert res.returncode == 0, res.stderr
    assert out["start_sha"] == origin_tip(work, "shared") and out["merge_sha"]


# --- claude.yml's Record base SHA (issue runs) ---------------------------------------


def record_base(tmp_path, gate, *, before=None):
    """Run the lifted step over the fixture clone (origin/main two commits
    deep; `release` forks off main~1 and is not in main's history). GATE(work)
    is the SHA the gate read, computed from that same clone (every fixture
    repo has its own SHAs); BEFORE(work) runs after the gate's read and before
    the step — what happens to origin, or to the checkout, in between."""
    work = sync_repo(tmp_path)
    git("checkout", "-q", "main", cwd=work)
    gate_sha = gate(work)
    if before:
        before(work)
    out = tmp_path / "output"
    out.write_text("")
    env = {"BASE": "main", "GATE_SHA": gate_sha, "GITHUB_OUTPUT": str(out),
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ALLOW_PROTOCOL": "file"}
    res = sh("bash", "--noprofile", "--norc", "-c", RECORD_BASE, cwd=work, check=False, env=env)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    return res, outputs, work, gate_sha


def checkout_resets_origin_main_to(event_sha):
    """What actions/checkout leaves behind on a default-base run: the event
    ref's remote-tracking ref reset to the EVENT commit (github.sha) — even
    at fetch-depth 0 — and the branch checked out there."""
    def before(work):
        git("fetch", "--quiet", "origin", f"+{event_sha(work)}:refs/remotes/origin/main", cwd=work)
        git("checkout", "--quiet", "-B", "main", "refs/remotes/origin/main", cwd=work)
    return before


def push_to_main(work):
    """A push to the base on origin after the gate's read (from another
    clone, so the checkout does not see it until the step fetches)."""
    other = work.parent / "other"
    git("clone", "-q", "-b", "main", str(work.parent / "origin.git"), str(other), cwd=work.parent)
    git("config", "user.email", "h@b", cwd=other)
    git("config", "user.name", "h", cwd=other)
    (other / "later").write_text("1\n")
    git("add", "later", cwd=other)
    git("commit", "-qm", "base advanced after the gate", cwd=other)
    git("push", "-q", "origin", "main", cwd=other)


def force_push_main_to_release(work):
    """The base rewritten after the gate's read: origin/main moved to a commit
    that does not descend from the gate's tip."""
    git("push", "-q", "--force", "origin", "origin/release:refs/heads/main", cwd=work)


def test_record_base_takes_the_gates_tip(tmp_path):
    res, out, _, gate = record_base(tmp_path, lambda w: origin_tip(w, "main"))
    assert res.returncode == 0, res.stderr
    assert out == {"sha": gate}
    assert "::warning::" not in res.stdout and "advanced" not in res.stdout


def test_record_base_refreshes_the_event_reset_tracking_ref_before_comparing(tmp_path):
    # Review round 1 (B2): a push landed between the comment and the gate,
    # or an older event was re-run — the gate read the live tip B, but
    # actions/checkout reset origin/main to the event commit A (older). A is
    # an ancestor of B, not the other way round, so a comparison against the
    # checkout's origin/main would call the base "rewritten". The step
    # fetches the live tip first: the run starts at B, origin/main (where
    # the codex prep cuts its branch) is B too.
    older = lambda w: git("rev-parse", "origin/main~1", cwd=w).stdout.strip()
    res, out, work, gate = record_base(tmp_path, lambda w: origin_tip(w, "main"), before=checkout_resets_origin_main_to(older))
    assert res.returncode == 0, res.stdout + res.stderr
    assert out == {"sha": gate}
    assert git("rev-parse", "origin/main", cwd=work).stdout.strip() == gate
    assert "::warning::" not in res.stdout and "advanced" not in res.stdout and "rewritten" not in res.stdout
    # The checkout itself (HEAD, the event commit) is left where it was.
    assert git("rev-parse", "HEAD", cwd=work).stdout.strip() == older(work)


def test_record_base_keeps_the_gates_tip_when_the_base_advanced_since(tmp_path):
    # A push moved main between the gate's read and this step. The run
    # starts from the gate's tip (the land job's pin) — the branch is cut
    # from the live tip, so the bundle carries the base's newer commit too.
    res, out, work, gate = record_base(tmp_path, lambda w: origin_tip(w, "main"), before=push_to_main)
    assert res.returncode == 0, res.stderr
    assert out == {"sha": gate}
    live = git("ls-remote", "--heads", str(work.parent / "origin.git"), "main", cwd=work).stdout.split("\t")[0]
    assert live != gate and git("rev-parse", "origin/main", cwd=work).stdout.strip() == live
    assert f"base branch main advanced from {gate} to {live} since the gate read it" in res.stdout


def test_record_base_advanced_base_over_an_event_reset_checkout(tmp_path):
    # Both at once: the checkout's origin/main is the event commit, origin
    # moved past the gate's tip. Still the gate's tip.
    older = lambda w: git("rev-parse", "origin/main~1", cwd=w).stdout.strip()
    reset = checkout_resets_origin_main_to(older)

    def before(work):
        reset(work)
        push_to_main(work)
    res, out, _, gate = record_base(tmp_path, lambda w: origin_tip(w, "main"), before=before)
    assert res.returncode == 0, res.stdout + res.stderr
    assert out == {"sha": gate} and "advanced from" in res.stdout


@pytest.mark.parametrize("which", ["force-push", "unknown"])
def test_record_base_refuses_a_base_rewritten_since_the_gate_read_it(tmp_path, which):
    # The gate's tip is not in origin/main's history any more (main
    # force-pushed to another branch's commit; a SHA this clone never had):
    # nothing is attempted.
    if which == "force-push":
        res, out, work, gate = record_base(tmp_path, lambda w: origin_tip(w, "main"), before=force_push_main_to_release)
    else:
        res, out, work, gate = record_base(tmp_path, lambda w: "e" * 40)
    assert res.returncode != 0
    assert out == {}
    live = git("ls-remote", "--heads", str(work.parent / "origin.git"), "main", cwd=work).stdout.split("\t")[0]
    assert f"::error::the gate read {gate} as the tip of base branch main, but origin/main is now {live}" in res.stdout
    assert "does not descend from it" in res.stdout


def test_record_base_fails_when_the_live_tip_cannot_be_fetched(tmp_path):
    # Unchecked is not agreed: a failed fetch is this step's failure (the
    # Surface step names it), not a fall-through to the checkout's ref.
    def before(work):
        git("remote", "set-url", "origin", str(work.parent / "gone.git"), cwd=work)
    res, out, _, _ = record_base(tmp_path, lambda w: origin_tip(w, "main"), before=before)
    assert res.returncode != 0 and out == {}


def test_record_base_without_a_gate_read_falls_back_to_the_live_tip_with_a_warning(tmp_path):
    older = lambda w: git("rev-parse", "origin/main~1", cwd=w).stdout.strip()
    res, out, work, _ = record_base(tmp_path, lambda w: "", before=checkout_resets_origin_main_to(older))
    assert res.returncode == 0, res.stderr
    live = git("ls-remote", "--heads", str(work.parent / "origin.git"), "main", cwd=work).stdout.split("\t")[0]
    assert out == {"sha": live} and live != older(work)
    assert "::warning::the gate did not record the tip of base branch main" in res.stdout
    assert "the land job refuses any commits this run produces" in res.stdout


# --- claude.yml's gate: Record run start ------------------------------------------------


def record_start(tmp_path, *, is_pr, head_branch="shared", base="main", branch_sha=SHA_A, fail=False):
    binp = tmp_path / "bin"
    binp.mkdir(parents=True, exist_ok=True)
    gh = binp / "gh"
    gh.write_text(GH_STUB)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    stub = tmp_path / "stub"
    stub.mkdir(parents=True, exist_ok=True)
    (stub / "calls").write_text("")
    if branch_sha is not None:
        (stub / "branch-sha").write_text(branch_sha)
    if fail:
        (stub / "branch-fail").write_text("")
    out = tmp_path / "output"
    out.write_text("")
    env = {"PATH": f"{binp}:{os.environ['PATH']}", "STUB": str(stub), "GITHUB_OUTPUT": str(out),
           "GH_TOKEN": "x", "REPO": "o/r", "NUM": "101", "IS_PR": "true" if is_pr else "false",
           "HEAD_BRANCH": head_branch, "BASE": base}
    # The retry's back-off is real time in the step; neutralised here.
    res = sh("bash", "--noprofile", "--norc", "-c", "sleep() { :; }\n" + RECORD_START, cwd=tmp_path, check=False, env=env)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    calls = [c for c in (stub / "calls").read_text().splitlines() if c]
    return res, outputs, calls


def test_record_start_reads_the_pr_head_branch_on_a_pr_run(tmp_path):
    res, out, calls = record_start(tmp_path, is_pr=True, head_branch="feature/x")
    assert res.returncode == 0, res.stderr
    assert out == {"sha": SHA_A}
    assert calls == ["api repos/o/r/branches/feature/x --jq .commit.sha"]
    assert "run start: the head branch of PR #101 (feature/x) is at " + SHA_A in res.stdout


def test_record_start_reads_the_base_branch_on_an_issue_run(tmp_path):
    res, out, calls = record_start(tmp_path, is_pr=False, base="meridian")
    assert res.returncode == 0, res.stderr
    assert out == {"sha": SHA_A}
    assert calls == ["api repos/o/r/branches/meridian --jq .commit.sha"]


def test_record_start_without_a_head_branch_reads_nothing_and_pins_nothing(tmp_path):
    # The gate's head-branch read failed on a PR run: no branch to read.
    res, out, calls = record_start(tmp_path, is_pr=True, head_branch="")
    assert res.returncode == 0, res.stderr
    assert out == {"sha": ""} and calls == []
    assert "::warning::could not read the tip of the head branch of PR #101 (unknown)" in res.stdout


def test_record_start_retries_and_leaves_the_run_unpinned_when_the_read_fails(tmp_path):
    # A deleted branch (404) and a persistent error alike: three attempts,
    # then empty — the gate does not fail (a comment-only run needs no
    # start; a closed PR's head may be gone), the land job refuses commits.
    for kw in ({"branch_sha": None}, {"fail": True}):
        res, out, calls = record_start(tmp_path / str(len(kw)), is_pr=True, **kw)
        assert res.returncode == 0, res.stderr
        assert out == {"sha": ""}
        assert len(calls) == 3
        assert "::warning::could not read the tip of the head branch of PR #101 (shared); the run starts unpinned" in res.stdout


def test_record_start_hex_checks_the_api_value(tmp_path):
    for i, junk in enumerate(("junk", '{"message":"Not Found"}', SHA_A[:-1], SHA_A.upper())):
        res, out, _ = record_start(tmp_path / str(i), is_pr=False, branch_sha=junk)
        assert res.returncode == 0
        assert out == {"sha": ""}


# --- the wiring ------------------------------------------------------------------------


# workflow → the gate output that IS the run's trusted start.
TRUSTED_START = {
    "claude.yml": "start_sha",            # the gate's Record run start
    "claude-auto.yml": "head_sha",        # bind-ci-run: the head the failed run tested
    "claude-auto-review.yml": "head",     # the gate's headRefOid / live-tip read
}

# workflow → its untrusted jobs, one per engine (findings 4628446 and
# 4629153): each carries its own copy of the sync (and, in claude.yml, the
# base step), and every copy must carry the pin.
ENGINE_JOBS = {
    "claude.yml": ("agent", "agent-codex"),
    "claude-auto.yml": ("fix", "fix-codex"),
    "claude-auto-review.yml": ("fix", "fix-codex"),
}


@pytest.mark.parametrize("name,output", sorted(TRUSTED_START.items()))
def test_every_pushing_land_job_and_its_sync_are_pinned_to_the_gates_start(name, output):
    text = (WORKFLOWS / name).read_text()
    land = step_block(text, "land")
    assert "uses: meridianlabs-ai/agents/.github/actions/land@main" in land
    assert f"          start-sha: ${{{{ needs.gate.outputs.{output} }}}}\n" in land
    assert "refuse-bundle" not in land
    for job in ENGINE_JOBS[name]:
        sync = step_block(job_block(text, job), "sync")
        assert "uses: meridianlabs-ai/agents/.github/actions/sync-branch@main" in sync, job
        assert f"          head-sha: ${{{{ needs.gate.outputs.{output} }}}}\n" in sync, job


def test_claude_yml_gate_records_the_start_and_the_issue_run_pins_to_it():
    text = CLAUDE.read_text()
    assert "      start_sha: ${{ steps.start.outputs.sha }}\n" in text
    start = step_block(text, "start")
    assert "if: steps.trig.outputs.ok == 'true'" in start
    assert "HEAD_BRANCH: ${{ steps.engine.outputs.head_branch }}" in start
    assert "BASE: ${{ inputs.base_branch || github.event.repository.default_branch }}" in start
    # Both engine jobs pin their base step to the gate's read, and the two
    # copies run the same script (the lifted tests exercise the first).
    scripts = []
    for job in ENGINE_JOBS["claude.yml"]:
        base = step_block(job_block(text, job), "base")
        assert "GATE_SHA: ${{ needs.gate.outputs.start_sha }}" in base, job
        assert "BASE: ${{ inputs.base_branch || github.event.repository.default_branch }}" in base, job
        # Its fetch of the live base tip runs under the one step-scoped
        # credential-helper block every runner-side git network call uses.
        assert "GIT_TOKEN: ${{ github.token }}" in base, job
        assert "GIT_CONFIG_VALUE_1: '!f() { echo username=x-access-token; echo \"password=$GIT_TOKEN\"; }; f'" in base, job
        assert 'git fetch --quiet origin "+refs/heads/$BASE:refs/remotes/origin/$BASE"' in base, job
        scripts.append(base[base.index("run: |"):])
    assert scripts[0] == scripts[1]
    # The gate step runs after the engine step whose head_branch it reads,
    # and before the ack (nothing it does needs to wait for the ack).
    assert text.index("        id: engine\n") < text.index("        id: start\n")
    # emit-landing's start is still the agent job's own record (the value
    # bundles are cut above); the pin makes it the gate's when the run is
    # honest, and the land job compares against the gate's directly.
    emit = [s for s in text.split("\n      - ") if "emit-landing@main" in s]
    assert len(emit) == len(ENGINE_JOBS["claude.yml"])
    for e in emit:
        assert "start-sha: ${{ steps.base.outputs.sha || steps.sync.outputs.start_sha || steps.sync.outputs.head_sha || github.sha }}" in e


def test_the_landing_smoke_example_is_pinned_like_a_real_caller():
    # The copyable three-job example (review round 1, B1): its gate records
    # the start, its agent job's checkout must agree with it, its land job
    # passes it — the shape every direct `land@main` caller needs.
    text = (ROOT / "examples" / "landing-smoke.yml").read_text()
    assert "      start: ${{ steps.start.outputs.sha }}\n" in text
    assert "START: ${{ needs.gate.outputs.start }}" in step_block(text, "base")
    assert '[ "$sha" = "$START" ]' in step_block(text, "base")
    land = step_block(text, "land")
    assert "start-sha: ${{ needs.gate.outputs.start }}" in land
    assert "    needs: [gate, agent]\n" in text
    readme = (ROOT / ".github" / "actions" / "emit-landing" / "README.md").read_text()
    assert "          start-sha: ${{ needs.gate.outputs.start_sha }}\n" in readme


def test_the_reviewers_land_job_refuses_bundles_and_needs_no_start():
    text = (WORKFLOWS / "claude-review.yml").read_text()
    land = step_block(text, "land")
    assert 'refuse-bundle: "true"' in land
    assert "start-sha" not in land


def test_land_hands_the_start_to_the_validator_before_the_fetch():
    text = LAND.read_text()
    validate_step = step_block(text, "validate", indent=4)
    assert "        START_SHA: ${{ inputs.start-sha }}\n" in validate_step
    assert '--start-sha "$START_SHA"' in validate_step
    # Validate, then plan (the manifest's fields, read only after it
    # passed), then the fetch — which reads start_sha from the plan step.
    order = [text.index(f"    - id: {s}\n") for s in ("validate", "plan", "fetch")]
    assert order == sorted(order)
    fetch = step_block(text, "fetch", indent=4)
    assert "START_SHA: ${{ steps.plan.outputs.start_sha }}" in fetch
    assert "inputs.start-sha" not in fetch
    # The input exists and defaults to empty (fail closed on a bundle).
    idx = text.index("  start-sha:\n")
    block = text[idx:re.compile(r"\n  \S").search(text, idx + 1).start()]      # up to the next input
    assert '    default: ""' in block and "required: false" in block
