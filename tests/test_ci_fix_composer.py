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
from test_land_helpers import sh, git  # noqa: E402


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
            error=None):
    landing = r["tmp"] / "landing"
    landing.mkdir(exist_ok=True)
    exec_file = r["tmp"] / "exec.json"
    exec_file.write_text(json.dumps([{"type": "result", "result": final_message}]))
    error_file = r["tmp"] / "agent-error.md"
    if error is not None:
        error_file.write_text(error)
    extra = r["tmp"] / "landing-extra.json"
    env = {
        "DIR": str(landing), "EXTRA": str(extra), "PR": "42", "ATTEMPT": "2", "ENGINE": engine,
        "START_SHA": r["start"], "MERGE_SHA": merge_sha, "EXEC": str(exec_file),
        "CLAUDE_OUTCOME": claude_outcome if engine == "claude" else "skipped",
        "CODEXGUARD_OUTCOME": "success" if engine == "codex" else "skipped",
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
