"""Tests for skills/merge-approved-prs/companion_mergeable.py — the merge
queue's check of a ts-mono companion's own review state before merging it
(Claude Security finding 4121986, fix criterion 2).

The decision function runs on canned API payloads; the command line runs end
to end against a stub `gh` on PATH that serves those payloads and logs every
call (GETs only, `--paginate` on the lists, the head re-read). Run with
`python3 -m pytest` from the repo root.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "merge-approved-prs" / "companion_mergeable.py"

spec = importlib.util.spec_from_file_location("companion_mergeable", SCRIPT)
cm = importlib.util.module_from_spec(spec)
sys.modules["companion_mergeable"] = cm
spec.loader.exec_module(cm)

REPO = "meridianlabs-ai/ts-mono"
PR_PATH = f"repos/{REPO}/pulls/7"
GENERATED = "packages/inspect-common/src/types/generated.ts"
INDEX = "packages/inspect-common/src/types/index.ts"
SHA1 = "a" * 40
SHA2 = "b" * 40
T_COMMIT = "2026-09-10T09:00:00Z"
T_REVIEW = "2026-09-10T12:00:00Z"
T_LATER = "2026-09-11T08:00:00Z"
TRUSTED = {"i-am-marvin", "epatey", "ransomr"}

_ids = iter(range(1000, 10_000))


def pr(head, author="i-am-marvin", state="open", base="main", head_repo=REPO):
    return {
        "number": 7,
        "state": state,
        "user": {"login": author},
        "head": {"sha": head, "ref": "regen", "repo": {"full_name": head_repo}},
        "base": {"ref": base, "repo": {"full_name": REPO, "default_branch": "main"}},
    }


def review(login, state, sha, at):
    return {"id": next(_ids), "user": {"login": login}, "state": state, "commit_id": sha, "submitted_at": at}


def commit(sha, date=T_COMMIT):
    return {"sha": sha, "commit": {"committer": {"date": date}, "author": {"date": date}}}


def file(name, status="modified", previous=None):
    return {"filename": name, "status": status, "previous_filename": previous}


def trust(login):
    return login in TRUSTED


def check(p, reviews=(), files=None, commits=None):
    if files is None:
        files = [file(GENERATED)]
    if commits is None:
        commits = [commit(p["head"]["sha"])]
    return cm.check(p, list(reviews), commits, list(files), trust, REPO)


# --- the decision: the five cases from the task, then the edges ----------------


def test_approved_at_head_passes_whatever_the_diff():
    v = check(
        pr(SHA1),
        [review("epatey", "APPROVED", SHA1, T_REVIEW)],
        [file(GENERATED), file(INDEX), file("apps/inspect/src/x.ts")],
    )
    assert v.ok
    assert v.message == f"approved {SHA1} by epatey at {T_REVIEW}"


def test_regenerate_only_companion_passes_without_a_review():
    v = check(pr(SHA1))
    assert v.ok
    assert v.message == f"regenerate-only {SHA1}: {GENERATED} by i-am-marvin"


def test_hand_written_change_without_approval_skips_with_both_reasons():
    v = check(pr(SHA1), files=[file(GENERATED), file(INDEX)])
    assert not v.ok
    assert (
        v.message
        == f"no approval for head {SHA1}; not regenerate-only: diff touches {INDEX}, outside the generated set"
    )


def test_approval_on_an_older_head_skips():
    # Approved at SHA1, then a hand-written commit pushed: the approval no longer
    # binds and the diff is not regenerate-only.
    v = check(
        pr(SHA2),
        [review("epatey", "APPROVED", SHA1, T_REVIEW)],
        [file(GENERATED), file(INDEX)],
        [commit(SHA1), commit(SHA2, T_LATER)],
    )
    assert not v.ok
    assert v.message == (
        f"approval is for {SHA1}, head is {SHA2}; 1 commit pushed after {T_REVIEW}; "
        f"not regenerate-only: diff touches {INDEX}, outside the generated set"
    )


def test_diff_whose_only_file_is_outside_the_generated_set_skips():
    v = check(pr(SHA1), files=[file("apps/scout/src/types/generated.ts")])
    assert not v.ok
    assert v.message.endswith(
        "not regenerate-only: diff touches apps/scout/src/types/generated.ts, outside the generated set"
    )


def test_approval_on_an_older_head_with_a_regenerate_only_diff_passes_as_regenerate_only():
    # The reviewed content was regenerated again after the approval; the diff is
    # still only the generated file, so the regenerate rule carries it.
    v = check(pr(SHA1), [review("epatey", "APPROVED", SHA2, T_REVIEW)], commits=[commit(SHA2), commit(SHA1, T_LATER)])
    assert v.ok
    assert v.message.startswith("regenerate-only")


def test_untrusted_approver_does_not_carry_a_hand_written_change():
    v = check(pr(SHA1), [review("outsider", "APPROVED", SHA1, T_REVIEW)], [file(GENERATED), file(INDEX)])
    assert not v.ok
    assert v.message.startswith(f"no approval for head {SHA1}: approval by outsider does not count")


def test_regenerate_only_needs_a_trusted_author():
    # ts-mono is public: anyone can open a PR that edits only generated.ts.
    v = check(pr(SHA1, author="outsider"))
    assert not v.ok
    assert v.message.endswith(f"not regenerate-only: author outsider is not trusted (no write access on {REPO})")


def test_regenerate_only_needs_a_same_repo_head():
    v = check(pr(SHA1, head_repo="outsider/ts-mono"))
    assert not v.ok
    assert v.message.endswith(f"not regenerate-only: head branch lives in outsider/ts-mono, not {REPO}")


def test_regenerate_only_needs_the_default_branch_as_base():
    v = check(pr(SHA1, base="release"))
    assert not v.ok
    assert v.message.endswith("not regenerate-only: base is release, not main")


def test_removed_added_or_renamed_generated_file_is_not_a_regeneration():
    for entry, detail in (
        (file(GENERATED, "removed"), f"{GENERATED} is removed, not modified"),
        (file(GENERATED, "added"), f"{GENERATED} is added, not modified"),
        (file(GENERATED, "renamed", previous="old.ts"), f"{GENERATED} is renamed (from old.ts), not modified"),
    ):
        v = check(pr(SHA1), files=[entry])
        assert not v.ok
        assert v.message.endswith(f"not regenerate-only: {detail}")


def test_empty_diff_is_not_a_regeneration():
    v = check(pr(SHA1), files=[])
    assert not v.ok
    assert v.message.endswith("not regenerate-only: empty diff")


def test_closed_pr_skips():
    v = check(pr(SHA1, state="closed"), [review("epatey", "APPROVED", SHA1, T_REVIEW)])
    assert not v.ok
    assert v.message == "PR is closed, not open"


def test_generated_set_is_exactly_what_types_generate_writes():
    assert cm.GENERATED_FILES == frozenset({GENERATED})


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
sys.stdout.write(json.dumps(body) + "\n")
"""


