"""The codex path's runner-side search path (Claude Security finding 4628448;
design/codex-engine.md → Runner-side search path).

The runner prepends every GITHUB_PATH entry to the PATH of every later step
and resolves each step's shell interpreter through it, so a directory the
codex user can write — the workspace venv provisioning used to put there —
would hand codex the `sudo`, `git` or `jq` the first post-codex step runs
as `runner`. These tests lift the composites' `run:` blocks (PyYAML is not
a test dependency) and run them against a PLANTED `.venv/bin` full of
executables that log `HIJACKED` when run, first on the job PATH:

- `assert-runner-only-path`: the check `create-codex-user` runs before the
  grant and `reclaim-codex-workspace` runs after codex — a workspace entry
  (by path or through a symlink), a relative or empty entry, an entry or
  ancestor the user can write, a not-yet-existing entry the user could
  create, are refused with the entry named; the sticky-directory exception
  holds; a clean PATH passes; and the check's own probes never resolve
  through the PATH under test.
- `reclaim-codex-workspace`, `codex-usage`, `unresolved-merge-guard` and
  `emit-landing`'s `write` step complete their real work with the planted
  directory first on PATH and touch none of it.
- `provision-fallback` with `add-to-path: false` writes nothing to
  GITHUB_PATH (and both lines with the default).
- The wiring: the four workflows pass `add-to-path` from the gate's engine,
  the four compose steps discover the tools from `.venv/bin` first, the
  three commit steps pin PATH, and `create-codex-user` creates the user,
  runs the check, then grants — in that order.

`sudo` is a stub in the tests' "system" directory (no codex user and no
root here): it runs the command as the current user, answers `-u <user>
test -w`/`-O` probes from FAKE_CODEX_WRITABLE / FAKE_CODEX_OWNED, finds no
codex process to kill and makes `chown` a no-op. On macOS the same
directory carries a `cp` shim for GNU's `--remove-destination`.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_land_helpers import emit_landing_script, git  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = ROOT / ".github" / "actions"
ASSERT = ACTIONS / "assert-runner-only-path" / "action.yml"
CREATE = ACTIONS / "create-codex-user" / "action.yml"
RECLAIM = ACTIONS / "reclaim-codex-workspace" / "action.yml"
USAGE = ACTIONS / "codex-usage" / "action.yml"
GUARD = ACTIONS / "unresolved-merge-guard" / "action.yml"
PROVISION = ACTIONS / "provision-fallback" / "action.yml"
WORKFLOWS = {name: ROOT / ".github" / "workflows" / name
             for name in ("claude.yml", "claude-review.yml", "claude-auto.yml", "claude-auto-review.yml")}
ASSERT_USES = "uses: meridianlabs-ai/agents/.github/actions/assert-runner-only-path@main"
SYSTEM_DIRS = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ME = subprocess.run(["id", "-un"], check=True, text=True, capture_output=True).stdout.strip()

# The tools the planted directory shadows: everything the lifted scripts
# call by bare name, plus the interpreter names.
PLANTED = ("sudo", "bash", "sh", "git", "jq", "find", "sort", "cmp", "diff", "cp", "mv", "rm",
           "mkdir", "cat", "grep", "tr", "head", "cut", "mktemp", "sleep", "dirname", "basename",
           "id", "test", "pkill", "chown", "chmod")


def composite_runs(action: Path) -> list:
    """Every step's `run: |` block scalar of a composite, in order: the lines
    under each `      run: |` indented by its 8 spaces, up to the first that
    is not."""
    lines = action.read_text().splitlines()
    out = []
    for i, line in enumerate(lines):
        if line != "      run: |":
            continue
        body = []
        for later in lines[i + 1:]:
            if later.strip() == "":
                body.append("")
            elif later.startswith("        "):
                body.append(later[8:])
            else:
                break
        out.append("\n".join(body) + "\n")
    return out


def composite_steps(action: Path) -> list:
    """The `- ` step blocks of a composite's `steps:`, as text, in order."""
    text = action.read_text()
    steps_at = text.index("\nruns:\n")
    blocks = text[steps_at:].split("\n    - ")[1:]
    return blocks


