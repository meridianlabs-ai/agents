"""Tests for claude-review.yml's `Prepare Claude review for landing` and
`Compose landing manifest` steps (issue #114).

The Claude reviewer posts nothing: it writes summary.md, verdict.txt and
inline.json under the review output directory, the prep step turns them into
landing files plus the inline-comment list, and the compose step turns those
into the manifest's `comments` (flagged `review`), `review_comments` and
`review_verdict` — the fields the land job posts, as it already did for the
codex engine (whose prep step and composer are exercised here too). Both
steps' bash is lifted from the workflow and run against scratch
directories, and the pr-mode result runs on through emit-landing
(read-only) and the validator with the reviewer's land inputs, as the land
job would. Also the reads that moved out of the review jobs in step 3 of
design/untrusted-agent-job.md: the responsible-human lookup (now in the
gate) and the loop-ownership read (now in the land job, which passes it as
`stage-override`).
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "claude-review.yml"
VALIDATOR = ROOT / ".github" / "scripts" / "validate_manifest.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_helpers import run_emit_landing, sh, job_block, lift_run  # noqa: E402
from test_review_fix_gate import lift_step  # noqa: E402

spec = importlib.util.spec_from_file_location("validate_manifest_rc", VALIDATOR)
vm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vm)

TEXT = WORKFLOW.read_text()
PREP = lift_step(WORKFLOW, "        id: claudepost")
COMPOSE = lift_step(WORKFLOW, "        id: landing")
# The codex engine has its own job (and composer) since 2026-09-22.
COMPOSE_CODEX = lift_run(job_block(TEXT, "review-codex"), "        id: landing")
PREP_CODEX = lift_run(job_block(TEXT, "review-codex"), "        id: codexpost")
WHO = lift_run(job_block(TEXT, "gate"), "        id: who")
AUTOLOOP = lift_run(job_block(TEXT, "land"), "        id: autoloop")
SETTINGS_STEP = lift_step(WORKFLOW, "        id: reviewsettings")
SHA = "a" * 40
RUNNER_TEMP = "/home/runner/work/_temp"
OUT_DIR = f"{RUNNER_TEMP}/review"
SCRATCH = f"{RUNNER_TEMP}/scratch"
WORKSPACE = "/home/runner/work/repo/repo"


def outputs(path: Path) -> dict:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def prep(tmp_path, *, mode="pr", summary=None, verdict=None, inline=None):
    out_dir = tmp_path / "review"
    out_dir.mkdir(exist_ok=True)
    if summary is not None:
        (out_dir / "summary.md").write_text(summary)
    if verdict is not None:
        (out_dir / "verdict.txt").write_text(verdict)
    if inline is not None:
        (out_dir / "inline.json").write_text(inline if isinstance(inline, str) else json.dumps(inline))
    landing = tmp_path / "landing"
    gh_out = tmp_path / "prep-out.txt"
    gh_out.write_text("")
    env = {"OUT_DIR": str(out_dir), "DIR": str(landing), "INLINE_LIST": str(tmp_path / "claude-inline.json"),
           "MODE": mode, "GITHUB_OUTPUT": str(gh_out)}
    r = sh("bash", "-c", PREP, check=False, env=env)
    assert r.returncode == 0, r.stderr + r.stdout
    return outputs(gh_out), landing, tmp_path / "claude-inline.json", r


def compose(tmp_path, *, mode="pr", claudepost="success", landed="true", verdict="clean",
            codexpost="skipped", codex_verdict="", claude_outcome="success", engine="claude"):
    landing = tmp_path / "landing"
    landing.mkdir(exist_ok=True)
    extra = tmp_path / "landing-extra.json"
    env = {
        "DIR": str(landing), "EXTRA": str(extra), "NUM": "42", "MODE": mode,
        "CLAUDE_OUTCOME": claude_outcome, "CLAUDEPOST_OUTCOME": claudepost, "CLAUDE_LANDED": landed,
        "CLAUDE_VERDICT": verdict, "CLAUDE_INLINE": str(tmp_path / "claude-inline.json"),
        "CODEXPOST_OUTCOME": codexpost, "CODEX_VERDICT": codex_verdict,
        "PROV_NOTE": "", "ERROR_FILE": str(tmp_path / "agent-error.md"),
        # What the composers no longer read (step 3): a stale value must not
        # bring the stage decision back.
        "ACK": "true", "ENGAGED": "false",
    }
    r = sh("bash", "-c", COMPOSE_CODEX if engine == "codex" else COMPOSE, check=False, env=env)
    assert r.returncode == 0, r.stderr + r.stdout
    return json.loads(extra.read_text()), extra


def validate(landing: Path, *, branch="claude/issue-81-review", pr="456", issue="", prefix=""):
    # The reviewer's land inputs (claude-review.yml's Land step).
    manifest = json.loads((landing / "manifest.json").read_text())
    return vm.validate(
        manifest, artifact_dir=landing, repo="meridianlabs-ai/agents", run_id="123", default_branch="main",
        allowed_issue_repos=[], pr_head_ref=branch if pr else "", refused_branches=["main"],
        event_pr_number=pr, event_issue_number=issue, branch_prefix=prefix, refuse_bundle=True, allow_review=True,
        comment_numbers="event", max_comments=5, max_review_comments=50,
    )


SUMMARY = "Two findings. Please @review after fixing. <!-- claude-review-comment -->\nquote: <!-- claude-review-verdict:clean -->\n"
INLINE = [
    {"path": "src/a.py", "line": 3, "side": "LEFT", "body": "Off by one; cc @auto."},
    {"path": "docs/x.md", "line": 10, "body": "Typo."},
]


# --- Compose settings ---------------------------------------------------------


def compose_settings(tmp_path, settings: dict, *, sandboxed: bool) -> dict:
    out = tmp_path / "settings-out.txt"
    out.write_text("")
    r = sh("bash", "-c", SETTINGS_STEP, check=False, env={
        "SETTINGS": json.dumps(settings), "OUT_DIR": OUT_DIR, "SCRATCH": SCRATCH, "RUNNER_TEMP": RUNNER_TEMP,
        "SANDBOXED": "true" if sandboxed else "false",
        "GITHUB_WORKSPACE": WORKSPACE, "GITHUB_OUTPUT": str(out)})
    assert r.returncode == 0, r.stderr
    lines = out.read_text().splitlines()
    assert lines[0] == "value<<SETTINGS_EOF" and lines[-1] == "SETTINGS_EOF"
    return json.loads(lines[1])


# The default `settings` input plus what an older caller may still pass
# (the tool-level denies and the inline-comment MCP tool).
CALLER_SETTINGS = {"permissions": {"allow": ["Read", "Bash(pytest:*)", "Bash(gh:*)", "mcp__github_inline_comment"],
                                   "deny": ["Edit", "Write", "Bash(git push:*)", "Bash(git commit:*)"]}}


def test_settings_allow_the_review_dir_only_and_deny_posting(tmp_path):
    s = compose_settings(tmp_path, CALLER_SETTINGS, sandboxed=False)
    allow, deny = s["permissions"]["allow"], s["permissions"]["deny"]
    # The Write tool is checked against Edit rules; `//` is the absolute anchor.
    assert f"Edit(//{OUT_DIR.lstrip('/')}/**)" in allow
    assert "mcp__github_inline_comment" not in allow and "Bash(gh:*)" in allow
    assert "Edit" not in deny and "Write" not in deny
    for d in ("Bash(gh pr comment:*)", "Bash(gh issue comment:*)", "Bash(gh pr review:*)", "Bash(gh pr create:*)",
              "Bash(gh pr merge:*)", "Bash(gh issue create:*)", "Bash(git push:*)", "Bash(git commit:*)", "mcp__github_inline_comment"):
        assert d in deny, d
    assert "sandbox" not in s  # no overlay on a same-repo head


def test_sandboxed_settings_deny_subprocess_writes_to_the_review_dir(tmp_path):
    # Review round 1 of #116: the Edit allow rule and --add-dir both widen
    # what sandboxed COMMANDS may write, like allowWrite — so on a fork head
    # or an External proxy a build hook could write the review files. The
    # sandbox's denyWrite closes that; the deny holds inside the wider allow
    # and does not govern the in-process Write tool. Caller entries survive.
    caller = json.loads(json.dumps(CALLER_SETTINGS))
    caller["sandbox"] = {"filesystem": {"denyWrite": ["~/.ssh"], "allowWrite": ["~/.kube"]},
                         "credentials": {"files": [{"path": "~/.npmrc", "mode": "deny"}]},
                         "network": {"tlsTerminate": {"enabled": True}}}
    caller["claudeMdExcludes"] = ["**/other-team/CLAUDE.md"]
    s = compose_settings(tmp_path, caller, sandboxed=True)
    # Claude Security 4628445: the checkout is read-only to sandboxed
    # commands — the strip's paths cannot be re-planted at any depth — and
    # the scratch copy is the one writable tree; a caller's allowWrite would
    # only widen and is not carried over.
    assert s["sandbox"]["filesystem"]["denyWrite"] == ["~/.ssh", OUT_DIR, WORKSPACE]
    assert s["sandbox"]["filesystem"]["allowWrite"] == [SCRATCH]
    assert s["sandbox"]["enabled"] is True and s["sandbox"]["allowUnsandboxedCommands"] is False
    assert s["sandbox"]["excludedCommands"] == ["gh *"]
    # The caller's entry first; no `.git/config` mask any more (the
    # launcher's wrapper resets the origin URL and refuses a launch with the
    # App token there), the gh and gitconfig denies stay.
    assert s["sandbox"]["credentials"]["files"] == [{"path": "~/.npmrc", "mode": "deny"},
                                                     {"path": "~/.config/gh", "mode": "deny"},
                                                     {"path": "~/.gitconfig", "mode": "deny"}]
    assert "tlsTerminate" not in s["sandbox"]["network"]
    assert f"Edit(//{OUT_DIR.lstrip('/')}/**)" in s["permissions"]["allow"]
    # The load side: no CLAUDE.md under the checkout or the runner temp is
    # ever loaded (caller entries kept), and AGENTS.md is not read as
    # project instructions.
    assert s["claudeMdExcludes"] == ["**/other-team/CLAUDE.md", f"{WORKSPACE}/**", f"{RUNNER_TEMP}/**"]
    assert s["pluginConfigs"] == {"agents-md@builtin": {"options": {"instructionFiles": "claude-md"}}}
    assert s["disableAllHooks"] is True
    # Without caller sandbox settings the lists are the overlay's alone.
    s = compose_settings(tmp_path, CALLER_SETTINGS, sandboxed=True)
    assert s["sandbox"]["filesystem"]["denyWrite"] == [OUT_DIR, WORKSPACE]
    assert s["sandbox"]["filesystem"]["allowWrite"] == [SCRATCH]
    assert s["claudeMdExcludes"] == [f"{WORKSPACE}/**", f"{RUNNER_TEMP}/**"]
    # Same-repo heads get none of it.
    s = compose_settings(tmp_path, caller, sandboxed=False)
    assert "claudeMdExcludes" not in s or s["claudeMdExcludes"] == ["**/other-team/CLAUDE.md"]
    assert "pluginConfigs" not in s and s["sandbox"]["filesystem"]["allowWrite"] == ["~/.kube"]


# --- Prepare Claude review for landing --------------------------------------


def test_prep_lands_the_summary_verdict_and_inline_comments(tmp_path):
    o, landing, inline_list, r = prep(tmp_path, summary=SUMMARY, verdict="clean\n", inline=INLINE)
    assert o == {"landed": "true", "verdict": "clean"}
    body = (landing / "claude-review.md").read_text()
    # De-fanged here as well as in land: no live trigger or marker leaves this job.
    assert "@review" not in body and "claude-review-" not in body
    assert "`review`" in body and "claude-review comment" in body and "claude-review verdict:clean" in body
    assert json.loads(inline_list.read_text()) == [
        {"path": "src/a.py", "line": 3, "side": "LEFT", "body_file": "claude-inline-0.md"},
        {"path": "docs/x.md", "line": 10, "side": "RIGHT", "body_file": "claude-inline-1.md"},
    ]
    assert (landing / "claude-inline-0.md").read_text() == "Off by one; cc `auto`.\n"
    assert (landing / "claude-inline-1.md").read_text() == "Typo.\n"
    assert "inline comments: 2" in r.stdout


@pytest.mark.parametrize("text,expected", [(None, "suggestions"), ("Clean \n", "clean"), ("`suggestions`\n", "suggestions"),
                                           ("maybe\n", "suggestions"), ("", "suggestions")])
def test_prep_reads_the_verdict_leniently_and_defaults_to_suggestions(tmp_path, text, expected):
    # A fix round over a real review, never a false clean.
    o, _, _, r = prep(tmp_path, summary="ok\n", verdict=text)
    assert o["verdict"] == expected
    if expected == "suggestions" and text != "`suggestions`\n":
        assert "not clean or suggestions" in r.stdout


def test_prep_drops_malformed_inline_entries_with_a_note(tmp_path):
    bad = INLINE + [{"path": "a", "line": 0, "body": "x"}, {"path": "a", "line": 2}, "junk", {"path": "a", "line": 2, "side": "TOP", "body": "x"}]
    o, landing, inline_list, r = prep(tmp_path, summary="ok\n", verdict="clean", inline=bad)
    assert len(json.loads(inline_list.read_text())) == 2
    assert not (landing / "claude-inline-2.md").exists()
    assert "_[4 inline comment(s) were dropped: not of the form {path, line, side?, body}]_" in (landing / "claude-review.md").read_text()
    assert o["verdict"] == "clean"


def test_prep_drops_every_inline_comment_when_the_file_is_not_an_array(tmp_path):
    o, landing, inline_list, r = prep(tmp_path, summary="ok\n", verdict="clean", inline='{"path": "a"}')
    assert not inline_list.exists()
    assert "were dropped: inline.json was not a JSON array" in (landing / "claude-review.md").read_text()
    sub = tmp_path / "b"
    sub.mkdir()
    o, landing, inline_list, r = prep(sub, summary="ok\n", verdict="clean", inline="not json")
    assert not inline_list.exists()
    assert "not a JSON array" in r.stdout


def test_prep_without_a_summary_lands_nothing(tmp_path):
    o, landing, inline_list, r = prep(tmp_path, verdict="clean", inline=INLINE)
    assert o == {"landed": "false"}
    assert not (landing / "claude-review.md").exists() and not inline_list.exists()
    assert "no review to land" in r.stdout


def test_prep_external_mode_lands_the_summary_only(tmp_path):
    # One comment for the human on the proxy issue: no verdict, no inline comments.
    o, landing, inline_list, _ = prep(tmp_path, mode="external", summary="looks ready\n", verdict="clean", inline=INLINE)
    assert o == {"landed": "true", "verdict": ""}
    assert (landing / "claude-review.md").read_text() == "looks ready\n"
    assert not inline_list.exists() and not (landing / "claude-inline-0.md").exists()


def test_prep_caps_the_summary_under_lands_recap(tmp_path):
    # 59,000 here so land's 60,000 re-cap and the marker it appends still fit.
    o, landing, _, _ = prep(tmp_path, summary="x" * 70000, verdict="clean")
    body = (landing / "claude-review.md").read_text()
    assert len(body) < 60000 and body.endswith("_[truncated: the review summary exceeded the comment size cap]_\n")


# --- Compose landing manifest ------------------------------------------------


def test_compose_claude_review_in_pr_mode_validates_end_to_end(tmp_path):
    prep(tmp_path, summary=SUMMARY, verdict="clean", inline=INLINE)
    m, extra = compose(tmp_path)
    assert m == {
        "comments": [{"number": 42, "body_file": "claude-review.md", "review": True}],
        "review_verdict": "clean",
        "review_comments": [
            {"path": "src/a.py", "line": 3, "side": "LEFT", "body_file": "claude-inline-0.md"},
            {"path": "docs/x.md", "line": 10, "side": "RIGHT", "body_file": "claude-inline-1.md"},
        ],
    }
    # Through emit-landing (read-only, as the workflow runs it) and the land
    # job's validator under refuse-bundle: the manifest the land job posts.
    r, landing, _ = run_emit_landing(tmp_path, cwd=tmp_path, read_only=True, start_sha=SHA, extra=extra,
                                     pr_number="42")
    assert r.returncode == 0, r.stderr
    assert validate(landing, pr="42") == []
    manifest = json.loads((landing / "manifest.json").read_text())
    assert manifest["review_comments"] == m["review_comments"] and manifest["comments"][0]["review"] is True


def test_compose_claude_review_without_inline_comments(tmp_path):
    prep(tmp_path, summary="fine\n", verdict="clean")
    m, extra = compose(tmp_path)
    assert "review_comments" not in m
    assert m["comments"] == [{"number": 42, "body_file": "claude-review.md", "review": True}] and m["review_verdict"] == "clean"
    r, landing, _ = run_emit_landing(tmp_path, cwd=tmp_path, read_only=True, start_sha=SHA, extra=extra,
                                     pr_number="42")
    assert validate(landing, pr="42") == []


def test_compose_external_mode_is_one_plain_comment_on_the_proxy_issue(tmp_path):
    prep(tmp_path, mode="external", summary="looks ready\n")
    m, extra = compose(tmp_path, mode="external", verdict="")
    assert m == {"comments": [{"number": 42, "body_file": "claude-review.md"}], "stage": "Review"}
    r, landing, _ = run_emit_landing(tmp_path, cwd=tmp_path, read_only=True, start_sha=SHA, extra=extra,
                                     branch="review/external-42", pr_number="", issue_number="42")
    assert validate(landing, branch="review/external-42", pr="", issue="42", prefix="review/external-42") == []


def test_compose_lands_no_review_when_the_prep_step_found_no_summary(tmp_path):
    prep(tmp_path, verdict="clean")
    m, _ = compose(tmp_path, landed="false", verdict="")
    assert "comments" not in m and "review_verdict" not in m and "review_comments" not in m
    # The stage is the land job's (stage-override) on a PR.
    assert m == {}


def test_compose_codex_review_is_flagged_and_moves_no_stage(tmp_path):
    # The codex job's own composer: its review is flagged `review` like the
    # Claude review, so land appends the review-comment marker after the
    # de-fang, and the stage is the land job's (step 3).
    landing = tmp_path / "landing"
    landing.mkdir()
    (landing / "codex-review.md").write_text("codex says\n\n🤖 Reviewed by Codex\n")
    m, extra = compose(tmp_path, engine="codex", claudepost="skipped", landed="", verdict="", codexpost="success",
                       codex_verdict="suggestions", claude_outcome="skipped")
    assert m == {"comments": [{"number": 42, "body_file": "codex-review.md", "review": True}], "review_verdict": "suggestions"}
    r, out, _ = run_emit_landing(tmp_path, cwd=tmp_path, read_only=True, start_sha=SHA, extra=extra, pr_number="42")
    assert r.returncode == 0, r.stderr
    assert validate(out, pr="42") == []
    # A failed codex step: no review, the error only.
    (tmp_path / "agent-error.md").write_text("⚠️ codex failed")
    m, _ = compose(tmp_path, engine="codex", codexpost="skipped")
    assert m == {"error": {"message": "⚠️ codex failed", "fail_run": True}}


def test_codex_prep_writes_no_footer(tmp_path):
    # The old `engine: codex` footer anchored a review round before step 3;
    # no review carries it now, and a review quoting it has it split (by
    # land).
    out = tmp_path / "codex-review.json"
    out.write_text(json.dumps({"verdict": "clean", "review": "Looks fine; cc @review.\n"}))
    landing = tmp_path / "landing"
    gh_out = tmp_path / "out.txt"
    gh_out.write_text("")
    r = sh("bash", "-eo", "pipefail", "-c", PREP_CODEX, check=False,
           env={"OUT": str(out), "DIR": str(landing), "GITHUB_OUTPUT": str(gh_out)})
    assert r.returncode == 0, r.stderr
    body = (landing / "codex-review.md").read_text()
    assert body.endswith("\n\n🤖 Reviewed by Codex\n")
    assert "engine: codex" not in body and "@review" not in body
    assert outputs(gh_out) == {"verdict": "clean"}


def test_prep_caps_inline_comments_at_the_land_jobs_limit(tmp_path):
    # The land job refuses more than 50 (max-review-comments); the prep keeps
    # the first 50 and says so, so a long review still lands.
    many = [{"path": f"src/f{i}.py", "line": i + 1, "body": f"finding {i}"} for i in range(55)]
    o, landing, inline_list, r = prep(tmp_path, summary="ok\n", verdict="suggestions", inline=many)
    kept = json.loads(inline_list.read_text())
    assert len(kept) == 50 and kept[-1]["path"] == "src/f49.py"
    assert not (landing / "claude-inline-50.md").exists()
    assert "_[5 inline comment(s) over the 50-comment cap were dropped]_" in (landing / "claude-review.md").read_text()
    m, extra = compose(tmp_path, verdict="suggestions")
    r, out, _ = run_emit_landing(tmp_path, cwd=tmp_path, read_only=True, start_sha=SHA, extra=extra, pr_number="42")
    assert validate(out, pr="42") == []


def test_reviewer_land_inputs_refuse_the_forged_probe_manifest(tmp_path):
    # One review comment plus 51 comment_on entries on another issue: the
    # manifest the design's review probe showed passing before step 3.
    landing = tmp_path / "landing"
    landing.mkdir()
    (landing / "r.md").write_text("review\n")
    m = {"schema": 1, "repo": "meridianlabs-ai/agents", "run_id": 123, "branch": "claude/issue-81-review",
         "start_sha": SHA, "head_sha": SHA, "has_bundle": False, "pr_number": 42,
         "comments": [{"number": 42, "body_file": "r.md", "review": True}], "review_verdict": "clean",
         "issues": [{"repo": "meridianlabs-ai/agents", "title": "t", "body_file": "r.md", "comment_on": 999}] * 51}
    (landing / "manifest.json").write_text(json.dumps(m))
    errors = validate(landing, pr="42")
    assert any("not in the allowed issue repos" in e for e in errors)


# --- the reads that moved out of the review jobs (step 3) --------------------

# `gh` for the lifted reads: the PR body names a Fixes ref, the issue has
# an assignee (or only the machine account), the labels come from $LABELS.
READ_STUB = r"""
gh() {
  echo "$*" >>"$STATE/calls"
  case "$*" in
    "pr view "*"--json body"*) printf '%s' "$PR_BODY" ;;
    "pr view "*"--json headRefName"*) printf '%s' "$HEAD" ;;
    "api repos/"*"/issues/7") printf '%s' "$ISSUE_JSON" ;;
    "api repos/"*"/labels "*) printf '%s' "$LABELS" | jq '[.[] | select(.name == "auto")] | length' ;;
    *) echo "unexpected gh $*" >&2; return 2 ;;
  esac
}
"""


def run_read(tmp_path, script, **env):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    out = tmp_path / "read-out.txt"
    out.write_text("")
    full = {"STATE": str(state), "GITHUB_OUTPUT": str(out), "REPO": "o/r", "NUM": "42", "PATH": os.environ["PATH"],
            "TRUSTED_LOGINS": "i-am-marvin,meridian-marvin[bot]", "IS_PR": "true", "ANCHOR_REPO": "",
            "PR_BODY": "", "HEAD": "", "ISSUE_JSON": "{}", "LABELS": "[]", **env}
    r = sh("bash", "-eo", "pipefail", "-c", READ_STUB + script, check=False, env=full)
    assert r.returncode == 0, r.stderr
    return outputs(out)


def test_the_gate_derives_the_responsible_human_before_any_agent_runs(tmp_path):
    issue = json.dumps({"assignees": [{"login": "i-am-marvin", "type": "User"}, {"login": "alice", "type": "User"}],
                        "user": {"login": "bob", "type": "User"}})
    o = run_read(tmp_path, WHO, PR_BODY="Fixes #7\n", ISSUE_JSON=issue)
    assert o == {"mention": "alice"}
    # No human on the issue: nobody.
    issue = json.dumps({"assignees": [], "user": {"login": "meridian-marvin[bot]", "type": "Bot"}})
    assert run_read(tmp_path, WHO, PR_BODY="Fixes #7", ISSUE_JSON=issue) == {"mention": ""}


def test_mention_comes_from_the_gate_and_no_review_job_outputs_it():
    gate = job_block(TEXT, "gate")
    assert "      mention: ${{ steps.who.outputs.mention }}\n" in gate
    assert "        if: steps.trig.outputs.ok == 'true'\n" in gate[gate.index("        id: who"):]
    for job in ("review", "review-codex"):
        block = job_block(TEXT, job)
        assert "id: who" not in block and "id: autoloop" not in block and "mention:" not in block.split("    steps:")[0]
        assert "MENTION: ${{ needs.gate.outputs.mention }}" in block
        assert "steps.who." not in block and "ENGAGED" not in block
    assert "MENTION: ${{ needs.gate.outputs.mention }}" in job_block(TEXT, "land")
    assert "needs.review.outputs.mention" not in TEXT


@pytest.mark.parametrize("labels,engaged", [("[]", "false"), ('[{"name": "auto"}]', "true"), ('[{"name": "autox"}]', "false")])
def test_the_land_job_reads_loop_ownership_itself(tmp_path, labels, engaged):
    assert run_read(tmp_path, AUTOLOOP, LABELS=labels) == {"engaged": engaged}


def test_the_land_job_passes_the_strict_values_and_its_own_stage():
    land = job_block(TEXT, "land")
    step = land[land.index("      - name: Land\n"):]
    for line in ('comment-numbers: event', 'max-comments: "5"', 'max-review-comments: "50"', 'allowed-issue-repos: ""',
                 "stage-override: ${{ needs.gate.outputs.mode != 'external' && ((needs.gate.outputs.ack == 'true' && "
                 "steps.autoloop.outputs.engaged != 'true') && 'Review' || 'none') || '' }}"):
        assert "          " + line + "\n" in step, line
    # The read runs before the Land step, on the job token.
    assert land.index("id: autoloop") < land.index("      - name: Land\n")
    assert "GH_TOKEN: ${{ github.token }}" in land[land.index("id: autoloop"):land.index("      - name: Land\n")]
