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
from test_land_helpers import run_emit_landing, sh, git  # noqa: E402


def composer_script() -> str:
    """The step's bash, lifted from the workflow (text extraction: PyYAML is
    not a test dependency). The block scalar under `run: |` is every
    following line indented by at least its 10 spaces, up to the first that
    is not."""
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


def commit(r, subject="feat: add the thing", body="Because the issue asked for it.\n\nCo-Authored-By: X <x@y>"):
    (r["work"] / "f").write_text("2\n")
    git("commit", "-qam", subject, "-m", body, cwd=r["work"])


def compose(r, *, is_pr, engine="claude", trigger="@claude", auto="false", agent_extra=None,
            claude_outcome="success", codex_commit="skipped", codex_guard=None, codex_ids="",
            codex_summary=None, final_message="Here is the answer.", error=None, req_review="false",
            base="", pr_labels='["auto"]', claude_branch="claude/issue-12-20260911", head_branch="feature",
            merge_sha="", prov_note=""):
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
        "START_SHA": r["start"], "MERGE_SHA": merge_sha,
        "CLAUDE_BRANCH": claude_branch if (engine == "claude" and not is_pr) else "",
        "CODEX_BRANCH": "claude/issue-12-codex-7" if (engine == "codex" and not is_pr) else "",
        "EXEC": str(exec_file),
        "CLAUDE_OUTCOME": claude_outcome if engine == "claude" else "skipped",
        "CODEXGUARD_OUTCOME": codex_guard, "CODEXCOMMIT_OUTCOME": codex_commit, "CODEX_IDS": codex_ids,
        "PROV_NOTE": prov_note, "ERROR_FILE": str(error_file),
        "RUN_URL": "https://github.com/meridianlabs-ai/agents/actions/runs/123",
        "GITHUB_RUN_ID": "123", "GITHUB_OUTPUT": str(output),
        "GIT_DIR": str(r["work"] / ".git"), "GIT_COMMON_DIR": str(r["work"] / ".git"),
        "GIT_WORK_TREE": str(r["work"]),
    }
    res = sh("bash", "-c", composer_script(), cwd=r["work"], check=False, env=env)
    assert res.returncode == 0, res.stderr
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
    return json.loads(extra.read_text()), res, landing, outputs


def validate(landing, *args):
    return sh(sys.executable, str(VALIDATOR), "--dir", str(landing), "--repo", "meridianlabs-ai/agents",
              "--run-id", "123", "--default-branch", "main", *args, check=False)


# --- issue runs: the PR is the land job's -------------------------------------


def test_issue_run_with_commit_opens_pr_and_validates(repo):
    commit(repo)
    m, _, landing, out = compose(repo, is_pr=False, base="main")
    assert out["branch"] == "claude/issue-12-20260911"
    assert m["pr"] == {"open": True, "title": "feat: add the thing", "body_file": "pr-body.md",
                       "labels": ["auto"], "issue": 12, "base": "main"}
    body = (landing / "pr-body.md").read_text()
    assert body.startswith("Fixes #12\n\nBecause the issue asked for it.")
    assert "Co-Authored-By: X" in body and "[this run](https://github.com/meridianlabs-ai/agents/actions/runs/123)" in body
    assert "handback" not in m                      # @claude: no reviewer requested
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
    commit(repo)
    m, res, _, _ = compose(repo, is_pr=False, trigger="@auto", auto="true", req_review="true",
                           pr_labels='["auto","engine:codex"]')
    assert m["pr"]["labels"] == ["auto", "engine:codex"]
    assert m["handback"] is True                    # request_review_after_open (the fork)
    assert "stage" not in m                         # the loop owns the stage from here
    assert "stage stays at Agent" in res.stdout


def test_issue_run_with_no_commit_opens_nothing_and_relays_the_answer(repo):
    m, _, landing, out = compose(repo, is_pr=False, claude_branch="",
                                 final_message="It already works; see @review's note. engine: codex")
    assert "pr" not in m and "handback" not in m
    assert m["stage"] == "Review"
    assert m["comments"] == [{"number": 12, "body_file": "agent-summary.md"}]
    body = (landing / "agent-summary.md").read_text()
    assert body.startswith("🤖 claude (dev agent): no code changes were made — the agent's summary:")
    assert "`review`'s note" in body and "engine  codex" in body and "@review" not in body
    # No branch from the action (a failed step loses its outputs) and HEAD
    # never moved: a prefix-conforming placeholder nothing lands on.
    assert out["branch"] == "claude/issue-12-run-123"


def test_issue_run_reads_the_local_branch_when_the_action_output_is_lost(repo):
    git("checkout", "-qb", "claude/issue-12-20260911-1200", cwd=repo["work"])
    commit(repo)
    m, _, _, out = compose(repo, is_pr=False, claude_branch="", claude_outcome="failure")
    assert out["branch"] == "claude/issue-12-20260911-1200"
    assert m["pr"]["open"] is True                  # committed work lands even when the step failed
    assert m["stage"] == "Review"


