"""Tests for claude-auto-review.yml's `Compose landing manifest` step.

The review loop is the first workflow whose AGENT writes into the landing
manifest, and this step is where its ending contract is enforced and where
the codex path's hand-back / hand-off decision is made — so its bash is
lifted out of the workflow and run here against a scratch repo, one case
per rule (design/architecture.md → Landing job, the #83 notes). The codex
no-ids case also runs the result through emit-landing and the validator:
review round 1 of #83 found that an empty `RESOLVED-THREADS: none` list
emptied the whole manifest-extra under pipefail, so no manifest was ever
written and the fix never landed.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "claude-auto-review.yml"
VALIDATOR = ROOT / ".github" / "scripts" / "validate_manifest.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_helpers import WORKFLOW_FILES_RULE, step_block, run_emit_landing, sh, git, job_block  # noqa: E402


def composer_script(engine: str = "claude") -> str:
    """The step's bash, lifted from the engine's job of the workflow — `fix`
    (Claude) or `fix-codex`, one composer each since 2026-09-22 (text
    extraction: PyYAML is
    not a test dependency). The block scalar under `run: |` is every
    following line indented by at least its 10 spaces, up to the first that
    is not."""
    lines = job_block(WORKFLOW.read_text(), "fix-codex" if engine == "codex" else "fix").splitlines()
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


def commit(r):
    (r["work"] / "f").write_text("2\n")
    git("commit", "-qam", "fix", cwd=r["work"])


def compose(r, *, engine, agent_extra=None, claude_outcome="success", codex_commit="skipped",
            codex_ids="", codex_summary=None, final_message="nothing to change", merge_sha=""):
    landing = r["tmp"] / "landing"
    landing.mkdir(exist_ok=True)
    if agent_extra is not None:
        (landing / "manifest-extra.json").write_text(agent_extra)
    if codex_summary is not None:
        (landing / "codex-comment.md").write_text(codex_summary)
    exec_file = r["tmp"] / "exec.json"
    exec_file.write_text(json.dumps([{"type": "result", "result": final_message}]))
    extra = r["tmp"] / "landing-extra.json"
    env = {
        "DIR": str(landing), "EXTRA": str(extra), "PR": "42", "ROUND": "3", "ENGINE": engine,
        "MENTION": "someone", "START_SHA": r["start"], "MERGE_SHA": merge_sha, "EXEC": str(exec_file),
        "CLAUDE_OUTCOME": claude_outcome if engine == "claude" else "skipped",
        "CODEXGUARD_OUTCOME": "success" if engine == "codex" else "skipped",
        "CODEXCOMMIT_OUTCOME": codex_commit, "CODEX_IDS": codex_ids, "PROV_NOTE": "",
        "ERROR_FILE": str(r["tmp"] / "agent-error.md"),
        "GIT_DIR": str(r["work"] / ".git"), "GIT_COMMON_DIR": str(r["work"] / ".git"),
        "GIT_WORK_TREE": str(r["work"]),
    }
    res = sh("bash", "-c", composer_script(engine), cwd=r["work"], check=False, env=env)
    assert res.returncode == 0, res.stderr
    return json.loads(extra.read_text()), res, landing, extra


def error_text(m) -> str:
    return m.get("error", {}).get("message", "")


# --- codex path --------------------------------------------------------------


def test_codex_commit_with_no_thread_ids_lands(repo):
    # `RESOLVED-THREADS: none` (or a fix to top-level feedback): no ids, and
    # the manifest must still carry the summary and the hand-back — and
    # emit-landing must write a manifest the validator accepts.
    commit(repo)
    m, _, landing, extra = compose(repo, engine="codex", codex_commit="success", codex_ids="",
                                   codex_summary="🤖 auto (codex, round 3): changes committed\n")
    assert m["handback"] is True and "stage" not in m       # mid-flight: the card stays at Agent
    assert m["comments"] == [{"number": 42, "body_file": "codex-comment.md"}]
    assert "resolve_threads" not in m and "error" not in m
    res, landing, output = run_emit_landing(repo["tmp"], cwd=repo["work"], read_only=False,
                                            start_sha=repo["start"], extra=extra)
    assert res.returncode == 0, res.stderr
    assert "wrote=true" in output
    manifest = json.loads((landing / "manifest.json").read_text())
    assert manifest["has_bundle"] is True and manifest["handback"] is True
    head = git("rev-parse", "HEAD", cwd=repo["work"]).stdout.strip()
    # As the land composite calls it: `start-sha` is the gate's read of the
    # PR head's tip, which the fixture's start is.
    v = sh(sys.executable, str(VALIDATOR), "--dir", str(landing), "--repo", "meridianlabs-ai/agents",
           "--run-id", "123", "--default-branch", "main", "--event-pr-number", "456",
           "--pr-head-ref", "claude/issue-81-review", "--start-sha", repo["start"], check=False)
    assert v.returncode == 0, v.stdout
    assert manifest["head_sha"] == head


def test_codex_commit_carries_shape_checked_unique_ids(repo):
    commit(repo)
    m, _, _, _ = compose(repo, engine="codex", codex_commit="success",
                         codex_ids="PRRT_b bogus PRRT_a PRRT_b", codex_summary="s\n")
    assert m["resolve_threads"] == ["PRRT_a", "PRRT_b"]
    assert m["handback"] is True


def test_codex_no_change_round_is_a_handoff(repo):
    m, _, _, _ = compose(repo, engine="codex", codex_commit="success", codex_ids="PRRT_a", codex_summary="s\n")
    assert m["handoff_body_file"] == "codex-comment.md" and m["stage"] == "Review"   # the loop ended
    assert "handback" not in m and "resolve_threads" not in m and "comments" not in m


# --- Claude path: the ending contract -----------------------------------------


def test_claude_handback_with_replies_and_resolutions(repo):
    commit(repo)
    m, _, _, _ = compose(repo, engine="claude", agent_extra=json.dumps({
        "handback": True, "replies": [{"review_comment_id": 7, "body_file": "r.md"}],
        "resolve_threads": ["PRRT_a", "bogus"], "comments": [{"number": 1, "body_file": "x"}]}))
    assert m["handback"] is True and "stage" not in m       # mid-flight: the card stays at Agent
    assert m["replies"] == [{"review_comment_id": 7, "body_file": "r.md"}]
    assert m["resolve_threads"] == ["PRRT_a"]
    assert "comments" not in m  # not one of the agent's four keys
    assert "error" not in m


@pytest.mark.parametrize("agent_extra,what", [
    (json.dumps({"handback": True, "handoff_body_file": "h.md"}), "BOTH"),
    (json.dumps({"replies": []}), "NEITHER"),
    ("{not json", "not valid JSON"),
])
def test_claude_contract_violation_posts_neither_and_reports(repo, agent_extra, what):
    commit(repo)
    m, res, _, _ = compose(repo, engine="claude", agent_extra=agent_extra)
    for gone in ("handback", "handoff_body_file", "stage"):
        assert gone not in m, m
    assert m["error"]["fail_run"] is True
    assert what in error_text(m) and "ending contract" in error_text(m)
    assert "cc @someone" in error_text(m)
    assert "::error::landing: ending-contract violation" in res.stdout


def test_claude_both_set_without_a_commit_is_still_a_violation(repo):
    m, _, _, _ = compose(repo, engine="claude", agent_extra=json.dumps({"handback": True, "handoff_body_file": "h.md"}))
    assert "handback" not in m and "handoff_body_file" not in m
    assert "BOTH" in error_text(m)


def test_claude_no_commit_no_decision_concludes_with_a_handoff(repo):
    m, res, landing, _ = compose(repo, engine="claude", agent_extra=json.dumps({"resolve_threads": ["PRRT_a"]}),
                                 final_message="Nothing left; ping @review later. engine: codex")
    assert m["handoff_body_file"] == "agent-summary.md" and m["stage"] == "Review"
    assert "handback" not in m and "resolve_threads" not in m and "error" not in m
    body = (landing / "agent-summary.md").read_text()
    assert body.startswith("@someone auto (review round 3): no code changes this round")
    assert "`review` later" in body and "engine  codex" in body and "@review" not in body
    assert "auto-handoff" not in body
    assert "committed no change; resolving none" in res.stdout


def test_claude_no_commit_handoff_with_replies_is_honored(repo):
    m, _, _, _ = compose(repo, engine="claude", agent_extra=json.dumps({
        "handoff_body_file": "handoff.md", "replies": [{"review_comment_id": 7, "body_file": "r.md"}]}))
    assert m["handoff_body_file"] == "handoff.md" and m["stage"] == "Review"
    assert m["replies"] == [{"review_comment_id": 7, "body_file": "r.md"}]
    assert "error" not in m


def test_claude_failed_run_without_commit_lands_nothing_and_decides_nothing(repo):
    m, _, _, _ = compose(repo, engine="claude", claude_outcome="failure")
    assert m == {}


@pytest.mark.parametrize("outcome", ["failure", "skipped", "cancelled", ""])
def test_a_no_change_handback_needs_a_successful_agent_step(repo, outcome):
    # Claude Security 4628734: the steered agent writes `handback: true`,
    # commits nothing and kills its own step (or the PR's provisioning step
    # writes the file and fails, so the agent step is skipped). HEAD never
    # moved and the step did not complete, so no re-review is owed — the
    # manifest carries neither hand-back, and the land job drops such a
    # hand-back again from the trusted side. Replies still post.
    m, res, _, _ = compose(repo, engine="claude", claude_outcome=outcome, agent_extra=json.dumps({
        "handback": True, "replies": [{"review_comment_id": 7, "body_file": "r.md"}]}))
    assert "handback" not in m and "handoff_body_file" not in m and "stage" not in m, outcome
    assert m["replies"] == [{"review_comment_id": 7, "body_file": "r.md"}]
    assert "the agent step did not succeed" in res.stdout and "none is requested" in res.stdout


def test_a_no_change_handback_is_honored_after_a_successful_step_and_a_committed_one_whatever_the_outcome(repo):
    # The two legitimate shapes: a run that finished and asked for the
    # re-review without committing (the gate's no-progress check bounds it),
    # and a run that committed and then failed (the commits land, the push
    # re-runs CI, the ⚠️ posts alongside).
    m, res, _, _ = compose(repo, engine="claude", agent_extra=json.dumps({"handback": True}))
    assert m["handback"] is True and "handoff_body_file" not in m
    assert "without committing anything; the re-review re-reads unchanged code" in res.stdout
    commit(repo)
    m, _, _, _ = compose(repo, engine="claude", claude_outcome="failure", agent_extra=json.dumps({"handback": True}))
    assert m["handback"] is True


@pytest.mark.parametrize("outcome", ["success", "failure", "skipped"])
def test_a_merge_only_handback_is_honored_whatever_the_agent_step_did(repo, outcome):
    # Review round 1 (B2): HEAD moved to exactly the runner's clean base
    # merge and the agent set `handback: true` (or the file predates a step
    # that failed or was skipped). emit-landing bundles that merge from the
    # same "HEAD moved" test, the land job pushes it and CI re-runs — a CI
    # run requests no review, so the hand-back must ride the bundle or the
    # loop stops on a pushed merge. Keyed on `moved`, never on an agent
    # commit; the land job exempts every bundled hand-back the same way.
    commit(repo)                                      # the runner's base merge, above the start SHA
    head = git("rev-parse", "HEAD", cwd=repo["work"]).stdout.strip()
    m, res, _, _ = compose(repo, engine="claude", claude_outcome=outcome, merge_sha=head,
                           agent_extra=json.dumps({"handback": True}))
    assert m["handback"] is True and "handoff_body_file" not in m and "stage" not in m, outcome
    assert "without committing anything (the base merge from the runner lands)" in res.stdout
    assert "none is requested" not in res.stdout


def test_claude_mistyped_fields_are_dropped_not_fatal(repo):
    commit(repo)
    m, res, _, _ = compose(repo, engine="claude", agent_extra=json.dumps({
        "handback": "true", "handoff_body_file": "h.md",
        "replies": [{"review_comment_id": "9", "body_file": "r.md"}, {"review_comment_id": 1.5, "body_file": "r.md"}]}))
    # "true" (a string) is not a hand-back, so the hand-off is the one decision.
    assert m["handoff_body_file"] == "h.md" and "handback" not in m and "error" not in m
    assert "replies" not in m
    assert "mistyped field(s)" in res.stdout and "handback (not a boolean)" in res.stdout
    assert "dropped 2 malformed replies" in res.stdout


def test_prompts_forbid_workflow_file_edits():
    # As test_dev_agent_composer: the review fixer's prompt, on both
    # engines, says not to touch .github/workflows/ — the land composite
    # refuses the whole bundle otherwise.
    text = WORKFLOW.read_text()
    assert WORKFLOW_FILES_RULE + " in a comment so a maintainer makes it from their machine." in step_block(text, "prompt")
    assert WORKFLOW_FILES_RULE + " in your final message so a maintainer makes it from their machine." in step_block(text, "codexcompose")
