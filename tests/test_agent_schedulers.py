"""The agent users cannot schedule host-side work (Claude Security finding
4773340, criterion 1; design/untrusted-agent-job.md → What stays in the
untrusted job).

- `create-codex-user/deny-schedulers.sh`: the user is appended to
  `cron.deny` and `at.deny` (created when missing, once, on its own line),
  and an allow file that lists the user fails the step.
- `.github/scripts/purge_agent_schedules.sh`: a crontab named after the
  user or owned by it, and an at job owned by it, are removed; the user's
  processes are killed again and the script fails; nothing else in the
  spools is touched; missing spools are nothing to purge; a failing scan
  fails the script.
- The three kill sites run the purge right after their kill: the Claude
  launcher's pre-launch kill, the codex `reset-home` kill (in
  test_engine_job_isolation.py) and the reclaim's kill, which fails before
  it touches `.git`.
- no_new_privs: the launcher's privilege drop passes `--no-new-privs` to
  setpriv, and the namespace's isolation check requires it
  (provisioning's `sudo -u` is in test_engine_job_isolation.py).
- The engine isolation canary's `scheduler-boundary` job runs the hosted
  check in the designed order.
"""

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_codex_path import composite_runs, reclaim, reclaim_fixture, world  # noqa: E402,F401

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = ROOT / ".github" / "actions"
DENY = ACTIONS / "create-codex-user" / "deny-schedulers.sh"
CREATE = ACTIONS / "create-codex-user" / "action.yml"
PURGE = ROOT / ".github" / "scripts" / "purge_agent_schedules.sh"
LAUNCHER = ACTIONS / "claude-agent-launcher"
RECLAIM_SH = ACTIONS / "reclaim-codex-workspace" / "reclaim.sh"
CANARY = ROOT / ".github" / "workflows" / "engine-isolation-canary.yml"
SMOKE = ROOT / "tests" / "scheduler_boundary_smoke.sh"

spec = importlib.util.spec_from_file_location("agent_ns", LAUNCHER / "agent_ns.py")
agent_ns = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent_ns)


def write_exe(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


# --- deny-schedulers.sh ---------------------------------------------------------


def deny(etc: Path, user: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(DENY), user, str(etc)], capture_output=True, text=True, check=False)


@pytest.mark.parametrize("user", ["codex", "claude-agent"])
def test_deny_creates_both_files_with_the_user(tmp_path, user):
    r = deny(tmp_path, user)
    assert r.returncode == 0, r.stdout + r.stderr
    for tool in ("cron", "at"):
        f = tmp_path / f"{tool}.deny"
        assert f.read_text() == f"{user}\n"
        assert stat.S_IMODE(f.stat().st_mode) == 0o644
        assert f"{tool} denied to {user}" in r.stdout


def test_deny_appends_once_on_its_own_line(tmp_path):
    (tmp_path / "at.deny").write_text("daemon\nbin")  # the package's file, last newline missing
    (tmp_path / "cron.deny").write_text("nobody\n")
    for _ in range(2):
        r = deny(tmp_path, "codex")
        assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "at.deny").read_text() == "daemon\nbin\ncodex\n"
    assert (tmp_path / "cron.deny").read_text() == "nobody\ncodex\n"
    r = deny(tmp_path, "claude-agent")
    assert (tmp_path / "cron.deny").read_text() == "nobody\ncodex\nclaude-agent\n"


@pytest.mark.parametrize("tool", ["cron", "at"])
def test_an_allow_file_that_lists_the_user_fails(tmp_path, tool):
    # With an allow file, the tools read only it: a listed user would be
    # allowed whatever the deny file says.
    (tmp_path / f"{tool}.allow").write_text("root\n  claude-agent \n")
    r = deny(tmp_path, "claude-agent")
    assert r.returncode == 1
    assert f"{tool}.allow lists claude-agent" in r.stdout


def test_an_allow_file_without_the_user_is_fine(tmp_path):
    (tmp_path / "cron.allow").write_text("root\ncodex-other\n")
    r = deny(tmp_path, "codex")
    assert r.returncode == 0, r.stdout
    assert (tmp_path / "cron.deny").read_text() == "codex\n"


@pytest.mark.parametrize("user", ["root", "runner", "", "codex\nroot"])
def test_deny_refuses_an_unknown_user(tmp_path, user):
    r = deny(tmp_path, user)
    assert r.returncode == 1 and "user must be codex or claude-agent" in r.stdout
    assert list(tmp_path.iterdir()) == []


