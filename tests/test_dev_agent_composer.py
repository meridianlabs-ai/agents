"""Tests for claude.yml's `Compose landing manifest` step (issue #84).

The dev agent's landing manifest is where the old post-agent steps live on
now — the PR open for an issue run, the `@review` hand-back on an @auto PR
run, the stage move, the codex summary and thread ids, and the one field
the agent itself may write (`comments`) — so its bash is lifted out of the
workflow and run here against a scratch repo, one case per rule
(design/architecture.md → Landing job, the #84 notes). The issue-run case
also runs the result through emit-landing and the validator with the land
job's `branch-prefix`, the way an issue run is validated.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "claude.yml"
VALIDATOR = ROOT / ".github" / "scripts" / "validate_manifest.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_helpers import WORKFLOW_FILES_RULE, run_emit_landing, sh, git, step_block, job_block  # noqa: E402


def composer_script(engine: str = "claude") -> str:
    """The step's bash, lifted from the engine's job of the workflow (text
    extraction: PyYAML is not a test dependency). The block scalar under
    `run: |` is every following line indented by at least its 10 spaces, up
    to the first that is not. Each engine has its own job and so its own
    composer since 2026-09-22: `agent` (Claude) and `agent-codex`."""
    lines = job_block(WORKFLOW.read_text(), "agent-codex" if engine == "codex" else "agent").splitlines()
    start = lines.index("        id: landing")
    run_at = next(i for i in range(start, len(lines)) if lines[i] == "        run: |")
    body = []
    for line in lines[run_at + 1:]:
        if line.strip() == "":
            body.append("")
        elif line.startswith("          "):
            body.append(line[10:])
        else:
            break
    return "\n".join(body) + "\n"


@pytest.fixture
def repo(tmp_path):
    work = tmp_path / "work"
    git("init", "-q", str(work), cwd=tmp_path)
    git("config", "user.email", "a@b", cwd=work)
    git("config", "user.name", "a", cwd=work)
    (work / "f").write_text("1\n")
    git("add", "f", cwd=work)
    git("commit", "-qm", "base", cwd=work)
    start = git("rev-parse", "HEAD", cwd=work).stdout.strip()
    return {"work": work, "start": start, "tmp": tmp_path}


def on(r, branch):
    """Put HEAD on the run's branch — the evidence the composer requires
    before it bundles anything (claude-code-action's setupBranch, the codex
    prep step or sync-branch check it out; the initial checkout is on the
    event's default ref, which is not agent work)."""
    git("checkout", "-qB", branch, cwd=r["work"])


ISSUE_BRANCH = "claude/issue-12-20260911"
CODEX_BRANCH = "claude/issue-12-codex-7"
PR_BRANCH = "feature"


def commit(r, subject="feat: add the thing", body="Because the issue asked for it.\n\nCo-Authored-By: X <x@y>"):
    (r["work"] / "f").write_text("2\n")
    git("commit", "-qam", subject, "-m", body, cwd=r["work"])


def compose(r, *, is_pr, engine="claude", trigger="@claude", auto="false", agent_extra=None,
            claude_outcome="success", codex_commit="skipped", codex_guard=None, codex_ids="",
            codex_summary=None, final_message="Here is the answer.", error=None, req_review="false",
            base="", pr_labels=None, claude_branch=ISSUE_BRANCH, head_branch=PR_BRANCH,
            merge_sha="", prov_note="", checkout_sha=None, sync_branch=None):
    # PR_LABELS as the gate composes it: `auto` exactly when the run is
    # autonomous (an @auto trigger or an `auto`-labelled item), unless a test
    # says otherwise.
    if pr_labels is None:
        pr_labels = '["auto"]' if auto == "true" else '[]'
    landing = r["tmp"] / "landing"
    landing.mkdir(exist_ok=True)
    if agent_extra is not None:
        (landing / "manifest-extra.json").write_text(agent_extra)
    if codex_summary is not None:
        (landing / "codex-comment.md").write_text(codex_summary)
    exec_file = r["tmp"] / "exec.json"
    exec_file.write_text(json.dumps([{"type": "result", "result": final_message}]))
    error_file = r["tmp"] / "agent-error.md"
    if error is not None:
        error_file.write_text(error)
    extra = r["tmp"] / "landing-extra.json"
    output = r["tmp"] / "out.txt"
    output.write_text("")
    if codex_guard is None:
        codex_guard = "success" if engine == "codex" else "skipped"
    env = {
        "DIR": str(landing), "EXTRA": str(extra),
        "NUM": "34" if is_pr else "12", "IS_PR": "true" if is_pr else "false",
        "ISSUE": "" if is_pr else "12",
        "ENGINE": engine, "TRIGGER_PHRASE": trigger, "AUTO": auto, "PR_LABELS": pr_labels,
        "HEAD_BRANCH": head_branch if is_pr else "", "REQ_REVIEW": req_review, "BASE": base,
        # Where checkout left HEAD (github.sha): the scratch repo's first
        # commit unless a test says otherwise. SYNC_BRANCH: sync-branch
        # checked the PR head out (open PRs) unless a test says otherwise.
        "CHECKOUT_SHA": r["start"] if checkout_sha is None else checkout_sha,
        "SYNC_BRANCH": (head_branch if sync_branch is None else sync_branch) if is_pr else "",
        "START_SHA": r["start"], "MERGE_SHA": merge_sha,
        "CLAUDE_BRANCH": claude_branch if (engine == "claude" and not is_pr) else "",
        "CODEX_BRANCH": CODEX_BRANCH if (engine == "codex" and not is_pr) else "",
        "EXEC": str(exec_file),
        "CLAUDE_OUTCOME": claude_outcome if engine == "claude" else "skipped",
        "CODEXGUARD_OUTCOME": codex_guard, "CODEXCOMMIT_OUTCOME": codex_commit, "CODEX_IDS": codex_ids,
        "PROV_NOTE": prov_note, "ERROR_FILE": str(error_file),
        "RUN_URL": "https://github.com/meridianlabs-ai/agents/actions/runs/123",
        "GITHUB_RUN_ID": "123", "GITHUB_OUTPUT": str(output),
        "GIT_DIR": str(r["work"] / ".git"), "GIT_COMMON_DIR": str(r["work"] / ".git"),
        "GIT_WORK_TREE": str(r["work"]),
    }
    res = sh("bash", "-c", composer_script(engine), cwd=r["work"], check=False, env=env)
    assert res.returncode == 0, res.stderr
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
    return json.loads(extra.read_text()), res, landing, outputs


def validate(landing, *args):
    return sh(sys.executable, str(VALIDATOR), "--dir", str(landing), "--repo", "meridianlabs-ai/agents",
              "--run-id", "123", "--default-branch", "main", *args, check=False)


# --- issue runs: the PR is the land job's -------------------------------------


def test_issue_run_with_commit_opens_pr_and_validates(repo):
    on(repo, ISSUE_BRANCH)
    commit(repo)
    m, _, landing, out = compose(repo, is_pr=False, base="main")
    assert out["branch"] == ISSUE_BRANCH and out["read_only"] == "false"
    assert m["pr"] == {"open": True, "title": "feat: add the thing", "body_file": "pr-body.md",
                       "labels": [], "issue": 12, "base": "main"}
    body = (landing / "pr-body.md").read_text()
    assert body.startswith("Fixes #12\n\nBecause the issue asked for it.")
    assert "Co-Authored-By: X" in body and "[this run](https://github.com/meridianlabs-ai/agents/actions/runs/123)" in body
    assert "handback" not in m                      # @claude, no `auto` label: reviews stay on demand
    assert m["stage"] == "Review"                   # one-shot: hand back to a human
    assert "comments" not in m and "error" not in m
    # Through emit-landing and the validator, as an issue run (no PR number,
    # the land job's branch-prefix pin).
    extra = repo["tmp"] / "landing-extra.json"
    res, landing, output = run_emit_landing(repo["tmp"], cwd=repo["work"], read_only=False, start_sha=repo["start"],
                                            extra=extra, branch=out["branch"], pr_number="", issue_number="12")
    assert res.returncode == 0, res.stderr
    assert "wrote=true" in output
    manifest = json.loads((landing / "manifest.json").read_text())
    assert manifest["has_bundle"] is True and manifest["pr"]["open"] is True
    v = validate(landing, "--event-pr-number", "", "--event-issue-number", "12", "--branch-prefix", "claude/issue-12-")
    assert v.returncode == 0, v.stdout


def test_issue_run_at_auto_stays_at_agent_and_can_request_review(repo):
    on(repo, ISSUE_BRANCH)
    commit(repo)
    m, res, _, _ = compose(repo, is_pr=False, trigger="@auto", auto="true", req_review="true",
                           pr_labels='["auto","engine:codex"]')
    assert m["pr"]["labels"] == ["auto", "engine:codex"]
    assert m["handback"] is True                    # the `auto` label (and request_review_after_open)
    assert "stage" not in m                         # the loop owns the stage from here
    assert "stage stays at Agent" in res.stdout


def test_auto_kickoff_opens_pr_with_handback_and_validates(repo):
    # The `auto` label on an issue (or an @auto mention) with the caller's
    # request_review_after_open off, as every Meridian stub has it: the PR
    # the land job opens carries `auto`, and that same label array owes the
    # loop's first `@review` — auto-review-on-open is off everywhere
    # (2026-09-16), so this hand-back is where the loop starts (inspect_ai#497
    # → PR #507, run 35269648600, landed with HANDBACK_PLANNED false and was
    # never reviewed). Through emit-landing and the validator as an issue run.
    on(repo, ISSUE_BRANCH)
    commit(repo)
    m, res, _, out = compose(repo, is_pr=False, trigger="@auto", auto="true", base="main")
    assert m["pr"]["open"] is True and m["pr"]["labels"] == ["auto"]
    assert m["handback"] is True
    assert "stage" not in m                         # the loop owns the stage from here
    assert "stage stays at Agent" in res.stdout
    extra = repo["tmp"] / "landing-extra.json"
    res, landing, output = run_emit_landing(repo["tmp"], cwd=repo["work"], read_only=False, start_sha=repo["start"],
                                            extra=extra, branch=out["branch"], pr_number="", issue_number="12")
    assert res.returncode == 0, res.stderr
    assert "wrote=true" in output
    manifest = json.loads((landing / "manifest.json").read_text())
    assert manifest["has_bundle"] is True and manifest["handback"] is True and manifest["pr"]["labels"] == ["auto"]
    v = validate(landing, "--event-pr-number", "", "--event-issue-number", "12", "--branch-prefix", "claude/issue-12-")
    assert v.returncode == 0, v.stdout


def test_pr_labels_the_gate_did_not_read_are_refused_by_the_land_job(repo):
    # Finding 4628441: the composer copies the gate's read into `pr.labels`,
    # but it runs in the agent job after the agent, and emit-landing writes
    # manifest.json there too. The land job passes the gate's read as the
    # validator's allow-list, so the manifest the composer wrote passes and
    # one grown afterwards — `auto` to arm the loop, an engine switch — is
    # refused whole, before anything is pushed, labelled or handed back.
    on(repo, ISSUE_BRANCH)
    commit(repo)
    gate_read = '["engine:codex"]'
    m, _, _, out = compose(repo, is_pr=False, trigger="@claude", auto="false", pr_labels=gate_read)
    assert m["pr"]["labels"] == ["engine:codex"] and "handback" not in m
    extra = repo["tmp"] / "landing-extra.json"
    res, landing, _ = run_emit_landing(repo["tmp"], cwd=repo["work"], read_only=False, start_sha=repo["start"],
                                       extra=extra, branch=out["branch"], pr_number="", issue_number="12")
    assert res.returncode == 0, res.stderr
    pin = ("--event-pr-number", "", "--event-issue-number", "12", "--branch-prefix", "claude/issue-12-",
           "--allowed-pr-labels", gate_read)
    assert validate(landing, *pin).returncode == 0
    manifest = json.loads((landing / "manifest.json").read_text())
    for grown in (["engine:codex", "auto"], ["auto"], ["engine:other"]):
        manifest["pr"]["labels"] = grown
        (landing / "manifest.json").write_text(json.dumps(manifest))
        v = validate(landing, *pin)
        assert v.returncode == 1 and "is not in the allowed pull-request labels (engine:codex)" in v.stdout, v.stdout
    # A gate read that failed (an empty allow-list) lands the composer's
    # empty label list and nothing else.
    manifest["pr"]["labels"] = []
    (landing / "manifest.json").write_text(json.dumps(manifest))
    assert validate(landing, *pin[:-1], "").returncode == 0
    manifest["pr"]["labels"] = ["auto"]
    (landing / "manifest.json").write_text(json.dumps(manifest))
    assert validate(landing, *pin[:-1], "").returncode == 1


def test_claude_trigger_on_an_auto_labelled_issue_owes_the_handback(repo):
    # `@claude` (or the `claude` label) on an issue that carries `auto`: the
    # gate reads the label, the PR gets it, so the PR is the loop's and owes
    # the `@review` — and, the hand-back being mid-flight, no stage, whatever
    # the trigger phrase (review round 3: keyed on the @auto phrase, this run
    # set Review alongside its hand-back).
    on(repo, ISSUE_BRANCH)
    commit(repo)
    m, res, _, _ = compose(repo, is_pr=False, trigger="@claude", auto="true")
    assert m["pr"]["labels"] == ["auto"] and m["handback"] is True
    assert "stage" not in m and "stage stays at Agent" in res.stdout


def test_handback_follows_the_label_array_not_the_auto_flag(repo):
    # The hand-back keys on the labels `land` applies: a PR_LABELS value that
    # fails the composer's shape check lands no label, so it owes no `@review`
    # either — the two cannot disagree.
    on(repo, ISSUE_BRANCH)
    commit(repo)
    m, _, _, _ = compose(repo, is_pr=False, trigger="@auto", auto="true", pr_labels="not json")
    assert m["pr"]["labels"] == [] and "handback" not in m


def test_request_review_after_open_still_requests_without_the_label(repo):
    # The input's remaining role: a hand-back for a PR outside the loop —
    # unchanged, so an errored run still gets it as before.
    on(repo, ISSUE_BRANCH)
    commit(repo)
    m, _, _, _ = compose(repo, is_pr=False, req_review="true")
    assert m["pr"]["labels"] == [] and m["handback"] is True
    m, _, _, _ = compose(repo, is_pr=False, req_review="true", claude_outcome="failure", error="⚠️ it broke\n")
    assert m["pr"]["labels"] == [] and m["handback"] is True and m["stage"] == "Review"


def test_request_review_after_open_does_not_bypass_the_auto_error_rule(repo):
    # An `auto`-labelled PR is the loop's whatever the caller's input says:
    # an errored run owes it no `@review` (review round 1 caught the input
    # slipping past the error check).
    on(repo, ISSUE_BRANCH)
    commit(repo)
    m, _, _, _ = compose(repo, is_pr=False, trigger="@auto", auto="true", req_review="true",
                         claude_outcome="failure", error="⚠️ it broke\n")
    assert m["pr"]["labels"] == ["auto"] and "handback" not in m and m["stage"] == "Review"


def test_auto_kickoff_read_only_landing_owes_no_handback(repo):
    # The fork's early-failure shape under an `auto` kickoff: the agent never
    # initialized its branch, nothing is bundled, no PR — and no `@review`
    # (through emit-landing: read-only, so even a handback set by mistake
    # would be dropped with the bundle).
    on(repo, "meridian")
    commit(repo, subject="someone else's merged work", body="")
    meridian = git("rev-parse", "HEAD", cwd=repo["work"]).stdout.strip()
    m, _, _, out = compose(repo, is_pr=False, trigger="@auto", auto="true", claude_branch="",
                           claude_outcome="skipped", error="⚠️ provisioning failed\n", checkout_sha=meridian)
    assert out["read_only"] == "true" and "pr" not in m and "handback" not in m
    assert m["stage"] == "Review"
    extra = repo["tmp"] / "landing-extra.json"
    res, landing, output = run_emit_landing(repo["tmp"], cwd=repo["work"], read_only=True, start_sha=repo["start"],
                                            extra=extra, branch=out["branch"], pr_number="", issue_number="12")
    assert res.returncode == 0 and "wrote=true" in output
    manifest = json.loads((landing / "manifest.json").read_text())
    assert manifest["has_bundle"] is False and "handback" not in manifest and "pr" not in manifest


def test_auto_kickoff_that_errored_opens_the_pr_but_owes_no_handback(repo):
    # The agent committed, then its step failed (the Surface step wrote the
    # error): the work lands and the PR opens labelled `auto`, but it goes to
    # a human (stage Review, the ⚠️ posted by `land`), not to the loop.
    on(repo, ISSUE_BRANCH)
    commit(repo)
    m, _, _, _ = compose(repo, is_pr=False, trigger="@auto", auto="true", claude_outcome="failure",
                         error="⚠️ it broke\n")
    assert m["pr"]["open"] is True and m["pr"]["labels"] == ["auto"]
    assert "handback" not in m
    assert m["stage"] == "Review" and m["error"]["fail_run"] is True


def test_issue_run_with_no_commit_opens_nothing_and_relays_the_answer(repo):
    on(repo, ISSUE_BRANCH)
    m, _, landing, out = compose(repo, is_pr=False,
                                 final_message="It already works; see @review's note. engine: codex")
    assert "pr" not in m and "handback" not in m
    assert m["stage"] == "Review"
    assert m["comments"] == [{"number": 12, "body_file": "agent-summary.md"}]
    body = (landing / "agent-summary.md").read_text()
    assert body.startswith("🤖 claude (dev agent): no code changes were made — the agent's summary:")
    assert "`review`'s note" in body and "engine  codex" in body and "@review" not in body
    assert out["branch"] == ISSUE_BRANCH and out["read_only"] == "false"


def test_issue_run_reads_the_local_branch_when_the_action_output_is_lost(repo):
    on(repo, "claude/issue-12-20260911-1200")
    commit(repo)
    m, _, _, out = compose(repo, is_pr=False, claude_branch="", claude_outcome="failure")
    assert out["branch"] == "claude/issue-12-20260911-1200" and out["read_only"] == "false"
    assert m["pr"]["open"] is True                  # committed work lands even when the step failed
    assert m["stage"] == "Review"


def test_early_failure_on_an_alternate_base_bundles_nothing(repo):
    # The fork's shape: default branch `meridian` is AHEAD of base_branch
    # `main`, the checkout landed on `meridian`, and provisioning or the
    # action step failed before setupBranch ran — no branch output, no local
    # issue branch. `main..meridian` must not read as agent work (review
    # round 1 of #84): no PR, nothing bundled, a placeholder branch nothing
    # lands on, and emit-landing told to run read-only.
    on(repo, "meridian")
    commit(repo, subject="someone else's merged work", body="")
    meridian = git("rev-parse", "HEAD", cwd=repo["work"]).stdout.strip()
    m, res, _, out = compose(repo, is_pr=False, claude_branch="", claude_outcome="skipped",
                             error="⚠️ provisioning failed\n", checkout_sha=meridian)
    assert out["branch"] == "claude/issue-12-run-123" and out["read_only"] == "true"
    assert "pr" not in m and "handback" not in m and "comments" not in m
    assert m["error"] == {"message": "⚠️ provisioning failed\n", "fail_run": True}   # the Surface error alone
    assert m["stage"] == "Review"
    assert "never initialized it" in res.stdout


def test_commits_off_the_run_branch_are_not_bundled(repo):
    # The action reported its branch, but it does not exist locally and HEAD
    # is elsewhere: the action created the branch (its branch_name output
    # names it) and the agent removed it — whatever sits on HEAD is not the
    # run's work, the branch the action named must not receive foreign
    # history, and the run is an error, not a quiet no-change.
    on(repo, "somewhere-else")
    commit(repo)
    m, res, _, out = compose(repo, is_pr=False)
    assert out["branch"] == ISSUE_BRANCH and out["read_only"] == "true"
    assert "pr" not in m and "comments" not in m and m["stage"] == "Review"
    assert m["error"]["fail_run"] is True and "no longer exists locally" in m["error"]["message"]
    assert "::error::landing: the run's branch" in res.stdout


def test_renamed_branch_after_a_successful_run_is_an_error(repo):
    # commit → `git branch -m` to a descriptive name → the agent finishes
    # green. The action's branch_name output keeps the original name, so the
    # named branch is gone: refused with an error, no relay, nothing bundled
    # — through emit-landing (read-only) and the validator (review round 3
    # of #84 reproduced a green "no code changes" run here).
    on(repo, ISSUE_BRANCH)
    commit(repo)
    git("branch", "-m", "claude/issue-12-descriptive-name", cwd=repo["work"])
    m, _, landing, out = compose(repo, is_pr=False, final_message="Done and dusted.")
    assert out["read_only"] == "true" and "pr" not in m and "comments" not in m
    assert m["error"]["fail_run"] is True and "renamed or deleted" in m["error"]["message"]
    assert not (landing / "agent-summary.md").exists()
    extra = repo["tmp"] / "landing-extra.json"
    res, landing, output = run_emit_landing(repo["tmp"], cwd=repo["work"], read_only=True, start_sha=repo["start"],
                                            extra=extra, branch=out["branch"], pr_number="", issue_number="12")
    assert res.returncode == 0 and "wrote=true" in output
    manifest = json.loads((landing / "manifest.json").read_text())
    assert manifest["has_bundle"] is False and manifest["error"]["fail_run"] is True and "pr" not in manifest
    v = validate(landing, "--event-pr-number", "", "--event-issue-number", "12", "--branch-prefix", "claude/issue-12-")
    assert v.returncode == 0, v.stdout


def test_work_off_an_unnamed_branch_is_an_error(repo):
    # No branch was ever named (the action step died before its outputs) but
    # HEAD is no longer where the checkout left it: the agent worked
    # somewhere, and that is not the fence.
    on(repo, "somewhere-else")
    commit(repo)
    m, _, _, out = compose(repo, is_pr=False, claude_branch="", claude_outcome="failure")
    assert out["read_only"] == "true" and "pr" not in m
    assert m["error"]["fail_run"] is True and "away from the checkout" in m["error"]["message"]


def test_detached_head_at_the_run_branch_tip_still_lands(repo):
    # `git checkout --detach` after the commit leaves the work at the
    # branch's tip; a name-only test dropped it with a green run (review
    # round 2 of #84). Through emit-landing and the validator too.
    on(repo, ISSUE_BRANCH)
    commit(repo)
    git("checkout", "-q", "--detach", "HEAD", cwd=repo["work"])
    m, _, _, out = compose(repo, is_pr=False)
    assert out["read_only"] == "false" and m["pr"]["open"] is True and "error" not in m
    extra = repo["tmp"] / "landing-extra.json"
    res, landing, output = run_emit_landing(repo["tmp"], cwd=repo["work"], read_only=False, start_sha=repo["start"],
                                            extra=extra, branch=out["branch"], pr_number="", issue_number="12")
    assert res.returncode == 0 and "wrote=true" in output
    manifest = json.loads((landing / "manifest.json").read_text())
    assert manifest["has_bundle"] is True
    v = validate(landing, "--event-pr-number", "", "--event-issue-number", "12", "--branch-prefix", "claude/issue-12-")
    assert v.returncode == 0, v.stdout


def test_leaving_the_run_branch_is_an_error_not_a_silent_no_change(repo):
    # The branch was initialized and committed on, then HEAD moved to another
    # branch: emit-landing bundles START..HEAD, so that work cannot be
    # packaged. Refused loudly — an error that fails the run, and no relay
    # claiming "no code changes were made".
    on(repo, ISSUE_BRANCH)
    commit(repo)
    on(repo, "scratch")
    (repo["work"] / "g").write_text("scratch\n")
    git("add", "g", cwd=repo["work"])
    git("commit", "-qm", "scratch work", cwd=repo["work"])
    m, res, landing, out = compose(repo, is_pr=False, final_message="All done, pushed it myself.")
    assert out["read_only"] == "true"
    assert "pr" not in m and "comments" not in m and m["stage"] == "Review"
    assert m["error"]["fail_run"] is True
    assert "left its branch" in m["error"]["message"] and "NOT landed" in m["error"]["message"]
    assert "::error::landing: HEAD ended the run" in res.stdout
    assert not (landing / "agent-summary.md").exists()


def test_leaving_the_run_branch_appends_to_the_surface_error(repo):
    on(repo, ISSUE_BRANCH)
    commit(repo)
    git("checkout", "-q", "--detach", repo["start"], cwd=repo["work"])   # HEAD back at the start, off the tip
    m, _, _, _ = compose(repo, is_pr=False, error="⚠️ it broke\n", claude_outcome="failure")
    assert m["error"]["message"].startswith("⚠️ it broke\n\n---\n\n⚠️ The agent run finished but its work was NOT landed")


def test_pr_run_with_head_off_the_pr_branch_lands_nothing(repo):
    # A closed PR the sync skipped (no SYNC_BRANCH): HEAD is still the
    # default ref, whose history may contain the PR's merged tip — never a
    # push to the branch. The PR branch does not exist locally and HEAD is
    # where the checkout left it, so this is the fence, not a rejection: no
    # error of the composer's own.
    m, _, _, out = compose(repo, is_pr=True, trigger="@auto", auto="true", sync_branch="")
    assert out["read_only"] == "true" and "handback" not in m and "error" not in m
    assert m["stage"] == "Review"                   # nothing handed back to the loop: the loop stopped here


def test_rejected_autonomous_pr_run_hands_back_to_a_human(repo):
    # An @auto PR run that committed, then detached at the start SHA: the
    # work is refused, nothing lands and no re-review is requested — so the
    # stage must be Review, not left at Agent with a red run (review round 3
    # of #84).
    on(repo, PR_BRANCH)
    commit(repo)
    git("checkout", "-q", "--detach", repo["start"], cwd=repo["work"])
    m, _, _, out = compose(repo, is_pr=True, trigger="@auto", auto="true")
    assert out["read_only"] == "true" and "handback" not in m
    assert m["error"]["fail_run"] is True and "left its branch" in m["error"]["message"]
    assert m["stage"] == "Review"


def test_issue_run_title_falls_back_and_is_capped(repo):
    on(repo, ISSUE_BRANCH)
    commit(repo, subject="x" * 300, body="")
    m, _, _, _ = compose(repo, is_pr=False)
    assert len(m["pr"]["title"]) == 256
    assert "base" not in m["pr"]                    # empty base_branch: the land job's default


# --- PR runs: the hand-back ----------------------------------------------------


def test_pr_run_on_auto_pr_owes_exactly_one_handback(repo):
    on(repo, PR_BRANCH)
    commit(repo)
    m, _, _, out = compose(repo, is_pr=True, auto="true")
    assert out["branch"] == PR_BRANCH and out["read_only"] == "false"
    assert m["handback"] is True and "pr" not in m
    assert "stage" not in m                         # @claude on an auto PR: handed back to the loop, mid-flight


def test_pr_run_at_auto_with_commit_stays_at_agent(repo):
    on(repo, PR_BRANCH)
    commit(repo)
    m, _, _, _ = compose(repo, is_pr=True, trigger="@auto", auto="true")
    assert m["handback"] is True and "stage" not in m


def test_pr_run_merge_only_at_auto_still_owes_the_handback(repo):
    on(repo, PR_BRANCH)
    commit(repo)                                    # stands in for the runner's clean base merge
    head = git("rev-parse", "HEAD", cwd=repo["work"]).stdout.strip()
    m, _, landing, _ = compose(repo, is_pr=True, trigger="@auto", auto="true", merge_sha=head,
                               final_message="Nothing to do.")
    assert m["handback"] is True
    # The agent committed nothing: its answer is relayed, with the merge named.
    assert m["comments"] == [{"number": 34, "body_file": "agent-summary.md"}]
    assert "base-branch merge from the workflow was committed" in (landing / "agent-summary.md").read_text()


def test_pr_run_without_auto_owes_nothing(repo):
    on(repo, PR_BRANCH)
    commit(repo)
    m, _, _, _ = compose(repo, is_pr=True, auto="false")
    assert "handback" not in m and m["stage"] == "Review"


def test_rebased_head_lands_nothing_and_owes_nothing(repo):
    on(repo, PR_BRANCH)
    git("commit", "-q", "--amend", "-m", "rewritten", cwd=repo["work"])
    m, _, _, _ = compose(repo, is_pr=True, trigger="@auto", auto="true")
    assert "handback" not in m


# --- the agent's own comments -------------------------------------------------


def test_agent_comments_are_pinned_checked_and_capped(repo):
    landing = repo["tmp"] / "landing"
    landing.mkdir()
    for name in ("ok.md", "other.md", "big.md") + tuple(f"n{i}.md" for i in range(6)):
        (landing / name).write_text("hello @review\n")
    (landing / "big.md").write_text("x" * 70000)
    (landing / "link.md").symlink_to(landing / "ok.md")
    (landing / "ok.md").write_text("Fixed as @review asked. <!-- claude-review-summary -->\n\n🤖 engine: codex · auto-handoff\n")
    on(repo, PR_BRANCH)
    commit(repo)
    m, res, landing, _ = compose(repo, is_pr=True, auto="true", agent_extra=json.dumps({
        "comments": [
            {"number": 34, "body_file": "ok.md"},
            {"body_file": "big.md"},                         # no number: pinned to this run's
            {"number": 99, "body_file": "other.md"},         # another thread: dropped
            {"number": 34, "body_file": "../etc/passwd"},    # not a plain file name
            {"number": 34, "body_file": ".hidden"},          # dot-named: the artifact would drop it
            {"number": 34, "body_file": "missing.md"},
            {"number": 34, "body_file": "link.md"},          # symlink
            {"number": 34},                                  # no body_file
            "not an object",
        ] + [{"body_file": f"n{i}.md"} for i in range(6)],
        "replies": [{"review_comment_id": 1, "body_file": "ok.md"}],   # not the agent's to set here
    }))
    files = [c["body_file"] for c in m["comments"]]
    assert files == ["ok.md", "big.md", "n0.md", "n1.md", "n2.md"]
    assert all(c["number"] == 34 for c in m["comments"])
    assert (landing / "big.md").stat().st_size < 60000
    assert (landing / "big.md").read_text().endswith("_[truncated: the body exceeded the comment size cap]_\n")
    assert "replies" not in m and m["handback"] is True
    for warned in ("number 99 is not this run's #34", "'../etc/passwd'", "'.hidden'", "'missing.md'", "'link.md'",
                   "no body_file", "at most five comments", "ignored keys the agent may not set in its manifest: replies"):
        assert warned in res.stdout, warned
    # The agent left a comment of its own, so the final message is not relayed.
    assert "agent-summary.md" not in files
    # De-fanged in place, footer included: posted as marvin, a body carrying
    # the codex reviewer's footer would be the next fix round's review anchor
    # (`land` leaves the footer alone), and a quoted marker would forge a
    # verdict.
    body = (landing / "ok.md").read_text()
    assert body == "Fixed as `review` asked. <!-- claude-review summary -->\n\n🤖 engine  codex · auto handoff\n"


def test_agent_manifest_that_is_not_json_is_dropped_not_fatal(repo):
    on(repo, ISSUE_BRANCH)
    commit(repo)
    m, res, _, _ = compose(repo, is_pr=False, agent_extra="{not json")
    assert "comments" not in m and m["pr"]["open"] is True
    assert "manifest-extra.json is not a JSON object" in res.stdout


# --- errors and codex -----------------------------------------------------------


def test_error_is_carried_and_no_relay_posts_over_it(repo):
    # The action died before naming a branch; HEAD is the checkout: the
    # Surface error is the manifest's, untouched.
    m, _, _, _ = compose(repo, is_pr=False, error="⚠️ it broke", claude_outcome="failure", claude_branch="")
    assert m["error"] == {"message": "⚠️ it broke", "fail_run": True}
    assert "comments" not in m and m["stage"] == "Review"


def test_error_at_auto_lands_but_owes_no_handback(repo):
    # A PR run in the loop whose agent step errored after committing: the
    # commits land and the ⚠️ posts, but the PR goes to a human (stage
    # Review), not back to the loop — the same rule as the PR this workflow
    # opens (it used to owe the `@review` whatever the step's outcome). Both
    # engines, and the merge-only shape.
    on(repo, PR_BRANCH)
    commit(repo)
    m, _, _, _ = compose(repo, is_pr=True, trigger="@auto", auto="true", error="⚠️ it broke")
    assert "handback" not in m and m["stage"] == "Review" and m["error"]["fail_run"] is True
    m, _, _, _ = compose(repo, is_pr=True, engine="codex", auto="true", codex_commit="success",
                         codex_summary="s\n", error="⚠️ it broke")
    assert "handback" not in m and m["stage"] == "Review" and m["comments"][0]["body_file"] == "codex-comment.md"
    head = git("rev-parse", "HEAD", cwd=repo["work"]).stdout.strip()
    m, _, _, _ = compose(repo, is_pr=True, trigger="@auto", auto="true", merge_sha=head, error="⚠️ it broke")
    assert "handback" not in m and m["stage"] == "Review"


def test_codex_pr_run_carries_summary_ids_and_handback(repo):
    on(repo, PR_BRANCH)
    commit(repo)
    m, _, _, _ = compose(repo, is_pr=True, engine="codex", auto="true", codex_commit="success",
                         codex_ids="PRRT_b bogus PRRT_a PRRT_b", codex_summary="🤖 codex (dev agent):\n\ndone\n")
    assert m["comments"] == [{"number": 34, "body_file": "codex-comment.md"}]
    assert m["resolve_threads"] == ["PRRT_a", "PRRT_b"]
    assert m["handback"] is True and "stage" not in m


def test_codex_issue_run_opens_pr_and_resolves_nothing(repo):
    on(repo, CODEX_BRANCH)
    commit(repo)
    m, _, _, out = compose(repo, is_pr=False, engine="codex", codex_commit="success",
                           codex_ids="PRRT_a", codex_summary="s\n")
    assert out["branch"] == CODEX_BRANCH and out["read_only"] == "false"
    assert m["pr"]["open"] is True and m["comments"][0]["body_file"] == "codex-comment.md"
    assert "resolve_threads" not in m


def test_codex_guard_failure_runs_no_git_and_lands_nothing(repo):
    on(repo, CODEX_BRANCH)
    commit(repo)
    m, res, _, out = compose(repo, is_pr=False, engine="codex", codex_guard="failure", codex_commit="skipped",
                             error="⚠️ conflict markers")
    assert "pr" not in m and "handback" not in m and "comments" not in m
    assert m["error"]["fail_run"] is True and m["stage"] == "Review"
    assert "running no git" in res.stdout
    assert out["branch"] == CODEX_BRANCH and out["read_only"] == "true"


def test_provenance_note_travels_as_a_file(repo):
    on(repo, PR_BRANCH)
    m, _, landing, _ = compose(repo, is_pr=True, prov_note="served by another model")
    assert m["provenance_comment_file"] == "prov.md"
    assert (landing / "prov.md").read_text() == "served by another model\n"


# --- the workflow's own shape ---------------------------------------------------


def test_every_workflow_call_input_declares_a_type():
    # A reusable-workflow input without `type` fails to load for EVERY caller
    # (review round 1 of #84 caught one that lost its declaration in an edit).
    # Text-based like the script extraction above: PyYAML is not a test
    # dependency. Inputs are the 6-space keys under `on.workflow_call.inputs`,
    # their bodies the 8-space lines that follow.
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index("    inputs:") + 1
    inputs, current = {}, None
    for line in lines[start:]:
        if line.startswith("    ") and not line.startswith("     "):
            break                                   # `secrets:` — the next 4-space key
        if line.startswith("      ") and not line.startswith("       ") and line.rstrip().endswith(":"):
            current = line.strip()[:-1]
            inputs[current] = set()
        elif current and line.startswith("        ") and not line.startswith("         "):
            inputs[current].add(line.strip().split(":")[0])
    assert inputs, "no inputs found"
    missing = [name for name, keys in inputs.items() if "type" not in keys]
    assert not missing, f"inputs without a type: {missing}"
    assert {"required", "type", "default"} <= inputs["request_review_after_open"]


def test_workflow_declares_trusted_logins_once_and_passes_it_to_the_reset():
    # The re-engagement reset rewrites only the loops' own counter comments;
    # Phase 2 flips the one env value.
    text = WORKFLOW.read_text()
    assert text.count("\nenv:\n") == 1 and "\n  TRUSTED_LOGINS: i-am-marvin,meridian-marvin[bot]\n" in text
    assert "trusted-logins: ${{ env.TRUSTED_LOGINS }}" in text


def test_prompts_forbid_workflow_file_edits():
    # The land composite refuses a bundle touching .github/workflows/ (the
    # machine account has no Workflows permission), so the dev agent hears
    # it in its LANDING paragraph before spending the run — on both
    # engines: the Claude system prompt and codex's CONSTRAINTS line.
    text = WORKFLOW.read_text()
    assert WORKFLOW_FILES_RULE + " in a comment so a maintainer makes it from their machine." in step_block(text, "sysprompt")
    assert WORKFLOW_FILES_RULE + " in your final message so a maintainer makes it from their machine." in step_block(text, "codexcompose")
