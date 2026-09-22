"""Tests for claude-auto.yml's `Compose landing manifest` step.

The CI-fix loop's composer is the smallest of the three: a landed commit
(or the runner's base merge) owes the re-review request, and nothing else
is decided here. Its bash is lifted out of the workflow and run against a
scratch repo the way the other two composers' tests do. The stage rule it
must NOT break (design/atlas-tracking.md → Who moves the card; Ransom,
2026-09-18): a hand-back is mid-flight, so it sets no `stage` — the card
stays at Agent until the loop ends, and Review is the gate's, on
escalation.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "claude-auto.yml"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_helpers import WORKFLOW_FILES_RULE, sh, git, step_block  # noqa: E402


def composer_script() -> str:
    """The step's bash, lifted from the workflow (text extraction: PyYAML is
    not a test dependency), as in test_review_fix_composer.py."""
    lines = WORKFLOW.read_text().splitlines()
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


def compose(r, *, engine="claude", claude_outcome="success", merge_sha="", final_message="Nothing to fix.",
            error=None, agent_started=None, codexguard_outcome=None):
    landing = r["tmp"] / "landing"
    landing.mkdir(exist_ok=True)
    exec_file = r["tmp"] / "exec.json"
    if agent_started is False:
        exec_file.write_text("")                 # the action produced no execution output
    else:
        exec_file.write_text(json.dumps([{"type": "result", "result": final_message}]))
    error_file = r["tmp"] / "agent-error.md"
    if error is not None:
        error_file.write_text(error)
    extra = r["tmp"] / "landing-extra.json"
    if agent_started is None:
        agent_started = engine == "claude"
    if codexguard_outcome is None:
        codexguard_outcome = "success" if engine == "codex" else "skipped"
    env = {
        "DIR": str(landing), "EXTRA": str(extra), "PR": "42", "ATTEMPT": "2", "ENGINE": engine,
        "START_SHA": r["start"], "MERGE_SHA": merge_sha, "EXEC": str(exec_file),
        "CLAUDE_OUTCOME": claude_outcome if engine == "claude" else "skipped",
        "AGENT_STARTED": "true" if agent_started else "false",
        "CODEXGUARD_OUTCOME": codexguard_outcome,
        "CODEXCOMMIT_OUTCOME": "skipped", "PROV_NOTE": "", "ERROR_FILE": str(error_file),
        "GIT_DIR": str(r["work"] / ".git"), "GIT_COMMON_DIR": str(r["work"] / ".git"),
        "GIT_WORK_TREE": str(r["work"]),
    }
    res = sh("bash", "-c", composer_script(), cwd=r["work"], check=False, env=env)
    assert res.returncode == 0, res.stderr
    return json.loads(extra.read_text()), landing


def test_landed_fix_owes_the_handback_and_no_stage(repo):
    commit(repo)
    m, _ = compose(repo)
    assert m["handback"] is True
    assert "stage" not in m                         # mid-flight: the card stays at Agent
    assert "comments" not in m and "error" not in m


def test_merge_only_attempt_owes_the_handback_relays_the_answer_and_sets_no_stage(repo):
    commit(repo)                                    # stands in for the runner's clean base merge
    head = git("rev-parse", "HEAD", cwd=repo["work"]).stdout.strip()
    m, landing = compose(repo, merge_sha=head)
    assert m["handback"] is True and "stage" not in m
    assert m["comments"] == [{"number": 42, "body_file": "agent-summary.md"}]
    assert (landing / "agent-summary.md").read_text().startswith("🤖 auto (CI-fix attempt 2): base merge only")


def test_no_commit_owes_nothing_and_sets_no_stage(repo):
    m, _ = compose(repo)
    assert "handback" not in m and "stage" not in m
    assert m["comments"] == [{"number": 42, "body_file": "agent-summary.md"}]


def test_errored_attempt_with_a_commit_still_owes_the_handback_and_sets_no_stage(repo):
    # The CI-fix loop's own rule, unchanged: the landed commit owes the
    # re-review whatever the step's outcome (the push re-runs CI); the ⚠️
    # travels as the error. Still no stage — escalation is the gate's.
    commit(repo)
    m, _ = compose(repo, claude_outcome="failure", error="⚠️ it broke\n")
    assert m["handback"] is True and "stage" not in m and m["error"]["fail_run"] is True


def test_a_claude_step_that_failed_without_launching_lands_nothing_not_even_the_base_merge(repo):
    # Finding 4628657's local reproduction: the action refused the actor
    # (or failed to start) and the round still bundled the runner's clean
    # base merge — a marvin push, a CI re-run and a kept attempt for a round
    # nobody authorized. Now: no hand-back, no relay, no git; the error
    # travels and the land job's refund (agent not started, nothing pushed)
    # gives the attempt back. Same shape as the codex refusal path.
    commit(repo)                                    # the runner's base merge, above the start SHA
    head = git("rev-parse", "HEAD", cwd=repo["work"]).stdout.strip()
    m, landing = compose(repo, claude_outcome="failure", agent_started=False, merge_sha=head,
                         error="⚠️ the CI-fix step failed before the agent launched\n")
    assert "handback" not in m and "comments" not in m and "stage" not in m
    assert m["error"]["fail_run"] is True
    assert not (landing / "agent-summary.md").exists()


def test_a_claude_step_that_launched_and_then_failed_still_lands_its_commit(repo):
    # Unchanged: a failure AFTER launch keeps the CI-fix loop's own rule (the
    # landed commit owes the re-review; the push re-runs CI).
    commit(repo)
    m, _ = compose(repo, claude_outcome="failure", agent_started=True, error="⚠️ it broke\n")
    assert m["handback"] is True and m["error"]["fail_run"] is True


def test_a_codex_round_whose_guard_did_not_succeed_lands_nothing(repo):
    # The codex refusal path (the action's permission check fails the run
    # step, so the guard is skipped): the counterexample the investigation
    # measured the Claude path against — no git, no hand-back, no comment.
    commit(repo)
    m, landing = compose(repo, engine="codex", codexguard_outcome="skipped", merge_sha=repo["start"])
    assert "handback" not in m and "comments" not in m
    assert not (landing / "agent-summary.md").exists()


def test_prompts_forbid_workflow_file_edits():
    # As test_dev_agent_composer: the CI fixer's prompt, on both engines,
    # says not to touch .github/workflows/ — the land composite refuses the
    # whole bundle otherwise.
    text = WORKFLOW.read_text()
    assert WORKFLOW_FILES_RULE + " in a comment so a maintainer makes it from their machine." in step_block(text, "prompt")
    assert WORKFLOW_FILES_RULE + " in your final message so a maintainer makes it from their machine." in step_block(text, "codexprep")