def test_create_mode_denies_right_after_the_user_exists():
    first = composite_runs(CREATE)[0]
    assert first.index('sudo usermod -a -G runner "$AGENT_USER"') < first.index('sudo bash "$DENY_SCRIPT" "$AGENT_USER"')
    assert "        DENY_SCRIPT: ${{ github.action_path }}/deny-schedulers.sh\n" in CREATE.read_text()
    assert os.access(DENY, os.X_OK)
    # The path smoke runs the same block with the step's env.
    smoke = (ROOT / "tests" / "codex_path_smoke.sh").read_text()
    assert 'DENY_SCRIPT="$root/.github/actions/create-codex-user/deny-schedulers.sh"' in smoke


# --- purge_agent_schedules.sh ---------------------------------------------------


def purge_world(tmp_path, uid, pkill_rc="1"):
    """Spools under tmp_path, a sudo that runs the command as the caller and
    logs pkill, and an `id -u` that answers `uid` for the agent user."""
    bins = tmp_path / "bin"
    bins.mkdir()
    log = tmp_path / "log"
    write_exe(bins / "sudo", '#!/usr/bin/env bash\n'
              'if [ "$1" = pkill ]; then printf "%s\\n" "$*" >>"$LOG"; exit "$PKILL_RC"; fi\n'
              'exec "$@"\n')
    write_exe(bins / "id", f'#!/usr/bin/env bash\n[ "$1" = -u ] && [ -n "$2" ] && {{ echo {uid}; exit 0; }}\nexec /usr/bin/id "$@"\n')
    cron = tmp_path / "spool" / "crontabs"
    at = tmp_path / "spool" / "atjobs"
    env = {"PATH": "/usr/bin:/bin", "SYSTEM_PATH": f"{bins}:/usr/bin:/bin", "LOG": str(log), "PKILL_RC": pkill_rc,
           "CRON_SPOOL": str(cron), "AT_SPOOL": str(at)}
    return cron, at, log, env


def purge(env, user):
    return subprocess.run(["bash", str(PURGE)], capture_output=True, text=True, check=False,
                          env={**env, "AGENT_USER": user})


def test_missing_or_empty_spools_are_nothing_to_purge(tmp_path):
    cron, at, log, env = purge_world(tmp_path, os.getuid())
    r = purge(env, "codex")
    assert r.returncode == 0, r.stdout + r.stderr
    cron.mkdir(parents=True)
    at.mkdir(parents=True)
    r = purge(env, "claude-agent")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == "" and not log.exists()


@pytest.mark.parametrize("user", ["codex", "claude-agent"])
def test_a_crontab_named_after_the_user_is_removed_and_fails(tmp_path, user):
    # Owned by someone else (root, on the runner): cron still runs a root-owned
    # crontab as the user it is named after.
    cron, at, log, env = purge_world(tmp_path, 99999)
    cron.mkdir(parents=True)
    at.mkdir(parents=True)
    (cron / user).write_text("* * * * * sleep 600\n")
    (cron / "runner").write_text("# not the agent's\n")
    (at / ".SEQ").write_text("1\n")
    (at / f"a0000101{user}").write_text("# named like the user, owned by another: not the user's job\n")
    r = purge(env, user)
    assert r.returncode == 1
    assert not (cron / user).exists()
    assert sorted(p.name for p in cron.iterdir()) == ["runner"]
    assert sorted(p.name for p in at.iterdir()) == [".SEQ", f"a0000101{user}"]
    assert f"::error::purge-agent-schedules: removed {cron / user}, a scheduler entry of {user}" in r.stdout
    assert f"::error::{user} had 1 cron or at entries" in r.stdout
    # Killed again after the removal: the daemon may have started one.
    assert log.read_text() == f"pkill -KILL -u {user}\n"


def test_entries_the_user_owns_are_removed_in_both_spools(tmp_path):
    # Every file here is the test user's, so with `id -u` answering that uid
    # every entry is the agent's.
    cron, at, log, env = purge_world(tmp_path, os.getuid())
    cron.mkdir(parents=True)
    at.mkdir(parents=True)
    (cron / "root").write_text("* * * * * sleep 600\n")
    (at / "a0000101d5e2f1").write_text("sleep 600\n")
    (at / "=0000201d5e2f1").write_text("sleep 600\n")  # a running job
    r = purge(env, "codex")
    assert r.returncode == 1
    assert list(cron.iterdir()) == [] and list(at.iterdir()) == []
    assert "had 3 cron or at entries" in r.stdout


