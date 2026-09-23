"""No root for the agent uid (Claude Security finding 4629153, criterion 2;
design/architecture.md → No root for the agent uid): structural checks on
the four reusable workflows and tests of the `drop-runner-root` composite's
fail-closed checks.

- Claude jobs (`agent`, `review`, `fix`, `fix`): the agent runs as the
  runner, so one `drop-runner-root` step sits after every step that needs
  root (the checkouts, the caller's `claude-setup`, the fallback
  provisioning, the reviewer's sandbox install) and before the
  claude-code-action step, with an `if:` that holds whenever the agent
  step's does; nothing after it calls sudo or docker, and the Surface step
  reports its failure.
- Codex jobs: the agent uid is `codex`, which `create-codex-user` makes
  with no sudo grant and no group beyond `runner`, and codex-action starts
  codex as that user (`safety-strategy: unprivileged-user`, `codex-user:
  codex`) after the user exists and after its home is reset. The runner
  keeps its sudo there (the reclaim and codex-usage need it after codex),
  so the codex jobs do not run the drop step.

`.github/workflows/root-boundary-smoke.yml` runs the composite on a hosted
runner and checks the same properties from the real `runner` and `codex`
users.
"""

import os
import re
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
COMPOSITE = ROOT / ".github" / "actions" / "drop-runner-root" / "action.yml"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_app_token_minting import AGENT_JOBS, REUSABLE, code_lines, jobs, steps  # noqa: E402
from test_land_helpers import lift_run, sh  # noqa: E402

CLAUDE_ACTION = "anthropics/claude-code-action@v1"
CODEX_ACTION = "openai/codex-action@v1"
DROP = "uses: meridianlabs-ai/agents/.github/actions/drop-runner-root@main"


def workflow_text(name: str) -> str:
    return (WORKFLOWS / name).read_text()


def step_if(step: str):
    """The step's `if:` expression (single-line or a `>-` block, folded), or
    None when it has none."""
    lines = step.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^      (?:- |  )if: (.*)$", line)
        if not m:
            continue
        if m.group(1) != ">-":
            return m.group(1).strip()
        folded = []
        for later in lines[i + 1:]:
            if not later.startswith("          "):
                break
            folded.append(later.strip())
        return " ".join(folded)
    return None


def needs_root(step: str) -> bool:
    """A step the job runs with root: a checkout, the caller's composite, the
    fallback provisioning, or anything whose code calls sudo."""
    # Error texts name sudo as prose; commands only.
    code = "\n".join(line for line in code_lines(step) if 'err="' not in line)
    return ("uses: actions/checkout@" in code or "uses: ./.github/actions/claude-setup" in code
            or "provision-fallback@main" in code or re.search(r"\bsudo\b", code) is not None)


def claude_job(name: str) -> str:
    return jobs(workflow_text(name))[AGENT_JOBS[name][0]]


def positions(job: str):
    all_steps = steps(job)
    drops = [i for i, s in enumerate(all_steps) if DROP in s]
    agents = [i for i, s in enumerate(all_steps) if CLAUDE_ACTION in s]
    assert len(drops) == 1 and len(agents) == 1, (drops, agents)
    return all_steps, drops[0], agents[0]


# --- Claude jobs --------------------------------------------------------------


@pytest.mark.parametrize("name", REUSABLE)
def test_the_claude_job_drops_root_once_with_a_fixed_id(name):
    job = claude_job(name)
    all_steps, drop, _ = positions(job)
    assert "\n        id: noroot\n" in all_steps[drop]
    assert "with:" not in all_steps[drop], "the composite's defaults are the boundary"


@pytest.mark.parametrize("name", REUSABLE)
def test_the_drop_comes_after_every_step_that_needs_root(name):
    all_steps, drop, _ = positions(claude_job(name))
    rooted = [i for i, s in enumerate(all_steps) if needs_root(s)]
    assert rooted and max(rooted) < drop, [all_steps[i][:70] for i in rooted if i > drop]
    # The named ones the task lists are among them, each before the drop.
    for needle in ("uses: actions/checkout@", "uses: ./.github/actions/claude-setup", "provision-fallback@main"):
        at = [i for i, s in enumerate(all_steps) if needle in s]
        assert at and max(at) < drop, (needle, at, drop)
    if name == "claude-review.yml":
        sandbox = [i for i, s in enumerate(all_steps) if "\n        id: sandbox\n" in s]
        assert len(sandbox) == 1 and sandbox[0] < drop and "sudo apt-get install" in all_steps[sandbox[0]]


@pytest.mark.parametrize("name", REUSABLE)
def test_the_drop_comes_before_the_agent_step(name):
    _, drop, agent = positions(claude_job(name))
    assert drop < agent


