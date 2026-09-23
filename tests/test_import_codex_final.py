"""Tests for the `import-codex-final` composite and the read side of
`resolve-reported-threads` (finding 4628447).

codex-action writes the final message into a directory the codex user owns,
so codex can replace the entry with a symlink; every later step runs as the
runner and used to open that entry with symlink-following primitives and
publish what it read. The composite's script is the one place the entry is
opened now: it must copy a regular, expected-owner, non-symlinked file byte
for byte and refuse everything else with no copy left behind — the callers'
readers then fall back to the default commit subject and the "(codex
produced no final message)" placeholder. The composite's own step is lifted
from its action.yml and run against a symlinked final message, and a
structural check pins the three write-path workflows to reading the
imported copy only.

The script's `dir` mode (the Claude agent's landing directory,
design/executed-paths-residual.md → After the agent) is tested the same
way: every passing entry copied into a fresh runner-only directory, every
other entry skipped with a warning, and the caps refusing the whole import
rather than truncating.
"""

import os
import pwd
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "import_codex_final.py"
COMPOSITE = ROOT / ".github" / "actions" / "resolve-reported-threads" / "action.yml"
IMPORT_ACTION = ROOT / ".github" / "actions" / "import-codex-final" / "action.yml"
WORKFLOWS = ROOT / ".github" / "workflows"

ME = pwd.getpwuid(os.getuid()).pw_name
# A real account that is not the test's own: the file the test writes is
# owned by ME, so demanding this owner exercises the refusal without chown.
OTHER = "root"


def run(source, dest, owner=ME, tmp_path=None, **extra):
    env = {**os.environ}
    out = None
    if tmp_path is not None:
        out = tmp_path / "github-output"
        out.write_text("")
        env["GITHUB_OUTPUT"] = str(out)
    cmd = [sys.executable, str(SCRIPT), "--source", str(source), "--dest", str(dest), "--owner", owner]
    for k, v in extra.items():
        cmd += [f"--{k.replace('_', '-')}", str(v)]
    r = subprocess.run(cmd, text=True, capture_output=True, env=env, timeout=20, check=False)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines()) if out is not None else {}
    return r, outputs


@pytest.fixture
def paths(tmp_path):
    src_dir = tmp_path / "codex"
    src_dir.mkdir()
    return {"src": src_dir / "codex-final.md", "dest": tmp_path / "codex-final.md", "tmp": tmp_path}


# --- the script --------------------------------------------------------------


def test_regular_file_owned_by_expected_user_is_copied_byte_for_byte(paths):
    body = "Fixed the thing.\n\nRESOLVED-THREADS: PRRT_abc\n" + "é" * 10
    paths["src"].write_text(body, encoding="utf-8")
    r, outputs = run(paths["src"], paths["dest"], tmp_path=paths["tmp"])
    assert r.returncode == 0, r.stderr
    assert paths["dest"].read_bytes() == body.encode("utf-8")
    assert outputs == {"path": str(paths["dest"])}
    assert "::warning::" not in r.stdout
    assert not paths["dest"].with_name("codex-final.md.tmp").exists()


def test_symlink_to_a_runner_file_is_refused_without_reading_the_target(paths):
    secret = paths["tmp"] / "runner-private"
    secret.write_text("GH_TOKEN=ghs_secret\n")
    paths["src"].symlink_to(secret)
    r, outputs = run(paths["src"], paths["dest"], tmp_path=paths["tmp"])
    assert r.returncode == 0, r.stderr
    assert not paths["dest"].exists()
    assert outputs == {"path": ""}
    assert "::warning::" in r.stdout and "symlink" in r.stdout
    assert "ghs_secret" not in r.stdout + r.stderr


def test_dangling_symlink_is_refused(paths):
    paths["src"].symlink_to(paths["tmp"] / "nowhere")
    r, outputs = run(paths["src"], paths["dest"], tmp_path=paths["tmp"])
    assert r.returncode == 0, r.stderr
    assert not paths["dest"].exists()
    assert "::warning::" in r.stdout and outputs == {"path": ""}