def test_the_purge_reports_survivors_after_its_kill(tmp_path):
    cron, at, log, env = purge_world(tmp_path, 99999, pkill_rc="0")
    cron.mkdir(parents=True)
    (cron / "codex").write_text("* * * * * sleep 600\n")
    r = purge(env, "codex")
    assert r.returncode == 1 and log.read_text().count("pkill -KILL -u codex") == 10
    assert "still running as codex after repeated kills" in r.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_a_failing_scan_fails_the_purge(tmp_path):
    cron, at, log, env = purge_world(tmp_path, 99999)
    cron.mkdir(parents=True)
    (cron / "codex").write_text("* * * * * sleep 600\n")
    cron.chmod(0)
    try:
        r = purge(env, "codex")
    finally:
        cron.chmod(0o755)
    assert r.returncode != 0
    assert (cron / "codex").exists() and "had 0" not in r.stdout


@pytest.mark.parametrize("user", ["root", "runner", ""])
def test_the_purge_refuses_an_unknown_user(tmp_path, user):
    cron, at, log, env = purge_world(tmp_path, os.getuid())
    cron.mkdir(parents=True)
    (cron / "keep").write_text("x\n")
    r = purge(env, user)
    assert r.returncode == 1 and "user must be codex or claude-agent" in r.stdout
    assert (cron / "keep").exists()


def test_the_purge_pins_its_path_first_and_is_executable():
    code = [line for line in PURGE.read_text().splitlines() if line and not line.startswith("#")]
    assert code[:2] == ["set -euo pipefail", 'export PATH="${SYSTEM_PATH:-/usr/sbin:/usr/bin:/sbin:/bin}"']
    assert 'cron_spool="${CRON_SPOOL:-/var/spool/cron/crontabs}"' in code
    assert 'at_spool="${AT_SPOOL:-/var/spool/cron/atjobs}"' in code
    assert os.access(PURGE, os.X_OK)


# --- the kill sites ----------------------------------------------------------------


def test_every_kill_site_names_the_one_purge_script():
    rel = "/../../scripts/purge_agent_schedules.sh"
    for action in (LAUNCHER / "action.yml", CREATE):
        assert f"        PURGE_SCRIPT: ${{{{ github.action_path }}}}{rel}\n" in action.read_text(), action
        assert Path(str(action.parent) + rel).resolve() == PURGE
    assert (RECLAIM_SH.parent / "../../scripts/purge_agent_schedules.sh").resolve() == PURGE
    assert 'AGENT_USER="$user" bash "$(dirname "${BASH_SOURCE[0]}")/../../scripts/purge_agent_schedules.sh"' in RECLAIM_SH.read_text()


def test_the_launcher_purges_after_its_kill_and_before_the_landing_reset():
    body = composite_runs(LAUNCHER / "action.yml")[0]
    kill = body.index('sudo pkill -KILL -u "$user"')
    purge_at = body.index('AGENT_USER="$user" bash "$PURGE_SCRIPT" || err ')
    assert kill < purge_at < body.index('sudo rm -rf "$RUNNER_TEMP/$user"\n')


def test_the_reset_mode_purges_after_its_kill_and_before_the_home():
    text = CREATE.read_text()
    reset = text[text.index("      if: inputs.mode == 'reset-home'\n      env:"):]
    kill = reset.index("sudo pkill -KILL -u codex")
    assert kill < reset.index('bash "$PURGE_SCRIPT"') < reset.index('bash "$HOME_SCRIPT" codex "$BIN"')


def test_the_reclaim_purges_after_its_kill_and_before_anything_else():
    text = RECLAIM_SH.read_text()
    purge_at = text.index("purge_agent_schedules.sh")
    assert text.index('sudo pkill -KILL -u "$user"') < purge_at < text.index('if [ "$user" = claude-agent ]; then')


@pytest.mark.parametrize("user", ["codex", "claude-agent"])
def test_the_reclaim_fails_at_a_planted_crontab_before_touching_git(world, user):  # noqa: F811
    w = world
    snapshot, embedded = reclaim_fixture(w)
    write_exe(w["system"] / "id", f'#!/bin/bash\n[ "$1" = -u ] && [ "$2" = {user} ] && {{ echo 99999; exit 0; }}\nexec /usr/bin/id "$@"\n')
    spool = w["tmp"] / "spool" / "crontabs"
    spool.mkdir(parents=True)
    (spool / user).write_text("* * * * * sleep 600\n")
    r = reclaim(w, AGENT_USER=user, SNAPSHOT=snapshot, EMBEDDED_SNAPSHOT=embedded)
    assert r.returncode == 1
    assert f"removed {spool / user}, a scheduler entry of {user}" in r.stdout
    assert not (spool / user).exists()
    # The reclaim stopped there: the tampered config is still in place.
    assert "TAMPERED" in (w["ws"] / ".git" / "config").read_text()
    assert "reclaimed .git" not in r.stdout


