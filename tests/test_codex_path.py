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
root here): it runs the command as the current user; a `-u <user> test -w`
probe answers from the real mode bits of paths under the test's directory
(world-writable = writable by the user, so `chmod o+w` makes a directory
codex-writable and the check's `chmod go-w` protection is observable;
nothing outside FAKE_ROOT is ever "writable", whatever the host's own
quirks) or from FAKE_CODEX_OWNED (paths the user "owns": `find -user`
positive, and writable when the owner write bit is set, as for a real
owner); `chown` moves a path out of that list (logged to CHOWN_LOG) so
protection of an owned path is observable too; the candidate `find` gets
`-user` answered from the list; `pkill` finds nothing. On the runner the
check re-executes itself once under sudo and probes directly; the tests set
ASSERT_RUNNER_ONLY_PATH_ELEVATED so it stays this user and goes through the
stub instead. On macOS the same
directory carries a `cp` shim for GNU's `--remove-destination`. The job
PATH under test ends in the stub directory and `/bin` (`tail`), not this
process's whole PATH: the check now follows every symlink in every entry,
and a host's own link farms are not what is under test.
"""

import os
import re
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
SYSTEM_DIRS = "/usr/sbin:/usr/bin:/sbin:/bin"
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
# "Owned by the user": listed in FAKE_CODEX_OWNED and not chowned away since.
owned() { grep -qxF -- "$1" <<<"${FAKE_CODEX_OWNED:-}" && ! grep -qxF -- "$1" "${CHOWN_LOG:-/dev/null}" 2>/dev/null; }
# Under the test's directory, by physical path (macOS: /var is /private/var,
# and the check hands over paths both as written and resolved).
root_real=$(cd "${FAKE_ROOT:?}" && pwd -P)
under_root() { local d; d=$(cd "$(dirname "$1")" 2>/dev/null && pwd -P) || return 1; case "$d/" in "$root_real"/*) return 0 ;; esac; return 1; }
if [ "$1" = -u ]; then
  user=$2; shift 2
  [ "$1" = -H ] && shift
  if [ "$1" = test ] && [ "$2" = -w ]; then
    # Like test -w, a symlink is judged by its target (-L), never by itself.
    # An owner writes when its own write bit is set; anyone writes what is
    # world-writable — under FAKE_ROOT only (the host's own tree is never
    # "writable" here).
    if [ ! -L "$3" ] && owned "$3"; then [ -n "$(find "$3" -maxdepth 0 -perm -0200 2>/dev/null)" ]; exit; fi
    under_root "$3" || exit 1
    [ -n "$(find -L "$3" -maxdepth 0 -perm -0002 2>/dev/null)" ]; exit
  fi
  exec "$@"
fi
if [ "$1" = find ] && [ "$3" = -maxdepth ] && [ "$5" = -user ]; then
  # `find <p> -maxdepth 0 -user <u>`: the ownership probe, from the list.
  owned "$2" && printf '%s\n' "$2"; exit 0
fi
if [ "$1" = find ] && [ "$3" = -mindepth ]; then
  # The candidate scan: run it with `-user <u>` answered false, then add the
  # "owned" entries of that directory from the list.
  shift; dir=$1; args=(); skip=""
  for a in "$@"; do
    if [ -n "$skip" ]; then skip=""; continue; fi
    if [ "$a" = -user ]; then args+=(-false); skip=1; else args+=("$a"); fi
  done
  find "${args[@]}" || exit $?   # a missing directory fails, as the real one does
  while IFS= read -r o; do
    [ -n "$o" ] && [ "$(dirname "$o")" = "$dir" ] && owned "$o" && printf '%s\n' "$o"
  done <<<"${FAKE_CODEX_OWNED:-}"
  exit 0
fi
case "$1" in
  pkill) exit 1 ;;               # nothing runs as codex
  chown) printf '%s\n' "$3" >>"${CHOWN_LOG:-/dev/null}"; exit 0 ;;
  adduser|usermod) exit 0 ;;
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
    # The system PATH the steps pin to: the stub directory, then the
    # directories the real tools live in — not this process's whole PATH,
    # whose extra entries (a macOS /System/Cryptexes chain passes through a
    # world-writable directory) are not what is under test.
    tool_dirs = []
    for tool in ("git", "jq", "find", "sort", "cmp", "diff", "cp", "mv", "rm", "mkdir", "cat", "grep", "tr",
                 "head", "cut", "mktemp", "sleep", "dirname", "basename", "id", "pkill", "chmod", "wc", "sed"):
        found = shutil.which(tool)
        assert found, tool
        d = os.path.dirname(found)
        if d not in tool_dirs:
            tool_dirs.append(d)
    system_path = ":".join([str(system), *tool_dirs])
    planted = ws / ".venv" / "bin"
    planted.mkdir(parents=True)
    for name in PLANTED:
        (planted / name).write_text(PLANT)
        (planted / name).chmod(0o755)
    temp = tmp_path / "_temp"
    temp.mkdir()
    hijack = tmp_path / "hijack.log"
    sudo_log = tmp_path / "sudo.log"
    chown_log = tmp_path / "chown.log"
    env = {
        "GITHUB_WORKSPACE": str(ws),
        "RUNNER_TEMP": str(temp),
        "PATH": f"{planted}:{system_path}",
        "SYSTEM_PATH": system_path,
        "REAL_PATH": system_path,
        "HIJACK_LOG": str(hijack),
        "SUDO_LOG": str(sudo_log),
        "CHOWN_LOG": str(chown_log),
        "FAKE_ROOT": str(tmp_path),
        # The check re-executes itself once under sudo on the runner; here it
        # runs as this user against the stub, so the elevation is skipped.
        "ASSERT_RUNNER_ONLY_PATH_ELEVATED": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return {"tmp": tmp_path, "ws": ws, "planted": planted, "system": system, "system_path": system_path,
            "tail": f"{system}:/bin", "temp": temp, "hijack": hijack, "sudo_log": sudo_log,
            "chown_log": chown_log, "env": env}


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


def check(w, job_path, user="nobody-here", protect="false", **more):
    """The check with the given job PATH; `user` defaults to a login that does
    not exist, so only the path rules apply; pass ME to probe writability
    (answered by the stub sudo from mode bits and FAKE_CODEX_OWNED)."""
    return run(ASSERT_SCRIPT, w, PATH=job_path, USER_NAME=user, PROTECT=protect, **more)


def writable(*paths):
    """World-writable: what the stub sudo reports as writable by the user."""
    for p in paths:
        p.chmod(p.stat().st_mode | 0o002)


def test_refuses_a_workspace_directory_on_the_job_path(world):
    w = world
    r = check(w, f"{w['planted']}:{w['tail']}")
    assert r.returncode == 1
    assert f"::error::job PATH entry '{w['planted']}' lies inside the workspace" in r.stdout
    assert hijacked(w) == ""


def test_refuses_the_workspace_itself_and_a_symlink_into_it(world):
    w = world
    assert check(w, f"{w['ws']}:{w['tail']}").returncode == 1
    link = w["tmp"] / "tools"
    link.symlink_to(w["planted"])
    r = check(w, f"{link}:{w['tail']}")
    assert r.returncode == 1 and f"'{link}' lies inside the workspace" in r.stdout
    # The link's target chain is walked first, so the workspace hop it
    # reaches is what the refusal names.
    assert f"(at {w['planted']})" in r.stdout
    # A workspace given through a symlink is resolved the same way.
    wslink = w["tmp"] / "wslink"
    wslink.symlink_to(w["ws"])
    r = check(w, f"{w['planted']}:{w['tail']}", GITHUB_WORKSPACE=str(wslink))
    assert r.returncode == 1 and "lies inside the workspace" in r.stdout


def test_refuses_relative_and_empty_entries(world):
    w = world
    r = check(w, f"bin:{w['tail']}")
    assert r.returncode == 1 and "'bin' is a relative path" in r.stdout
    r = check(w, f".:{w['tail']}")
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
    # With the user present every entry and ancestor is probed as that user
    # — the real tool directories included, symlinked tools and all.
    r = check(w, w["system_path"], user=ME)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"or owned or writable by {ME}" in r.stdout
    probes = w["sudo_log"].read_text().splitlines()
    for d in (os.path.dirname(shutil.which("cat")), "/"):
        assert f"-u {ME} test -w {d}" in probes


def test_refuses_an_entry_or_ancestor_the_user_can_write(world):
    w = world
    opt = w["tmp"] / "opt"
    tools = opt / "tools" / "bin"
    tools.mkdir(parents=True)
    job = f"{tools}:{w['tail']}"
    r = check(w, job, user=ME)
    assert r.returncode == 0, r.stdout + r.stderr
    writable(tools)
    r = check(w, job, user=ME)
    assert r.returncode == 1 and f"'{tools}' is writable by the {ME} user (at {tools})" in r.stdout
    tools.chmod(0o755)
    # A writable ANCESTOR is a rename-and-replace path (write on the parent
    # is all a same-parent rename needs).
    writable(opt)
    r = check(w, job, user=ME)
    assert r.returncode == 1 and f"(at {opt})" in r.stdout
    # So is one the user owns, whatever its mode (an owner can chmod it).
    opt.chmod(0o755)
    r = check(w, job, user=ME, FAKE_CODEX_OWNED=str(opt))
    assert r.returncode == 1 and f"is owned by the {ME} user (at {opt})" in r.stdout
    opt.chmod(0o555)
    r = check(w, job, user=ME, FAKE_CODEX_OWNED=str(opt))
    assert r.returncode == 1 and f"is owned by the {ME} user (at {opt})" in r.stdout
    # ... and protection takes it away (chown), after which it passes.
    r = check(w, job, user=ME, protect="true", FAKE_CODEX_OWNED=str(opt))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"protected job PATH hop {opt}" in r.stdout
    assert str(opt) in w["chown_log"].read_text()


def test_refuses_a_not_yet_existing_entry_the_user_could_create(world):
    w = world
    parent = w["tmp"] / "later"
    parent.mkdir()
    missing = parent / "bin"
    job = f"{missing}:{w['tail']}"
    writable(parent)
    r = check(w, job, user=ME)
    assert r.returncode == 1 and f"'{missing}' is writable by the {ME} user (at {parent})" in r.stdout
    # Nobody can create it: accepted (the runner's own later addition) —
    # also when more than one level is missing (`~/.local/bin` before uv
    # is installed).
    parent.chmod(0o755)
    r = check(w, job, user=ME)
    assert r.returncode == 0, r.stdout + r.stderr
    r = check(w, f"{parent / 'deeper' / 'bin'}:{w['tail']}", user=ME)
    assert r.returncode == 0, r.stdout + r.stderr
    # A missing entry under the workspace is refused by path, user or not.
    r = check(w, f"{w['ws'] / 'nope' / 'bin'}:{w['tail']}")
    assert r.returncode == 1 and "lies inside the workspace" in r.stdout


def test_sticky_directory_allows_only_an_existing_child_the_user_does_not_own(world):
    w = world
    sticky = w["tmp"] / "tmp"
    sticky.mkdir()
    sticky.chmod(0o1777)
    mine = sticky / "runner-bin"
    mine.mkdir()
    job = f"{mine}:{w['tail']}"
    # /tmp-like: the user can create there but not rename our directory away.
    r = check(w, job, user=ME)
    assert r.returncode == 0, r.stdout + r.stderr
    # ... unless the child is the user's own (refused at the child itself:
    # an owner can chmod and write it),
    r = check(w, job, user=ME, FAKE_CODEX_OWNED=str(mine))
    assert r.returncode == 1 and f"is owned by the {ME} user (at {mine})" in r.stdout
    # ... or the sticky directory ITSELF is the user's: its owner may rename
    # or unlink anyone's entries there (unlink(2)), so no exception,
    r = check(w, job, user=ME, FAKE_CODEX_OWNED=str(sticky))
    assert r.returncode == 1 and f"is owned by the {ME} user (at {sticky})" in r.stdout
    r = check(w, job, user=ME, protect="true", FAKE_CODEX_OWNED=str(sticky))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"protected job PATH hop {sticky}" in r.stdout
    assert not sticky.stat().st_mode & 0o002 and sticky.stat().st_mode & 0o1000  # go-w, sticky bit kept
    sticky.chmod(0o1777)  # back to /tmp's shape for the cases below
    # ... a symlink child the user owns counts too (lstat, not its target —
    # the owner of a link can repoint it),
    link = sticky / "runner-link"
    link.symlink_to(mine)
    r = check(w, f"{link}:{w['tail']}", user=ME, FAKE_CODEX_OWNED=str(link))
    assert r.returncode == 1 and f"is owned by the {ME} user (at {link})" in r.stdout
    # ... or does not exist yet (the user creates it),
    r = check(w, f"{sticky / 'not-yet'}:{w['tail']}", user=ME)
    assert r.returncode == 1 and f"(at {sticky})" in r.stdout
    # ... and without the sticky bit a writable parent is refused outright.
    sticky.chmod(0o777)
    r = check(w, job, user=ME)
    assert r.returncode == 1 and f"(at {sticky})" in r.stdout


def test_a_sticky_directory_accepted_for_one_child_vouches_for_no_other(world):
    # Review round 3 of #131: the cache remembered a writable sticky
    # directory as safe after a safe child, so a later entry through it —
    # a missing child codex could create, the directory itself, a dangling
    # link into it — was waved through with the planted interpreter to
    # follow.
    w = world
    sticky = w["tmp"] / "tmp"
    sticky.mkdir()
    sticky.chmod(0o1777)
    good = sticky / "runner-bin"
    good.mkdir()
    # The safe child alone, and ahead of the unsafe uses: accepted on its own.
    r = check(w, f"{good}:{w['tail']}", user=ME)
    assert r.returncode == 0, r.stdout + r.stderr
    # 1. a missing child after the safe sibling.
    missing = sticky / "not-yet" / "bin"
    for protect in ("false",):
        r = check(w, f"{good}:{missing}:{w['tail']}", user=ME, protect=protect)
        assert r.returncode == 1 and f"'{missing}' is writable by the {ME} user (at {sticky})" in r.stdout
    # 2. the sticky directory itself as an entry, after the safe sibling.
    r = check(w, f"{good}:{sticky}:{w['tail']}", user=ME)
    assert r.returncode == 1 and f"'{sticky}' is writable by the {ME} user (at {sticky})" in r.stdout
    # 3. a runner-only directory whose `bash` dangles into it.
    links = w["tmp"] / "runner-links"
    links.mkdir()
    (links / "bash").symlink_to(sticky / "not-yet" / "bash")
    r = check(w, f"{good}:{links}:{w['tail']}", user=ME)
    assert r.returncode == 1 and f"'{links}' is writable by the {ME} user (at {sticky})" in r.stdout
    assert sticky.stat().st_mode & 0o1777 == 0o1777  # nothing touched without protect
    # With protect the sticky directory is made runner-only for the unsafe
    # use, and the safe sibling keeps working.
    r = check(w, f"{good}:{missing}:{w['tail']}", user=ME, protect="true")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"protected job PATH hop {sticky}" in r.stdout
    assert not sticky.stat().st_mode & 0o002 and sticky.stat().st_mode & 0o1000


def test_symlink_hops_are_checked_where_they_live_not_only_where_they_point(world):
    # Review round 1 of #131: resolving the entry first and checking the
    # target let `ws/tools -> /usr/bin` through; after the grant codex
    # replaces `tools` with a directory holding `bash`.
    w = world
    out = w["ws"] / "tools"
    out.symlink_to("/usr/bin")
    r = check(w, f"{out}:{w['tail']}")
    assert r.returncode == 1, r.stdout
    assert f"'{out}' lies inside the workspace" in r.stdout and f"(at {w['ws']})" in r.stdout
    # An external symlink under a directory the user can write: the link is
    # the user's to replace.
    parent = w["tmp"] / "codex-owned"
    parent.mkdir()
    link = parent / "tools"
    link.symlink_to("/usr/bin")
    writable(parent)
    r = check(w, f"{link}:{w['tail']}", user=ME)
    assert r.returncode == 1 and f"'{link}' is writable by the {ME} user (at {parent})" in r.stdout
    parent.chmod(0o755)
    r = check(w, f"{link}:{w['tail']}", user=ME)
    assert r.returncode == 0, r.stdout + r.stderr
    # An intermediate symlink component: `x/link/bin` with `link -> target`
    # is refused for what `target` is (writable) or where it is (the
    # workspace), and passes when neither.
    x = w["tmp"] / "x"
    x.mkdir()
    target = w["tmp"] / "target"
    (target / "bin").mkdir(parents=True)
    (x / "link").symlink_to(target)
    entry = x / "link" / "bin"
    writable(target)
    r = check(w, f"{entry}:{w['tail']}", user=ME)
    assert r.returncode == 1 and f"(at {target})" in r.stdout
    target.chmod(0o755)
    r = check(w, f"{entry}:{w['tail']}", user=ME)
    assert r.returncode == 0, r.stdout + r.stderr
    (x / "link").unlink()
    (x / "link").symlink_to(w["ws"] / "sub")
    (w["ws"] / "sub" / "bin").mkdir(parents=True)
    r = check(w, f"{entry}:{w['tail']}")
    assert r.returncode == 1 and "lies inside the workspace" in r.stdout
    # A relative link target is resolved against the link's directory (and
    # reported as walked, `..` and all).
    (x / "link").unlink()
    (x / "link").symlink_to("../target")
    writable(target)
    r = check(w, f"{entry}:{w['tail']}", user=ME)
    assert r.returncode == 1 and f"(at {x}/../target)" in r.stdout
    # A loop is refused (the hop limit), not passed.
    (x / "link").unlink()
    (x / "loop").symlink_to("loop")
    r = check(w, f"{x / 'loop' / 'bin'}:{w['tail']}")
    assert r.returncode == 1 and "more than 8 symlinks" in r.stdout


def test_protect_makes_writable_image_hops_runner_only_instead_of_refusing(world):
    # The hosted image ships /opt (pipx_bin, hostedtoolcache) and
    # /usr/local/bin mode 777: create-codex-user protects such hops before
    # the grant rather than failing every codex run — and never a workspace
    # one.
    w = world
    opt = w["tmp"] / "opt"
    pipx = opt / "pipx_bin"
    pipx.mkdir(parents=True)
    tool = pipx / "pipx"
    tool.write_text("#!/bin/bash\n")
    tool.chmod(0o777)
    writable(opt, pipx)
    job = f"{pipx}:{w['tail']}"
    # Without protection: refused at the first writable hop.
    r = check(w, job, user=ME)
    assert r.returncode == 1 and f"(at {pipx})" in r.stdout
    # With it: the hops and the file lose their write bits and the check
    # passes; `chown` is the stub's no-op, so the mode change carries it.
    r = check(w, job, user=ME, protect="true")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"protected job PATH hop {pipx}" in r.stdout and f"protected job PATH hop {opt}" in r.stdout
    assert "(3 hop(s)/file(s) protected)" in r.stdout
    assert not (pipx.stat().st_mode & 0o022) and not (opt.stat().st_mode & 0o022) and not (tool.stat().st_mode & 0o022)
    assert f"chown {ME} {pipx}" in w["sudo_log"].read_text()
    # Afterwards the plain check passes too (what the reclaim runs).
    r = check(w, job, user=ME)
    assert r.returncode == 0, r.stdout + r.stderr
    # A hop that stays the user's after protection (chown refused, say) is
    # still refused: the stub's chown is disabled by pointing its log at a
    # read-only path.
    r = check(w, job, user=ME, protect="true", FAKE_CODEX_OWNED=str(opt), CHOWN_LOG="/dev/full")
    assert r.returncode == 1 and "is still owned or writable" in r.stdout and f"(at {opt})" in r.stdout
    # Protection never reaches into the workspace: that entry is refused.
    r = check(w, f"{w['planted']}:{w['tail']}", user=ME, protect="true")
    assert r.returncode == 1 and "lies inside the workspace" in r.stdout
    assert w["planted"].stat().st_mode & 0o777 == 0o755
    # And a writable file inside an otherwise clean entry is refused without
    # protection, fixed with it.
    other = w["tmp"] / "ub"
    other.mkdir()
    f = other / "git"
    f.write_text("#!/bin/bash\n")
    f.chmod(0o777)
    r = check(w, f"{other}:{w['tail']}", user=ME)
    assert r.returncode == 1 and f"is writable by the {ME} user (at {f})" in r.stdout
    r = check(w, f"{other}:{w['tail']}", user=ME, protect="true")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"protected job PATH file {f}" in r.stdout
    assert f.stat().st_mode & 0o777 == 0o755


def test_executables_inside_an_entry_are_checked_through_their_symlinks_and_owners(world):
    # Review round 2 of #131: a runner-only directory holding `bash ->
    # <somewhere codex can change>` is the planted interpreter by another
    # name, and a codex-owned 0755 file can be rewritten after a chmod.
    w = world
    rb = w["tmp"] / "runner-bin"
    rb.mkdir()
    job = f"{rb}:{w['tail']}"
    # 1. a link into the workspace: refused in both modes, never protected.
    (rb / "bash").symlink_to(w["planted"] / "bash")
    for protect in ("false", "true"):
        r = check(w, job, user=ME, protect=protect)
        assert r.returncode == 1 and f"'{rb}' lies inside the workspace" in r.stdout and f"(at {w['planted']})" in r.stdout
    (rb / "bash").unlink()
    # 2. a link to an external file the user can write.
    ext = w["tmp"] / "ext"
    ext.mkdir()
    target = ext / "bash"
    target.write_text("#!/bin/bash\n")
    target.chmod(0o777)
    (rb / "bash").symlink_to(target)
    r = check(w, job, user=ME)
    assert r.returncode == 1 and f"is writable by the {ME} user (at {target})" in r.stdout
    r = check(w, job, user=ME, protect="true")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"protected job PATH file {target}" in r.stdout and target.stat().st_mode & 0o777 == 0o755
    (rb / "bash").unlink()
    # 3. a link to a file in a directory the user can replace.
    rep = w["tmp"] / "rep"
    rep.mkdir()
    (rep / "bash").write_text("#!/bin/bash\n")
    (rep / "bash").chmod(0o755)
    writable(rep)
    (rb / "bash").symlink_to(rep / "bash")
    r = check(w, job, user=ME)
    assert r.returncode == 1 and f"is writable by the {ME} user (at {rep})" in r.stdout
    r = check(w, job, user=ME, protect="true")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"protected job PATH hop {rep}" in r.stdout and not rep.stat().st_mode & 0o002
    (rb / "bash").unlink()
    # 4. a regular file the user owns, mode 0755 (no group/other write bit):
    # ownership is authority.
    own = rb / "bash"
    own.write_text("#!/bin/bash\n")
    own.chmod(0o755)
    r = check(w, job, user=ME, FAKE_CODEX_OWNED=str(own))
    assert r.returncode == 1 and f"is owned by the {ME} user (at {own})" in r.stdout
    r = check(w, job, user=ME, protect="true", FAKE_CODEX_OWNED=str(own))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"protected job PATH file {own}" in r.stdout and str(own) in w["chown_log"].read_text()
    # A symlink the user owns is refused even under protect (never chowned).
    own.unlink()
    (rb / "git").symlink_to("/usr/bin/true")
    r = check(w, job, user=ME, protect="true", FAKE_CODEX_OWNED=str(rb / "git"))
    assert r.returncode == 1 and f"is owned by the {ME} user (at {rb / 'git'})" in r.stdout
    (rb / "git").unlink()
    # A relative link target resolves against the link's directory, and a
    # chain of links is followed to the end.
    (rb / "jq").symlink_to("../ext/jq")
    (ext / "jq").symlink_to(rep / "jq")
    (rep / "jq").write_text("#!/bin/bash\n")
    (rep / "jq").chmod(0o777)
    rep.chmod(0o755)
    r = check(w, job, user=ME)
    assert r.returncode == 1 and f"(at {rep / 'jq'})" in r.stdout
    # Two links in one directory to two writable targets: the directory is
    # listed once, before the first target is protected, so the second
    # link's answer must not come from that listing (the hosted image's
    # /opt/pipx_bin has a dozen such links; round 3 of #131's first push).
    two = w["tmp"] / "two-links"
    two.mkdir()
    for name in ("ansible", "ansible-pull"):
        (ext / name).write_text("#!/bin/bash\n")
        (ext / name).chmod(0o777)
        (two / name).symlink_to(ext / name)
    r = check(w, f"{two}:{w['tail']}", user=ME, protect="true")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("protected job PATH file") == 2
    # Safe links pass, and a chain many links share is probed once.
    (rep / "jq").chmod(0o755)
    (rb / "sed").symlink_to(rep / "jq")
    w["sudo_log"].write_text("")
    r = check(w, job, user=ME)
    assert r.returncode == 0, r.stdout + r.stderr
    probes = w["sudo_log"].read_text().splitlines()
    assert probes.count(f"-u {ME} test -w {rep}") == 1


def test_an_owned_read_only_directory_on_the_path_is_refused_or_protected(world):
    # Round 2 of #131: codex-owned 0555 passes `test -w` today and is chmod'ed
    # writable tomorrow.
    w = world
    d = w["tmp"] / "codex-owned-bin"
    d.mkdir()
    d.chmod(0o555)
    job = f"{d}:{w['tail']}"
    r = check(w, job, user=ME, FAKE_CODEX_OWNED=str(d))
    assert r.returncode == 1 and f"is owned by the {ME} user (at {d})" in r.stdout
    r = check(w, job, user=ME, protect="true", FAKE_CODEX_OWNED=str(d))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"protected job PATH hop {d}" in r.stdout
    d.chmod(0o755)


def test_the_checks_own_probes_never_resolve_through_the_job_path(world):
    # A planted directory OUTSIDE the workspace, first on the job PATH and
    # not flagged as writable: the entries pass, and every tool the check
    # ran (sudo, tr, dirname, basename, id) came from the pinned system
    # PATH, not from the entry under test.
    w = world
    evil = w["tmp"] / "evil"
    shutil.copytree(w["planted"], evil)
    # The planted files are 755 (not writable by the user, only runnable).
    r = check(w, f"{evil}:{w['tail']}", user=ME)
    assert r.returncode == 0, r.stdout + r.stderr
    assert hijacked(w) == ""
    assert w["sudo_log"].read_text()  # the stub sudo, from the system PATH, did the probing


# --- create-codex-user: user, check, grant — in that order --------------------


def test_create_codex_user_checks_the_path_after_the_user_and_before_the_grant():
    # Three create-mode steps, then the two of `reset-home` (the engine
    # split's boundary step between provisioning-as-codex and codex-action,
    # findings 4628446 and 4629153), which re-runs the check without
    # `protect` and pins its own PATH like the reclaim.
    steps = composite_steps(CREATE)
    assert len(steps) == 5, [s.splitlines()[0] for s in steps]
    first, check_step, grant, reset_check, reset = steps
    for s in (first, check_step, grant):
        assert "if: inputs.mode == 'create'" in s
    for s in (reset_check, reset):
        assert "if: inputs.mode == 'reset-home'" in s
    assert ASSERT_USES in reset_check and "user: codex" in reset_check and "protect" not in reset_check
    assert f"SYSTEM_PATH: {SYSTEM_DIRS}" in reset and reset.index('export PATH="$SYSTEM_PATH"') < reset.index("sudo ")
    assert "sudo adduser --system --home /home/codex --shell /bin/bash --group codex" in first
    assert "sudo usermod -a -G runner codex" in first
    assert "chown" not in first and "chmod" not in first
    assert ASSERT_USES in check_step and "user: codex" in check_step and 'protect: "true"' in check_step
    assert 'sudo chown -R runner:codex "$GITHUB_WORKSPACE"' in grant
    assert 'sudo chmod -R g+rwX "$GITHUB_WORKSPACE"' in grant
    # The snapshots stay the first thing, before any grant.
    assert first.index('cp "$GITHUB_WORKSPACE/.git/config"') < first.index("sudo adduser")


def test_assert_runner_only_path_defaults():
    text = ASSERT.read_text()
    assert "default: codex" in text
    assert f"default: {SYSTEM_DIRS}" in text
    assert "-perm -0020" in text and '-user "$USER_NAME"' in text and "-type l" in text  # the candidate scan
    # One sudo for the whole check: it re-executes itself as root with the
    # job PATH and the step's user carried over, and never loops.
    assert 'exec sudo JOB_PATH="$job_path" STEP_USER="$(id -un)" ASSERT_RUNNER_ONLY_PATH_ELEVATED=1' in text
    # ... and the PATH pin comes before even that `id`/`sudo`.
    assert ASSERT_SCRIPT.index('export PATH="$SYSTEM_PATH"') < ASSERT_SCRIPT.index('$(id -u)')
    assert 'runuser -u "$USER_NAME" -- "$@"' in text
    assert 'default: "false"' in text  # protect is opt-in: the reclaim refuses
    assert "/usr/local" not in SYSTEM_DIRS  # the image ships /usr/local/bin mode 777
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
    assert "protect" not in steps[0]  # after codex a writable hop is refused, not fixed
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
    # The recipe is provision.sh next to the composite since the engine
    # split; the composite's step runs it directly as the runner (no
    # `user`), with ADD_TO_PATH from `add-to-path` in its environment.
    r = subprocess.run(["/bin/bash", str(PROVISION.parent / "provision.sh")], cwd=ws, text=True, capture_output=True,
                       env=env, check=False)
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


CLAUDE_JOB = {"claude.yml": "agent", "claude-review.yml": "review", "claude-auto.yml": "fix", "claude-auto-review.yml": "fix"}
CODEX_JOB = {"claude.yml": "agent-codex", "claude-review.yml": "review-codex", "claude-auto.yml": "fix-codex",
             "claude-auto-review.yml": "fix-codex"}


def job_text(path: Path, job: str) -> str:
    text = path.read_text()
    start = text.index(f"\n  {job}:\n")
    nxt = re.compile(r"\n  [a-z_-]+:\n").search(text, start + 1)
    return text[start:nxt.start() if nxt else len(text)]


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_only_the_claude_job_puts_the_venv_on_the_job_path(name):
    # One job per engine (findings 4628446 and 4629153): the Claude job runs
    # no codex, so its fallback keeps add-to-path's default ("true"); the
    # codex job provisions as the codex user, whose `env -i` recipe cannot
    # reach GITHUB_PATH, so nothing under the workspace goes on the job PATH
    # there (finding 4628448).
    claude = job_text(WORKFLOWS[name], CLAUDE_JOB[name])
    step = claude[claude.index("- name: Provision project environment (fallback)"):]
    step = step[:step.index("\n      - ", 1)]
    assert "uses: meridianlabs-ai/agents/.github/actions/provision-fallback@main" in step
    assert "add-to-path" not in step and "user:" not in step
    codex = job_text(WORKFLOWS[name], CODEX_JOB[name])
    assert "add-to-path" not in codex
    assert "uses: meridianlabs-ai/agents/.github/actions/provision-fallback@main\n        with:\n          user: codex\n" in codex


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_codex_tool_discovery_never_consults_a_path(name):
    # The codex job looks the tools up only in the directories the
    # provisioning composite reports (the venv's bin, node_modules/.bin,
    # ~codex/.local/bin), never through `command -v`: no job PATH entry
    # under the workspace exists to find them by.
    text = WORKFLOWS[name].read_text()
    assert text.count("for t in pytest ruff mypy pyright python3 node pnpm npm; do") == 1
    codex = job_text(WORKFLOWS[name], CODEX_JOB[name])
    loop = codex[codex.index('IFS=: read -r -a dirs <<<"$BIN"'):]
    loop = loop[:loop.index("\n            done\n") if "\n            done\n" in loop else len(loop)]
    assert '[ -x "$d/$t" ]' in loop and "command -v" not in loop
    assert "command -v" not in "\n".join(l for l in text.splitlines() if "for t in" in l or "path=" in l)


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
