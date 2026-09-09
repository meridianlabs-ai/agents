"""Tests for the `land` composite's shell helpers and its git contract.

The composite's bash cannot run here as a unit, but two things it relies on
can: the de-fang/retry helpers in .github/actions/land/lib.sh, and the git
sequence the fetch/push steps execute (bundle the agent's commits above the
start SHA in one repo; in an EMPTY bare repo fetch the start SHA by SHA from
origin, verify and unbundle, check the tip and ancestry, refuse a moved
branch, push without --force). The sequence here mirrors the steps line for
line, so a git behaviour change (or an edit to the steps that this does not
follow) shows up as a failing test rather than a red land job on every
caller.
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / ".github" / "actions" / "land" / "lib.sh"
EMIT = ROOT / ".github" / "actions" / "emit-landing" / "action.yml"
LAND = ROOT / ".github" / "actions" / "land" / "action.yml"


def sh(*cmd, cwd=None, check=True, env=None):
    e = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    if env:
        e.update(env)
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True, env=e)


def git(*args, cwd, check=True):
    return sh("git", *args, cwd=cwd, check=check)


def bash_lib(snippet: str, cwd=None) -> subprocess.CompletedProcess:
    return sh("bash", "-c", f". '{LIB}'\n{snippet}", cwd=cwd, check=False)


# --- lib.sh -----------------------------------------------------------------


def test_defang_breaks_triggers_and_markers_case_insensitively(tmp_path):
    src = tmp_path / "in.md"
    src.write_text(
        "Please @review this and @Claude too; @AUTO.\n"
        "<!-- claude-review-verdict --> <!-- Claude-Review-Summary --> claude-review-comment claude-review-nudge\n"
        "<!-- AUTO-HANDOFF --> auto-converged auto-review-rounds auto-review-head auto-fix-attempts\n"
    )
    dst = tmp_path / "out.md"
    r = bash_lib(f"defang '{src}' '{dst}'")
    assert r.returncode == 0, r.stderr
    out = dst.read_text()
    for live in ("@review", "@Claude", "@AUTO", "claude-review-", "AUTO-HANDOFF", "auto-converged",
                 "auto-review-rounds", "auto-review-head", "auto-fix-attempts"):
        assert live.lower() not in out.lower(), out
    assert "`review`" in out and "`Claude`" in out and "`AUTO`" in out
    # The replacement text is literal (lowercase); the captured suffix keeps its case.
    assert "claude-review verdict" in out and "auto HANDOFF" in out


def test_defang_caps_oversized_bodies(tmp_path):
    src = tmp_path / "in.md"
    src.write_text("x" * 70000)
    dst = tmp_path / "out.md"
    assert bash_lib(f"defang '{src}' '{dst}'").returncode == 0
    out = dst.read_text()
    assert len(out) < 65536
    assert out.endswith("_[truncated: the body exceeded the comment size cap]_\n")


def test_defang_str_strips_newlines_and_triggers():
    r = bash_lib("defang_str 'Fix @auto loop\nclaude-review-summary'")
    assert r.returncode == 0
    assert r.stdout == "Fix `auto` loopclaude-review summary"


def test_retry_returns_last_status_and_keeps_stdout_clean():
    # sleep is skipped after the final attempt; with n=1 there is no sleep at all.
    r = bash_lib("retry 1 what false; echo rc=$?")
    assert r.stdout.strip() == "rc=1"
    r = bash_lib("out=$(retry 1 what echo hello); echo \"[$out]\"")
    assert r.stdout.strip() == "[hello]"


# A stub `gh` for open_or_adopt_pr: `pr list` reports the PR once it exists;
# `pr create` performs the write, and when LOSE_FIRST is set its FIRST call
# exits 1 after writing (a timeout / 5xx after the server accepted the PR).
# Calls are logged to $STATE/calls. `sleep` is neutralised so the retry
# backoff does not slow the suite.
GH_STUB = r"""
sleep() { :; }
gh() {
  echo "$1 $2" >>"$STATE/calls"
  case "$1 $2" in
    "pr list")
      if [ -f "$STATE/pr" ]; then n=$(cat "$STATE/pr"); echo "$n https://x/pull/$n"; fi ;;
    "pr create")
      [ -f "$STATE/pr" ] || echo 42 >"$STATE/pr"
      if [ -n "${LOSE_FIRST:-}" ] && [ ! -f "$STATE/lost" ]; then touch "$STATE/lost"; echo "gh: timeout" >&2; return 1; fi
      echo "https://x/pull/$(cat "$STATE/pr")" ;;
    *) echo "unexpected gh $*" >&2; return 2 ;;
  esac
}
"""


def open_or_adopt(tmp_path, *, lose_first=False, existing=None):
    state = tmp_path / "state"
    state.mkdir()
    if existing is not None:
        (state / "pr").write_text(str(existing))
    body = tmp_path / "body.md"
    body.write_text("body\n")
    env = {"STATE": str(state), "LOSE_FIRST": "1" if lose_first else ""}
    r = sh(
        "bash", "-c",
        f". '{LIB}'\n{GH_STUB}\nresult=$(retry 3 what open_or_adopt_pr o/r feat main T '{body}') || exit 9\n"
        "read -r how number url <<<\"$result\"; echo \"$how|$number|$url\"",
        check=False, env=env,
    )
    calls = (state / "calls").read_text().splitlines() if (state / "calls").exists() else []
    return r, calls


def test_open_or_adopt_pr_creates_when_none_exists(tmp_path):
    r, calls = open_or_adopt(tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "opened|42|https://x/pull/42"
    assert calls == ["pr list", "pr create"]


def test_open_or_adopt_pr_adopts_an_agent_opened_pr(tmp_path):
    r, calls = open_or_adopt(tmp_path, existing=7)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "adopted|7|https://x/pull/7"
    assert "pr create" not in calls


def test_open_or_adopt_pr_adopts_after_a_lost_create_response(tmp_path):
    # Attempt 1 created the PR but its response was lost; attempt 2 must
    # find and adopt it rather than fail on "a pull request already exists".
    r, calls = open_or_adopt(tmp_path, lose_first=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "adopted|42|https://x/pull/42"
    assert calls.count("pr create") == 1
    assert "retrying" in r.stderr


@pytest.mark.parametrize(
    "failed,pushed,expected",
    [
        ("push", "", "The agent's commits were **not** pushed."),
        ("fetch", "", "The agent's commits were **not** pushed."),
        ("post (comment on #79 failed after 5 attempts; issue create in o/r failed)", "1",
         "The agent's commits were pushed; only what follows the push is affected."),
        ("handback", "1",
         "The agent's commits were pushed; only what follows the push is affected. Post the re-review request by hand."),
        ("pr, stage", "1",
         "The agent's commits were pushed; only what follows the push is affected. Move the Atlas stage by hand."),
        ("handoff", "", "Post the hand-off by hand."),
        ("pr", "", ""),
    ],
)
def test_landing_failure_hint(failed, pushed, expected):
    r = bash_lib(f"landing_failure_hint '{failed}' '{pushed}'")
    assert r.returncode == 0, r.stderr
    assert r.stdout == expected
    # The note is posted un-de-fanged: it must never carry a live trigger.
    assert "@" not in r.stdout


# --- the git contract ------------------------------------------------------


@pytest.fixture
def repos(tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    landing = tmp_path / "landing"
    landing.mkdir()
    git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    git("init", "-q", str(work), cwd=tmp_path)
    git("config", "user.email", "a@b", cwd=work)
    git("config", "user.name", "a", cwd=work)
    (work / "f").write_text("1\n")
    git("add", "f", cwd=work)
    git("commit", "-qm", "base", cwd=work)
    git("branch", "-M", "feature", cwd=work)
    git("push", "-q", str(origin), "feature", cwd=work)
    start = git("rev-parse", "HEAD", cwd=work).stdout.strip()
    (work / "f").write_text("2\n")
    git("commit", "-qam", "agent change", cwd=work)
    (work / "f").write_text("3\n")
    git("commit", "-qam", "agent change 2", cwd=work)
    head = git("rev-parse", "HEAD", cwd=work).stdout.strip()
    return {"origin": origin, "work": work, "landing": landing, "start": start, "head": head, "tmp": tmp_path}


def emit(r):
    """What emit-landing does with the agent's repo."""
    work, landing = r["work"], r["landing"]
    git("merge-base", "--is-ancestor", r["start"], r["head"], cwd=work)
    # `<start>..HEAD`, not `<start>..<sha>`: bundle create records REFS, and a
    # bare SHA on the positive side is refused (nothing to name).
    git("bundle", "create", "--quiet", str(landing / "commits.bundle"), f"{r['start']}..HEAD", cwd=work)
    git("bundle", "verify", "--quiet", str(landing / "commits.bundle"), cwd=work)