# --- no_new_privs --------------------------------------------------------------


def test_the_privilege_drop_sets_no_new_privs(monkeypatch):
    calls = []

    class Exec(Exception):
        pass

    def fake_execve(path, argv, env):
        calls.append((path, argv, env))
        raise Exec

    def fake_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(agent_ns.os, "close", lambda fd: None)
    monkeypatch.setattr(agent_ns.os, "chdir", lambda d: None)
    monkeypatch.setattr(agent_ns.os, "execve", fake_execve)
    monkeypatch.setattr(agent_ns.os, "_exit", fake_exit)
    with pytest.raises(SystemExit):
        agent_ns.drop_and_exec("/ws", ["/opt/cli", "--x"], {"PATH": "/usr/bin"})
    (path, argv, env), = calls
    assert path == agent_ns.SETPRIV
    sep = argv.index("--")
    assert "--no-new-privs" in argv[:sep]
    assert argv[sep + 1:] == ["/opt/cli", "--x"]
    assert argv[:sep] == ["setpriv", "--reuid", "claude-agent", "--regid", "claude-agent", "--init-groups",
                          "--inh-caps=-all", "--bounding-set=-all", "--no-new-privs"]


def test_the_namespace_check_requires_no_new_privs():
    text = (LAUNCHER / "check-isolation.sh").read_text()
    nnp = text.index("nnp=$(sed -n 's/^NoNewPrivs:[[:space:]]*//p' /proc/self/status")
    # Namespace phase only: the pre-action check runs under a plain sudo -u.
    assert text.index('if [ "$phase" = pre ]; then') < nnp
    assert '[ "$nnp" = 1 ] || fail "no_new_privs is not set' in text


# --- the hosted check ----------------------------------------------------------


def test_the_canary_runs_the_scheduler_boundary_for_both_users():
    text = CANARY.read_text()
    job = text[text.index("\n  scheduler-boundary:\n"):text.index("\n  codex-action-path:\n")]
    assert "user: [claude-agent, codex]" in job
    order = [
        "scheduler_boundary_smoke.sh image",
        "uses: ./.github/actions/create-codex-user\n        with:\n          user: ${{ matrix.user }}",
        'scheduler_boundary_smoke.sh layers "$U"',
        "uses: ./.github/actions/provision-fallback",
        "scheduler_boundary_smoke.sh attempt provisioning",
        'scheduler_boundary_smoke.sh check "$U" scheduler-provisioning.txt',
        "mode: reset-home",
        "uses: ./.github/actions/claude-agent-launcher",
        "bin/agent-ns-launch",
        'scheduler_boundary_smoke.sh check "$U" "$RUNNER_TEMP/claude-agent/scheduler-namespace.txt"',
        "Plant for the pre-launch kill",
        "id: launchpurge",
        "id: resetpurge",
        "The pre-launch purge failed the step and left nothing",
        "Plant for the reclaim",
        "id: reclaimpurge",
        "The reclaim's purge failed the step and left nothing",
    ]
    at = [job.index(o) for o in order]
    assert at == sorted(at), order
    # The purge sites may fail; the checks after them require that they did.
    for sid in ("launchpurge", "resetpurge", "reclaimpurge"):
        site = job[job.index(f"id: {sid}"):]
        assert site.index("continue-on-error: true") < site.index("uses: ")
    assert job.count('[ "$OUTCOME" = failure ]') == 2
    for p in (".github/scripts/purge_agent_schedules.sh", "tests/scheduler_boundary_smoke.sh",
              ".github/actions/create-codex-user/**", ".github/actions/provision-fallback/**"):
        assert f'      - "{p}"\n' in text, p
    assert os.access(SMOKE, os.X_OK)
    subprocess.run(["bash", "-n", str(SMOKE)], check=True)


def test_the_smoke_attempt_reports_one_line_per_route(tmp_path):
    # Run as whoever runs the tests: no spool is writable, no crontab or at
    # may be on PATH; every route is reported, and the mode exits 0.
    r = subprocess.run(["/bin/bash", str(SMOKE), "attempt", "unit"], capture_output=True, text=True, check=False,
                       cwd=tmp_path, env={"PATH": "/nonexistent"})
    assert r.returncode == 0, r.stderr
    keys = [line.split("=", 1)[0] for line in r.stdout.splitlines()]
    assert keys == ["where", "nnp", "crontab", "at", "cron-spool", "at-spool"], r.stdout
