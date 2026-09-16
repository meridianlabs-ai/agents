"""Tests for skills/merge-approved-prs/checks_at_head.py — the merge queue's
deferral of an External PR tree's tests to upstream CI (Claude Security
finding 4122327, fix criterion 2).

The decision function runs on canned API payloads (the base branch's rules,
the check runs on the SHA, the commit statuses); the command line runs end to
end against a stub `gh` on PATH that serves those payloads and logs every
call, so the request shape (GETs only, explicit paging) is pinned as well as
the verdicts. Run with `python3 -m pytest` from the repo root.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "merge-approved-prs" / "checks_at_head.py"

spec = importlib.util.spec_from_file_location("checks_at_head", SCRIPT)
cah = importlib.util.module_from_spec(spec)
sys.modules["checks_at_head"] = cah
spec.loader.exec_module(cah)

REPO = "UKGovernmentBEIS/inspect_ai"
PR_PATH = f"repos/{REPO}/pulls/42"
SHA1 = "a" * 40
SHA2 = "b" * 40
ACTIONS = 15368  # GitHub Actions' app id, as upstream's ruleset names it
OTHER_APP = 99999
REQUIRED = ["Build & inspect the package.", "mypy (3.10)", "mypy (3.11)", "test (3.10)", "test (3.11)"]

_ids = iter(range(1000, 10_000))


def pr(head, base="main"):
    return {"number": 42, "state": "open", "head": {"sha": head}, "base": {"ref": base}}


def rules(required=REQUIRED, integration=ACTIONS):
    checks = [{"context": c, "integration_id": integration} for c in required]
    return [
        {"type": "deletion"},
        {
            "type": "required_status_checks",
            "parameters": {"strict_required_status_checks_policy": True, "required_status_checks": checks},
        },
        {"type": "pull_request", "parameters": {"required_approving_review_count": 1}},
    ]


def run(name, conclusion="success", status="completed", app=ACTIONS, run_id=None):
    return {
        "id": run_id if run_id is not None else next(_ids),
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "app": {"id": app, "slug": "github-actions"},
    }


def green_runs(extra=()):
    return [run(name) for name in REQUIRED] + [run("ruff"), run("pre-commit"), run("docs", "skipped")] + list(extra)


def check(head=SHA1, rls=None, runs=None, statuses=(), expected=SHA1):
    return cah.check(
        pr(head),
        rules() if rls is None else rls,
        green_runs() if runs is None else runs,
        list(statuses),
        REPO,
        expected,
    )


# --- the decision -----------------------------------------------------------


def test_all_green_passes_and_names_the_required_checks():
    v = check()
    assert v.ok
    assert v.message == f"checks passed at {SHA1}: 8 check runs, 0 statuses; required (5): {', '.join(REQUIRED)}"


def test_head_is_not_the_approved_sha_fails_before_anything_else():
    v = check(head=SHA2, runs=[], rls=[])
    assert not v.ok
    assert v.message == f"head is {SHA2}, not the approved {SHA1}"


def test_without_expected_sha_the_head_is_checked():
    assert check(expected=None).ok


def test_a_required_check_that_never_reported_fails():
    # The fork-PR workflow-approval gate: the workflow awaits "Approve and run", so
    # its checks do not exist yet. GitHub leaves the PR blocked; so do we.
    runs = [r for r in green_runs() if r["name"] != "test (3.11)"]
    v = check(runs=runs)
    assert not v.ok
    assert v.message == f"checks at {SHA1} not green: missing required: test (3.11)"


def test_no_check_runs_at_all_fails_naming_every_required_check():
    v = check(runs=[])
    assert not v.ok
    assert v.message == f"checks at {SHA1} not green: missing required: {', '.join(REQUIRED)}"


def test_a_pending_required_check_fails():
    runs = [r if r["name"] != "test (3.10)" else run("test (3.10)", status="in_progress") for r in green_runs()]
    v = check(runs=runs)
    assert not v.ok
    assert v.message == f"checks at {SHA1} not green: pending: test (3.10) (in_progress)"


def test_a_failed_required_check_fails():
    runs = [r if r["name"] != "mypy (3.11)" else run("mypy (3.11)", "failure") for r in green_runs()]
    v = check(runs=runs)
    assert not v.ok
    assert v.message == f"checks at {SHA1} not green: failed: mypy (3.11) (failure)"


def test_a_failed_non_required_check_fails_too():
    # ruff is not in upstream's required set, but it is one of the checks the
    # skill used to run locally; a red one is a skip, not a shrug.
    v = check(runs=green_runs([run("check-schema-and-types", "failure")]))
    assert not v.ok
    assert v.message == f"checks at {SHA1} not green: failed: check-schema-and-types (failure)"


def test_a_pending_non_required_check_fails():
    v = check(runs=green_runs([run("slow-tests (checkpoint)", status="queued")]))
    assert not v.ok
    assert "pending: slow-tests (checkpoint) (queued)" in v.message


@pytest.mark.parametrize("conclusion", ["skipped", "neutral"])
def test_skipped_and_neutral_required_checks_pass_as_on_github(conclusion):
    runs = [r if r["name"] != "test (3.11)" else run("test (3.11)", conclusion) for r in green_runs()]
    assert check(runs=runs).ok


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "action_required", "stale", "startup_failure"])
def test_other_conclusions_fail(conclusion):
    v = check(runs=green_runs([run("viewer-tests", conclusion)]))
    assert not v.ok
    assert f"failed: viewer-tests ({conclusion})" in v.message


def test_every_problem_is_listed_in_one_line():
    runs = [r for r in green_runs() if r["name"] != "test (3.11)"]
    runs += [run("ruff", "failure"), run("docs", status="queued")]
    v = check(runs=cah.latest_runs(runs))
    assert not v.ok
    assert v.message.startswith(
        f"checks at {SHA1} not green: missing required: test (3.11); pending: docs (queued); failed: ruff (failure)"
    )


def test_a_required_check_from_another_app_does_not_count():
    # Upstream's rule names the reporting app; a same-named run from some other
    # integration is not the required check.
    runs = [r if r["name"] != "test (3.10)" else run("test (3.10)", app=OTHER_APP) for r in green_runs()]
    v = check(runs=runs)
    assert not v.ok
    assert v.message == f"checks at {SHA1} not green: missing required: test (3.10)"


def test_a_required_check_without_an_integration_matches_by_name_alone():
    runs = [r if r["name"] != "test (3.10)" else run("test (3.10)", app=OTHER_APP) for r in green_runs()]
    assert check(rls=rules(integration=None), runs=runs).ok


def test_no_required_checks_configured_fails_closed():
    v = check(rls=[{"type": "deletion"}])
    assert not v.ok
    assert v.message == f"no required status checks on {REPO} main: nothing to defer to"


def test_a_non_success_commit_status_fails():
    v = check(statuses=[{"context": "ci/external", "state": "pending"}])
    assert not v.ok
    assert v.message == f"checks at {SHA1} not green: failed: status ci/external (pending)"


def test_success_commit_statuses_are_counted():
    v = check(statuses=[{"context": "ci/external", "state": "success"}])
    assert v.ok
    assert "8 check runs, 1 statuses" in v.message


def test_latest_runs_keeps_the_newest_per_name_and_app():
    # A re-run: the first attempt failed, the newer one passed. GitHub's
    # `filter=latest` already returns only the newer; the dedupe is belt and braces.
    runs = green_runs([run("test (3.10)", "failure", run_id=1)])
    latest = cah.latest_runs(runs)
    assert len(latest) == len(green_runs())
    assert next(r for r in latest if r["name"] == "test (3.10)")["conclusion"] == "success"
    assert check(runs=runs).ok


# --- parsing ----------------------------------------------------------------


def test_parse_args_accepts_the_pr_forms_and_an_optional_sha():
    assert cah.parse_args([f"https://github.com/{REPO}/pull/42"]) == (REPO, 42, None)
    assert cah.parse_args([REPO, "42", "--sha", SHA1]) == (REPO, 42, SHA1)
    assert cah.parse_args(["--sha", SHA1, f"https://github.com/{REPO}/pull/42"]) == (REPO, 42, SHA1)
    assert cah.parse_args([REPO, "42", f"--sha={SHA1}"]) == (REPO, 42, SHA1)
    for bad in (
        [],
        ["--sha", SHA1],
        [REPO, "42", "--sha"],
        [REPO, "42", "--sha", "abc"],
        [REPO, "42", "--sha", SHA1, "--sha", SHA2],
    ):
        with pytest.raises(ValueError):
            cah.parse_args(bad)


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
if isinstance(body, list) and "--paginate" in args and len(body) > 1:
    # The older gh shape: one array per page, back to back.
    sys.stdout.write(json.dumps(body[:1]) + json.dumps(body[1:]))
else:
    sys.stdout.write(json.dumps(body) + "\n")
"""