def test_symlinked_parent_directory_entry_still_refuses_a_symlinked_file(paths):
    # The final component is what codex controls; O_NOFOLLOW refuses it even
    # when the path is reached through a legitimate symlinked parent.
    alias = paths["tmp"] / "alias"
    alias.symlink_to(paths["src"].parent)
    (paths["src"]).symlink_to(paths["tmp"] / "runner-private")
    (paths["tmp"] / "runner-private").write_text("private\n")
    r, _ = run(alias / "codex-final.md", paths["dest"], tmp_path=paths["tmp"])
    assert r.returncode == 0 and not paths["dest"].exists() and "symlink" in r.stdout


def test_directory_is_refused(paths):
    paths["src"].mkdir()
    r, outputs = run(paths["src"], paths["dest"], tmp_path=paths["tmp"])
    assert r.returncode == 0, r.stderr
    assert not paths["dest"].exists()
    assert "not a regular file" in r.stdout and outputs == {"path": ""}


def test_fifo_is_refused_without_blocking(paths):
    os.mkfifo(paths["src"])
    # run() has a 20 s timeout: a blocking open would fail the test there.
    r, outputs = run(paths["src"], paths["dest"], tmp_path=paths["tmp"])
    assert r.returncode == 0, r.stderr
    assert not paths["dest"].exists()
    assert "not a regular file" in r.stdout and outputs == {"path": ""}


def test_file_owned_by_another_user_is_refused(paths):
    paths["src"].write_text("looks fine\n")
    r, outputs = run(paths["src"], paths["dest"], owner=OTHER, tmp_path=paths["tmp"])
    assert r.returncode == 0, r.stderr
    assert not paths["dest"].exists()
    assert "owned by uid" in r.stdout and outputs == {"path": ""}


def test_hard_link_keeps_its_targets_owner_and_is_refused_by_the_owner_check(paths):
    # A hard link to a file owned by someone else carries that owner, so the
    # ownership check is what catches it. Modelled with the owner mismatch
    # (the test cannot own two users' files); the link itself is a regular
    # file and must not be refused on shape grounds.
    target = paths["tmp"] / "target"
    target.write_text("mine\n")
    os.link(target, paths["src"])
    r, _ = run(paths["src"], paths["dest"], owner=ME, tmp_path=paths["tmp"])
    assert r.returncode == 0 and paths["dest"].read_text() == "mine\n"
    paths["dest"].unlink()
    r, _ = run(paths["src"], paths["dest"], owner=OTHER, tmp_path=paths["tmp"])
    assert r.returncode == 0 and not paths["dest"].exists() and "owned by uid" in r.stdout


def test_missing_message_leaves_no_copy_and_no_warning(paths):
    r, outputs = run(paths["src"], paths["dest"], tmp_path=paths["tmp"])
    assert r.returncode == 0, r.stderr
    assert not paths["dest"].exists()
    assert "::warning::" not in r.stdout and "no codex final message" in r.stdout
    assert outputs == {"path": ""}


def test_stale_copy_is_removed_when_the_message_is_refused(paths):
    paths["dest"].write_text("from an earlier step\n")
    paths["src"].symlink_to(paths["tmp"] / "runner-private")
    r, _ = run(paths["src"], paths["dest"], tmp_path=paths["tmp"])
    assert r.returncode == 0 and not paths["dest"].exists()


def test_stale_copy_is_replaced_not_followed(paths):
    # A symlink at the destination (impossible on the runner — $RUNNER_TEMP
    # is runner-only — but the write must not depend on that) is unlinked,
    # never written through.
    elsewhere = paths["tmp"] / "elsewhere"
    elsewhere.write_text("untouched\n")
    paths["dest"].symlink_to(elsewhere)
    paths["src"].write_text("new message\n")
    r, _ = run(paths["src"], paths["dest"], tmp_path=paths["tmp"])
    assert r.returncode == 0, r.stderr
    assert not paths["dest"].is_symlink() and paths["dest"].read_text() == "new message\n"
    assert elsewhere.read_text() == "untouched\n"


def test_oversize_message_is_truncated_with_a_warning(paths):
    paths["src"].write_bytes(b"x" * 100 + b"\n")
    r, outputs = run(paths["src"], paths["dest"], tmp_path=paths["tmp"], max_bytes=64)
    assert r.returncode == 0, r.stderr
    assert paths["dest"].read_bytes() == b"x" * 64
    assert "exceeds 64 bytes" in r.stdout and outputs == {"path": str(paths["dest"])}