SUDO_STUB = r"""#!/bin/bash
# Stand-in for /usr/bin/sudo: the command runs as the current user.
printf '%s\n' "$*" >>"$SUDO_LOG"
if [ "$1" = -u ]; then
  shift 2
  [ "$1" = -H ] && shift
  if [ "$1" = test ] && [ "$2" = -w ]; then grep -qxF -- "$3" <<<"${FAKE_CODEX_WRITABLE:-}"; exit; fi
  if [ "$1" = test ] && [ "$2" = -O ]; then grep -qxF -- "$3" <<<"${FAKE_CODEX_OWNED:-}"; exit; fi
  exec "$@"
fi
case "$1" in
  pkill) exit 1 ;;               # nothing runs as codex
  chown|adduser|usermod) exit 0 ;;
esac
exec "$@"
"""

CP_SHIM = r"""#!/bin/bash
# GNU cp's --remove-destination for the macOS cp.
if [ "$1" = --remove-destination ]; then shift; rm -f -- "${@: -1}"; fi
exec /bin/cp "$@"
"""

PLANT = r"""#!/bin/bash
# What a prompt-injected codex would leave in .venv/bin: log, then run the
# real tool so the step carries on (a real attacker's would too). Builtins
# and parameter expansion only until the PATH is swapped: a bare `basename`
# here would be THIS directory's, forever.
echo "HIJACKED:${0##*/} $*" >>"$HIJACK_LOG"
export PATH="$REAL_PATH"
exec "${0##*/}" "$@"
"""