def land_fetch(r):
    """What the land composite's fetch step does, in an empty bare repo."""
    repo = r["tmp"] / "landing-repo"
    git("init", "-q", "--bare", str(repo), cwd=r["tmp"])
    git("fetch", "--quiet", "--no-tags", str(r["origin"]), r["start"], cwd=repo)
    git("bundle", "verify", "--quiet", str(r["landing"] / "commits.bundle"), cwd=repo)
    git("fetch", "--quiet", "--no-tags", str(r["landing"] / "commits.bundle"), "HEAD", cwd=repo)
    assert git("rev-parse", "FETCH_HEAD", cwd=repo).stdout.strip() == r["head"]
    git("merge-base", "--is-ancestor", r["start"], r["head"], cwd=repo)
    return repo


def remote_tip(r):
    out = git("ls-remote", "--quiet", "--heads", str(r["origin"]), "refs/heads/feature", cwd=r["tmp"]).stdout
    return out.split("\t")[0] if out else ""


def tip_is_ancestor(repo, tip, head) -> bool:
    if git("cat-file", "-e", f"{tip}^{{commit}}", cwd=repo, check=False).returncode != 0:
        return False
    return git("merge-base", "--is-ancestor", tip, head, cwd=repo, check=False).returncode == 0


def test_bundle_lands_exactly_head_sha(repos):
    r = repos
    emit(r)
    repo = land_fetch(r)
    tip = remote_tip(r)
    assert tip == r["start"]
    assert tip_is_ancestor(repo, tip, r["head"])
    git("push", "--quiet", str(r["origin"]), f"{r['head']}:refs/heads/feature", cwd=repo)
    assert remote_tip(r) == r["head"]