def test_unknown_owner_is_a_configuration_error(paths):
    paths["src"].write_text("hello\n")
    r, _ = run(paths["src"], paths["dest"], owner="no-such-user-4628447", tmp_path=paths["tmp"])
    assert r.returncode == 2
    assert "::error::" in r.stdout and not paths["dest"].exists()


# --- the script's dir mode ---------------------------------------------------


@pytest.fixture
def land(tmp_path):
    src = tmp_path / "claude-agent"
    src.mkdir()
    return {"src": src, "dest": tmp_path / "landing", "tmp": tmp_path}


def run_dir(land, owner=ME, **extra):
    return run(land["src"], land["dest"], owner=owner, tmp_path=land["tmp"], mode="dir", **extra)


def listing(d: Path) -> dict:
    return {p.name: p.read_bytes() for p in sorted(d.iterdir())}


def test_dir_mode_copies_every_regular_file_byte_for_byte(land):
    files = {"manifest-extra.json": b'{"comments": []}\n', "review.md": "Looks good.\n\né".encode(),
             "inline.json": b"[]", "comment-1.md": b"", "a_B-9.txt": b"x"}
    for name, body in files.items():
        (land["src"] / name).write_bytes(body)
    r, outputs = run_dir(land)
    assert r.returncode == 0, r.stderr
    assert listing(land["dest"]) == files
    assert stat_mode(land["dest"]) == 0o700
    assert outputs == {"path": str(land["dest"])}
    assert "::warning::" not in r.stdout and "imported 5 file(s)" in r.stdout


def stat_mode(p: Path) -> int:
    return p.stat().st_mode & 0o777


def test_dir_mode_skips_symlinks_without_reading_the_target(land):
    secret = land["tmp"] / "runner-private"
    secret.write_text("GH_TOKEN=ghs_secret\n")
    (land["src"] / "review.md").symlink_to(secret)
    (land["src"] / "dangling.md").symlink_to(land["tmp"] / "nowhere")
    (land["src"] / "subdir-link").symlink_to(land["tmp"])
    (land["src"] / "ok.md").write_text("ok\n")
    r, outputs = run_dir(land)
    assert r.returncode == 0, r.stderr
    assert listing(land["dest"]) == {"ok.md": b"ok\n"}
    assert r.stdout.count("it is a symlink") == 3
    assert "ghs_secret" not in r.stdout + r.stderr
    assert outputs == {"path": str(land["dest"])}


def test_dir_mode_skips_a_hard_link_even_with_the_expected_owner(land):
    # The owner check alone would pass it (the test owns both names); a hard
    # link is refused on its link count, since it may be a runner file the
    # agent linked in where fs.protected_hardlinks is off.
    target = land["tmp"] / "runner-file"
    target.write_text("runner's\n")
    os.link(target, land["src"] / "review.md")
    (land["src"] / "ok.md").write_text("ok\n")
    r, _ = run_dir(land)
    assert r.returncode == 0, r.stderr
    assert listing(land["dest"]) == {"ok.md": b"ok\n"}
    assert "review.md: it has 2 links" in r.stdout


def test_dir_mode_skips_files_of_another_owner(land):
    (land["src"] / "review.md").write_text("looks fine\n")
    r, outputs = run_dir(land, owner=OTHER)
    assert r.returncode == 0, r.stderr
    assert listing(land["dest"]) == {}
    assert "owned by uid" in r.stdout and outputs == {"path": str(land["dest"])}


@pytest.mark.parametrize("name", [".hidden", "a b.md", "x\n::error::forged", "caf\u00e9.md", "semi;colon", "-", "._x"])
def test_dir_mode_skips_bad_names_and_logs_them_escaped(land, name):
    (land["src"] / name).write_text("body\n")
    (land["src"] / "ok.md").write_text("ok\n")
    r, _ = run_dir(land)
    expected = {"ok.md": b"ok\n"}
    if name == "-":
        expected["-"] = b"body\n"  # a plain name, no leading dot: allowed
    assert r.returncode == 0, r.stderr
    assert listing(land["dest"]) == expected
    if name != "-":
        assert "the name is not" in r.stdout
    # No log line starts with anything the name carried: a newline in it
    # would otherwise open a workflow command.
    for line in r.stdout.splitlines():
        assert line.startswith(("::warning::skipping ", "imported ")), line