@pytest.fixture
def world(tmp_path):
    """A workspace with a planted `.venv/bin`, a stub "system" directory and
    the two PATHs: the job PATH (planted first) and the system PATH the
    steps pin to (the stub directory, then this process's PATH)."""
    ws = tmp_path / "work" / "repo"
    ws.mkdir(parents=True)
    system = tmp_path / "system"
    system.mkdir()
    (system / "sudo").write_text(SUDO_STUB)
    (system / "sudo").chmod(0o755)
    if subprocess.run(["cp", "--remove-destination", "/dev/null", str(tmp_path / "probe")],
                      capture_output=True, check=False).returncode != 0:
        (system / "cp").write_text(CP_SHIM)
        (system / "cp").chmod(0o755)
    system_path = f"{system}:{os.environ['PATH']}"
    planted = ws / ".venv" / "bin"
    planted.mkdir(parents=True)
    for name in PLANTED:
        (planted / name).write_text(PLANT)
        (planted / name).chmod(0o755)
    temp = tmp_path / "_temp"
    temp.mkdir()
    hijack = tmp_path / "hijack.log"
    sudo_log = tmp_path / "sudo.log"
    env = {
        "GITHUB_WORKSPACE": str(ws),
        "RUNNER_TEMP": str(temp),
        "PATH": f"{planted}:{system_path}",
        "SYSTEM_PATH": system_path,
        "REAL_PATH": system_path,
        "HIJACK_LOG": str(hijack),
        "SUDO_LOG": str(sudo_log),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return {"tmp": tmp_path, "ws": ws, "planted": planted, "system": system, "system_path": system_path,
            "temp": temp, "hijack": hijack, "sudo_log": sudo_log, "env": env}


def run(script: str, w, cwd=None, **more) -> subprocess.CompletedProcess:
    """The lifted step under the job PATH. The interpreter is named by
    absolute path: on the runner it is chosen before the script runs, from a
    PATH `Create codex user` verified, so the tests stand in for that and
    leave the planted `bash` to the script's own command resolution."""
    env = {**w["env"], **{k: str(v) for k, v in more.items()}}
    return subprocess.run(["/bin/bash", "-c", script], cwd=cwd or w["ws"], text=True, capture_output=True, env=env,
                          check=False)


def hijacked(w) -> str:
    return w["hijack"].read_text() if w["hijack"].exists() else ""


def repo(w):
    """The workspace as a git repo with one commit (the run's start SHA)."""
    ws = w["ws"]
    git("init", "-q", "-b", "main", cwd=ws)
    git("config", "user.email", "t@example.com", cwd=ws)
    git("config", "user.name", "t", cwd=ws)
    (ws / "README").write_text("hello\n")
    git("add", "README", cwd=ws)
    git("commit", "-q", "-m", "start", cwd=ws)
    # No hooks: the reclaim's hooks branch uses `find -printf` and `mv -T`,
    # GNU-only, and is not what is under test here.
    shutil.rmtree(ws / ".git" / "hooks", ignore_errors=True)
    return git("rev-parse", "HEAD", cwd=ws).stdout.strip()


# --- assert-runner-only-path ---------------------------------------------------

ASSERT_SCRIPT = composite_runs(ASSERT)[0]


def check(w, job_path, user="nobody-here", **more):
    """The check with the given job PATH; `user` defaults to a login that does
    not exist, so only the path rules apply; pass ME to probe writability
    (answered by the stub sudo from FAKE_CODEX_WRITABLE / FAKE_CODEX_OWNED)."""
    return run(ASSERT_SCRIPT, w, PATH=job_path, USER_NAME=user, **more)


def test_refuses_a_workspace_directory_on_the_job_path(world):
    w = world
    r = check(w, f"{w['planted']}:{w['system_path']}")
    assert r.returncode == 1
    assert f"::error::job PATH entry '{w['planted']}' lies inside the workspace" in r.stdout
    assert hijacked(w) == ""


def test_refuses_the_workspace_itself_and_a_symlink_into_it(world):
    w = world
    assert check(w, f"{w['ws']}:{w['system_path']}").returncode == 1
    link = w["tmp"] / "tools"
    link.symlink_to(w["planted"])
    r = check(w, f"{link}:{w['system_path']}")
    assert r.returncode == 1 and f"'{link}' lies inside the workspace" in r.stdout
    # A workspace given through a symlink is resolved the same way.
    wslink = w["tmp"] / "wslink"
    wslink.symlink_to(w["ws"])
    r = check(w, f"{w['planted']}:{w['system_path']}", GITHUB_WORKSPACE=str(wslink))
    assert r.returncode == 1 and "lies inside the workspace" in r.stdout


def test_refuses_relative_and_empty_entries(world):
    w = world
    r = check(w, f"bin:{w['system_path']}")
    assert r.returncode == 1 and "'bin' is a relative path" in r.stdout
    r = check(w, f".:{w['system_path']}")
    assert r.returncode == 1 and "'.' is a relative path" in r.stdout
    # An empty component means the current directory too: in the middle and
    # as a trailing colon.
    r = check(w, f"{w['system_path']}::/usr/bin")
    assert r.returncode == 1 and "is empty" in r.stdout
    r = check(w, f"{w['system_path']}:")
    assert r.returncode == 1 and "is empty" in r.stdout


def test_a_clean_path_passes_and_the_system_directories_are_checked_too(world):
    w = world
    r = check(w, w["system_path"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "none inside the workspace" in r.stdout and "writable" not in r.stdout
    # With the user present every entry and ancestor is probed as that user.
    r = check(w, w["system_path"], user=ME)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"or writable by {ME}" in r.stdout
    probes = w["sudo_log"].read_text().splitlines()
    for d in ("/usr/bin", "/usr", "/"):
        assert f"-u {ME} test -w {d}" in probes
    assert f"-u {ME} test -w {w['system']}" in probes and f"-u {ME} test -w {w['tmp']}" in probes


def test_refuses_an_entry_or_ancestor_the_user_can_write(world):
    w = world
    tools = w["tmp"] / "opt" / "tools" / "bin"
    tools.mkdir(parents=True)
    job = f"{tools}:{w['system_path']}"
    r = check(w, job, user=ME, FAKE_CODEX_WRITABLE=str(tools))
    assert r.returncode == 1 and f"'{tools}' is writable by the {ME} user (at {tools})" in r.stdout
    # A writable ANCESTOR is a rename-and-replace path (write on the parent
    # is all a same-parent rename needs).
    r = check(w, job, user=ME, FAKE_CODEX_WRITABLE=str(w["tmp"] / "opt"))
    assert r.returncode == 1 and f"(at {w['tmp'] / 'opt'})" in r.stdout
    r = check(w, job, user=ME, FAKE_CODEX_WRITABLE="")
    assert r.returncode == 0, r.stdout + r.stderr


def test_refuses_a_not_yet_existing_entry_the_user_could_create(world):
    w = world
    parent = w["tmp"] / "later"
    parent.mkdir()
    missing = parent / "bin"
    job = f"{missing}:{w['system_path']}"
    r = check(w, job, user=ME, FAKE_CODEX_WRITABLE=str(parent))
    assert r.returncode == 1 and f"'{missing}' is writable by the {ME} user (at {parent})" in r.stdout
    # Nobody can create it: accepted (the runner's own later addition).
    r = check(w, job, user=ME, FAKE_CODEX_WRITABLE="")
    assert r.returncode == 0, r.stdout + r.stderr
    # A missing entry under the workspace is refused by path, user or not.
    r = check(w, f"{w['ws'] / 'nope' / 'bin'}:{w['system_path']}")
    assert r.returncode == 1 and "lies inside the workspace" in r.stdout


def test_sticky_directory_allows_only_an_existing_child_the_user_does_not_own(world):
    w = world
    sticky = w["tmp"] / "tmp"
    sticky.mkdir()
    sticky.chmod(0o1777)
    mine = sticky / "runner-bin"
    mine.mkdir()
    job = f"{mine}:{w['system_path']}"
    # /tmp-like: the user can create there but not rename our directory away.
    r = check(w, job, user=ME, FAKE_CODEX_WRITABLE=str(sticky))
    assert r.returncode == 0, r.stdout + r.stderr
    # ... unless the child is the user's own,
    r = check(w, job, user=ME, FAKE_CODEX_WRITABLE=str(sticky), FAKE_CODEX_OWNED=str(mine))
    assert r.returncode == 1 and f"(at {sticky})" in r.stdout
    # ... or does not exist yet (the user creates it),
    r = check(w, f"{sticky / 'not-yet'}:{w['system_path']}", user=ME, FAKE_CODEX_WRITABLE=str(sticky))
    assert r.returncode == 1 and f"(at {sticky})" in r.stdout
    # ... and without the sticky bit a writable parent is refused outright.
    sticky.chmod(0o777)
    r = check(w, job, user=ME, FAKE_CODEX_WRITABLE=str(sticky))
    assert r.returncode == 1 and f"(at {sticky})" in r.stdout


def test_the_checks_own_probes_never_resolve_through_the_job_path(world):
    # A planted directory OUTSIDE the workspace, first on the job PATH and
    # not flagged as writable: the entries pass, and every tool the check
    # ran (sudo, tr, dirname, basename, id) came from the pinned system
    # PATH, not from the entry under test.
    w = world
    evil = w["tmp"] / "evil"
    shutil.copytree(w["planted"], evil)
    r = check(w, f"{evil}:{w['system_path']}", user=ME)
    assert r.returncode == 0, r.stdout + r.stderr
    assert hijacked(w) == ""
    assert w["sudo_log"].read_text()  # the stub sudo, from the system PATH, did the probing


# --- create-codex-user: user, check, grant — in that order --------------------


def test_create_codex_user_checks_the_path_after_the_user_and_before_the_grant():
    steps = composite_steps(CREATE)
    assert len(steps) == 3, [s.splitlines()[0] for s in steps]
    first, check_step, grant = steps
    assert "sudo adduser --system --home /home/codex --shell /bin/bash --group codex" in first
    assert "sudo usermod -a -G runner codex" in first
    assert "chown" not in first and "chmod" not in first
    assert ASSERT_USES in check_step and "user: codex" in check_step
    assert 'sudo chown -R runner:codex "$GITHUB_WORKSPACE"' in grant
    assert 'sudo chmod -R g+rwX "$GITHUB_WORKSPACE"' in grant
    # The snapshots stay the first thing, before any grant.
    assert first.index('cp "$GITHUB_WORKSPACE/.git/config"') < first.index("sudo adduser")


def test_assert_runner_only_path_defaults():
    text = ASSERT.read_text()
    assert "default: codex" in text
    assert f"default: {SYSTEM_DIRS}" in text
    # The script pins its own PATH before the first external command.
    script = ASSERT_SCRIPT
    assert script.index('export PATH="$SYSTEM_PATH"') < script.index("sudo ")
    assert script.index('export PATH="$SYSTEM_PATH"') < script.index("tr ")


# --- reclaim-codex-workspace against a planted .venv/bin -----------------------


def reclaim_fixture(w):
    """A codex run just ended: the repo, the pre-codex snapshots `Create codex
    user` writes, and a config codex tampered with."""
    repo(w)
    ws, temp = w["ws"], w["temp"]
    snapshot = temp / "git-config.pre-codex"
    shutil.copy(ws / ".git" / "config", snapshot)
    embedded = temp / "embedded-git.pre-codex"
    embedded.write_text("")
    with open(ws / ".git" / "config", "a") as f:
        f.write('[core]\n\tsshCommand = "echo TAMPERED"\n')
    return snapshot, embedded


def test_reclaim_runs_the_nested_check_first_then_pins_its_path():
    steps = composite_steps(RECLAIM)
    assert len(steps) == 2
    assert ASSERT_USES in steps[0] and "user: codex" in steps[0]
    assert "system-path: ${{ inputs.system-path }}" in steps[0]
    script = composite_runs(RECLAIM)[0]
    body = script[script.index("set -euo pipefail"):]
    assert body.index('export PATH="$SYSTEM_PATH"') < body.index("sudo pkill")
    # Nothing but comments and the shell options before the pin.
    before = body[:body.index('export PATH="$SYSTEM_PATH"')]
    assert all(line.startswith("#") or line == "set -euo pipefail" for line in before.splitlines() if line), before
    assert f"default: {SYSTEM_DIRS}" in RECLAIM.read_text()


def test_reclaim_ignores_a_planted_venv_on_the_job_path(world):
    w = world
    snapshot, embedded = reclaim_fixture(w)
    r = run(composite_runs(RECLAIM)[0], w, SNAPSHOT=snapshot, EMBEDDED_SNAPSHOT=embedded)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "reclaimed .git from codex" in r.stdout
    # The reclaim did its work with the system tools...
    config = (w["ws"] / ".git" / "config").read_text()
    assert "TAMPERED" not in config
    assert f"hooksPath = {w['temp']}/no-hooks" in config and "fsmonitor = false" in config
    calls = w["sudo_log"].read_text()
    assert "pkill -KILL -u codex" in calls and f"chmod -R g-w {w['ws']}/.git" in calls
    # ... and none of the planted ones ran.
    assert hijacked(w) == "", hijacked(w)


def test_reclaim_still_refuses_what_it_refused_before(world):
    # The pin changes what runs, not what is checked: a redirected git dir is
    # still refused, through the system tools.
    w = world
    snapshot, embedded = reclaim_fixture(w)
    (w["ws"] / ".git" / "commondir").write_text("/tmp/x\n")
    r = run(composite_runs(RECLAIM)[0], w, SNAPSHOT=snapshot, EMBEDDED_SNAPSHOT=embedded)
    assert r.returncode == 1
    assert "redirected git dir" in r.stdout
    assert hijacked(w) == ""


# --- codex-usage --------------------------------------------------------------

ROLLOUT = (
    '{"timestamp":"t","type":"turn_context","payload":{"model":"gpt-5.1-codex","effort":"high"}}\n'
    '{"timestamp":"t","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":'
    '{"input_tokens":1000,"cached_input_tokens":400,"output_tokens":200,"reasoning_output_tokens":50,"total_tokens":1200}}}}\n'
)


def test_codex_usage_reads_the_rollout_with_the_planted_venv_on_the_job_path(world):
    w = world
    home = w["tmp"] / "codex-home"
    sessions = home / "sessions" / "2026" / "09" / "22"
    sessions.mkdir(parents=True)
    (sessions / "rollout-1.jsonl").write_text(ROLLOUT)
    summary = w["tmp"] / "summary.md"
    output = w["tmp"] / "output.txt"
    summary.write_text("")
    output.write_text("")
    r = run(composite_runs(USAGE)[0], w, CODEX_HOME_DIR=home, REQ_MODEL="", REQ_EFFORT="",
            GITHUB_STEP_SUMMARY=summary, GITHUB_OUTPUT=output)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "input=1000\ncached=400\noutput=200\nreasoning=50\ntotal=1200\nblended=800\nthreads=1\n" in output.read_text()
    assert "| 1000 | 400 | 200 | 50 | 1200 | 800 |" in summary.read_text()
    assert "sudo test -d" not in hijacked(w) and hijacked(w) == "", hijacked(w)
    calls = w["sudo_log"].read_text()
    assert f"test -d {home}/sessions" in calls and "cat " in calls
    script = composite_runs(USAGE)[0]
    assert script.index('export PATH="$SYSTEM_PATH"') < script.index("sudo test -d")


# --- unresolved-merge-guard ----------------------------------------------------


def test_guard_runs_git_from_the_system_path_not_the_planted_venv(world):
    w = world
    repo(w)
    gitdir = w["ws"] / ".git"
    env = {"CONFLICTS": "", "GIT_DIR": gitdir, "GIT_COMMON_DIR": gitdir, "GIT_WORK_TREE": w["ws"],
           "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "false"}
    r = run(composite_runs(GUARD)[0], w, **env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "no unresolved merge state." in r.stdout
    assert hijacked(w) == "", hijacked(w)
    # And it still guards: a conflict marker in a reported file is refused.
    (w["ws"] / "README").write_text("<<<<<<< HEAD\nx\n>>>>>>> theirs\n")
    r = run(composite_runs(GUARD)[0], w, **{**env, "CONFLICTS": "README"})
    assert r.returncode == 1 and "conflict markers remain in README" in r.stdout
    assert hijacked(w) == ""
    script = composite_runs(GUARD)[0]
    assert script.index('export PATH="$SYSTEM_PATH"') < script.index("git ")


# --- emit-landing -----------------------------------------------------------------


def test_emit_landing_bundles_with_git_from_the_system_path(world):
    w = world
    start = repo(w)
    (w["ws"] / "README").write_text("changed\n")
    git("commit", "-q", "-am", "codex work", cwd=w["ws"])
    head = git("rev-parse", "HEAD", cwd=w["ws"]).stdout.strip()
    landing = w["tmp"] / "landing"
    output = w["tmp"] / "out.txt"
    output.write_text("")
    r = run(emit_landing_script(), w, START_SHA=start, BRANCH="claude/issue-1-codex", PR_NUMBER="", ISSUE_NUMBER="1",
            EXTRA="", DIR=landing, READ_ONLY="false", REPO="o/r", RUN_ID="7", GITHUB_OUTPUT=output)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"head_sha={head}" in output.read_text() and "has_bundle=true" in output.read_text()
    assert (landing / "commits.bundle").is_file() and (landing / "manifest.json").is_file()
    assert hijacked(w) == "", hijacked(w)


# --- provision-fallback -----------------------------------------------------------


def provision(w, add_to_path: str):
    """The composite's script with the network and uv stubbed: `curl | sh`
    installs nothing, `uv venv` makes `.venv/bin` with a python that has no
    tiktoken, `uv pip install` is a no-op."""
    stubs = w["tmp"] / f"stubs-{add_to_path}"
    stubs.mkdir()
    (stubs / "curl").write_text("#!/bin/bash\nexit 0\n")
    (stubs / "uv").write_text(
        "#!/bin/bash\n"
        'if [ "$1" = venv ]; then mkdir -p .venv/bin; printf \'#!/bin/bash\\nexit 1\\n\' >.venv/bin/python; chmod +x .venv/bin/python; fi\n'
        'echo "uv $*" >>"$UV_LOG"\n')
    for f in stubs.iterdir():
        f.chmod(0o755)
    ws = w["tmp"] / f"proj-{add_to_path}"
    (ws / ".git" / "info").mkdir(parents=True)
    (ws / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n')
    home = w["tmp"] / f"home-{add_to_path}"
    home.mkdir()
    github_path = w["tmp"] / f"github_path-{add_to_path}"
    github_path.write_text("")
    env = {"PATH": f"{stubs}:{os.environ['PATH']}", "HOME": str(home), "GITHUB_PATH": str(github_path),
           "ADD_TO_PATH": add_to_path, "UV_LOG": str(w["tmp"] / f"uv-{add_to_path}.log")}
    r = subprocess.run(["/bin/bash", "-c", composite_runs(PROVISION)[0]], cwd=ws, text=True, capture_output=True, env=env,
                       check=False)
    return r, ws, github_path.read_text(), Path(env["UV_LOG"]).read_text()


def test_provision_fallback_add_to_path_false_writes_nothing_to_github_path(world):
    r, ws, github_path, uv_log = provision(world, "false")
    assert r.returncode == 0, r.stdout + r.stderr
    assert github_path == ""
    assert f"nothing added to the job PATH (tools at {ws}/.venv/bin)" in r.stdout
    # The venv is still made and installed, and excluded from git.
    assert "uv venv" in uv_log and 'uv pip install -e .[dev]' in uv_log
    assert (ws / ".git" / "info" / "exclude").read_text() == ".venv/\n*.egg-info/\n"


def test_provision_fallback_default_puts_both_directories_on_the_job_path(world):
    r, ws, github_path, _ = provision(world, "true")
    assert r.returncode == 0, r.stdout + r.stderr
    assert github_path.splitlines() == [f"{world['tmp'] / 'home-true'}/.local/bin", f"{ws}/.venv/bin"]
    assert 'default: "true"' in PROVISION.read_text()


# --- the workflows' wiring ----------------------------------------------------------


def step_text(workflow: Path, name: str) -> str:
    """A job step's text, from its `- name:` line to the next step's."""
    text = workflow.read_text()
    start = text.index(f"      - name: {name}\n")
    end = text.find("\n      - name: ", start + 1)
    return text[start:end if end != -1 else None]


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_provision_fallback_gets_add_to_path_from_the_gates_engine(name):
    step = step_text(WORKFLOWS[name], "Provision project environment (fallback)")
    assert "uses: meridianlabs-ai/agents/.github/actions/provision-fallback@main" in step
    assert "add-to-path: ${{ needs.gate.outputs.engine == 'codex' && 'false' || 'true' }}" in step


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_codex_tool_discovery_looks_in_the_checkouts_venv_first(name):
    text = WORKFLOWS[name].read_text()
    loop = ('for t in pytest ruff mypy pyright python3; do\n'
            '{i}  path="$GITHUB_WORKSPACE/.venv/bin/$t"\n'
            '{i}  [ -x "$path" ] || path=$(command -v "$t" 2>/dev/null || true)\n')
    indent = "          " if name == "claude-review.yml" else "            "
    assert loop.format(i=indent) in text, name
    assert text.count("for t in pytest ruff mypy pyright python3; do") == 1


@pytest.mark.parametrize("name,step", [("claude.yml", "Commit codex work"),
                                       ("claude-auto.yml", "Commit codex fix"),
                                       ("claude-auto-review.yml", "Commit codex fix")])
def test_commit_steps_pin_path_before_their_first_command(name, step):
    text = step_text(WORKFLOWS[name], step)
    body = text[text.index("run: |"):]
    pin = f"export PATH={SYSTEM_DIRS}\n"
    assert pin in body
    assert body.index(pin) < body.index("git ")


def test_post_codex_composites_take_a_system_path_and_pin_it():
    for action in (RECLAIM, USAGE, GUARD, ACTIONS / "emit-landing" / "action.yml"):
        text = action.read_text()
        assert f"default: {SYSTEM_DIRS}" in text, action
        assert "SYSTEM_PATH: ${{ inputs.system-path }}" in text, action
        assert 'export PATH="$SYSTEM_PATH"' in text, action


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_surface_names_the_path_refusal_as_a_user_setup_cause(name):
    assert "a job PATH entry inside the workspace or writable by the codex user" in WORKFLOWS[name].read_text()