@pytest.mark.parametrize("name", REUSABLE)
def test_the_drop_runs_whenever_the_agent_step_does(name):
    """Not weaker than the agent step's `if:`: identical, or none at all when
    the agent's own `if:` carries no status function (an `if:` without one
    is implicitly `success() && …`, so a step with none runs on every path
    the agent step runs on). The agent is gated on success, so a failed drop
    skips it."""
    all_steps, drop, agent = positions(claude_job(name))
    ours, theirs = step_if(all_steps[drop]), step_if(all_steps[agent])
    if ours is not None:
        assert ours == theirs, (ours, theirs)
    else:
        assert theirs is None or not re.search(r"\b(always|failure|cancelled)\(\)", theirs), theirs
    assert theirs is None or not re.search(r"\b(always|failure|cancelled)\(\)", theirs), \
        "the agent step must stay gated on success, so a failed drop skips it"
    # And no step between the two can run after a failed drop either.
    for s in all_steps[drop + 1:agent]:
        cond = step_if(s) or ""
        assert not re.search(r"\b(always|failure|cancelled)\(\)", cond), s[:70]


@pytest.mark.parametrize("name", REUSABLE)
def test_nothing_after_the_drop_calls_sudo_or_docker(name):
    all_steps, drop, _ = positions(claude_job(name))
    for s in all_steps[drop + 1:]:
        # Error texts name sudo and the docker socket as prose; commands only.
        code = [line for line in code_lines(s) if 'err="' not in line]
        hits = [line.strip() for line in code if re.search(r"\b(sudo|docker|podman)\b", line)]
        assert hits == [], (s[:70], hits)


@pytest.mark.parametrize("name", REUSABLE)
def test_the_surface_step_reports_a_failed_drop(name):
    job = claude_job(name)
    surface = next(s for s in steps(job) if "\n        id: surface\n" in s)
    assert "          NOROOT_OUTCOME: ${{ steps.noroot.outcome }}\n" in surface
    assert '[ "${NOROOT_OUTCOME:-}" = "failure" ]' in surface
    assert "removing the runner's root access" in surface


# --- codex jobs ---------------------------------------------------------------


@pytest.mark.parametrize("name", REUSABLE)
def test_the_codex_job_runs_codex_as_the_codex_user_and_keeps_runner_sudo(name):
    job = jobs(workflow_text(name))[AGENT_JOBS[name][1]]
    assert "drop-runner-root" not in job, "the runner needs its sudo after codex (reclaim, codex-usage)"
    all_steps = steps(job)
    codex_at = [i for i, s in enumerate(all_steps) if CODEX_ACTION in s]
    assert len(codex_at) == 1
    action = all_steps[codex_at[0]]
    assert "          safety-strategy: unprivileged-user\n" in action
    assert "          codex-user: codex\n" in action
    # The user exists, and its home was reset after provisioning, before
    # codex-action starts it; the reclaim and codex-usage, which need the
    # runner's sudo, come after it.
    user = next(i for i, s in enumerate(all_steps) if "\n        id: codexuser\n" in s)
    home = next(i for i, s in enumerate(all_steps) if "\n        id: codexhome\n" in s)
    assert user < home < codex_at[0]
    # (The reviewer commits nothing, so its codex job has no reclaim.)
    for needle in ("reclaim-codex-workspace@main", "codex-usage@main"):
        at = [i for i, s in enumerate(all_steps) if needle in s]
        assert (at or (needle.startswith("reclaim") and name == "claude-review.yml")) and \
            all(i > codex_at[0] for i in at), needle


def test_the_codex_user_gets_no_sudo_and_no_group_that_reaches_root():
    text = (ROOT / ".github" / "actions" / "create-codex-user" / "action.yml").read_text()
    code = "\n".join(code_lines(text[text.index("\nruns:\n"):]))
    assert "sudoers" not in code and "visudo" not in code
    assert "sudo adduser --system --home /home/codex --shell /bin/bash --group codex\n" in code
    grants = [line.strip() for line in code.splitlines() if re.search(r"\b(usermod|gpasswd|adduser|addgroup)\b", line)]
    assert grants == ["sudo adduser --system --home /home/codex --shell /bin/bash --group codex",
                      "sudo usermod -a -G codex runner", "sudo usermod -a -G runner codex"], grants


# --- the composite -------------------------------------------------------------


def composite_body() -> str:
    text = COMPOSITE.read_text()
    assert "\npost:" not in text and "post-if" not in text
    assert "    default: /usr/sbin:/usr/bin:/sbin:/bin\n" in text
    return lift_run(text, "    - shell: bash")


def test_composite_pins_the_system_path_before_its_first_command():
    body = composite_body().splitlines()
    assert body[0] == "set -euo pipefail" and body[1] == 'export PATH="$SYSTEM_PATH"'