def test_dir_mode_skips_directories_and_fifos_without_blocking(land):
    (land["src"] / "sub").mkdir()
    (land["src"] / "sub" / "nested.md").write_text("nested\n")
    os.mkfifo(land["src"] / "pipe")
    (land["src"] / "ok.md").write_text("ok\n")
    r, _ = run_dir(land)
    assert r.returncode == 0, r.stderr
    assert listing(land["dest"]) == {"ok.md": b"ok\n"}
    assert r.stdout.count("not a regular file") == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_dir_mode_skips_an_unreadable_file(land):
    (land["src"] / "secret.md").write_text("x\n")
    (land["src"] / "secret.md").chmod(0)
    (land["src"] / "ok.md").write_text("ok\n")
    try:
        r, _ = run_dir(land)
    finally:
        (land["src"] / "secret.md").chmod(0o600)
    assert r.returncode == 0, r.stderr
    assert listing(land["dest"]) == {"ok.md": b"ok\n"}
    assert "secret.md: cannot open it" in r.stdout


def test_dir_mode_skips_a_file_over_the_per_file_cap_rather_than_truncating(land):
    (land["src"] / "big.md").write_bytes(b"x" * 65)
    (land["src"] / "exact.md").write_bytes(b"y" * 64)
    r, _ = run_dir(land, max_bytes=64)
    assert r.returncode == 0, r.stderr
    assert listing(land["dest"]) == {"exact.md": b"y" * 64}
    assert "big.md: it exceeds 64 bytes" in r.stdout


@pytest.mark.parametrize("caps", [{"max_total_bytes": 100}, {"max_files": 2}])
def test_dir_mode_refuses_the_whole_import_when_the_caps_are_exhausted(land, caps):
    for i in range(3):
        (land["src"] / f"part-{i}.md").write_bytes(b"z" * 40)   # 120 bytes, 3 files
    land["dest"].mkdir()
    (land["dest"] / "stale.md").write_text("from an earlier step\n")
    r, outputs = run_dir(land, **caps)
    assert r.returncode == 0, r.stderr
    assert not land["dest"].exists() and outputs == {"path": ""}
    assert "refusing landing directory" in r.stdout and "nothing imported" in r.stdout


def test_dir_mode_within_the_caps_imports_everything(land):
    for i in range(3):
        (land["src"] / f"part-{i}.md").write_bytes(b"z" * 40)
    r, _ = run_dir(land, max_total_bytes=120, max_files=3)
    assert r.returncode == 0, r.stderr
    assert len(listing(land["dest"])) == 3


def test_dir_mode_replaces_a_stale_destination_and_never_follows_it(land):
    elsewhere = land["tmp"] / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.md").write_text("untouched\n")
    land["dest"].symlink_to(elsewhere)
    (land["src"] / "new.md").write_text("new\n")
    r, _ = run_dir(land)
    assert r.returncode == 0, r.stderr
    assert not land["dest"].is_symlink() and listing(land["dest"]) == {"new.md": b"new\n"}
    assert listing(elsewhere) == {"keep.md": b"untouched\n"}
    # A stale directory is replaced wholesale: nothing from before survives.
    (land["dest"] / "stale.md").write_text("old\n")
    r, _ = run_dir(land)
    assert listing(land["dest"]) == {"new.md": b"new\n"}


def test_dir_mode_missing_directory_imports_nothing_quietly(land):
    land["src"].rmdir()
    r, outputs = run_dir(land)
    assert r.returncode == 0, r.stderr
    assert not land["dest"].exists() and outputs == {"path": ""}
    assert "::warning::" not in r.stdout and "no landing directory" in r.stdout


@pytest.mark.parametrize("shape", ["symlink", "file"])
def test_dir_mode_refuses_a_source_that_is_not_a_plain_directory(land, shape):
    real = land["tmp"] / "real"
    real.mkdir()
    (real / "review.md").write_text("redirected\n")
    land["src"].rmdir()
    if shape == "symlink":
        land["src"].symlink_to(real)
    else:
        land["src"].write_text("not a directory\n")
    r, outputs = run_dir(land)
    assert r.returncode == 0, r.stderr
    assert not land["dest"].exists() and outputs == {"path": ""}
    assert "::warning::refusing landing directory" in r.stdout