def test_new_branch_is_created_by_the_push(repos):
    r = repos
    emit(r)
    repo = land_fetch(r)
    out = git("ls-remote", "--heads", str(r["origin"]), "refs/heads/new-branch", cwd=repo).stdout
    assert out == ""
    git("push", "--quiet", str(r["origin"]), f"{r['head']}:refs/heads/new-branch", cwd=repo)
    assert git("ls-remote", "--heads", str(r["origin"]), "refs/heads/new-branch", cwd=repo).stdout.startswith(r["head"])


def test_moved_branch_is_refused(repos):
    r = repos
    emit(r)
    repo = land_fetch(r)
    # Someone else pushed to the branch during the run.
    work = r["work"]
    git("checkout", "-q", "-b", "other", r["start"], cwd=work)
    (work / "g").write_text("x\n")
    git("add", "g", cwd=work)
    git("commit", "-qm", "human push", cwd=work)
    git("push", "-q", str(r["origin"]), "other:feature", cwd=work)
    tip = remote_tip(r)
    assert tip != r["start"]
    assert not tip_is_ancestor(repo, tip, r["head"])
    # And a plain (non-force) push is rejected non-fast-forward as the backstop.
    assert git("push", "--quiet", str(r["origin"]), f"{r['head']}:refs/heads/feature", cwd=repo, check=False).returncode != 0


def test_partially_pushed_bundle_is_still_landable(repos):
    r = repos
    emit(r)
    repo = land_fetch(r)
    # The agent already pushed its first commit itself: the tip is a bundle
    # commit, an ancestor of head — fine.
    first = git("rev-parse", f"{r['head']}~1", cwd=r["work"]).stdout.strip()
    git("push", "-q", str(r["origin"]), f"{first}:refs/heads/feature", cwd=r["work"])
    tip = remote_tip(r)
    assert tip == first
    assert tip_is_ancestor(repo, tip, r["head"])


def test_tampered_bundle_tip_is_detected(repos):
    r = repos
    emit(r)
    # The manifest claims a head_sha the bundle does not deliver.
    fake = dict(r, head="0" * 40)
    repo = r["tmp"] / "landing-repo"
    git("init", "-q", "--bare", str(repo), cwd=r["tmp"])
    git("fetch", "--quiet", "--no-tags", str(r["origin"]), r["start"], cwd=repo)
    git("fetch", "--quiet", "--no-tags", str(r["landing"] / "commits.bundle"), "HEAD", cwd=repo)
    assert git("rev-parse", "FETCH_HEAD", cwd=repo).stdout.strip() != fake["head"]


def test_bundle_from_rewritten_history_is_refused_at_emit(repos):
    r = repos
    work = r["work"]
    # The agent reset and rewrote: HEAD no longer descends from start.
    git("checkout", "-q", "--orphan", "rewrite", cwd=work)
    git("commit", "-qam", "rewritten", cwd=work)
    head = git("rev-parse", "HEAD", cwd=work).stdout.strip()
    assert git("merge-base", "--is-ancestor", r["start"], head, cwd=work, check=False).returncode != 0


def test_start_sha_unknown_to_origin_is_refused(repos):
    r = repos
    emit(r)
    repo = r["tmp"] / "landing-repo"
    git("init", "-q", "--bare", str(repo), cwd=r["tmp"])
    assert git("fetch", "--quiet", "--no-tags", str(r["origin"]), "1" * 40, cwd=repo, check=False).returncode != 0


# --- the composites reference no secret ------------------------------------


def test_emit_landing_references_no_secret():
    text = EMIT.read_text()
    assert "secrets." not in text
    assert "GIT_TOKEN" not in text  # not even a token input: nothing to leak


def test_land_tokens_are_step_scoped():
    text = LAND.read_text()
    assert "secrets." not in text
    assert "persist-credentials" not in text and "actions/checkout" not in text
    # Every credential-helper block reads the one variable name (AGENTS.md).
    assert text.count("GIT_CONFIG_VALUE_1: '!f() { echo username=x-access-token; echo \"password=$GIT_TOKEN\"; }; f'") == 2