def fixtures_for(p, reviews=(), files=None, perms=None):
    if files is None:
        files = [file(GENERATED)]
    fx = {
        PR_PATH: p,
        f"{PR_PATH}/reviews": list(reviews),
        f"{PR_PATH}/commits": [commit(p["head"]["sha"])],
        f"{PR_PATH}/files": list(files),
    }
    for login, perm in (perms or {}).items():
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


WRITE = {"permission": "write", "role_name": "write"}


def test_cli_regenerate_only_passes_with_reads_only(tmp_path):
    r, calls = run_cli(
        tmp_path, [f"https://github.com/{REPO}/pull/7"], fixtures_for(pr(SHA1), perms={"i-am-marvin": WRITE})
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout == f"regenerate-only {SHA1}: {GENERATED} by i-am-marvin\n"
    assert [c[1] for c in calls] == [
        PR_PATH,
        f"{PR_PATH}/reviews",
        f"{PR_PATH}/commits",
        f"{PR_PATH}/files",
        PR_PATH,  # re-read: the head must not have moved while the lists were fetched
        f"repos/{REPO}/collaborators/i-am-marvin/permission",
    ]
    for c in calls:
        assert c[0] == "api"
        assert all(a == "--paginate" for a in c[2:]), c  # GETs only
    assert all("--paginate" in c for c in calls[1:4]) and "--paginate" not in calls[0]


def test_cli_approved_at_head_passes(tmp_path):
    fx = fixtures_for(
        pr(SHA1), [review("epatey", "APPROVED", SHA1, T_REVIEW)], [file(GENERATED), file(INDEX)], {"epatey": WRITE}
    )
    r, _ = run_cli(tmp_path, [REPO, "7"], fx)
    assert r.returncode == 0, r.stderr
    assert r.stdout == f"approved {SHA1} by epatey at {T_REVIEW}\n"


def test_cli_hand_written_change_by_untrusted_author_is_exit_1(tmp_path):
    # No permission fixtures: the lookup 404s, so nobody is trusted (fail closed).
    fx = fixtures_for(
        pr(SHA1, author="outsider"), [review("outsider", "APPROVED", SHA1, T_REVIEW)], [file(GENERATED), file(INDEX)]
    )
    r, calls = run_cli(tmp_path, [REPO, "7"], fx)
    assert r.returncode == 1
    assert r.stdout == (
        f"no approval for head {SHA1}: approval by outsider does not count (no write access on {REPO}); "
        f"not regenerate-only: author outsider is not trusted (no write access on {REPO})\n"
    )
    assert sum("/collaborators/" in c[1] for c in calls) == 1  # cached: one lookup for the reviewer-and-author


def test_cli_head_moved_during_the_check_is_exit_1_without_a_lookup(tmp_path):
    fx = fixtures_for(pr(SHA1), perms={"i-am-marvin": WRITE})
    fx[PR_PATH] = {"__sequence__": [pr(SHA1), pr(SHA2)]}
    r, calls = run_cli(tmp_path, [REPO, "7"], fx)
    assert r.returncode == 1
    assert r.stdout == f"head moved during the check: was {SHA1}, now {SHA2}\n"
    assert not any("/collaborators/" in c[1] for c in calls)


def test_cli_gh_failure_is_exit_2(tmp_path):
    r, _ = run_cli(tmp_path, [REPO, "7"], {})
    assert r.returncode == 2
    assert r.stdout == ""
    assert "HTTP 404" in r.stderr


def test_cli_usage_error_is_exit_2_without_a_request(tmp_path):
    r, calls = run_cli(tmp_path, ["nonsense"], {})
    assert r.returncode == 2
    assert calls == []
    assert "usage" in r.stderr
