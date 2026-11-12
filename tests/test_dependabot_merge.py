"""Tests for .github/scripts/dependabot_merge.py and its workflow,
.github/workflows/dependabot-merge.yml.

The rules before the policy, the policy itself, the required-check reading,
and the writes: none in a dry run, and in a live run an approval followed by
a merge or by auto-merge, each pinned to the head commit the job checked.
Every `gh` call goes through the module's `gh()`, so the tests stand in a fake
for it. Run with `python3 -m pytest` from the repo root.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "dependabot_merge.py"
WORKFLOW = ROOT / ".github" / "workflows" / "dependabot-merge.yml"

spec = importlib.util.spec_from_file_location("dependabot_merge", SCRIPT)
dm = importlib.util.module_from_spec(spec)
sys.modules["dependabot_merge"] = dm
spec.loader.exec_module(dm)

REPO = "meridianlabs-ai/inspect_scout"
SHA = "d21865bd30511382189b1431621db17f7e2bc0f2"
OLD_SHA = "0" * 40

# The head commit message of inspect_scout pr 822 (2026-11-12), trimmed.
GROUPED_MESSAGE = """build: bump the python-packages group with 3 updates

Bumps the python-packages group with 3 updates: pandas, pydantic and arize-phoenix-client.

---
updated-dependencies:
- dependency-name: pandas
  dependency-version: 3.1.1
  dependency-type: direct:production
  update-type: version-update:semver-patch
  dependency-group: python-packages
- dependency-name: pydantic
  dependency-version: 2.14.3
  dependency-type: direct:production
  update-type: version-update:semver-patch
  dependency-group: python-packages
- dependency-name: arize-phoenix-client
  dependency-version: 3.7.0
  dependency-type: direct:production
  update-type: version-update:semver-minor
  dependency-group: python-packages
...

