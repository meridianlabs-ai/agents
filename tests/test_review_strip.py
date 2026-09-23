"""Tests for claude-review.yml's untrusted-checkout preparation and the
post-agent re-plant check (Claude Security 4628445).

The strip step moves the checkout's instruction files aside and deletes its
executable configuration; the scratch step copies the stripped tree to the
one place sandboxed commands may write; the post-agent check compares the
whole checkout with the strip step's raw snapshot tree, accepting only what
claude-code-action's own prepare phase does on PR events (restore roots
object-identical to `origin/<base>`, its `.claude-pr/` copy), re-runs the
strip's predicates, and keeps what configuration roots reach inside the
tree. The steps' bash is lifted from the workflow and run against local
repos.
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


def make_checkout(tmp_path, *, base_files=BASE_FILES, base_links=None, head_files=HEAD_FILES, delete=()) -> Path:
    """A base repo (branch `main`, holding `base_files` and the symlinks in
    `base_links`, path -> target) and a clone of it — the workspace — with a
    PR head commit on top (adding `head_files`, deleting `delete`), so
    `refs/remotes/origin/main` is there as an actions/checkout with
    fetch-depth 0 leaves it."""
    base = tmp_path / "base"
    base.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=base)
    write(base, base_files)
    for rel, target in (base_links or {}).items():
        (base / rel).symlink_to(target)
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


STRIP_TREES: dict = {}  # workspace -> the strip step's snapshot tree (its `tree` output)


def strip(ws: Path):
    out = ws.parent / f"strip-output-{ws.name}.txt"
    out.write_text("")
    r = sh("bash", "-c", STRIP, cwd=ws, check=False, env={"GITHUB_OUTPUT": str(out)})
    assert r.returncode == 0, r.stderr + r.stdout
    trees = [l[len("tree="):] for l in out.read_text().splitlines() if l.startswith("tree=")]
    assert len(trees) == 1 and re.fullmatch(r"[0-9a-f]{40,64}", trees[0]), out.read_text()
    STRIP_TREES[ws] = trees[0]
    return r


def replant(ws: Path, *, base_ref="main", strip_tree=None):
    tree = STRIP_TREES.get(ws, "") if strip_tree is None else strip_tree
    return sh("bash", "-c", REPLANT, cwd=ws, check=False, env={
        "BASE_REF": base_ref, "STRIP_TREE": tree, "GIT_DIR": str(ws / ".git"), "GIT_WORK_TREE": str(ws)})


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
    assert "the checkout is the strip step's snapshot" in r.stdout


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
    # The link differs from the base's file, and the new file is a change to
    # the checkout in its own right.
    assert r.returncode == 1 and len(error) == 1 and error[0].endswith(": ./CLAUDE.md ./elsewhere.md — the review is withheld")
    assert "root ./.claude matches" in r.stdout
    ws2 = make_checkout(tmp_path / "two")
    strip(ws2)
    restore_from_base(ws2)
    (ws2 / ".claude/settings.json").chmod(0o755)
    r = replant(ws2)
    assert r.returncode == 1 and "./.claude" in r.stdout


def test_replant_check_compares_symlink_targets_byte_for_byte(tmp_path):
    # Review round 3 (B6): the link comparison went through command
    # substitutions, which strip trailing newlines, so a link retargeted from
    # `AGENTS.md` to `AGENTS.md<LF>` (a different file) compared equal. Several
    # callers keep `CLAUDE.md -> AGENTS.md` and ts-mono `.claude -> .agents`.
    base = {k: v for k, v in BASE_FILES.items() if not k.startswith(".claude/") and k != "CLAUDE.md"}
    base[".agents/settings.json"] = '{"permissions": {"allow": ["Bash(pytest:*)"]}}\n'
    links = {"CLAUDE.md": "AGENTS.md", ".claude": ".agents"}
    head = {k: v for k, v in HEAD_FILES.items() if not k.startswith(".claude/")}
    # Unchanged restored links: exempt (the strip renamed the link's target,
    # AGENTS.md, but the link itself is the base's).
    ws = make_checkout(tmp_path, base_files=base, base_links=links, head_files=head)
    strip(ws)
    restore_from_base(ws)
    assert (ws / "CLAUDE.md").is_symlink() and (ws / ".claude").is_symlink()
    r = replant(ws)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "root ./CLAUDE.md matches" in r.stdout and "root ./.claude matches" in r.stdout
    # A file link retargeted to a name that differs only by a trailing
    # newline byte, holding planted instructions: survivor.
    (ws / "AGENTS.md\n").write_text("planted instructions\n")
    (ws / "CLAUDE.md").unlink()
    (ws / "CLAUDE.md").symlink_to("AGENTS.md\n")
    r = replant(ws)
    assert r.returncode == 1 and "./CLAUDE.md" in r.stdout and "root ./.claude matches" in r.stdout
    # A directory link retargeted the same way, to settings that turn the
    # sandbox off: survivor.
    (ws / ".agents\n").mkdir()
    (ws / ".agents\n/settings.json").write_text('{"sandbox": {"enabled": false}}\n')
    (ws / ".claude").unlink()
    (ws / ".claude").symlink_to(".agents\n")
    r = replant(ws)
    assert r.returncode == 1 and "./.claude" in r.stdout and "./CLAUDE.md" in r.stdout
    # Retargeted to an entirely different name, or the newline dropped from a
    # base target that has one: survivor too. (The head carries no CLAUDE.md
    # here: writing one through the base's dangling link would create the
    # newline-named target.)
    head2 = {k: v for k, v in head.items() if k != "CLAUDE.md"}
    ws2 = make_checkout(tmp_path / "two", base_files=base, base_links={"CLAUDE.md": "README.md\n"}, head_files=head2)
    strip(ws2)
    restore_from_base(ws2)
    assert not (ws2 / "CLAUDE.md").exists() and replant(ws2).returncode == 0  # dangling: bytes alone
    (ws2 / "CLAUDE.md").unlink()
    (ws2 / "CLAUDE.md").symlink_to("README.md")
    r = replant(ws2)
    assert r.returncode == 1 and "./CLAUDE.md" in r.stdout
    # The original link with its newline-ending target made live: the
    # referent name cannot be resolved without dropping the newline, so the
    # check refuses it (conservative) rather than compare the wrong file.
    (ws2 / "CLAUDE.md").unlink()
    (ws2 / "CLAUDE.md").symlink_to("README.md\n")
    (ws2 / "README.md\n").write_text("base\n")
    r = replant(ws2)
    assert r.returncode == 1 and "./CLAUDE.md" in r.stdout


def test_replant_check_verifies_the_content_behind_a_trusted_link(tmp_path):
    # Review round 4 (B7): a link blob authenticates a pathname, not what it
    # reaches. The referent is compared against the strip step's raw
    # snapshot of the checkout — "unchanged since the run started" — so the
    # PR's own content behind a trusted link passes and anything changed or
    # added during the run does not.
    base = {k: v for k, v in BASE_FILES.items() if k != "CLAUDE.md"}
    base.update({".agents/settings.json": '{"permissions": {"allow": ["Bash(pytest:*)"]}}\n',
                 "skills/example/SKILL.md": "base skill\n", "config": "trusted instructions\n"})
    base.pop(".claude/settings.json")  # `.claude` is a link in this layout
    for k in list(base):
        if k.startswith(".claude/"):
            del base[k]
    base[".claude-real/settings.json"] = "{}\n"
    links = {"CLAUDE.md": "config", ".claude": ".agents", ".agents/skills": "../skills"}
    # The PR changes the skill behind the link: legitimate head content. (No
    # head CLAUDE.md: writing one through the base's link would rewrite
    # `config`.)
    head = {k: v for k, v in HEAD_FILES.items() if not k.startswith(".claude/") and k != "CLAUDE.md"}
    head["skills/example/SKILL.md"] = "the PR's skill\n"
    ws = make_checkout(tmp_path, base_files=base, base_links=links, head_files=head)
    strip(ws)
    restore_from_base(ws)
    assert (ws / ".claude").is_symlink() and (ws / ".claude/skills").is_symlink()
    assert (ws / ".claude/skills/example/SKILL.md").read_text() == "the PR's skill\n"
    r = replant(ws)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "root ./CLAUDE.md matches" in r.stdout and "root ./.claude matches" in r.stdout
    # Changed settings behind the directory link: survivor.
    (ws / ".agents/settings.json").write_text('{"sandbox": {"enabled": false}}\n')
    r = replant(ws)
    assert r.returncode == 1 and r.stdout.splitlines()[-1].endswith(": ./.agents/settings.json — the review is withheld")
    (ws / ".agents/settings.json").write_text('{"permissions": {"allow": ["Bash(pytest:*)"]}}\n')
    assert replant(ws).returncode == 0
    # An added file behind it: survivor.
    (ws / ".agents/settings.local.json").write_text('{"sandbox": {"enabled": false}}\n')
    r = replant(ws)
    assert r.returncode == 1 and r.stdout.splitlines()[-1].endswith(": ./.agents/settings.local.json — the review is withheld")
    (ws / ".agents/settings.local.json").unlink()
    # A changed skill behind the nested link, two links deep: survivor.
    (ws / "skills/example/SKILL.md").write_text("planted skill\n")
    r = replant(ws)
    assert r.returncode == 1 and r.stdout.splitlines()[-1].endswith(": ./skills/example/SKILL.md — the review is withheld")
    (ws / "skills/example/SKILL.md").write_text("the PR's skill\n")
    # Changed instructions behind the file link: survivor.
    (ws / "config").write_text("planted instructions\n")
    r = replant(ws)
    assert r.returncode == 1 and r.stdout.splitlines()[-1].endswith(": ./config — the review is withheld")
    (ws / "config").write_text("trusted instructions\n")
    assert replant(ws).returncode == 0
    # Without the strip's snapshot nothing can be verified: fail closed.
    for tree in ("", "0" * 40, "not-a-sha"):
        r = replant(ws, strip_tree=tree)
        assert r.returncode == 1 and "snapshot of the checkout is unavailable" in r.stdout, tree


def test_replant_check_resolves_referents_as_the_filesystem_does(tmp_path):
    # Review round 5 (B7 remaining): lexical `..` collapsing compared `config`
    # for `CLAUDE.md -> route/../config` although, with `route ->
    # actual/sub`, the filesystem reaches `actual/config`; and a shell loop
    # glob-expanded a `[a]` target to the decoy `a`. The whole checkout is
    # now compared with the strip snapshot tree against tree, so what any
    # link reaches inside the tree is verified whatever its path spelling.
    base = {k: v for k, v in BASE_FILES.items() if k not in ("CLAUDE.md", "CLAUDE.local.md")}
    base.update({"config": "decoy\n", "actual/config": "trusted instructions\n", "actual/sub/.keep": "",
                 "[a]": "trusted literal\n", "a": "decoy a\n",
                 "skills/example/SKILL.md": "base skill\n", "actual/payload/SKILL.md": "base payload\n"})
    for k in list(base):
        if k.startswith(".claude/"):
            del base[k]
    base[".claude/settings.json"] = "{}\n"
    links = {"route": "actual/sub", "CLAUDE.md": "route/../config", "CLAUDE.local.md": "[a]",
             ".claude/skills": "../skills"}
    head = {k: v for k, v in HEAD_FILES.items()
            if not k.startswith(".claude/") and k not in ("CLAUDE.md", "CLAUDE.local.md")}
    ws = make_checkout(tmp_path, base_files=base, base_links=links, head_files=head)
    # The PR adds a nested skill link spelled through the intermediate link.
    (ws / "skills/extra").symlink_to("../route/../payload")
    git("add", "-A", cwd=ws)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "extra skill link", cwd=ws)
    strip(ws)
    restore_from_base(ws)
    assert (ws / "CLAUDE.md").read_text() == "trusted instructions\n"  # the filesystem's referent
    assert (ws / ".claude/skills/extra/SKILL.md").read_text() == "base payload\n"
    r = replant(ws)
    assert r.returncode == 0, r.stderr + r.stdout
    for changed, body in (("actual/config", "planted\n"), ("[a]", "planted\n"), ("actual/payload/SKILL.md", "planted\n")):
        original = (ws / changed).read_text()
        (ws / changed).write_text(body)
        r = replant(ws)
        last = r.stdout.splitlines()[-1]
        assert r.returncode == 1 and "the review is withheld" in last, changed
        assert changed.replace("[", "\\[").replace("]", "\\]") in last, (changed, last)
        (ws / changed).write_text(original)
        assert replant(ws).returncode == 0, changed
    # A link spelled to stay inside lexically but resolving outside through
    # an intermediate link: `escape -> ../..` makes `skills/out ->
    # ../escape/outside.md` leave the checkout. Unchanged PR content, but
    # what a configuration root reaches must stay where step 1 verified it.
    (tmp_path / "outside.md").write_text("outside\n")
    ws2 = make_checkout(tmp_path / "two", base_files=base, base_links=links, head_files=head)
    (ws2 / "escape").symlink_to("../..")
    (ws2 / "skills/out").symlink_to("../escape/outside.md")
    git("add", "-A", cwd=ws2)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "escaping link", cwd=ws2)
    strip(ws2)
    restore_from_base(ws2)
    assert (ws2 / ".claude/skills/out").read_text() == "outside\n"
    r = replant(ws2)
    assert r.returncode == 1 and "./.claude/skills/out is reached through a verified configuration root" in r.stdout
    assert r.stdout.splitlines()[-1].endswith(": ./.claude/skills/out — the review is withheld")


def test_replant_check_allows_the_actions_other_restores_and_its_snapshot_dir(tmp_path):
    # The action's restore covers more than configuration (.gitmodules,
    # .husky, ...) and it copies the PR's remaining sensitive paths to
    # .claude-pr/ first; neither is a re-plant. A changed non-configuration
    # file elsewhere is still a change to a read-only checkout.
    base = dict(BASE_FILES, **{".gitmodules": "[submodule]\n", ".husky/pre-commit": "base hook\n"})
    head = dict(HEAD_FILES, **{".gitmodules": "[submodule head]\n", ".husky/pre-commit": "head hook\n"})
    ws = make_checkout(tmp_path, base_files=base, head_files=head)
    strip(ws)
    (ws / ".claude-pr/.husky").mkdir(parents=True)
    (ws / ".claude-pr/.husky/pre-commit").write_text("head hook\n")
    (ws / ".claude-pr/.gitmodules").write_text("[submodule head]\n")
    # The action deletes every sensitive path before its fetch, then checks
    # them out from the base.
    import shutil
    (ws / ".gitmodules").unlink()
    shutil.rmtree(ws / ".husky")
    restore_from_base(ws)
    for p in (".gitmodules", ".husky"):
        git("checkout", "origin/main", "--", p, cwd=ws)
    r = replant(ws)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "root ./.gitmodules matches" in r.stdout and "root ./.husky matches" in r.stdout
    # A config name inside .claude-pr is still a survivor.
    (ws / ".claude-pr/CLAUDE.md").write_text("planted\n")
    r = replant(ws)
    assert r.returncode == 1 and r.stdout.splitlines()[-1].endswith(": ./.claude-pr/CLAUDE.md — the review is withheld")
    (ws / ".claude-pr/CLAUDE.md").unlink()
    (ws / "pkg/__init__.py").write_text("changed\n")
    r = replant(ws)
    assert r.returncode == 1 and r.stdout.splitlines()[-1].endswith(": ./pkg/__init__.py — the review is withheld")


def test_replant_check_refuses_links_that_leave_the_tree(tmp_path):
    base = {k: v for k, v in BASE_FILES.items() if k != "CLAUDE.md" and k != "CLAUDE.local.md"}
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n")
    links = {"CLAUDE.md": str(outside), "CLAUDE.local.md": "../outside.md"}
    ws = make_checkout(tmp_path, base_files=base, base_links=links)
    strip(ws)
    restore_from_base(ws)
    assert (ws / "CLAUDE.md").is_symlink() and (ws / "CLAUDE.md").exists()
    r = replant(ws)
    assert r.returncode == 1 and "./CLAUDE.md" in r.stdout and "./CLAUDE.local.md" in r.stdout
    # A link into .git is never exempt either.
    ws2 = make_checkout(tmp_path / "two", base_files=base, base_links={"CLAUDE.md": ".git/description"})
    strip(ws2)
    restore_from_base(ws2)
    r = replant(ws2)
    assert r.returncode == 1 and "./CLAUDE.md" in r.stdout


def test_strip_snapshot_is_raw_and_leaves_the_checkout_as_it_was(tmp_path):
    # The snapshot stores the stripped tree's bytes with the head's
    # attributes switched off (an `ident` file keeps its `$Id: x $`), and the
    # temporary attributes override is gone afterwards.
    head = dict(HEAD_FILES, **{".gitattributes": "pkg/x.py ident\n", "pkg/x.py": "# $Id: keep-me $\n"})
    ws = make_checkout(tmp_path, head_files=head)
    strip(ws)
    tree = STRIP_TREES[ws]
    blob = git("rev-parse", f"{tree}:pkg/x.py", cwd=ws).stdout.strip()
    assert git("cat-file", "blob", blob, cwd=ws).stdout == "# $Id: keep-me $\n"
    assert git("rev-parse", f"{tree}:CLAUDE.md.untrusted", cwd=ws).returncode == 0
    assert git("rev-parse", f"{tree}:CLAUDE.md", cwd=ws, check=False).returncode != 0
    assert not (ws / ".git/info/attributes").exists()
    assert git("status", "--porcelain", cwd=ws).stdout.count("\n") > 0  # the renames, nothing staged
    assert git("diff", "--cached", "--quiet", cwd=ws, check=False).returncode == 0


def test_replant_check_compares_the_restore_roots_mode_and_type(tmp_path):
    # Review round 6 (B8): the object id alone let a symlink whose target
    # bytes equal the base file's contents pass, and an executable bit.
    base = dict(BASE_FILES, **{"CLAUDE.md": "target", "target": "trusted\n"})
    ws = make_checkout(tmp_path, base_files=base)
    strip(ws)
    restore_from_base(ws)
    assert replant(ws).returncode == 0
    # Same blob (the link's target bytes are the file's contents), other type;
    # `target` itself is unchanged, so only the entry's type differs.
    (ws / "CLAUDE.md").unlink()
    (ws / "CLAUDE.md").symlink_to("target")
    r = replant(ws)
    assert r.returncode == 1 and r.stdout.splitlines()[-1].endswith(": ./CLAUDE.md — the review is withheld")
    ws2 = make_checkout(tmp_path / "two")
    strip(ws2)
    restore_from_base(ws2)
    (ws2 / ".mcp.json").chmod(0o755)
    r = replant(ws2)
    assert r.returncode == 1 and r.stdout.splitlines()[-1].endswith(": ./.mcp.json — the review is withheld")


def test_replant_check_rejects_reach_into_unverified_storage(tmp_path):
    # Review round 6 (B7): the action's .claude-pr/ copy is exempt from the
    # tree comparison and an embedded repository is hashed as a gitlink, so
    # content reached there was never verified. Direct root links and a
    # skill link behind the callers' `.claude/skills -> ../skills` alike.
    base = {k: v for k, v in BASE_FILES.items() if not k.startswith(".claude/") and k != "CLAUDE.md"}
    base["skills/example/SKILL.md"] = "skill\n"
    base[".claude/settings.json"] = "{}\n"
    cases = (
        ({"CLAUDE.md": ".claude-pr/content"}, ".claude-pr/content", "./CLAUDE.md"),
        ({"CLAUDE.md": "payload/data"}, "payload/data", "./CLAUDE.md"),
        ({".claude/skills": "../skills", "skills/pr": "../.claude-pr/payload"}, ".claude-pr/payload/SKILL.md", "./.claude/skills/pr"),
        ({".claude/skills": "../skills", "skills/pr": "../payload"}, "payload/data", "./.claude/skills/pr"),
    )
    for n, (links, referent, reported) in enumerate(cases):
        head = {k: v for k, v in HEAD_FILES.items() if k != "CLAUDE.md" and not k.startswith(".claude/")}
        ws = make_checkout(tmp_path / str(n), base_files=base, head_files=head,
                           base_links={k: v for k, v in links.items() if k != "skills/pr"})
        if "skills/pr" in links:
            (ws / "skills/pr").symlink_to(links["skills/pr"])
            git("add", "-A", cwd=ws)
            git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "pr link", cwd=ws)
        if referent.startswith("payload/"):
            (ws / "payload").mkdir()
            git("init", "-q", cwd=ws / "payload")
            (ws / "payload/data").write_text("inside an embedded repository\n")
            git("add", "-A", cwd=ws / "payload")
            git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "embedded", cwd=ws / "payload")
        strip(ws)
        if referent.startswith(".claude-pr/"):
            (ws / referent).parent.mkdir(parents=True, exist_ok=True)
            (ws / referent).write_text("in the action's copy\n")
        restore_from_base(ws)
        r = replant(ws)
        assert r.returncode == 1, (links, r.stdout)
        assert f"{reported} is reached through a verified configuration root" in r.stdout, (links, r.stdout)


def test_replant_check_allows_the_clis_empty_cc_writes_dir(tmp_path):
    # Observed on Linux with the pinned CLI: Claude Code creates an empty
    # `.claude/.cc-writes/` in its working directory on every run, outside
    # the sandbox. With no `.claude/` in the base (nothing restored) that is
    # the only `.claude` in the checkout, and it carries nothing.
    base = {k: v for k, v in BASE_FILES.items() if not k.startswith(".claude/")}
    ws = make_checkout(tmp_path, base_files=base)
    strip(ws)
    restore_from_base(ws)
    assert not (ws / ".claude").exists()
    (ws / ".claude/.cc-writes").mkdir(parents=True)
    r = replant(ws)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "./.claude holds only empty directories" in r.stdout
    # A file in it, a link as `.claude`, or an empty `.claude` elsewhere is
    # not that shape.
    (ws / ".claude/.cc-writes/settings.json").write_text("{}\n")
    r = replant(ws)
    # (A changed restore root is reported as the root.)
    assert r.returncode == 1 and r.stdout.splitlines()[-1].endswith(": ./.claude — the review is withheld")
    assert "holds only empty directories" not in r.stdout
    (ws / ".claude/.cc-writes/settings.json").unlink()
    (ws / ".claude/.cc-writes").rmdir()
    (ws / ".claude").rmdir()
    (ws / "emptydir").mkdir()
    (ws / ".claude").symlink_to("emptydir")
    r = replant(ws)
    assert r.returncode == 1 and "./.claude" in r.stdout.splitlines()[-1]
    (ws / ".claude").unlink()
    (ws / "emptydir").rmdir()
    (ws / "pkg/.claude").mkdir()
    r = replant(ws)
    assert r.returncode == 1 and r.stdout.splitlines()[-1].endswith(": ./pkg/.claude — the review is withheld")
    # With a restored base `.claude/` the CLI's empty directory inside it is
    # invisible to the tree comparison and the root still verifies.
    ws2 = make_checkout(tmp_path / "two")
    strip(ws2)
    restore_from_base(ws2)
    (ws2 / ".claude/.cc-writes").mkdir()
    r = replant(ws2)
    assert r.returncode == 0 and "root ./.claude matches" in r.stdout


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