def test_dir_mode_unknown_owner_is_a_configuration_error(land):
    (land["src"] / "review.md").write_text("x\n")
    r, _ = run_dir(land, owner="no-such-user-4628447")
    assert r.returncode == 2 and "::error::" in r.stdout and not land["dest"].exists()


# --- the import-codex-final composite ----------------------------------------


def import_step() -> str:
    """The composite's single run block (its only `run: |`)."""
    lines = IMPORT_ACTION.read_text().splitlines()
    run_at = lines.index("      run: |")
    body = []
    for line in lines[run_at + 1:]:
        if line.strip() == "":
            body.append("")
        elif line.startswith("        "):
            body.append(line[8:])
        else:
            break
    return "\n".join(body) + "\n"


def run_import_step(tmp_path, **env_extra):
    out = tmp_path / "github-output"
    out.write_text("")
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(out), "SCRIPT": str(SCRIPT),
           "MODE": "file", "SOURCE": "", "DEST": "", "OWNER": "", **env_extra}
    r = subprocess.run(["bash", "-c", import_step()], text=True, capture_output=True, env=env, timeout=20, check=False)
    return r, dict(line.split("=", 1) for line in out.read_text().splitlines())


def test_import_step_file_mode_defaults_are_the_codex_paths(tmp_path):
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "codex-final.md").write_text("done\n")
    # The owner is overridden (no codex account here); the default owner is
    # pinned by test_import_action_defaults_match_the_callers_paths.
    r, outputs = run_import_step(tmp_path, OWNER=ME)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "codex-final.md").read_text() == "done\n" and outputs == {"path": f"{tmp_path}/codex-final.md"}


def test_import_step_dir_mode_defaults_are_the_claude_agent_paths(tmp_path):
    (tmp_path / "claude-agent").mkdir()
    (tmp_path / "claude-agent" / "review.md").write_text("review\n")
    r, outputs = run_import_step(tmp_path, MODE="dir", OWNER=ME)
    assert r.returncode == 0, r.stderr
    assert listing(tmp_path / "landing") == {"review.md": b"review\n"}
    assert outputs == {"path": f"{tmp_path}/landing"}


def test_import_step_refuses_an_unknown_mode(tmp_path):
    r, _ = run_import_step(tmp_path, MODE="tree")
    assert r.returncode == 1 and "mode must be file or dir" in r.stdout


# --- the resolve-reported-threads composite ---------------------------------


def composite_script() -> str:
    """The composite's single step, lifted from its action.yml the way the
    composer tests lift theirs (text extraction; PyYAML is not a test
    dependency): the block under `run: |` at the composite's indent."""
    lines = COMPOSITE.read_text().splitlines()
    run_at = lines.index("      run: |")
    body = []
    for line in lines[run_at + 1:]:
        if line.strip() == "":
            body.append("")
        elif line.startswith("        "):
            body.append(line[8:])
        else:
            break
    return "\n".join(body) + "\n"


def run_composite(tmp_path, final: Path, stripped: Path):
    out = tmp_path / "github-output"
    out.write_text("")
    env = {
        **os.environ,
        "GH_TOKEN": "unused",
        "REPO": "o/r",
        "PR": "1",
        "FINAL": str(final),
        "STRIPPED": str(stripped),
        "RESOLVE": "false",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(out),
    }
    r = subprocess.run(["bash", "-c", composite_script()], text=True, capture_output=True, env=env, timeout=20, check=False)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
    return r, outputs


def test_composite_strips_and_reports_ids_from_a_regular_final_message(tmp_path):
    final = tmp_path / "codex-final.md"
    final.write_text("Addressed both.\n\nRESOLVED-THREADS: PRRT_a1, PRRT_b2\n")
    stripped = tmp_path / "codex-final.stripped.md"
    r, outputs = run_composite(tmp_path, final, stripped)
    assert r.returncode == 0, r.stderr
    assert stripped.read_text() == "Addressed both.\n"
    assert outputs == {"resolved": "0", "ids": "PRRT_a1 PRRT_b2"}


