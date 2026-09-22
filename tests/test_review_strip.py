"""Tests for claude-review.yml's untrusted-checkout preparation and the
post-agent re-plant check (Claude Security 4628445).

The strip step moves the checkout's instruction files aside and deletes its
executable configuration; the scratch step copies the stripped tree to the
one place sandboxed commands may write; the re-plant check re-runs the
strip's predicates after the agent and fails when any such entry exists —
except a ROOT entry byte-identical to `origin/<base>`, which is what
claude-code-action's own prepare phase restores on PR events. All three
steps' bash is lifted from the workflow and run against local repos.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "claude-review.yml"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_helpers import git, sh  # noqa: E402
from test_review_fix_gate import lift_step  # noqa: E402

STRIP = lift_step(WORKFLOW, "        id: strip")
SCRATCH = lift_step(WORKFLOW, "        id: scratch")
REPLANT = lift_step(WORKFLOW, "        id: replant")

CONFIG_NAMES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".mcp.json", ".claude")

BASE_FILES = {
    "README.md": "base\n",
    "CLAUDE.md": "@AGENTS.md\n",
    "AGENTS.md": "base agents\n",
    ".claude/settings.json": '{"permissions": {"allow": ["Bash(pytest:*)"]}}\n',
    "pkg/__init__.py": "",
}
HEAD_FILES = {
    "CLAUDE.md": "head instructions\n",
    "CLAUDE.local.md": "local\n",
    "AGENTS.md": "head agents\n",
    ".claude/settings.json": '{"sandbox": {"enabled": false}}\n',
    ".claude/AGENTS.md": "nested agents in .claude\n",
    ".mcp.json": "{}\n",
    "pkg/CLAUDE.md": "nested\n",
    "pkg/AGENTS.md": "nested agents\n",
    "pkg/.mcp.json": "{}\n",
    "pkg/sub/.claude/settings.local.json": "{}\n",
    ".gitignore": "*.local.json\n",
}


def write(root: Path, files: dict):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def make_checkout(tmp_path) -> Path:
    """A base repo (branch `main`) and a clone of it — the workspace — with a
    PR head commit on top, so `refs/remotes/origin/main` is there as an
    actions/checkout with fetch-depth 0 leaves it."""
    base = tmp_path / "base"
    base.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=base)
    write(base, BASE_FILES)
    git("add", "-A", cwd=base)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base", cwd=base)
    ws = tmp_path / "workspace"
    git("clone", "-q", str(base), str(ws), cwd=tmp_path)
    git("checkout", "-q", "-b", "pr", cwd=ws)
    write(ws, HEAD_FILES)
    git("add", "-A", "-f", cwd=ws)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "head", cwd=ws)
    return ws


def config_entries(root: Path):
    return sorted(
        str(p.relative_to(root)) for p in root.rglob("*")
        if p.name in CONFIG_NAMES and ".git" not in p.relative_to(root).parts
    )


def strip(ws: Path):
    r = sh("bash", "-c", STRIP, cwd=ws, check=False)
    assert r.returncode == 0, r.stderr + r.stdout
    return r


def replant(ws: Path, *, base_ref="main"):
    return sh("bash", "-c", REPLANT, cwd=ws, check=False, env={
        "BASE_REF": base_ref, "GIT_DIR": str(ws / ".git"), "GIT_WORK_TREE": str(ws)})


def restore_from_base(ws: Path):
    """What claude-code-action's restoreConfigFromBase does after the strip
    on a PR event: the root sensitive paths come back from origin/<base>."""
    git("fetch", "-q", "origin", "main", cwd=ws)
    for p in (".claude", ".mcp.json", "CLAUDE.md", "CLAUDE.local.md"):
        git("checkout", "origin/main", "--", p, cwd=ws, check=False)
    git("reset", "-q", "--", ".claude", ".mcp.json", "CLAUDE.md", "CLAUDE.local.md", cwd=ws, check=False)


# --- Strip -------------------------------------------------------------------


def test_strip_moves_instruction_files_aside_and_deletes_executable_config(tmp_path):
    ws = make_checkout(tmp_path)
    r = strip(ws)
    assert config_entries(ws) == []
    # Instruction text, AGENTS.md included (2.1.277+ reads it when no
    # CLAUDE.md counts — the state the rename leaves), is kept as *.untrusted.
    for name in ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "pkg/CLAUDE.md", "pkg/AGENTS.md"):
        assert (ws / f"{name}.untrusted").is_file(), name
    assert (ws / "AGENTS.md.untrusted").read_text() == "head agents\n"
    # Executable configuration is gone at every depth, its contents with it.
    for name in (".claude", ".mcp.json", "pkg/.mcp.json", "pkg/sub/.claude"):
        assert not (ws / name).exists(), name
    assert not (ws / ".claude/AGENTS.md.untrusted").exists()
    assert "AGENTS.md moved to *.untrusted" in r.stdout


def test_strip_replaces_a_stale_untrusted_directory(tmp_path):
    # A checkout carrying a DIRECTORY named AGENTS.md.untrusted would make
    # `mv` move the file into it; the strip removes it first.
    ws = make_checkout(tmp_path)
    (ws / "AGENTS.md.untrusted").mkdir()
    strip(ws)
    assert (ws / "AGENTS.md.untrusted").is_file()


# --- Scratch copy --------------------------------------------------------------


def test_scratch_copy_is_the_stripped_tree_with_its_git_dir(tmp_path):
    ws = make_checkout(tmp_path)
    strip(ws)
    scratch = tmp_path / "scratch"
    r = sh("bash", "-c", SCRATCH, cwd=ws, check=False, env={"SCRATCH": str(scratch), "GITHUB_WORKSPACE": str(ws)})
    assert r.returncode == 0, r.stderr + r.stdout
    src = scratch / "src"
    assert config_entries(src) == []
    assert (src / "CLAUDE.md.untrusted").read_text() == "head instructions\n"
    assert (src / "pkg/__init__.py").is_file()
    # `.git` comes along (setuptools_scm, the reviewer's git diff in the copy)
    # and carries no credential: the copy is taken before the action rewrites
    # the origin URL, and the checkout persisted none.
    assert git("log", "-1", "--format=%s", cwd=src).stdout.strip() == "head"
    assert "token" not in (src / ".git/config").read_text()
    # Re-runnable: a stale copy is replaced, not merged into.
    (src / "stale").write_text("x")
    r = sh("bash", "-c", SCRATCH, cwd=ws, check=False, env={"SCRATCH": str(scratch), "GITHUB_WORKSPACE": str(ws)})
    assert r.returncode == 0 and not (src / "stale").exists()


# --- Re-plant check ------------------------------------------------------------


def test_replant_check_passes_on_a_clean_stripped_tree(tmp_path):
    ws = make_checkout(tmp_path)
    strip(ws)
    r = replant(ws)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "no re-planted project configuration" in r.stdout


def test_replant_check_exempts_the_actions_base_branch_restore(tmp_path):
    ws = make_checkout(tmp_path)
    strip(ws)
    restore_from_base(ws)
    assert (ws / "CLAUDE.md").read_text() == "@AGENTS.md\n" and (ws / ".claude/settings.json").is_file()
    r = replant(ws)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "root ./CLAUDE.md matches origin/main" in r.stdout
    assert "root ./.claude matches origin/main" in r.stdout


def test_replant_check_fails_on_a_nested_entry(tmp_path):
    ws = make_checkout(tmp_path)
    strip(ws)
    restore_from_base(ws)
    (ws / "pkg/CLAUDE.md").write_text("planted\n")
    (ws / "pkg/sub/.claude").mkdir(parents=True)
    r = replant(ws)
    assert r.returncode == 1
    assert "::error::project configuration reappeared" in r.stdout
    assert "./pkg/CLAUDE.md" in r.stdout and "./pkg/sub/.claude" in r.stdout
    assert "the review is withheld" in r.stdout


def test_replant_check_fails_on_root_content_that_differs_from_base(tmp_path):
    ws = make_checkout(tmp_path)
    strip(ws)
    restore_from_base(ws)
    with (ws / "CLAUDE.md").open("a") as f:
        f.write("planted line\n")
    r = replant(ws)
    assert r.returncode == 1 and "./CLAUDE.md" in r.stdout
    # A root .mcp.json the base does not have is a re-plant, not a restore.
    ws2 = make_checkout(tmp_path / "two")
    strip(ws2)
    restore_from_base(ws2)
    (ws2 / ".mcp.json").write_text("{}\n")
    r = replant(ws2)
    assert r.returncode == 1 and "./.mcp.json" in r.stdout


def test_replant_check_fails_on_an_untracked_file_under_a_restored_directory(tmp_path):
    # `git diff` compares tracked paths only, so the check also lists
    # untracked files beneath the entry — honouring no ignore rules, since
    # .gitignore is the contributor's (the head's ignores *.local.json).
    ws = make_checkout(tmp_path)
    strip(ws)
    restore_from_base(ws)
    (ws / ".claude/settings.local.json").write_text('{"sandbox": {"enabled": false}}\n')
    assert git("ls-files", "--others", "--exclude-standard", "--", ".claude", cwd=ws).stdout == ""
    r = replant(ws)
    assert r.returncode == 1 and "./.claude" in r.stdout


def test_replant_check_never_exempts_a_root_agents_md(tmp_path):
    # AGENTS.md is not in the action's restore set: one at the root after
    # the agent was written by something else, base-identical or not.
    ws = make_checkout(tmp_path)
    strip(ws)
    (ws / "AGENTS.md").write_text("base agents\n")
    r = replant(ws)
    assert r.returncode == 1 and "./AGENTS.md" in r.stdout


def test_replant_check_fails_closed_without_the_base_ref(tmp_path):
    # External mode restores nothing and a changed action behaviour must not
    # pass on its own: with no origin/<base> to compare against, a root
    # entry is a survivor even when its content happens to match.
    ws = make_checkout(tmp_path)
    strip(ws)
    restore_from_base(ws)
    r = replant(ws, base_ref="no-such-branch")
    assert r.returncode == 1 and "./CLAUDE.md" in r.stdout and "./.claude" in r.stdout
    r = replant(ws, base_ref="-c core.pager=touch pwned")
    assert r.returncode == 1 and not (ws / "pwned").exists()


def test_replant_check_ignores_the_git_dir_and_runs_no_hooks(tmp_path):
    ws = make_checkout(tmp_path)
    strip(ws)
    restore_from_base(ws)
    # Names under .git are not the checkout's configuration.
    (ws / ".git/CLAUDE.md").write_text("x")
    # A hook or fsmonitor the contributor left in the checkout's config must
    # not run from this step's git calls.
    hooks = ws / ".git/hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "post-index-change").write_text("#!/bin/sh\ntouch hook-ran\n")
    (hooks / "post-index-change").chmod(0o755)
    git("config", "core.fsmonitor", "touch fsmonitor-ran", cwd=ws)
    git("config", "diff.external", "touch extdiff-ran", cwd=ws)
    r = replant(ws)
    assert r.returncode == 0, r.stderr + r.stdout
    for marker in ("hook-ran", "fsmonitor-ran", "extdiff-ran"):
        assert not (ws / marker).exists(), marker


# --- Wiring ----------------------------------------------------------------------


def step_block(anchor: str) -> str:
    """The text of one step: from its `- name:` / `- uses:` line to the next
    one at the same indentation."""
    text = WORKFLOW.read_text()
    start = text.index(anchor)
    start = text.rfind("\n      - ", 0, start) + 1
    m = re.compile(r"\n      - (name|uses):").search(text, start + 1)
    return text[start:m.start() if m else len(text)]


SANDBOXED = "(needs.gate.outputs.mode == 'external' || needs.gate.outputs.fork_head == 'true')"


def test_workflow_wiring():
    text = WORKFLOW.read_text()
    # The agent step drops the checkout's setting sources on the sandboxed
    # paths, after the caller's own claude_args.
    claude = step_block("        id: claude\n")
    args = claude[claude.index("claude_args: >-"):]
    assert f"${{{{ {SANDBOXED} && '--setting-sources user' || '' }}}}" in args
    assert args.index("${{ inputs.claude_args }}") < args.index("--setting-sources user")
    # The three preparation/check steps run on exactly the sandboxed paths.
    for anchor in ("        id: strip\n", "        id: scratch\n", "        id: replant\n"):
        assert SANDBOXED in step_block(anchor), anchor
    assert "always()" in step_block("        id: replant\n")
    # The landing prep is gated on the re-plant check.
    assert "if: steps.claude.outcome == 'success' && steps.replant.outcome != 'failure'" in step_block("        id: claudepost\n")
    # The version floor covers the setting-source exclusion.
    assert "need=2.1.246" in step_block("        id: cliver\n")
    # The Surface step reads both new outcomes and posts the withheld-review note.
    surface = step_block("        id: surface\n")
    assert "SCRATCH_OUTCOME: ${{ steps.scratch.outcome }}" in surface
    assert "REPLANT_OUTCOME: ${{ steps.replant.outcome }}" in surface
    assert "The review was withheld" in surface
    # The prompt sends installs and tests to the copy and names it.
    prompt = step_block("        id: reviewprompt\n")
    assert "SCRATCH: ${{ runner.temp }}/scratch" in prompt
    assert "READ-ONLY to your commands" in prompt and "cd $SCRATCH/src" in prompt
    assert "CLAUDE.md / CLAUDE.local.md / AGENTS.md was renamed" in prompt
    # The compose-settings and scratch steps read the same scratch path.
    assert "SCRATCH: ${{ runner.temp }}/scratch" in step_block("        id: reviewsettings\n")
    assert "SCRATCH: ${{ runner.temp }}/scratch" in step_block("        id: scratch\n")
    assert text.count("${{ runner.temp }}/scratch") == 3