def runs_path(sha, page=1):
    return f"repos/{REPO}/commits/{sha}/check-runs?filter=latest&per_page=100&page={page}"


def status_path(sha):
    return f"repos/{REPO}/commits/{sha}/status?per_page=100"


RULES_PATH = f"repos/{REPO}/rules/branches/main"


def fixtures_for(head, runs, statuses=(), rls=None):
    return {
        PR_PATH: pr(head),
        RULES_PATH: rules() if rls is None else rls,
        runs_path(head): {"total_count": len(runs), "check_runs": runs},
        status_path(head): {
            "state": "success" if statuses else "pending",
            "total_count": len(statuses),
            "statuses": list(statuses),
        },
    }


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
    r, calls = run_cli(
        tmp_path, [f"https://github.com/{REPO}/pull/42", "--sha", SHA1], fixtures_for(SHA1, green_runs())
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout == f"checks passed at {SHA1}: 8 check runs, 0 statuses; required (5): {', '.join(REQUIRED)}\n"
    assert [c[1] for c in calls] == [PR_PATH, RULES_PATH, runs_path(SHA1), status_path(SHA1)]
    for c in calls:
        # GETs only: `gh api <path>` and at most --paginate — no -X/--method, no -f/-F/--input.
        assert c[0] == "api" and all(a == "--paginate" for a in c[2:]), c
    # The rules are paged (30 a page, inherited rules included); the rest are single reads.
    assert [("--paginate" in c) for c in calls] == [False, True, False, False]


def test_cli_required_check_on_a_later_rules_page_is_enforced(tmp_path):
    # Page 1 of the rules requires only checks that passed; page 2 requires one
    # that never reported. Without paging the second page, this would pass.
    fx = fixtures_for(SHA1, green_runs())
    fx[RULES_PATH] = [rules(required=REQUIRED[:1])[1], rules(required=["slow-tests (full)"])[1]]
    r, calls = run_cli(tmp_path, [REPO, "42"], fx)
    assert r.returncode == 1
    assert r.stdout == f"checks at {SHA1} not green: missing required: slow-tests (full)\n"
    assert any(c[1] == RULES_PATH and "--paginate" in c for c in calls)


def test_cli_wrong_head_is_exit_1_before_any_other_read(tmp_path):
    r, calls = run_cli(tmp_path, [REPO, "42", "--sha", SHA1], fixtures_for(SHA2, green_runs()))
    assert r.returncode == 1
    assert r.stdout == f"head is {SHA2}, not the approved {SHA1}\n"
    assert [c[1] for c in calls] == [PR_PATH]


def test_cli_red_check_is_exit_1(tmp_path):
    fx = fixtures_for(SHA1, green_runs([run("check-schema-and-types", "failure")]))
    r, _ = run_cli(tmp_path, [REPO, "42"], fx)
    assert r.returncode == 1
    assert r.stdout == f"checks at {SHA1} not green: failed: check-schema-and-types (failure)\n"


def test_cli_pages_check_runs_by_total_count(tmp_path):
    first, second = green_runs()[:5], green_runs()[5:]
    fx = fixtures_for(SHA1, [])
    fx[runs_path(SHA1)] = {"total_count": 8, "check_runs": first}
    fx[runs_path(SHA1, 2)] = {"total_count": 8, "check_runs": second}
    r, calls = run_cli(tmp_path, [REPO, "42"], fx)
    assert r.returncode == 0, r.stderr
    assert runs_path(SHA1, 2) in [c[1] for c in calls]


def test_cli_short_page_is_exit_2(tmp_path):
    # total_count promises more than the pages deliver: unverifiable, fail closed.
    fx = fixtures_for(SHA1, [])
    fx[runs_path(SHA1)] = {"total_count": 8, "check_runs": green_runs()[:5]}
    fx[runs_path(SHA1, 2)] = {"total_count": 8, "check_runs": []}
    r, _ = run_cli(tmp_path, [REPO, "42"], fx)
    assert r.returncode == 2
    assert r.stdout == ""
    assert "read 5 of 8 check runs" in r.stderr


def test_cli_gh_failure_is_exit_2(tmp_path):
    r, _ = run_cli(tmp_path, [REPO, "42"], {})
    assert r.returncode == 2
    assert r.stdout == ""
    assert "HTTP 404" in r.stderr


def test_cli_usage_error_is_exit_2_without_a_request(tmp_path):
    r, calls = run_cli(tmp_path, [REPO, "42", "--sha", "nonsense"], {})
    assert r.returncode == 2
    assert calls == []
    assert "usage" in r.stderr