Signed-off-by: dependabot[bot] <support@github.com>
"""


def message(*deps: tuple[str, str | None]) -> str:
    entries = "".join(
        f"- dependency-name: {name}\n  dependency-type: direct:production\n"
        + (f"  update-type: version-update:semver-{bump}\n" if bump else "")
        for name, bump in deps
    )
    return f"build: bump\n\n---\nupdated-dependencies:\n{entries}...\n\nSigned-off-by: dependabot[bot]\n"


def commit(sha=SHA, msg=GROUPED_MESSAGE):
    return {"sha": sha, "commit": {"message": msg}}


def push(after=SHA, actor=dm.DEPENDABOT, kind="push", before=OLD_SHA):
    return {"activity_type": kind, "actor": {"login": actor} if actor else None, "before": before, "after": after}


def skip(p, commits, reviews, policy, activity=None):
    """skip_reason, with the branch created by Dependabot at the PR's head
    unless `activity` says otherwise."""
    if activity is None:
        activity = [push(p["head"]["sha"], kind="branch_creation", before="0" * 40)]
    return dm.skip_reason(p, commits, activity, reviews, policy)


def pr(number=822, ref="dependabot/uv/python-packages-0a0afc6848", sha=SHA, labels=(), auto_merge=None):
    return {
        "number": number,
        "node_id": f"PR_node_{number}",
        "user": {"login": dm.DEPENDABOT},
        "head": {"sha": sha, "ref": ref},
        "labels": [{"name": n} for n in labels],
        "auto_merge": auto_merge,
        "base": {"ref": "main"},
    }


def review(user=dm.APP_BOT, state="APPROVED", commit_id=SHA):
    return {"user": {"login": user}, "state": state, "commit_id": commit_id}


POLICY = dm.load_policy((ROOT / ".github" / "dependabot-merge-policy.yml").read_text())


# --- the policy file -------------------------------------------------------


def test_policy_file_shape():
    assert POLICY.allow == {
        t: frozenset({"patch", "minor", "major"}) for t in ("uv", "pip", "npm_and_yarn")
    }
    assert POLICY.always_ask == frozenset()


@pytest.mark.parametrize(
    "text",
    [
        "allow: {uv: [patch]}\nalways_ask: []\nextra: 1\n",
        "allow: {uv: [patch]}\n",
        "allow: {uv: [semver-patch]}\nalways_ask: []\n",
        # A string would otherwise become a set of its letters.
        "allow: {uv: [patch]}\nalways_ask: requests\n",
        "allow: {uv: [patch]}\nalways_ask: {requests: 1}\n",
        "allow: {uv: patch}\nalways_ask: []\n",
        "allow: [uv]\nalways_ask: []\n",
        "- allow\n",
    ],
)
def test_policy_rejects_bad_shape(text):
    with pytest.raises(ValueError):
        dm.load_policy(text)


# --- the commit metadata ---------------------------------------------------


def test_updated_dependencies_of_a_grouped_commit():
    deps = dm.updated_dependencies(GROUPED_MESSAGE)
    assert [(d["dependency-name"], d["update-type"]) for d in deps] == [
        ("pandas", "version-update:semver-patch"),
        ("pydantic", "version-update:semver-patch"),
        ("arize-phoenix-client", "version-update:semver-minor"),
    ]


def test_updated_dependencies_needs_exactly_one_block():
    assert dm.updated_dependencies(GROUPED_MESSAGE + GROUPED_MESSAGE) is None


def test_updated_dependencies_absent():
    assert dm.updated_dependencies("Merge branch 'main' into dependabot/uv/x") is None


# --- the rules and the policy, per PR --------------------------------------


def test_allowed_grouped_pr_is_acted_on():
    assert skip(pr(), [commit()], [], POLICY) is None


def test_opt_out_label():
    reason = skip(pr(labels=["dependencies", "no-auto-merge"]), [commit()], [], POLICY)
    assert reason == "labeled no-auto-merge"


def test_dependabot_rebase_is_still_dependabot():
    activity = [push(SHA, kind="force_push"), push(OLD_SHA, kind="branch_creation", before="0" * 40)]
    assert skip(pr(), [commit()], [], POLICY, activity) is None


@pytest.mark.parametrize(
    "activity, pushers",
    [
        # inspect_scout pr 816 (2026-11-11): a person merged main into the branch.
        ([push(SHA, actor="epatey"), push(OLD_SHA, kind="branch_creation")], "dependabot[bot], epatey"),
        # A person's push earlier in the branch's life, later rebased away by Dependabot.
        ([push(SHA, kind="force_push"), push(OLD_SHA, actor="epatey")], "dependabot[bot], epatey"),
        ([push(SHA, actor=None)], "unknown"),
        # No record of the push that made the head: unknown pusher.
        ([push(OLD_SHA, kind="branch_creation")], "dependabot[bot]"),
        ([], "nobody"),
    ],
)
def test_branch_pushed_by_someone_else(activity, pushers):
    # The commits themselves look like Dependabot's; only the pushes tell.
    reason = skip(pr(), [commit()], [], POLICY, activity)
    assert reason == f"branch not pushed only by Dependabot (pushed by: {pushers})"


def test_head_moved():
    assert skip(pr(sha="1" * 40), [commit()], [], POLICY) == "head moved while the job read it"
    assert skip(pr(), [], [], POLICY) == "head moved while the job read it"


def test_auto_merge_on_without_approval_at_head_is_acted_on():
    # Dependabot rebased a PR the job approved; the rules dismissed the approval.
    assert skip(pr(auto_merge={"merge_method": "squash"}), [commit()], [], POLICY) is None


def test_auto_merge_on_and_approved_at_head():
    p = pr(auto_merge={"merge_method": "squash"})
    assert skip(p, [commit()], [review()], POLICY) == "already approved at this head"


def test_already_approved_at_head():
    assert skip(pr(), [commit()], [review()], POLICY) == "already approved at this head"


@pytest.mark.parametrize(
    "other",
    [
        review(commit_id=OLD_SHA),  # approved an earlier head: Dependabot rebased since
        review(state="DISMISSED"),
        review(user="epatey"),  # a person's approval is not the job's
    ],
)
def test_other_reviews_do_not_count_as_approved(other):
    assert skip(pr(), [commit()], [other], POLICY) is None


def test_github_actions_waits_for_a_person():
    reason = skip(
        pr(ref="dependabot/github_actions/github-actions-7477375a64"),
        [commit(msg=message(("pnpm/action-setup", "patch")))],
        [],
        POLICY,
    )
    assert reason == "pnpm/action-setup: patch bump not allowed for github_actions"


def test_major_allowed_for_npm():
    p = pr(ref="dependabot/npm_and_yarn/react-19.0.0")
    assert skip(p, [commit(msg=message(("react", "major")))], [], POLICY) is None


def test_bump_type_limited_by_policy():
    policy = dm.load_policy("allow: {uv: [patch, minor]}\nalways_ask: []\n")
    reason = skip(pr(), [commit(msg=message(("pandas", "major")))], [], policy)
    assert reason == "pandas: major bump not allowed for uv"


def test_grouped_pr_needs_every_package_allowed():
    policy = dm.load_policy("allow: {uv: [patch, minor, major]}\nalways_ask: [pydantic]\n")
    assert skip(pr(), [commit()], [], policy) == "pydantic is in always_ask"


def test_always_ask_names_compare_as_pip_does():
    policy = dm.load_policy("allow: {uv: [patch, minor, major]}\nalways_ask: [Arize_Phoenix.Client]\n")
    assert skip(pr(), [commit()], [], policy) == "arize-phoenix-client is in always_ask"


def test_unknown_dependency_type_segment():
    # A multi-ecosystem group puts its own name where the dependency type goes.
    p = pr(ref="dependabot/python-deps/python-deps-0a0afc6848")
    assert skip(p, [commit()], [], POLICY) == (
        "pandas: patch bump not allowed for python-deps; pydantic: patch bump not allowed for "
        "python-deps; arize-phoenix-client: minor bump not allowed for python-deps"
    )


def test_missing_update_type_is_not_allowed():
    # Dependabot omits update-type for a bump between commit SHAs.
    msg = message(("pandas", "patch"), ("some-git-dep", None))
    assert skip(pr(), [commit(msg=msg)], [], POLICY) == (
        "some-git-dep: unknown bump not allowed for uv"
    )


def test_commit_without_metadata():
    commits = [commit(sha=OLD_SHA), commit(msg="fix lockfile")]
    assert skip(pr(), commits, [], POLICY) == "a commit has no updated-dependencies block"


def test_branch_without_dependency_type():
    assert skip(pr(ref="dependabot-uv-pandas"), [commit()], [], POLICY) == (
        "no dependency type in branch dependabot-uv-pandas"
    )


# --- required checks -------------------------------------------------------


def checks_rule(*checks):
    return {
        "type": "required_status_checks",
        "parameters": {"required_status_checks": [dict(c) for c in checks]},
    }


def test_required_checks_from_every_ruleset():
    rules = [
        {"type": "deletion"},
        checks_rule({"context": "lint", "integration_id": 15368}),
        checks_rule({"context": "external-ci"}),
    ]
    assert dm.required_checks(rules) == [("lint", 15368), ("external-ci", None)]


def test_no_required_checks():
    assert dm.required_checks([{"type": "deletion"}, {"type": "pull_request", "parameters": {}}]) == []


def run(name, conclusion="success", status="completed", app_id=15368):
    return {"name": name, "status": status, "conclusion": conclusion, "app": {"id": app_id}}


@pytest.mark.parametrize("conclusion", ["success", "neutral", "skipped"])
def test_check_passed(conclusion):
    assert dm.check_passed("lint", 15368, [run("lint", conclusion)], [])


@pytest.mark.parametrize(
    "runs",
    [
        [],
        [run("lint", None, status="in_progress")],
        [run("lint", "failure")],
        [run("lint"), run("lint", "failure")],
        [run("lint", app_id=99)],  # same name from another app
    ],
)
def test_check_not_passed(runs):
    assert not dm.check_passed("lint", 15368, runs, [])


@pytest.mark.parametrize("state", ["pending", "failure"])
def test_check_run_and_status_of_one_name_must_both_pass(state):
    statuses = [{"context": "external-ci", "state": state}]
    assert not dm.check_passed("external-ci", None, [run("external-ci", app_id=1)], statuses)
    # Bound to an app, the status is not GitHub's to count.
    assert dm.check_passed("external-ci", 1, [run("external-ci", app_id=1)], statuses)


def test_status_context_counts_only_without_integration():
    statuses = [{"context": "external-ci", "state": "success"}]
    assert dm.check_passed("external-ci", None, [], statuses)
    assert not dm.check_passed("external-ci", 15368, [], statuses)
    assert not dm.check_passed("external-ci", None, [], [{"context": "external-ci", "state": "pending"}])


def test_checks_passed_needs_all():
    required = [("lint", 15368), ("test", 15368)]
    assert dm.checks_passed(required, [run("lint"), run("test")], [])
    assert not dm.checks_passed(required, [run("lint")], [])


# --- one repository, end to end against a fake gh -------------------------


class FakeGH:
    """Stand-in for dm.gh: answers reads by path, records every call."""

    def __init__(self, reads):
        self.reads = reads
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if "-X" in args or args[:2] == ("api", "graphql"):
            return "{}"
        path = args[-1]
        for pattern, value in self.reads.items():
            if re.fullmatch(pattern, path):
                return json.dumps(value() if callable(value) else value)
        raise AssertionError(f"unrouted gh call: {args}")

    @property
    def writes(self):
        return [c for c in self.calls if "-X" in c or c[:2] == ("api", "graphql")]


def reads(prs, checks=(run("lint"),), labels=("no-auto-merge",), rules=None):
    return {
        f"repos/{REPO}": {"default_branch": "main", "allow_auto_merge": True, "allow_squash_merge": True},
        rf"repos/{REPO}/rules/branches/main\?.*": [
            rules
            if rules is not None
            else [approval_rule(), checks_rule({"context": "lint", "integration_id": 15368})]
        ],
        rf"repos/{REPO}/labels\?.*": [[{"name": n} for n in labels]],
        rf"repos/{REPO}/pulls\?state=open&base=main.*": [prs],
        **{f"repos/{REPO}/pulls/{p['number']}": p for p in prs},
        rf"repos/{REPO}/pulls/\d+/commits\?.*": [[commit()]],
        rf"repos/{REPO}/pulls/\d+/reviews\?.*": [[]],
        rf"repos/{REPO}/activity\?ref=refs/heads/dependabot/uv/.*": [[push(SHA)]],
        rf"repos/{REPO}/commits/{SHA}/check-runs\?.*": [{"check_runs": list(checks)}],
        rf"repos/{REPO}/commits/{SHA}/status\?per_page=100": [{"statuses": []}],
    }


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        f = FakeGH(reads(**kw))
        monkeypatch.setattr(dm, "gh", f)
        monkeypatch.setattr(dm, "log_lines", [])
        return f

    return install


def test_dry_run_writes_nothing(fake):
    f = fake(prs=[pr()], labels=())
    assert dm.handle_repo(REPO, POLICY, live=False)
    assert f.writes == []
    assert dm.log_lines[-3:] == [
        f"{REPO}#822 act: required checks passed",
        f"  would approve {REPO}#822 at {SHA[:10]}",
        f"  would squash-merge {REPO}#822 at {SHA[:10]}",
    ]
    assert f"  would create label no-auto-merge in {REPO}" in dm.log_lines


def test_dry_run_is_the_default(fake, monkeypatch):
    f = fake(prs=[pr()], labels=())
    monkeypatch.setattr(dm, "installation_repos", lambda: [REPO])
    assert dm.main([]) == 0
    assert f.writes == []
    assert dm.log_lines[0] == "mode: dry run (no writes)"


def test_live_approves_then_merges_at_head(fake):
    f = fake(prs=[pr()])
    assert dm.handle_repo(REPO, POLICY, live=True)
    approve, merge = f.writes
    assert approve[:4] == ("api", "-X", "POST", f"repos/{REPO}/pulls/822/reviews")
    assert {"commit_id=" + SHA, "event=APPROVE"} <= set(approve)
    assert merge[:4] == ("api", "-X", "PUT", f"repos/{REPO}/pulls/822/merge")
    assert {"sha=" + SHA, "merge_method=squash"} <= set(merge)


def test_live_pending_checks_enable_auto_merge_at_head(fake):
    f = fake(prs=[pr()], checks=[run("lint", None, status="queued")])
    assert dm.handle_repo(REPO, POLICY, live=True)
    approve, auto = f.writes
    assert approve[3] == f"repos/{REPO}/pulls/822/reviews"
    assert auto[:2] == ("api", "graphql")
    assert "enablePullRequestAutoMerge" in auto[3] and "expectedHeadOid:$sha" in auto[3]
    assert {"pr=PR_node_822", "sha=" + SHA} <= set(auto)


def test_auto_merge_on_gets_approval_only(fake):
    f = fake(prs=[pr(auto_merge={"merge_method": "squash"})])
    assert dm.handle_repo(REPO, POLICY, live=True)
    (approve,) = f.writes
    assert approve[3] == f"repos/{REPO}/pulls/822/reviews" and "commit_id=" + SHA in approve
    assert dm.log_lines[-2] == f"{REPO}#822 act: auto-merge already on, approval only"


def test_failed_check_still_gets_auto_merge(fake):
    # CI failure: the job does nothing special; the PR waits with auto-merge on.
    f = fake(prs=[pr()], checks=[run("lint", "failure")])
    assert dm.handle_repo(REPO, POLICY, live=True)
    assert [w[1] for w in f.writes] == ["-X", "graphql"]


def test_base_changed_after_listing(fake):
    f = fake(prs=[pr()])
    f.reads[f"repos/{REPO}/pulls/822"] = pr() | {"base": {"ref": "release"}}
    assert dm.handle_repo(REPO, POLICY, live=True)
    assert f.writes == []
    assert dm.log_lines[-1] == f"{REPO}#822 skip: base changed to release"


@pytest.mark.parametrize("auto_merge", [None, {"merge_method": "squash"}])
@pytest.mark.parametrize(
    "change", [{"base": {"ref": "release"}}, {"head": {"sha": "f" * 40, "ref": pr()["head"]["ref"]}}]
)
def test_base_or_head_changed_while_job_read_pr(fake, auto_merge, change):
    # A writer retargets or pushes after the job's last read before the writes.
    f = fake(prs=[pr(auto_merge=auto_merge)])
    last_read = "/activity?" if auto_merge else "/status?"
    f.reads[f"repos/{REPO}/pulls/822"] = lambda: pr(auto_merge=auto_merge) | (
        change if any(last_read in c[-1] for c in f.calls) else {}
    )
    assert dm.handle_repo(REPO, POLICY, live=True)
    assert f.writes == []
    assert dm.log_lines[-1] == f"{REPO}#822 skip: base or head changed while the job read it"


def test_label_added_after_listing_counts(fake):
    f = fake(prs=[pr()])
    f.reads[f"repos/{REPO}/pulls/822"] = pr(labels=["no-auto-merge"])
    assert dm.handle_repo(REPO, POLICY, live=True)
    assert f.writes == []
    assert dm.log_lines[-1] == f"{REPO}#822 skip: labeled no-auto-merge"


def test_live_skipped_pr_gets_no_write(fake):
    f = fake(prs=[pr(labels=["no-auto-merge"])])
    assert dm.handle_repo(REPO, POLICY, live=True)
    assert f.writes == []


def test_live_creates_missing_label(fake):
    f = fake(prs=[], labels=("dependencies",))
    assert dm.handle_repo(REPO, POLICY, live=True)
    (create,) = f.writes
    assert create[:4] == ("api", "-X", "POST", f"repos/{REPO}/labels")
    assert "name=no-auto-merge" in create


def test_repo_without_required_checks_is_skipped(fake):
    f = fake(prs=[pr()], rules=[{"type": "deletion"}])
    assert dm.handle_repo(REPO, POLICY, live=True)
    assert f.writes == []
    assert not any("pulls" in c[-1] or "labels" in c[-1] for c in f.calls)
    assert dm.log_lines == [f"{REPO} skip: no required status checks on main"]


def test_prs_from_others_are_ignored(fake):
    person = pr(number=900) | {"user": {"login": "epatey"}}
    f = fake(prs=[person])
    assert dm.handle_repo(REPO, POLICY, live=True)
    assert f.writes == []
    assert not any("/pulls/900/" in c[-1] for c in f.calls)


def test_one_failed_pr_does_not_stop_the_others(fake):
    f = fake(prs=[pr(number=1), pr(number=2)])
    original = f.__call__

    def failing(*args):
        if args[:4] == ("api", "-X", "POST", f"repos/{REPO}/pulls/1/reviews"):
            raise RuntimeError("422")
        return original(*args)

    dm.gh = failing
    assert not dm.handle_repo(REPO, POLICY, live=True)
    assert any(c[-1] == f"repos/{REPO}/pulls/2/reviews?per_page=100" for c in f.calls)
    assert f"::error::{REPO}#1: 422" in dm.log_lines


# --- the workflow ----------------------------------------------------------

WORKFLOW_TEXT = WORKFLOW.read_text()


def test_workflow_runs_dry():
    assert "--live" not in WORKFLOW_TEXT.split("name: Merge", 1)[1]
    assert re.search(r"^\s+run: uv run --no-sync python3 \.github/scripts/dependabot_merge\.py$", WORKFLOW_TEXT, re.M)


def test_workflow_token_is_read_only_while_dry():
    perms = re.findall(r"^\s+permission-[a-z-]+: (\w+)$", WORKFLOW_TEXT, re.M)
    assert perms and set(perms) == {"read"}


def test_workflow_key_only_in_main_only_environment():
    assert re.search(r"^    environment: dependabot-merger$", WORKFLOW_TEXT, re.M)
    assert WORKFLOW_TEXT.count("secrets.") == 1
    assert "secrets.DEPENDABOT_MERGER_PRIVATE_KEY" in WORKFLOW_TEXT
    assert "vars.DEPENDABOT_MERGER_APP_ID" in WORKFLOW_TEXT


def test_workflow_hourly_and_pinned():
    assert re.search(r'cron: "\d+ \* \* \* \*"', WORKFLOW_TEXT)
    uses = re.findall(r"uses: (\S+)", WORKFLOW_TEXT)
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u) for u in uses)
    assert "persist-credentials: false" in WORKFLOW_TEXT


# --- whole-repository skips ------------------------------------------------

CHECKS = checks_rule({"context": "lint", "integration_id": 15368})
INFO = {"default_branch": "main", "allow_auto_merge": True, "allow_squash_merge": True}


def approval_rule(dismiss=True, last_push=False, count=1, methods=("squash",)):
    return {
        "type": "pull_request",
        "parameters": {
            "required_approving_review_count": count,
            "dismiss_stale_reviews_on_push": dismiss,
            "require_last_push_approval": last_push,
            "allowed_merge_methods": list(methods),
        },
    }


# inspect_scout's rules on 2026-11-12, trimmed: a 1-approval rule that
# dismisses stale approvals, and a code-owner rule that requires none.
SCOUT_RULES = [
    approval_rule(methods=("merge", "squash", "rebase")),
    approval_rule(dismiss=False, count=0),
    CHECKS,
]


def test_inspect_scout_rules_pass():
    assert dm.repo_skip_reason(INFO, SCOUT_RULES, dm.required_checks(SCOUT_RULES)) is None
    last_push = [approval_rule(dismiss=False, last_push=True), CHECKS]
    assert dm.repo_skip_reason(INFO, last_push, dm.required_checks(last_push)) is None


@pytest.mark.parametrize(
    "info, rules, reason",
    [
        (INFO, [approval_rule()], "no required status checks on main"),
        # The approval may live in classic branch protection, which the rules omit.
        (INFO, [CHECKS], "no ruleset requires an approval"),
        (INFO, [approval_rule(dismiss=False, count=0), CHECKS], "no ruleset requires an approval"),
        (
            INFO,
            [approval_rule(), approval_rule(dismiss=False), CHECKS],
            "an approval rule neither dismisses stale approvals nor requires approval of the last push",
        ),
        (INFO | {"allow_auto_merge": False}, SCOUT_RULES, "Allow auto-merge is off"),
        (INFO | {"allow_squash_merge": False}, SCOUT_RULES, "squash merging is not allowed"),
        (INFO, [approval_rule(methods=("merge",)), CHECKS], "squash merging is not allowed"),
    ],
)
def test_repo_skipped(info, rules, reason):
    assert dm.repo_skip_reason(info, rules, dm.required_checks(rules)) == reason


def test_unreadable_settings_are_logged_not_skipped(fake):
    # A read-only token may not see the allow_* fields.
    f = fake(prs=[])
    f.reads[f"repos/{REPO}"] = {"default_branch": "main"}
    assert dm.handle_repo(REPO, POLICY, live=False)
    assert f"{REPO}: this token cannot read Allow auto-merge or squash merging; not checked" in dm.log_lines


def test_skipped_repo_gets_no_write(fake):
    f = fake(prs=[pr()], rules=[approval_rule(dismiss=False), CHECKS], labels=())
    assert dm.handle_repo(REPO, POLICY, live=True)
    assert f.writes == []
