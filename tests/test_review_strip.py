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

RESTORE_ROOTS = (".claude", ".mcp.json", "CLAUDE.md", "CLAUDE.local.md")  # claude-code-action's SENSITIVE_PATHS we strip

BASE_FILES = {
    "README.md": "base\n",
    "CLAUDE.md": "@AGENTS.md\n",
    "CLAUDE.local.md": "base local\n",
    "AGENTS.md": "base agents\n",
    ".mcp.json": '{"mcpServers": {}}\n',
    ".claude/settings.json": '{"permissions": {"allow": ["Bash(pytest:*)"]}}\n',
    ".claude/CLAUDE.md": "base .claude instructions\n",
    ".claude/CLAUDE.local.md": "base .claude local\n",
    ".claude/AGENTS.md": "base .claude agents\n",
    ".claude/.mcp.json": "{}\n",
    ".claude/rules/testing.md": "rules\n",
    ".claude/pkg/.claude/settings.json": "{}\n",
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


def make_checkout(tmp_path, *, base_files=BASE_FILES, head_files=HEAD_FILES, delete=()) -> Path:
    """A base repo (branch `main`, holding `base_files`) and a clone of it —
    the workspace — with a PR head commit on top (adding `head_files`,
    deleting `delete`), so `refs/remotes/origin/main` is there as an
    actions/checkout with fetch-depth 0 leaves it."""
    base = tmp_path / "base"
    base.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=base)
    write(base, base_files)
    git("add", "-A", cwd=base)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base", cwd=base)
    ws = tmp_path / "workspace"
    git("clone", "-q", str(base), str(ws), cwd=tmp_path)
    git("checkout", "-q", "-b", "pr", cwd=ws)
    for p in delete:
        git("rm", "-rq", "--", p, cwd=ws)
    write(ws, head_files)
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
    on a PR event: the root sensitive paths come back from origin/<base>,
    then the index is reset to HEAD (so a path the PR deleted is untracked)."""
    git("fetch", "-q", "origin", "main", cwd=ws)
    for p in RESTORE_ROOTS:
        git("checkout", "origin/main", "--", p, cwd=ws, check=False)
    git("reset", "-q", "--", *RESTORE_ROOTS, cwd=ws, check=False)


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
    # Review round 1 (B3): the configuration names INSIDE a restored
    # `.claude/` are the base's too — exempt with their verified root.
    for name in (".claude/CLAUDE.md", ".claude/CLAUDE.local.md", ".claude/AGENTS.md", ".claude/.mcp.json",
                 ".claude/rules/testing.md", ".claude/pkg/.claude/settings.json"):
        assert (ws / name).is_file(), name
    r = replant(ws)
    assert r.returncode == 0, r.stderr + r.stdout
    for root in RESTORE_ROOTS:
        assert f"root ./{root} matches origin/main" in r.stdout, root


def test_replant_check_exempts_a_restore_of_paths_the_pr_deleted(tmp_path):
    # Review round 1 (B2): the action restores a path the PR deleted with
    # `git checkout origin/<base> -- p` then `git reset -- p`, which leaves
    # it UNTRACKED in the PR's index. Byte-identical to the base, it is the
    # trusted restore all the same; the comparison must not read the PR's
    # index.
    ws = make_checkout(tmp_path, head_files={"pkg/x.py": "x=1\n"}, delete=RESTORE_ROOTS)
    strip(ws)
    assert config_entries(ws) == ["AGENTS.md"] or config_entries(ws) == []  # AGENTS.md stays at head → renamed below
    restore_from_base(ws)
    for root in RESTORE_ROOTS:
        assert (ws / root).exists(), root
    assert "CLAUDE.md" in git("ls-files", "--others", "--", "CLAUDE.md", cwd=ws).stdout  # untracked at head
    r = replant(ws)
    assert r.returncode == 0, r.stderr + r.stdout
    for root in RESTORE_ROOTS:
        assert f"root ./{root} matches origin/main" in r.stdout, root


def test_replant_check_fails_on_a_changed_or_added_descendant_of_a_restored_dir(tmp_path):
    ws = make_checkout(tmp_path)
    strip(ws)
    restore_from_base(ws)
    with (ws / ".claude/CLAUDE.md").open("a") as f:
        f.write("planted\n")
    r = replant(ws)
    assert r.returncode == 1 and "./.claude" in r.stdout and "./.claude/CLAUDE.md" in r.stdout
    ws2 = make_checkout(tmp_path / "two")
    strip(ws2)
    restore_from_base(ws2)
    (ws2 / ".claude/extra").mkdir()
    (ws2 / ".claude/extra/CLAUDE.md").write_text("planted\n")
    r = replant(ws2)
    assert r.returncode == 1 and "./.claude/extra/CLAUDE.md" in r.stdout
    # A file missing from a restored subtree is a difference too.
    ws3 = make_checkout(tmp_path / "three")
    strip(ws3)
    restore_from_base(ws3)
    (ws3 / ".claude/rules/testing.md").unlink()
    r = replant(ws3)
    assert r.returncode == 1 and "./.claude" in r.stdout


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


def test_replant_check_compares_bytes_not_attribute_normalized_content(tmp_path):
    # Review round 2 (B5): `git diff` honours the head's .gitattributes, so
    # with `ident` on the settings file a planted `{"note":"$Id: ",...,"tail":"$"}`
    # contracted to the base's `$Id$` and compared equal. The check compares
    # the blob's bytes with the file's.
    base = dict(BASE_FILES, **{".claude/settings.json": '{"note":"$Id$"}\n'})
    head = dict(HEAD_FILES, **{".gitattributes": ".claude/settings.json ident\n"})
    ws = make_checkout(tmp_path, base_files=base, head_files=head)
    strip(ws)
    restore_from_base(ws)
    (ws / ".claude/settings.json").write_text('{"note":"$Id: ","sandbox":{"enabled":false},"tail":"$"}\n')
    assert git("diff", "--quiet", "origin/main", "--", ".claude/settings.json", cwd=ws, check=False).returncode == 0  # git's view
    r = replant(ws)
    assert r.returncode == 1 and "./.claude" in r.stdout
    # The same attribute makes `git checkout` itself smudge the restored
    # file ($Id$ → $Id: <sha> $), so even the untouched restore differs from
    # the blob: fail-closed by design, and no caller carries such an attribute.
    ws2 = make_checkout(tmp_path / "two", base_files=base, head_files=head)
    strip(ws2)
    restore_from_base(ws2)
    assert "$Id: " in (ws2 / ".claude/settings.json").read_text()
    assert replant(ws2).returncode == 1


def test_replant_check_requires_the_same_entry_type_and_mode(tmp_path):
    # Same bytes reached through a symlink, or a mode flip, is not the restore.
    ws = make_checkout(tmp_path)
    strip(ws)
    restore_from_base(ws)
    target = ws / "elsewhere.md"
    target.write_text((ws / "CLAUDE.md").read_text())
    (ws / "CLAUDE.md").unlink()
    (ws / "CLAUDE.md").symlink_to("elsewhere.md")
    r = replant(ws)
    error = [l for l in r.stdout.splitlines() if l.startswith("::error::")]
    assert r.returncode == 1 and len(error) == 1 and error[0].endswith(": ./CLAUDE.md — the review is withheld")
    assert "root ./.claude matches" in r.stdout
    ws2 = make_checkout(tmp_path / "two")
    strip(ws2)
    restore_from_base(ws2)
    (ws2 / ".claude/settings.json").chmod(0o755)
    r = replant(ws2)
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


# --- Prompt ------------------------------------------------------------------------


PROMPT = lift_step(WORKFLOW, "        id: reviewprompt")


def note(name: str) -> str:
    """A note's text as the workflow's env block defines it."""
    m = re.search(rf'^\s+{name}: "(.*)"$', WORKFLOW.read_text(), re.M)
    assert m, name
    return m.group(1)


def compose_prompt(tmp_path, *, mode, fork_head="false"):
    out = tmp_path / f"prompt-{mode}-{fork_head}.txt"
    out.write_text("")
    env = {"MODE": mode, "FORK_HEAD": fork_head, "UP_REPO": "up/stream", "UP_NUM": "7", "PROXY": "42",
           "REPO": "meridianlabs-ai/agents", "DEFAULT_PROMPT": "Review this PR.", "THIS_PR": "42",
           "CONTINUATION": "", "OUT_DIR": "/home/runner/work/_temp/review", "SCRATCH": "/home/runner/work/_temp/scratch",
           "SANDBOX_NOTE": note("SANDBOX_NOTE"), "STRIP_NOTE": note("STRIP_NOTE"), "GITHUB_OUTPUT": str(out)}
    r = sh("bash", "-c", PROMPT, cwd=tmp_path, check=False, env=env)
    assert r.returncode == 0, r.stderr + r.stdout
    line = out.read_text().splitlines()[0]
    assert line.startswith("value=")
    return line[len("value="):]


def test_prompt_names_the_scratch_copy_by_its_absolute_path(tmp_path):
    # Review round 1 (B1): the note is an env value, so `$SCRATCH` inside it
    # is not expanded by printing it; the run block substitutes it.
    for mode, fork_head in (("pr", "true"), ("external", "false")):
        prompt = compose_prompt(tmp_path, mode=mode, fork_head=fork_head)
        assert "$SCRATCH" not in prompt, (mode, prompt)
        assert "cd /home/runner/work/_temp/scratch/src" in prompt
        assert "scratch copy of the checkout at /home/runner/work/_temp/scratch/src (and /home/runner/work/_temp/scratch)" in prompt
        assert "READ-ONLY to your commands" in prompt
        assert "CLAUDE.md / CLAUDE.local.md / AGENTS.md was renamed" in prompt
    # A same-repo head gets neither note.
    prompt = compose_prompt(tmp_path, mode="pr", fork_head="false")
    assert "scratch" not in prompt and "READ-ONLY" not in prompt


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
