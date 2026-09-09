"""Tests for .github/scripts/validate_manifest.py — the land job's trust boundary.

One valid manifest, then one failing case per rule. Run with `python3 -m pytest`
from the repo root (pytest.ini sets the test path).
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "validate_manifest.py"

spec = importlib.util.spec_from_file_location("validate_manifest", SCRIPT)
vm = importlib.util.module_from_spec(spec)
sys.modules["validate_manifest"] = vm
spec.loader.exec_module(vm)

REPO = "meridianlabs-ai/agents"
RUN_ID = "123456"
DEFAULT_BRANCH = "main"
# The land composite's `refused-branches` default.
REFUSED = ["main"]
ALLOWED = ["meridianlabs-ai/agents", "meridianlabs-ai/inspect_ai"]
START = "a" * 40
HEAD = "b" * 40
BRANCH = "claude/issue-79-plumbing"


def write(d: Path, name: str, text: str = "hello\n") -> str:
    p = d / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return name


def base_manifest(d: Path, **overrides) -> dict:
    (d / "commits.bundle").write_bytes(b"# v2 git bundle\n")
    m = {
        "schema": 1,
        "repo": REPO,
        "run_id": int(RUN_ID),
        "branch": BRANCH,
        "start_sha": START,
        "head_sha": HEAD,
        "has_bundle": True,
        "pr_number": 456,
        "issue_number": 79,
        "pr": {
            "open": True,
            "title": "Add the plumbing",
            "body_file": write(d, "pr-body.md"),
            "base": "main",
            "labels": ["auto"],
            "issue": 79,
        },
        "comments": [{"number": 79, "body_file": write(d, "c1.md")}],
        "replies": [{"review_comment_id": 789, "body_file": write(d, "r1.md")}],
        "resolve_threads": ["PRRT_kwDOC7YMCM5abc-123_x"],
        "issues": [
            {
                "repo": "meridianlabs-ai/inspect_ai",
                "title": "Follow-up",
                "body_file": write(d, "i1.md"),
                "labels": ["auto"],
                "comment_on": None,
            }
        ],
        "stage": "Review",
        "handback": True,
        "handoff_body_file": write(d, "handoff.md"),
        "error": {"message": "something", "fail_run": False},
        "provenance_comment_file": write(d, "prov.md"),
    }
    m.update(overrides)
    return m


# What the run's event names — base_manifest's pr_number / issue_number.
EVENT_PR = "456"
EVENT_ISSUE = "79"


def run(
    d: Path,
    manifest,
    *,
    pr_head_ref=BRANCH,
    default_branch=DEFAULT_BRANCH,
    repo=REPO,
    run_id=RUN_ID,
    allowed=None,
    refused=None,
    event_pr=EVENT_PR,
    event_issue=EVENT_ISSUE,
):
    return vm.validate(
        manifest,
        artifact_dir=d,
        repo=repo,
        run_id=run_id,
        default_branch=default_branch,
        allowed_issue_repos=ALLOWED if allowed is None else allowed,
        pr_head_ref=pr_head_ref,
        refused_branches=REFUSED if refused is None else refused,
        event_pr_number=event_pr,
        event_issue_number=event_issue,
    )


def test_valid_manifest(tmp_path):
    assert run(tmp_path, base_manifest(tmp_path)) == []


def test_minimal_manifest_without_bundle(tmp_path):
    # A `schedule` / `workflow_dispatch` shape: the event names no PR or
    # issue, and the manifest names none either.
    m = {
        "schema": 1,
        "repo": REPO,
        "run_id": int(RUN_ID),
        "branch": BRANCH,
        "start_sha": START,
        "head_sha": START,
        "has_bundle": False,
    }
    assert run(tmp_path, m, pr_head_ref="", event_pr="", event_issue="") == []


# --- schema / identity -------------------------------------------------------


def test_wrong_schema_version(tmp_path):
    errs = run(tmp_path, base_manifest(tmp_path, schema=2))
    assert any("schema must be 1" in e for e in errs)


def test_repo_mismatch(tmp_path):
    errs = run(tmp_path, base_manifest(tmp_path, repo="someone/else"))
    assert any("not the repo this land job operates on" in e for e in errs)


def test_repo_comparison_is_case_insensitive(tmp_path):
    assert run(tmp_path, base_manifest(tmp_path, repo=REPO.upper())) == []


def test_run_id_mismatch(tmp_path):
    errs = run(tmp_path, base_manifest(tmp_path, run_id=999))
    assert any("run_id" in e for e in errs)


def test_unknown_top_level_key(tmp_path):
    errs = run(tmp_path, base_manifest(tmp_path, slack={"text": "hi"}))
    assert any("unknown key 'slack'" in e for e in errs)


def test_unknown_nested_key(tmp_path):
    m = base_manifest(tmp_path)
    m["pr"]["draft"] = True
    errs = run(tmp_path, m)
    assert any("pr: unknown key 'draft'" in e for e in errs)


def test_top_level_must_be_object(tmp_path):
    errs = run(tmp_path, ["not", "an", "object"])
    assert errs == ["manifest: top level must be a JSON object"]


# --- branch ------------------------------------------------------------------


@pytest.mark.parametrize(
    "branch,needle",
    [
        ("refs/heads/main", "starts with 'refs/'"),
        ("feature/../main", "'..'"),
        ("has space", "characters outside"),
        ("x" * 201, "characters outside"),
        ("bad;rm -rf", "characters outside"),
        ("", "must not be empty"),
    ],
)
def test_bad_branch(tmp_path, branch, needle):
    errs = run(tmp_path, base_manifest(tmp_path, branch=branch), pr_head_ref=branch)
    assert any(needle in e for e in errs), errs


def test_branch_is_default_branch(tmp_path):
    errs = run(tmp_path, base_manifest(tmp_path, branch="main"), pr_head_ref="main", refused=[])
    assert any("must not be the default branch" in e for e in errs)
    assert any("must not equal pr.base" in e for e in errs)


def test_refused_branch_that_is_not_the_default(tmp_path):
    # The inspect_ai fork: `main` is the pristine mirror but `meridian` is the
    # default branch, so the default-branch rule alone would let `main` pass.
    m = base_manifest(tmp_path, branch="main")
    m["pr"]["base"] = "meridian"
    errs = run(tmp_path, m, pr_head_ref="main", default_branch="meridian")
    assert errs == ["manifest: branch 'main' is on the land job's refused list (main)"]
    # Exact, case-sensitive names: a branch merely resembling one is fine …
    m = base_manifest(tmp_path, branch="Main")
    assert run(tmp_path, m, pr_head_ref="Main", default_branch="meridian") == []
    # … and `main` as pr.base stays legitimate (fork PRs base on it).
    assert run(tmp_path, base_manifest(tmp_path), default_branch="meridian") == []


def test_refused_branches_list_is_trimmed_and_may_be_empty(tmp_path):
    m = base_manifest(tmp_path, branch="release")
    m["pr"]["base"] = "meridian"
    errs = run(tmp_path, m, pr_head_ref="release", default_branch="meridian", refused=[" main", "release ", ""])
    assert any("'release' is on the land job's refused list (main, release)" in e for e in errs)
    assert run(tmp_path, m, pr_head_ref="release", default_branch="meridian", refused=[""]) == []


def test_empty_default_branch_fails_closed(tmp_path):
    # A caller event without a `repository` payload and a failed lookup must
    # not silently skip the default-branch rule.
    errs = run(tmp_path, base_manifest(tmp_path), default_branch="")
    assert any("no default branch was supplied" in e for e in errs)
    errs = run(tmp_path, base_manifest(tmp_path, branch="main"), pr_head_ref="main", default_branch="")
    assert any("no default branch was supplied" in e for e in errs)


def test_branch_equals_pr_base(tmp_path):
    m = base_manifest(tmp_path)
    m["pr"]["base"] = BRANCH
    errs = run(tmp_path, m)
    assert any("must not equal pr.base" in e for e in errs)


@pytest.mark.parametrize("base", [None, ""])
def test_pr_base_is_optional(tmp_path, base):
    # Absent or empty: the land job uses its default branch, as gh would.
    m = base_manifest(tmp_path)
    if base is None:
        del m["pr"]["base"]
    else:
        m["pr"]["base"] = base
    assert run(tmp_path, m) == []


def test_pr_base_shape(tmp_path):
    m = base_manifest(tmp_path)
    m["pr"]["base"] = "has space"
    errs = run(tmp_path, m)
    assert any("pr: base has characters outside" in e for e in errs)


def test_branch_must_match_pr_head_ref(tmp_path):
    errs = run(tmp_path, base_manifest(tmp_path), pr_head_ref="some/other-branch")
    assert any("is not PR #456's head ref" in e for e in errs)


def test_pr_number_without_head_ref_fails_closed(tmp_path):
    errs = run(tmp_path, base_manifest(tmp_path), pr_head_ref="")
    assert any("no PR head ref was supplied" in e for e in errs)


# --- the event tie: pr_number / issue_number are the run's, not the agent's --


def test_pr_number_must_be_the_events_pr(tmp_path):
    # A run for PR 456 whose manifest names PR 457 (with 457's head as
    # branch and 457's tip as start_sha) would otherwise push onto, reply
    # on, thread-resolve and hand back a PR the agent picked.
    errs = run(tmp_path, base_manifest(tmp_path, pr_number=457), pr_head_ref=BRANCH)
    assert "manifest: pr_number 457 is not the PR this run's event names (#456)" in errs


def test_pr_number_set_but_event_names_no_pr(tmp_path):
    errs = run(tmp_path, base_manifest(tmp_path), event_pr="")
    assert any("pr_number is set (456) but this run's event names no PR" in e for e in errs)


def test_pr_number_null_but_event_names_a_pr(tmp_path):
    # Dropping pr_number on a PR run would skip the head-ref rule and let
    # `pr.open` adopt whatever open PR `branch` belongs to.
    m = base_manifest(tmp_path, pr_number=None, replies=[], resolve_threads=[])
    errs = run(tmp_path, m, pr_head_ref="")
    assert errs == ["manifest: pr_number is null but this run's event names PR #456"]


def test_pr_number_of_wrong_type_is_not_also_reported_as_null(tmp_path):
    # The type error is the one to fix; the tie does not pile a misleading
    # "pr_number is null" error on top of it.
    m = base_manifest(tmp_path, pr_number="456", replies=[], resolve_threads=[])
    assert run(tmp_path, m) == ["manifest: pr_number must be a positive integer"]


def test_issue_number_must_be_the_events_issue(tmp_path):
    errs = run(tmp_path, base_manifest(tmp_path, issue_number=80))
    assert errs == ["manifest: issue_number 80 is not the issue this run's event names (#79)"]


def test_issue_number_set_but_event_names_no_issue(tmp_path):
    errs = run(tmp_path, base_manifest(tmp_path), event_issue="")
    assert errs == ["manifest: issue_number is set (79) but this run's event names no issue (--event-issue-number is empty)"]


def test_issue_number_null_but_event_names_an_issue(tmp_path):
    errs = run(tmp_path, base_manifest(tmp_path, issue_number=None))
    assert errs == ["manifest: issue_number is null but this run's event names issue #79"]


def test_pr_comment_run_names_the_pr_as_both(tmp_path):
    # `issue_comment` on a PR: github.event.issue.number IS the PR number,
    # and callers pass it as both inputs — the manifest carries both too.
    m = base_manifest(tmp_path, issue_number=456)
    assert run(tmp_path, m, event_issue="456") == []


def test_event_numbers_are_trimmed(tmp_path):
    assert run(tmp_path, base_manifest(tmp_path), event_pr=" 456 ", event_issue="79\n") == []


@pytest.mark.parametrize("value", ["abc", "0", "-1", "456 457", "01"])
def test_malformed_event_number_fails_closed(tmp_path, value):
    # A caller passing a non-number is misconfigured; refuse rather than
    # treat it as "names none" (which a null pr_number would then satisfy).
    errs = run(tmp_path, base_manifest(tmp_path), event_pr=value)
    assert any(f"--event-pr-number {value!r} is not a positive integer" in e for e in errs)


# --- shas / bundle ------------------------------------------------------------


@pytest.mark.parametrize("field", ["start_sha", "head_sha"])
@pytest.mark.parametrize("value", ["abc", "A" * 40, "g" * 40, 123, None])
def test_bad_sha(tmp_path, field, value):
    errs = run(tmp_path, base_manifest(tmp_path, **{field: value}))
    assert any(field in e for e in errs), errs


def test_has_bundle_true_but_shas_equal(tmp_path):
    errs = run(tmp_path, base_manifest(tmp_path, head_sha=START))
    assert any("has_bundle is true but head_sha equals start_sha" in e for e in errs)


def test_has_bundle_false_but_shas_differ(tmp_path):
    errs = run(tmp_path, base_manifest(tmp_path, has_bundle=False))
    assert any("has_bundle is false but head_sha differs" in e for e in errs)


def test_has_bundle_must_be_boolean(tmp_path):
    errs = run(tmp_path, base_manifest(tmp_path, has_bundle="yes"))
    assert any("has_bundle must be a boolean" in e for e in errs)


def test_bundle_file_missing(tmp_path):
    m = base_manifest(tmp_path)
    (tmp_path / "commits.bundle").unlink()
    errs = run(tmp_path, m)
    assert any("commits.bundle is missing" in e for e in errs)


# --- file references ---------------------------------------------------------


def test_symlinked_body_file(tmp_path):
    m = base_manifest(tmp_path)
    (tmp_path / "c1.md").unlink()
    os.symlink("/etc/hostname", tmp_path / "c1.md")
    errs = run(tmp_path, m)
    assert any("comments[0]: body_file 'c1.md' is (or passes through) a symlink" in e for e in errs)


def test_symlinked_directory_in_path(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "x.md").write_text("x")
    os.symlink(outside, tmp_path / "link")
    m = base_manifest(tmp_path)
    m["comments"][0]["body_file"] = "link/x.md"
    errs = run(tmp_path, m)
    assert any("passes through) a symlink" in e for e in errs)


def test_dotdot_body_file(tmp_path):
    m = base_manifest(tmp_path)
    m["comments"][0]["body_file"] = "../manifest.json"
    errs = run(tmp_path, m)
    assert any("must not contain '..'" in e for e in errs)


def test_absolute_body_file(tmp_path):
    m = base_manifest(tmp_path)
    m["comments"][0]["body_file"] = "/etc/passwd"
    errs = run(tmp_path, m)
    assert any("must be a relative path" in e for e in errs)


@pytest.mark.parametrize("name", ["c 1.md", "c\n1.md", "c;1.md", "c\\1.md"])
def test_body_file_charset(tmp_path, name):
    m = base_manifest(tmp_path)
    (tmp_path / name).write_text("x")
    m["comments"][0]["body_file"] = name
    errs = run(tmp_path, m)
    assert any("has characters outside" in e for e in errs), errs


@pytest.mark.parametrize("name", [".handoff.md", ".bodies/c1.md", "bodies/.c1.md"])
def test_hidden_body_file_is_refused(tmp_path, name):
    # upload-artifact drops dot-named paths (include-hidden-files: false), so
    # the reference is refused by name even though the file exists here.
    m = base_manifest(tmp_path)
    m["comments"][0]["body_file"] = write(tmp_path, name)
    errs = run(tmp_path, m)
    assert any("starting with '.'" in e for e in errs), errs
    assert not any("does not exist" in e for e in errs)


def test_missing_body_file(tmp_path):
    m = base_manifest(tmp_path)
    m["comments"][0]["body_file"] = "nope.md"
    errs = run(tmp_path, m)
    assert any("does not exist" in e for e in errs)


def test_body_file_is_directory(tmp_path):
    m = base_manifest(tmp_path)
    (tmp_path / "dir").mkdir()
    m["comments"][0]["body_file"] = "dir"
    errs = run(tmp_path, m)
    assert any("is not a regular file" in e for e in errs)


def test_body_file_too_large(tmp_path):
    m = base_manifest(tmp_path)
    (tmp_path / "c1.md").write_bytes(b"x" * (64 * 1024 + 1))
    errs = run(tmp_path, m)
    assert any("the cap is 65536" in e for e in errs)


def test_nested_body_file_is_allowed(tmp_path):
    m = base_manifest(tmp_path)
    m["comments"][0]["body_file"] = write(tmp_path, "bodies/c1.md")
    assert run(tmp_path, m) == []


# --- lists -------------------------------------------------------------------


def test_issue_repo_outside_allow_list(tmp_path):
    m = base_manifest(tmp_path)
    m["issues"][0]["repo"] = "evil/repo"
    errs = run(tmp_path, m)
    assert any("not in the allowed issue repos" in e for e in errs)


def test_issue_repo_allow_list_empty_rejects_everything(tmp_path):
    errs = run(tmp_path, base_manifest(tmp_path), allowed=[])
    assert any("not in the allowed issue repos" in e for e in errs)


def test_issue_repo_malformed(tmp_path):
    m = base_manifest(tmp_path)
    m["issues"][0]["repo"] = "not-a-repo"
    errs = run(tmp_path, m)
    assert any("is not owner/name" in e for e in errs)


@pytest.mark.parametrize("value", [0, -1, "5", 1.5, True, None])
def test_comment_number_must_be_positive_int(tmp_path, value):
    m = base_manifest(tmp_path)
    m["comments"][0]["number"] = value
    errs = run(tmp_path, m)
    assert any("comments[0]: number" in e for e in errs), errs


def test_comment_target_was_dropped(tmp_path):
    # `target` was removed from the schema (the land job never read it — the
    # issues endpoint serves PRs and issues alike); a manifest still carrying
    # it is refused like any other unknown key.
    m = base_manifest(tmp_path)
    m["comments"][0]["target"] = "issue"
    errs = run(tmp_path, m)
    assert any("comments[0]: unknown key 'target'" in e for e in errs)


@pytest.mark.parametrize("value", [0, "789", None])
def test_reply_id_must_be_positive_int(tmp_path, value):
    m = base_manifest(tmp_path)
    m["replies"][0]["review_comment_id"] = value
    errs = run(tmp_path, m)
    assert any("replies[0]: review_comment_id" in e for e in errs)


def test_replies_need_pr_number(tmp_path):
    m = base_manifest(tmp_path, pr_number=None)
    errs = run(tmp_path, m, pr_head_ref="", event_pr="")
    assert any("replies need pr_number" in e for e in errs)
    assert any("resolve_threads need pr_number" in e for e in errs)


@pytest.mark.parametrize("value", [0, "0", "abc", True])
def test_pr_number_must_be_positive_int(tmp_path, value):
    errs = run(tmp_path, base_manifest(tmp_path, pr_number=value))
    assert any("pr_number must be a positive integer" in e for e in errs)


@pytest.mark.parametrize("thread", ["PRR_abc", "PRRT_", "PRRT_abc def", "prrt_abc", 12])
def test_bad_thread_id(tmp_path, thread):
    errs = run(tmp_path, base_manifest(tmp_path, resolve_threads=[thread]))
    assert any("resolve_threads[0]" in e for e in errs)


def test_labels_must_be_strings(tmp_path):
    m = base_manifest(tmp_path)
    m["issues"][0]["labels"] = ["ok", 3]
    errs = run(tmp_path, m)
    assert any("issues[0]: labels must be a list" in e for e in errs)


# --- scalars -----------------------------------------------------------------


def test_bad_stage(tmp_path):
    errs = run(tmp_path, base_manifest(tmp_path, stage="Done"))
    assert any("stage must be one of" in e for e in errs)


@pytest.mark.parametrize("stage", vm.STAGES)
def test_every_stage_option(tmp_path, stage):
    assert run(tmp_path, base_manifest(tmp_path, stage=stage)) == []


def test_handback_needs_pr(tmp_path):
    m = base_manifest(tmp_path, pr_number=None, pr=None, replies=[], resolve_threads=[])
    errs = run(tmp_path, m, pr_head_ref="", event_pr="")
    assert any("handback needs a PR" in e for e in errs)


def test_handback_must_be_boolean(tmp_path):
    errs = run(tmp_path, base_manifest(tmp_path, handback="yes"))
    assert any("handback must be a boolean" in e for e in errs)


def test_error_shape(tmp_path):
    errs = run(tmp_path, base_manifest(tmp_path, error={"message": 5, "fail_run": "no", "extra": 1}))
    assert any("error: message must be a string" in e for e in errs)
    assert any("error: fail_run must be a boolean" in e for e in errs)
    assert any("error: unknown key 'extra'" in e for e in errs)


def test_pr_title_too_long(tmp_path):
    m = base_manifest(tmp_path)
    m["pr"]["title"] = "t" * 257
    errs = run(tmp_path, m)
    assert any("pr: title exceeds 256" in e for e in errs)


# --- CLI ---------------------------------------------------------------------


def cli(d: Path, *extra, pr_head_ref=BRANCH, event_pr=EVENT_PR, event_issue=EVENT_ISSUE):
    argv = [
        "--dir", str(d),
        "--repo", REPO,
        "--run-id", RUN_ID,
        "--default-branch", DEFAULT_BRANCH,
        "--refused-branches", ",".join(REFUSED),
        "--allowed-issue-repos", ",".join(ALLOWED),
        "--pr-head-ref", pr_head_ref,
        "--event-pr-number", event_pr,
        "--event-issue-number", event_issue,
        *extra,
    ]
    return vm.main(argv)


def test_cli_valid(tmp_path, capsys):
    (tmp_path / "manifest.json").write_text(json.dumps(base_manifest(tmp_path)))
    assert cli(tmp_path) == 0
    assert "manifest ok." in capsys.readouterr().out


def test_cli_event_numbers_default_to_none_named(tmp_path, capsys):
    # The flags' own default is the empty string — "the event names no PR /
    # issue" — so a manifest naming either is refused when a caller omits them.
    (tmp_path / "manifest.json").write_text(json.dumps(base_manifest(tmp_path)))
    argv = [
        "--dir", str(tmp_path), "--repo", REPO, "--run-id", RUN_ID, "--default-branch", DEFAULT_BRANCH,
        "--allowed-issue-repos", ",".join(ALLOWED), "--pr-head-ref", BRANCH,
    ]
    assert vm.main(argv) == 1
    out = capsys.readouterr().out
    assert "pr_number is set (456) but this run's event names no PR" in out
    assert "issue_number is set (79) but this run's event names no issue" in out


def test_cli_event_pr_mismatch(tmp_path, capsys):
    (tmp_path / "manifest.json").write_text(json.dumps(base_manifest(tmp_path)))
    assert cli(tmp_path, event_pr="457") == 1
    assert "pr_number 456 is not the PR this run's event names (#457)" in capsys.readouterr().out


def test_cli_prints_one_line_per_violation(tmp_path, capsys):
    m = base_manifest(tmp_path, branch="refs/heads/main", schema=3)
    (tmp_path / "manifest.json").write_text(json.dumps(m))
    assert cli(tmp_path, pr_head_ref="refs/heads/main") == 1
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith("manifest violation: ")]
    assert len(lines) >= 2
    assert any("refs/" in line for line in lines)
    assert any("schema" in line for line in lines)


def test_cli_empty_default_branch_refuses(tmp_path, capsys):
    # argparse accepts "" for a required flag; the validator must not.
    (tmp_path / "manifest.json").write_text(json.dumps(base_manifest(tmp_path)))
    assert cli(tmp_path, "--default-branch", "") == 1
    assert "no default branch was supplied" in capsys.readouterr().out


def test_cli_refused_branches(tmp_path, capsys):
    m = base_manifest(tmp_path, branch="main")
    m["pr"]["base"] = "meridian"
    (tmp_path / "manifest.json").write_text(json.dumps(m))
    # Later flags override the fixture's: the fork shape, `main` refused.
    assert cli(tmp_path, "--default-branch", "meridian", "--refused-branches", "main", pr_head_ref="main") == 1
    assert "is on the land job's refused list (main)" in capsys.readouterr().out
    # An empty list (the flag's own default) leaves only the default-branch rule.
    assert cli(tmp_path, "--default-branch", "meridian", "--refused-branches", "", pr_head_ref="main") == 0


def test_cli_missing_manifest(tmp_path, capsys):
    assert cli(tmp_path) == 1
    assert "manifest.json is missing" in capsys.readouterr().out


def test_cli_invalid_json(tmp_path, capsys):
    (tmp_path / "manifest.json").write_text("{not json")
    assert cli(tmp_path) == 1
    assert "not valid UTF-8 JSON" in capsys.readouterr().out


def test_cli_symlinked_manifest(tmp_path, capsys):
    real = tmp_path / "real.json"
    real.write_text(json.dumps(base_manifest(tmp_path)))
    os.symlink(real, tmp_path / "manifest.json")
    assert cli(tmp_path) == 1
    assert "manifest.json is a symlink" in capsys.readouterr().out


def test_cli_oversized_manifest(tmp_path, capsys):
    (tmp_path / "manifest.json").write_bytes(b" " * (vm.MAX_MANIFEST_BYTES + 1))
    assert cli(tmp_path) == 1
    assert "the cap is" in capsys.readouterr().out
