"""Tests for the `compose-settings` composite and its reader,
`.github/scripts/read_settings.py` (Claude Security finding 4773338).

The three `Compose … settings` steps (claude.yml's agent job, both loops'
fix jobs) accept the caller's `settings` input as inline JSON or a path.
The path is read as the runner, from the checkout, which on a PR run is
the PR head. They used `[ -f ]` and `cat`, so a symlink committed at that
path redirected the read to any runner-readable file, and its content
became the agent's settings. The composite's step is lifted from its
action.yml and run against a symlinked file, a symlinked parent, a `..`
escape, a non-regular file and an oversized one (each refused with no
output), a valid in-tree path and inline JSON. A structural check pins
the three workflows to the composite and their deny lists.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github" / "actions" / "compose-settings" / "action.yml"
SCRIPT = ROOT / ".github" / "scripts" / "read_settings.py"
WORKFLOWS = ROOT / ".github" / "workflows"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_helpers import job_block, lift_run, step_block  # noqa: E402

STEP = lift_run(ACTION.read_text(), "    - id: compose")

LOOP_DENY = ["Bash(git push:*)", "Bash(gh pr comment:*)", "Bash(gh issue comment:*)", "Bash(gh pr review:*)",
             "Bash(gh pr create:*)", "Bash(gh pr merge:*)", "Bash(gh issue create:*)"]
DEV_DENY = LOOP_DENY[:1] + ["Bash(*/scripts/git-push.sh *)"] + LOOP_DENY[1:]

CALLER = {"permissions": {"allow": ["Read", "Bash(pytest:*)", "Bash(git push:*)"], "deny": ["WebFetch"]},
          "env": {"X": "1"}}
# What the runner could read outside the workspace: a JSON object, so the
# old `cat` would have made it the agent's settings.
OUTSIDE = {"permissions": {"allow": ["Bash(*)"]}, "secret": "runner-only"}


@pytest.fixture
def ws(tmp_path):
    ws = tmp_path / "work" / "repo"
    (ws / ".github").mkdir(parents=True)
    outside = tmp_path / "runner"
    outside.mkdir()
    (outside / ".credentials").write_text(json.dumps(OUTSIDE))
    return {"ws": ws, "outside": outside, "tmp": tmp_path}


def compose(ws, settings, deny=LOOP_DENY):
    out = ws["tmp"] / "github-output"
    out.write_text("")
    env = {**os.environ, "SETTINGS": settings, "DENY": json.dumps(deny), "SCRIPT": str(SCRIPT),
           "GITHUB_WORKSPACE": str(ws["ws"]), "GITHUB_OUTPUT": str(out)}
    r = subprocess.run(["bash", "-c", STEP], cwd=ws["ws"], text=True, capture_output=True, env=env,
                       timeout=30, check=False)
    return r, out.read_text()


def value(output: str) -> dict:
    lines = output.splitlines()
    assert lines[0] == "value<<SETTINGS_EOF" and lines[-1] == "SETTINGS_EOF" and len(lines) == 3, output
    return json.loads(lines[1])


def refused(r, output, why):
    assert r.returncode != 0, r.stdout + r.stderr
    assert output == "", output
    log = r.stdout + r.stderr
    assert "::error::" in log and why in log, log
    assert "runner-only" not in log


def assert_merged(s, deny=LOOP_DENY):
    assert s["permissions"]["allow"] == ["Read", "Bash(pytest:*)"]
    assert s["permissions"]["deny"] == sorted(set(["WebFetch"] + deny))
    assert s["env"] == {"X": "1"}


# --- accepted ----------------------------------------------------------------


def test_inline_json_is_merged_with_the_deny_overlay(ws):
    r, out = compose(ws, json.dumps(CALLER))
    assert r.returncode == 0, r.stderr
    assert_merged(value(out))
    assert "reading" not in r.stdout


def test_empty_input_is_an_empty_object_plus_the_overlay(ws):
    r, out = compose(ws, "")
    assert r.returncode == 0, r.stderr
    assert value(out) == {"permissions": {"allow": [], "deny": sorted(LOOP_DENY)}}


@pytest.mark.parametrize("form", ["relative", "dot-relative", "absolute"])
def test_valid_in_tree_path_is_read_and_merged(ws, form):
    f = ws["ws"] / ".github" / "claude-settings.json"
    f.write_text(json.dumps(CALLER, indent=2) + "\n")
    path = {"relative": ".github/claude-settings.json", "dot-relative": "./.github/claude-settings.json",
            "absolute": str(f)}[form]
    r, out = compose(ws, path, deny=DEV_DENY)
    assert r.returncode == 0, r.stderr
    assert_merged(value(out), deny=DEV_DENY)
    assert f"reading {path}" in r.stdout


def test_file_at_the_size_cap_is_read(ws):
    import importlib.util
    spec = importlib.util.spec_from_file_location("read_settings", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    body = json.dumps({"pad": ""})
    body = json.dumps({"pad": "x" * (mod.MAX_BYTES - len(body))})
    assert len(body) == mod.MAX_BYTES
    (ws["ws"] / "s.json").write_text(body)
    r, out = compose(ws, "s.json")
    assert r.returncode == 0, r.stderr
    assert len(value(out)["pad"]) > 0


# --- refused -----------------------------------------------------------------


def test_symlink_to_a_json_object_outside_the_tree_is_refused(ws):
    (ws["ws"] / ".github" / "claude-settings.json").symlink_to(ws["outside"] / ".credentials")
    r, out = compose(ws, ".github/claude-settings.json")
    refused(r, out, "is a symlink")


def test_symlink_to_an_in_tree_file_is_refused_too(ws):
    (ws["ws"] / "real.json").write_text(json.dumps(CALLER))
    (ws["ws"] / ".github" / "claude-settings.json").symlink_to("../real.json")
    r, out = compose(ws, ".github/claude-settings.json")
    refused(r, out, "is a symlink")


def test_symlinked_parent_directory_is_refused(ws):
    (ws["outside"] / "claude-settings.json").write_text(json.dumps(OUTSIDE))
    (ws["ws"] / "cfg").symlink_to(ws["outside"], target_is_directory=True)
    r, out = compose(ws, "cfg/claude-settings.json")
    refused(r, out, "has a symlink at cfg")


def test_symlinked_deeper_parent_directory_is_refused(ws):
    (ws["outside"] / "claude-settings.json").write_text(json.dumps(OUTSIDE))
    (ws["ws"] / ".github" / "sub").symlink_to(ws["outside"], target_is_directory=True)
    r, out = compose(ws, ".github/sub/claude-settings.json")
    refused(r, out, "has a symlink at .github/sub")


@pytest.mark.parametrize("path", ["../../runner/.credentials", ".github/../../../runner/.credentials",
                                  ".github/.."])
def test_dotdot_escape_is_refused(ws, path):
    r, out = compose(ws, path)
    refused(r, out, "'..' component")


def test_absolute_path_outside_the_workspace_is_refused(ws):
    r, out = compose(ws, str(ws["outside"] / ".credentials"))
    refused(r, out, "not a file inside the workspace")


def test_fifo_is_refused_without_blocking(ws):
    os.mkfifo(ws["ws"] / ".github" / "claude-settings.json")
    r, out = compose(ws, ".github/claude-settings.json")
    refused(r, out, "not a regular file")


def test_directory_is_refused(ws):
    (ws["ws"] / ".github" / "claude-settings.json").mkdir()
    r, out = compose(ws, ".github/claude-settings.json")
    refused(r, out, "not a regular file")


def test_the_workspace_itself_is_refused(ws):
    r, out = compose(ws, ".")
    refused(r, out, "not a file inside the workspace")


def test_oversized_file_is_refused(ws):
    (ws["ws"] / "big.json").write_text(json.dumps({"pad": "x" * (256 * 1024)}))
    r, out = compose(ws, "big.json")
    refused(r, out, "larger than 262144 bytes")


def test_non_utf8_file_is_refused(ws):
    (ws["ws"] / "bin.json").write_bytes(b'{"a": "\xff"}')
    r, out = compose(ws, "bin.json")
    refused(r, out, "not UTF-8")


def test_missing_file_is_refused(ws):
    r, out = compose(ws, ".github/nope.json")
    refused(r, out, "does not exist")


def test_file_under_a_non_directory_is_refused(ws):
    (ws["ws"] / "plain").write_text("{}")
    r, out = compose(ws, "plain/x.json")
    refused(r, out, "non-directory at plain")


@pytest.mark.parametrize("body", ['["not", "an", "object"]', "not json", ""])
def test_in_tree_file_that_is_not_a_json_object_is_refused(ws, body):
    (ws["ws"] / "s.json").write_text(body)
    r, out = compose(ws, "s.json")
    refused(r, out, "neither a JSON object nor a path to a file holding one")


def test_inline_json_that_is_not_an_object_is_refused(ws):
    r, out = compose(ws, '["Read"]')
    refused(r, out, "does not exist")


# --- the three workflows -----------------------------------------------------


@pytest.mark.parametrize("wf,job,sid,deny", [
    ("claude.yml", "agent", "agentsettings", DEV_DENY),
    ("claude-auto.yml", "fix", "fixsettings", LOOP_DENY),
    ("claude-auto-review.yml", "fix", "fixsettings", LOOP_DENY),
])
def test_workflows_compose_settings_through_the_composite(wf, job, sid, deny):
    text = (WORKFLOWS / wf).read_text()
    block = step_block(job_block(text, job), sid)
    assert "        uses: meridianlabs-ai/agents/.github/actions/compose-settings@main\n" in block
    assert "          settings: ${{ inputs.settings }}\n" in block
    m = re.search(r"^          deny: '(.*)'$", block, re.M)
    assert m and json.loads(m.group(1)) == deny
    assert "run:" not in block
    # No other step reads the settings input's file as the runner.
    assert 'cat "$SETTINGS"' not in text and '[ -f "$SETTINGS" ]' not in text
    # The step outcome and output the job reads keep their names.
    assert f"steps.{sid}.outputs.value" in text and f"steps.{sid}.outcome" in text


def test_composite_reads_the_path_only_through_the_reader():
    text = ACTION.read_text()
    assert "SCRIPT: ${{ github.action_path }}/../../scripts/read_settings.py" in text
    assert 'SETTINGS=$(python3 "$SCRIPT" "$SETTINGS")' in STEP
    assert "cat " not in STEP and "[ -f" not in STEP