def test_composite_ends_sudo_last_and_checks_after():
    body = composite_body()
    sudo_calls = [line.strip() for line in body.splitlines() if re.match(r"\s*(sudo -n |.*\$\(sudo -n )", line)]
    assert sudo_calls[-1] == "sudo -n mv -f /etc/sudoers.drop-runner-root /etc/sudoers", sudo_calls
    swap = body.index("sudo -n mv -f /etc/sudoers.drop-runner-root /etc/sudoers")
    for later in ('if sudo -n true >/dev/null 2>&1; then fail', 'if sudo -n -l >/dev/null 2>&1; then fail',
                  '[ "$scope" -ge 2 ] || fail', 'if [ -w "$s" ]; then fail'):
        assert body.index(later) > swap, later
    assert body.index("sudo -n sysctl -q -w kernel.yama.ptrace_scope=2") < swap
    assert body.index("sudo -n visudo -c -q -f /etc/sudoers.drop-runner-root") < swap


def write_exe(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def run_composite(tmp_path: Path, *, sudo_true_rc="1", sudo_list_rc="1", scope="1", sysctl_sets="2",
                  docker_group=False, sockets="", groups="runner adm docker", root_dirs=None,
                  chmod_takes=True):
    """The composite's body with its system paths pointed at a scratch tree
    and every privileged or identity command stubbed: `sudo` records its
    argv and dispatches (sysctl writes the scratch Yama file, find prints
    SOCKETS, the final `true`/`-l` answer with the given codes); `id` and
    `getent` describe a runner in the docker group."""
    bins = tmp_path / "bin"
    bins.mkdir()
    log = tmp_path / "log"
    yama = tmp_path / "ptrace_scope"
    yama.write_text(scope + "\n")
    etc = tmp_path / "etc"
    etc.mkdir()
    write_exe(bins / "sudo", '#!/bin/bash\n[ "$1" = -n ] && shift\nprintf "sudo %s\\n" "$*" >>"$LOG"\n'
              'case "$1" in\n'
              '  true) exit "$SUDO_TRUE_RC" ;;\n'
              '  -l) exit "$SUDO_LIST_RC" ;;\n'
              '  sysctl) printf "%s\\n" "$SYSCTL_SETS" >"$YAMA" ;;\n'
              '  find) [ -n "$SOCKETS" ] && printf "%s\\n" "$SOCKETS" ;;\n'
              '  install) shift; while [ "$#" -gt 2 ]; do case "$1" in -o|-g|-m) shift 2 ;; *) break ;; esac; done; cp "$1" "$2" ;;\n'
              '  mv) shift; [ "$1" = -f ] && shift; mv -f "$1" "$2" ;;\n'
              # A directory chmod stands for root's chown + chmod: the runner
              # can no longer write it. On a file (the socket stub) it does nothing.
              '  chmod) [ "$CHMOD_TAKES" = 1 ] && [ -d "${@: -1}" ] && chmod -R a-w "${@: -1}"; exit 0 ;;\n'
              '  visudo|gpasswd|chown) ;;\n'
              '  *) echo "unexpected sudo $*" >&2; exit 99 ;;\n'
              'esac\n')
    write_exe(bins / "id", '#!/bin/bash\ncase "$1" in -un) echo runner ;; -u) echo 1001 ;; -nG) echo "$GROUPS_OUT" ;; esac\n')
    write_exe(bins / "getent", f'#!/bin/bash\nexit {0 if docker_group else 2}\n')
    body = (composite_body()
            .replace("/proc/sys/kernel/yama/ptrace_scope", str(yama))
            .replace("/etc/sudoers", str(etc / "sudoers"))
            .replace("Runner.Worker", "Not-A-Runner-Worker")
            .replace("/run/docker.sock /var/run/docker.sock", str(tmp_path / "no-such.sock"))
            .replace('root_dirs="/usr/local/sbin /usr/local/bin"',
                     f'root_dirs="{root_dirs if root_dirs is not None else tmp_path / "no-such-dir"}"'))
    env = {"SYSTEM_PATH": f"{bins}:/usr/bin:/bin", "LOG": str(log), "YAMA": str(yama), "SUDO_TRUE_RC": sudo_true_rc,
           "SUDO_LIST_RC": sudo_list_rc, "SYSCTL_SETS": sysctl_sets, "SOCKETS": sockets, "GROUPS_OUT": groups,
           "CHMOD_TAKES": "1" if chmod_takes else "0"}
    r = sh("bash", "-c", body, check=False, env=env)
    return r, log.read_text().splitlines() if log.exists() else [], etc


def test_composite_drops_in_order_and_installs_a_root_only_policy(tmp_path):
    r, log, etc = run_composite(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert [line.split()[1] for line in log] == ["sysctl", "install", "visudo", "mv", "true", "-l"], log
    assert log[0] == "sudo sysctl -q -w kernel.yama.ptrace_scope=2"
    policy = (etc / "sudoers").read_text().splitlines()
    assert [line for line in policy if not line.startswith(("#", "Defaults"))] == ["root ALL=(ALL:ALL) ALL"]
    assert not any("include" in line for line in policy)
    assert "runner has no sudo" in r.stdout


def test_composite_takes_the_runner_out_of_the_docker_group_and_locks_each_socket(tmp_path):
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    sock.chmod(0o400)   # what root:root 0600 looks like to the runner
    r, log, _ = run_composite(tmp_path, docker_group=True, sockets=str(sock))
    assert r.returncode == 0, r.stdout + r.stderr
    assert log[1:5] == ["sudo gpasswd -d runner docker", "sudo find /run -xdev -type s -group docker",
                        f"sudo chown root:root {sock}", f"sudo chmod 0600 {sock}"], log


def test_composite_makes_roots_search_path_directories_root_only(tmp_path):
    dirs = [tmp_path / "local-sbin", tmp_path / "local-bin"]
    for d in dirs:
        d.mkdir()
        d.chmod(0o777)
    r, log, _ = run_composite(tmp_path, root_dirs=" ".join(map(str, dirs)))
    try:
        assert r.returncode == 0, r.stdout + r.stderr
        want = []
        for d in dirs:
            want += [f"sudo chown -R root:root {d}", f"sudo chmod -R go-w {d}", f"sudo chmod 0755 {d}"]
        assert log[1:7] == want, log
        swap = log.index(next(line for line in log if line.startswith("sudo mv ")))
        assert swap > 6, "the directories are protected while sudo still works"
        assert f"{dirs[1]} is root's alone" in r.stdout
    finally:
        for d in dirs:
            d.chmod(0o755)


def test_composite_fails_when_a_search_path_directory_stays_writable(tmp_path):
    d = tmp_path / "local-bin"
    d.mkdir()
    (d / "tool").write_text("")
    r, _, _ = run_composite(tmp_path, root_dirs=str(d), chmod_takes=False)
    assert r.returncode != 0 and "(on root's default PATH) is still writable by runner" in r.stdout, r.stdout


def test_the_search_path_directories_are_roots_default_path():
    body = composite_body()
    assert 'root_dirs="/usr/local/sbin /usr/local/bin"\n' in body
    swap = body.index("sudo -n mv -f /etc/sudoers.drop-runner-root /etc/sudoers")
    for line in ('sudo -n chown -R root:root "$d"', 'sudo -n chmod -R go-w "$d"', 'sudo -n chmod 0755 "$d"'):
        assert body.index(line) < swap, line
    check = 'entries=$(find "$d" -xdev ! -type l)'
    assert body.index(check) > swap


def test_composite_fails_when_a_docker_socket_stays_writable(tmp_path):
    sock = tmp_path / "docker.sock"
    sock.write_text("")          # the stubbed chown/chmod change nothing: still the runner's
    r, _, _ = run_composite(tmp_path, docker_group=True, sockets=str(sock))
    assert r.returncode != 0 and "is still writable by runner" in r.stdout


@pytest.mark.parametrize("which,kwargs,message", [
    ("true", {"sudo_true_rc": "0"}, "sudo -n true still succeeds"),
    ("list", {"sudo_list_rc": "0"}, "sudo -n -l still lists a passwordless rule"),
    ("scope", {"sysctl_sets": "1"}, "kernel.yama.ptrace_scope is 1"),
    ("disk", {"groups": "runner adm disk"}, "hold the disk group"),
    ("lxd", {"groups": "runner lxd"}, "hold the lxd group"),
])
def test_composite_fails_closed(tmp_path, which, kwargs, message):
    r, _, _ = run_composite(tmp_path, **kwargs)
    assert r.returncode != 0 and message in r.stdout, (which, r.stdout, r.stderr)


def test_composite_leaves_a_scope_already_at_two_alone(tmp_path):
    r, log, _ = run_composite(tmp_path, scope="2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not any("sysctl" in line for line in log)


def test_composite_refuses_to_run_as_root(tmp_path):
    bins = tmp_path / "rootbin"
    bins.mkdir()
    write_exe(bins / "id", '#!/bin/bash\ncase "$1" in -un) echo root ;; -u) echo 0 ;; esac\n')
    r = sh("bash", "-c", composite_body(), check=False, env={"SYSTEM_PATH": f"{bins}:/usr/bin:/bin"})
    assert r.returncode != 0 and "not as root" in r.stdout


def test_agents_md_lists_the_composite():
    agents = (ROOT / "AGENTS.md").read_text()
    listing = agents[agents.index("`.github/actions/*`"):agents.index("Referenced fully-qualified")]
    assert "`drop-runner-root`" in listing
    assert os.path.isfile(COMPOSITE)