def test_composite_refuses_a_symlinked_final_message(tmp_path):
    secret = tmp_path / "runner-private"
    secret.write_text("RESOLVED-THREADS: PRRT_forged\nGH_TOKEN=ghs_secret\n")
    final = tmp_path / "codex-final.md"
    final.symlink_to(secret)
    stripped = tmp_path / "codex-final.stripped.md"
    r, outputs = run_composite(tmp_path, final, stripped)
    assert r.returncode == 0, r.stderr
    assert not stripped.exists()
    assert outputs == {"resolved": "0", "ids": ""}
    assert "::warning::" in r.stdout and "symlink" in r.stdout
    assert "ghs_secret" not in r.stdout + r.stderr


def test_composite_refuses_a_directory_at_the_final_message(tmp_path):
    final = tmp_path / "codex-final.md"
    final.mkdir()
    stripped = tmp_path / "codex-final.stripped.md"
    r, outputs = run_composite(tmp_path, final, stripped)
    assert r.returncode == 0, r.stderr
    assert not stripped.exists() and outputs == {"resolved": "0", "ids": ""}
    assert "::warning::" in r.stdout


# --- the callers -------------------------------------------------------------


CODEX_OWNED = "${{ runner.temp }}/codex/codex-final.md"
IMPORTED = "${{ runner.temp }}/codex-final.md"
CALLERS = {"claude.yml": "codexrun", "claude-auto-review.yml": "codexfix", "claude-auto.yml": "codexfix"}


@pytest.mark.parametrize("name,codex_step", sorted(CALLERS.items()))
def test_callers_read_only_the_imported_copy(name, codex_step):
    text = (WORKFLOWS / name).read_text()
    # The codex-owned path appears exactly once: codex-action's output-file.
    naming = [line for line in text.splitlines() if CODEX_OWNED in line]
    assert naming == [f"          output-file: {CODEX_OWNED}"], naming
    # Nothing else names the codex-owned directory at all (a step could
    # still reach it with a different spelling).
    assert not re.search(r"RUNNER_TEMP/codex/|temp \}\}/codex/(?!codex-final\.md)", text)
    # Every reader takes the imported copy: the commit step's subject source
    # and the summary's fallback (OUT / FINAL env), and the composite input.
    readers = [line.strip() for line in text.splitlines() if IMPORTED in line]
    assert readers, name
    for line in readers:
        assert line.split(":")[0] in ("OUT", "FINAL", "final-message"), line
    if name != "claude-auto.yml":
        assert f"final-message: {IMPORTED}" in text
    # The import step sits right after the reclaim (whose kill loop ends the
    # codex processes that could swap the entry), gated like the readers,
    # and best-effort.
    m = re.search(
        r"uses: meridianlabs-ai/agents/\.github/actions/reclaim-codex-workspace@main\n"
        r"(?:\n|      #.*\n)*"
        r"      - name: Import codex final message\n"
        r"        id: codexfinal\n"
        r"        continue-on-error: true\n"
        rf"        if: steps\.{codex_step}\.outcome == 'success' && steps\.codexreclaim\.outcome == 'success'\n"
        r"        uses: meridianlabs-ai/agents/\.github/actions/import-codex-final@main\n",
        text,
    )
    assert m, f"{name}: import step missing or not directly after the reclaim"


def test_import_action_defaults_match_the_callers_paths():
    text = IMPORT_ACTION.read_text()
    assert '--source "${SOURCE:-$RUNNER_TEMP/codex/codex-final.md}"' in text
    assert '--dest "${DEST:-$RUNNER_TEMP/codex-final.md}"' in text
    assert '--owner "${OWNER:-codex}"' in text
    assert "github.action_path }}/../../scripts/import_codex_final.py" in text
    # The mode defaults to the single file, so today's callers are unchanged;
    # dir mode has its own defaults (create-codex-user's claude-agent dir).
    assert "  mode:\n" in text and "    default: file\n" in text
    assert '--source "${SOURCE:-$RUNNER_TEMP/claude-agent}"' in text
    assert '--dest "${DEST:-$RUNNER_TEMP/landing}"' in text
    assert '--owner "${OWNER:-claude-agent}"' in text