def test_issue_run_title_falls_back_and_is_capped(repo):
    commit(repo, subject="x" * 300, body="")
    m, _, _, _ = compose(repo, is_pr=False)
    assert len(m["pr"]["title"]) == 256
    assert "base" not in m["pr"]                    # empty base_branch: the land job's default


# --- PR runs: the hand-back ----------------------------------------------------


def test_pr_run_on_auto_pr_owes_exactly_one_handback(repo):
    commit(repo)
    m, _, _, out = compose(repo, is_pr=True, auto="true")
    assert out["branch"] == "feature"
    assert m["handback"] is True and "pr" not in m
    assert m["stage"] == "Review"                   # @claude on an auto PR: still hands back to a human


def test_pr_run_at_auto_with_commit_stays_at_agent(repo):
    commit(repo)
    m, _, _, _ = compose(repo, is_pr=True, trigger="@auto", auto="true")
    assert m["handback"] is True and "stage" not in m


def test_pr_run_merge_only_at_auto_still_owes_the_handback(repo):
    commit(repo)                                    # stands in for the runner's clean base merge
    head = git("rev-parse", "HEAD", cwd=repo["work"]).stdout.strip()
    m, _, landing, _ = compose(repo, is_pr=True, trigger="@auto", auto="true", merge_sha=head,
                               final_message="Nothing to do.")
    assert m["handback"] is True
    # The agent committed nothing: its answer is relayed, with the merge named.
    assert m["comments"] == [{"number": 34, "body_file": "agent-summary.md"}]
    assert "base-branch merge from the workflow was committed" in (landing / "agent-summary.md").read_text()


def test_pr_run_without_auto_owes_nothing(repo):
    commit(repo)
    m, _, _, _ = compose(repo, is_pr=True, auto="false")
    assert "handback" not in m and m["stage"] == "Review"


def test_rebased_head_lands_nothing_and_owes_nothing(repo):
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


def test_agent_manifest_that_is_not_json_is_dropped_not_fatal(repo):
    commit(repo)
    m, res, _, _ = compose(repo, is_pr=False, agent_extra="{not json")
    assert "comments" not in m and m["pr"]["open"] is True
    assert "manifest-extra.json is not a JSON object" in res.stdout


# --- errors and codex -----------------------------------------------------------


def test_error_is_carried_and_no_relay_posts_over_it(repo):
    m, _, _, _ = compose(repo, is_pr=False, error="⚠️ it broke", claude_outcome="failure")
    assert m["error"] == {"message": "⚠️ it broke", "fail_run": True}
    assert "comments" not in m and m["stage"] == "Review"


def test_error_at_auto_still_hands_back_to_a_human(repo):
    commit(repo)
    m, _, _, _ = compose(repo, is_pr=True, trigger="@auto", auto="true", error="⚠️ it broke")
    assert m["handback"] is True and m["stage"] == "Review"


def test_codex_pr_run_carries_summary_ids_and_handback(repo):
    commit(repo)
    m, _, _, _ = compose(repo, is_pr=True, engine="codex", auto="true", codex_commit="success",
                         codex_ids="PRRT_b bogus PRRT_a PRRT_b", codex_summary="🤖 codex (dev agent):\n\ndone\n")
    assert m["comments"] == [{"number": 34, "body_file": "codex-comment.md"}]
    assert m["resolve_threads"] == ["PRRT_a", "PRRT_b"]
    assert m["handback"] is True and m["stage"] == "Review"


def test_codex_issue_run_opens_pr_and_resolves_nothing(repo):
    commit(repo)
    m, _, _, out = compose(repo, is_pr=False, engine="codex", codex_commit="success",
                           codex_ids="PRRT_a", codex_summary="s\n")
    assert out["branch"] == "claude/issue-12-codex-7"
    assert m["pr"]["open"] is True and m["comments"][0]["body_file"] == "codex-comment.md"
    assert "resolve_threads" not in m


def test_codex_guard_failure_runs_no_git_and_lands_nothing(repo):
    commit(repo)
    m, res, _, out = compose(repo, is_pr=False, engine="codex", codex_guard="failure", codex_commit="skipped",
                             error="⚠️ conflict markers")
    assert "pr" not in m and "handback" not in m and "comments" not in m
    assert m["error"]["fail_run"] is True and m["stage"] == "Review"
    assert "running no git" in res.stdout
    assert out["branch"] == "claude/issue-12-codex-7"


def test_provenance_note_travels_as_a_file(repo):
    m, _, landing, _ = compose(repo, is_pr=True, prov_note="served by another model")
    assert m["provenance_comment_file"] == "prov.md"
    assert (landing / "prov.md").read_text() == "served by another model\n"
