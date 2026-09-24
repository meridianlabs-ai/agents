"""The `claude-agent-launcher` composite (design/executed-paths-residual.md →
The launcher (step 5) and the wrapper, → The agent namespace).

- The wrapper (`claude`), run from a throwaway /opt/meridian-agent layout
  whose run directory names a stub directory as its system PATH: `sudo`
  there records the exec of agent-ns-launch and keeps the handoff, `git`
  answers `ls-remote` with a chosen exit status (and passes everything else
  to the real git). What reaches the handoff is read back with
  agent_ns.parse_handoff: the five servers the action composes are dropped
  from --mcp-config, as is any server holding a privileged token, and a
  caller's server without one survives; a privileged token anywhere else
  (argv, the built env, the settings, .git/config) refuses the launch; job-
  token mode proceeds with the job token as GH_TOKEN; a malformed or
  file-path --mcp-config, a `plugin` argv and a second --settings refuse;
  --version and a call from outside the action step pass through; the env
  is the allow-list; the origin URL is reset and the snapshot retaken; and
  the new-branch precondition (a branch the action's prepare checked out
  must be absent on origin) in each case the design lists.
- agent_ns.py's pure parts: the handoff format, the bind plan per grant
  mode, the POSIX ACL encoding, the action-process check against a fake
  /proc, the run-file and handoff-file checks, exit codes.
- The composite's steps as text (order, ownership, the version pattern run
  against sample run.ts files), check-isolation.sh's argument handling,
  and reclaim.sh's cleanup of the WIF ACL and the agent's config dir
  (Linux: real xattrs).

The namespace itself (mounts, the drop, teardown) needs root and a runner's
process tree; the hosted canary's `claude-launcher` job and codex_path_smoke.sh
run the composite on a real runner.
"""

import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_codex_path import composite_runs, composite_steps  # noqa: E402
from test_land_helpers import git  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / ".github" / "actions" / "claude-agent-launcher"
ACTION = LAUNCHER / "action.yml"
WRAPPER = LAUNCHER / "claude"
AGENT_NS = LAUNCHER / "agent_ns.py"
CHECK = LAUNCHER / "check-isolation.sh"
RECLAIM_SH = ROOT / ".github" / "actions" / "reclaim-codex-workspace" / "reclaim.sh"

spec = importlib.util.spec_from_file_location("agent_ns", AGENT_NS)
agent_ns = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent_ns)

APP = "ghs_APPtoken0123456789abcdefghijklmnopqrstu"
JOB = "ghs_JOBtoken0123456789abcdefghijklmnopqrstu"
PAT = "github_pat_0123456789abcdefghijklmnopqrstuvwxyz"

SUDO_STUB = r"""#!/bin/bash
printf '%s\n' "$*" >>"$STUB_LOG"
[ "$1" = -n ] && shift
case "$1" in
  */bin/agent-ns-launch) cp "$2" "$HANDOFF_COPY"; printf '%s\n' "$3" >"$PID_COPY"; exit 0 ;;
esac
exit 99
"""

GIT_STUB = r"""#!/bin/bash
for a in "$@"; do
  if [ "$a" = ls-remote ]; then
    printf 'ls-remote %s token=%s count=%s\n' "${*: -1}" "${GIT_TOKEN:-}" "${GIT_CONFIG_COUNT:-}" >>"$STUB_LOG"
    exit "${LS_REMOTE_RC:-2}"
  fi
done
exec "$REAL_GIT" "$@"
"""


def mcp(servers):
    import json
    return json.dumps({"mcpServers": servers})


ACTION_SERVERS = {
    "github_comment": {"command": "bun", "args": ["run", "x.ts"], "env": {"GITHUB_TOKEN": APP}},
    "github_inline_comment": {"command": "bun", "env": {"GITHUB_TOKEN": APP}},
    "github_file_ops": {"command": "bun", "env": {"GITHUB_TOKEN": APP}},
    "github": {"command": "docker", "args": ["run", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN", "img"],
               "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": APP}},
    "github_ci": {"command": "bun", "env": {"GITHUB_TOKEN": JOB}},
}


@pytest.fixture
def w(tmp_path):
    opt = tmp_path / "opt" / "meridian-agent"
    (opt / "bin").mkdir(parents=True)
    (opt / "run").mkdir()
    shutil.copy(WRAPPER, opt / "bin" / "claude")
    (opt / "bin" / "claude").chmod(0o755)
    stub = tmp_path / "stub"
    stub.mkdir()
    for name, body in (("sudo", SUDO_STUB), ("git", GIT_STUB)):
        (stub / name).write_text(body)
        (stub / name).chmod(0o755)
    cli = tmp_path / "realcli"
    cli.write_text('#!/bin/sh\necho "REALCLI $*"\n')
    cli.chmod(0o755)
    ws = tmp_path / "ws"
    ws.mkdir()
    git("init", "-q", "-b", "main", cwd=ws)
    git("config", "user.email", "t@example.com", cwd=ws)
    git("config", "user.name", "t", cwd=ws)
    git("commit", "-q", "--allow-empty", "-m", "start", cwd=ws)
    git("remote", "add", "origin", f"https://x-access-token:{APP}@github.com/o/r.git", cwd=ws)
    rt = tmp_path / "temp"
    wif = rt / "claude-workload-identity"
    (wif / "config-abc" / "configs").mkdir(parents=True)
    (wif / "identity-token").write_text("jwt")
    (wif / "config-abc" / "configs" / "default.json").write_text('{"version":"1.0"}')
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text('{"permissions":{"deny":["Bash(git push:*)"]}}')
    tools = {os.path.dirname(shutil.which(t)) for t in ("jq", "git", "cp")}
    run = {
        "grant": "workspace", "cli": str(cli), "workspace": str(ws), "runner-temp": str(rt),
        "path": "/ws/.venv/bin:/opt/meridian-agent/claude/.local/bin:/usr/bin:/bin",
        "runner-root": "/runner-root", "head": "refs/heads/main",
        "system-path": ":".join([str(stub), *sorted(tools), "/usr/bin", "/bin"]),
    }
    for k, v in run.items():
        (opt / "run" / k).write_text(v + "\n")
    env = {
        "PATH": os.environ["PATH"], "HOME": str(home), "LANG": "C.UTF-8",
        "CLAUDE_CODE_ACTION": "1", "CLAUDE_CODE_ENTRYPOINT": "claude-code-github-action",
        "DEFAULT_WORKFLOW_TOKEN": JOB, "GH_TOKEN": APP, "GITHUB_TOKEN": APP,
        "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "7",
        "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_x", "ANTHROPIC_ORGANIZATION_ID": "org",
        "ANTHROPIC_IDENTITY_TOKEN_FILE": str(wif / "identity-token"),
        "ANTHROPIC_CONFIG_DIR": str(wif / "config-abc"), "ANTHROPIC_PROFILE": "default",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc-request", "ACTIONS_RUNTIME_TOKEN": "runtime",
        "GITHUB_ENV": str(rt / "set_env"), "GITHUB_OUTPUT": str(rt / "set_output"),
        "RUNNER_WORKSPACE": str(tmp_path / "work" / "r"),
        "GIT_TOKEN": JOB, "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_1": '!f() { echo username=x-access-token; echo "password=$GIT_TOKEN"; }; f',
        "INPUT_SETTINGS": "{}", "PROMPT": "issue text",
        "STUB_LOG": str(tmp_path / "stub.log"), "HANDOFF_COPY": str(tmp_path / "handoff"),
        "PID_COPY": str(tmp_path / "pid"), "REAL_GIT": shutil.which("git"),
    }
    return {"tmp": tmp_path, "opt": opt, "ws": ws, "rt": rt, "wif": wif, "env": env, "cli": cli}


def launch(w, *args, **env):
    e = dict(w["env"])
    for k, v in env.items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    return subprocess.run([str(w["opt"] / "bin" / "claude"), *args], env=e, capture_output=True, text=True, check=False)


def handoff(w):
    return agent_ns.parse_handoff((w["tmp"] / "handoff").read_bytes())


def argv(h):
    return [a.decode() for a in h["argv"]]


def envmap(h):
    return {k.decode(): v.decode() for k, v in h["env-map"].items()}


def stub_log(w):
    p = w["tmp"] / "stub.log"
    return p.read_text() if p.exists() else ""


BASE = ["--output-format", "stream-json", "--verbose", "--input-format", "stream-json"]


# --- the wrapper: passthrough ---------------------------------------------------


def test_version_passes_through_to_the_real_cli(w):
    r = launch(w, "--version")
    assert r.returncode == 0 and r.stdout.strip() == "REALCLI --version", r.stderr
    assert "agent-ns-launch" not in stub_log(w)


def test_a_call_from_outside_the_action_step_passes_through(w):
    r = launch(w, "-p", "hi", CLAUDE_CODE_ACTION=None)
    assert r.returncode == 0 and r.stdout.strip() == "REALCLI -p hi", r.stderr


def test_the_wrapper_must_be_run_by_its_absolute_path(w):
    r = subprocess.run(["bash", "bin/claude", "-p"], cwd=w["opt"], env=w["env"], capture_output=True, text=True, check=False)
    assert r.returncode == 1 and "absolute path" in r.stderr


# --- the wrapper: the launch ------------------------------------------------------


def test_the_launch_execs_agent_ns_launch_with_the_handoff_and_the_parent(w):
    r = launch(w, *BASE)
    assert r.returncode == 0, r.stderr
    log = stub_log(w)
    assert re.search(rf"^-n {re.escape(str(w['opt']))}/bin/agent-ns-launch {re.escape(str(w['rt']))}/claude-agent-launch/handoff \d+$", log, re.M), log
    assert (w["tmp"] / "pid").read_text().strip() == str(os.getpid())
    hdir = w["rt"] / "claude-agent-launch"
    assert stat.S_IMODE(hdir.stat().st_mode) == 0o700
    assert stat.S_IMODE((hdir / "handoff").stat().st_mode) == 0o600
    h = handoff(w)
    assert argv(h) == BASE + ["--settings", f"{w['opt']}/run/settings.json"]
    assert h["settings"][0].decode() == '{"permissions":{"deny":["Bash(git push:*)"]}}'
    assert h["wif-config"][0].decode() == '{"version":"1.0"}'
    unreach = [p.decode() for p in h["unreachable"]]
    for p in (w["env"]["GITHUB_ENV"], w["env"]["GITHUB_OUTPUT"], "/runner-root/.credentials_rsaparams",
              f"{w['tmp']}/work/_actions", f"{w['env']['HOME']}/.claude/settings.json", str(hdir)):
        assert p in unreach, p


def test_the_agent_env_is_the_allow_list_with_the_job_token(w):
    assert launch(w, *BASE).returncode == 0
    env = envmap(handoff(w))
    assert env["GH_TOKEN"] == env["GITHUB_TOKEN"] == env["GIT_TOKEN"] == JOB
    assert env["HOME"] == "/home/claude-agent"
    assert env["PATH"] == "/ws/.venv/bin:/opt/meridian-agent/claude/.local/bin:/usr/bin:/bin"
    assert env["ANTHROPIC_CONFIG_DIR"] == "/home/claude-agent/" + agent_ns.AGENT_CONFIG_DIR_NAME
    assert env["ANTHROPIC_IDENTITY_TOKEN_FILE"] == f"{w['wif']}/identity-token"
    assert env["CLAUDE_CODE_ACTION"] == "1" and env["GITHUB_REPOSITORY"] == "o/r"
    assert env["GIT_CONFIG_KEY_1"] == "credential.https://github.com.helper"
    for name in env:
        assert not name.startswith(("ACTIONS_", "INPUT_", "OVERRIDE_")), name
    for gone in ("GITHUB_ENV", "GITHUB_OUTPUT", "PROMPT", "DEFAULT_WORKFLOW_TOKEN", "RUNNER_WORKSPACE"):
        assert gone not in env
    assert not any(APP in v for v in env.values())


def test_the_five_action_servers_and_any_token_bearing_server_are_dropped(w):
    servers = dict(ACTION_SERVERS)
    servers["caller_with_token"] = {"command": "node", "args": ["srv.js", "--token", APP]}
    servers["caller_ok"] = {"command": "node", "args": ["srv.js"]}
    r = launch(w, *BASE, "--mcp-config", mcp(servers))
    assert r.returncode == 0, r.stderr
    a = argv(handoff(w))
    value = a[a.index("--mcp-config") + 1]
    import json
    assert json.loads(value) == {"mcpServers": {"caller_ok": {"command": "node", "args": ["srv.js"]}}}
    assert not any(APP in x for x in a)


def test_every_value_of_a_variadic_or_equals_mcp_config_is_rewritten(w):
    r = launch(w, "--mcp-config", mcp(ACTION_SERVERS), mcp({"x": {"command": "y"}}), "--verbose",
               f"--mcp-config={mcp({'github': ACTION_SERVERS['github']})}")
    assert r.returncode == 0, r.stderr
    a = argv(handoff(w))
    assert a[:4] == ["--mcp-config", '{"mcpServers":{}}', '{"mcpServers":{"x":{"command":"y"}}}', "--verbose"]
    assert a[4] == '--mcp-config={"mcpServers":{}}'


@pytest.mark.parametrize("value", ["{not json", "/path/to/mcp.json", "[1]", '{"mcpServers":[]}', "{} {}"])
def test_a_malformed_mcp_config_aborts(w, value):
    r = launch(w, "--mcp-config", value)
    assert r.returncode == 1 and "not a JSON MCP config" in r.stderr, r.stderr
    assert "agent-ns-launch" not in stub_log(w)


def test_an_mcp_config_flag_without_a_value_aborts(w):
    r = launch(w, "--mcp-config", "--verbose")
    assert r.returncode == 1 and "has no value" in r.stderr


@pytest.mark.parametrize("where", ["argv", "env", "git-helper", "settings", "git-config"])
def test_the_app_token_anywhere_else_refuses_the_launch(w, where):
    args, env = list(BASE), {}
    if where == "argv":
        args += ["--append-system-prompt", f"token {APP}"]
    elif where == "env":
        env["ANTHROPIC_BASE_URL"] = f"https://proxy/{APP}"
    elif where == "git-helper":
        env["GIT_CONFIG_VALUE_1"] = f"!echo {APP}"
    elif where == "settings":
        (Path(w["env"]["HOME"]) / ".claude" / "settings.json").write_text('{"env":{"T":"%s"}}' % APP)
    else:
        git("config", "http.extraheader", f"AUTHORIZATION: bearer {APP}", cwd=w["ws"])
    r = launch(w, *args, **env)
    assert r.returncode == 1 and "privileged token" in r.stderr, r.stderr
    assert "agent-ns-launch" not in stub_log(w)


def test_job_token_mode_proceeds_with_the_job_token(w):
    r = launch(w, *BASE, "--mcp-config", mcp({"github_ci": ACTION_SERVERS["github_ci"], "mine": {"command": "x", "env": {"T": JOB}}}),
               GH_TOKEN=JOB, GITHUB_TOKEN=JOB, OVERRIDE_GITHUB_TOKEN=JOB)
    assert r.returncode == 0, r.stderr
    h = handoff(w)
    assert envmap(h)["GH_TOKEN"] == JOB
    a = argv(h)
    # Nothing is privileged, so only the by-name drop applies.
    assert '"mine"' in a[a.index("--mcp-config") + 1] and "github_ci" not in a[a.index("--mcp-config") + 1]


def test_a_github_token_override_other_than_the_job_token_is_privileged(w):
    servers = {"mine": {"command": "x", "env": {"T": PAT}}, "ok": {"command": "y"}}
    r = launch(w, *BASE, "--mcp-config", mcp(servers), GH_TOKEN=PAT, GITHUB_TOKEN=PAT, OVERRIDE_GITHUB_TOKEN=PAT)
    assert r.returncode == 0, r.stderr
    a = argv(handoff(w))
    assert not any(PAT in x for x in a) and '"ok"' in a[a.index("--mcp-config") + 1]
    assert envmap(handoff(w))["GH_TOKEN"] == JOB
    r = launch(w, *BASE, "--append-system-prompt", PAT, GH_TOKEN=PAT, GITHUB_TOKEN=PAT, OVERRIDE_GITHUB_TOKEN=PAT)
    assert r.returncode == 1 and "privileged token" in r.stderr


def test_plugin_argv_and_a_second_settings_are_refused(w):
    r = launch(w, "plugin", "install", "x")
    assert r.returncode == 1 and "'plugin'" in r.stderr
    for args in (["--settings", "/x.json"], ["--settings={}"]):
        r = launch(w, *args)
        assert r.returncode == 1 and "--settings" in r.stderr


@pytest.mark.parametrize("env", [{"DEFAULT_WORKFLOW_TOKEN": None}, {"GIT_TOKEN": APP}, {"GIT_CONFIG_COUNT": "99"},
                                 {"ANTHROPIC_IDENTITY_TOKEN_FILE": "/tmp/other"}, {"ANTHROPIC_CONFIG_DIR": "/tmp/cfg"}])
def test_unexpected_credential_plumbing_refuses(w, env):
    r = launch(w, *BASE, **env)
    assert r.returncode == 1, r.stderr
    assert "agent-ns-launch" not in stub_log(w)


def test_the_origin_url_is_reset_and_the_snapshot_retaken(w):
    assert launch(w, *BASE).returncode == 0
    assert git("remote", "get-url", "origin", cwd=w["ws"]).stdout.strip() == "https://github.com/o/r.git"
    snap = (w["rt"] / "git-config.pre-codex").read_text()
    assert snap == (w["ws"] / ".git" / "config").read_text() and APP not in snap and "email = t@example.com" in snap


def test_an_unknown_grant_mode_refuses(w):
    (w["opt"] / "run" / "grant").write_text("all\n")
    r = launch(w, *BASE)
    assert r.returncode == 1 and "unknown grant mode" in r.stderr


# --- the new-branch precondition --------------------------------------------------


def checkout(w, ref):
    git("checkout", "-q", "-b", ref, cwd=w["ws"])


@pytest.mark.parametrize("case,recorded,new", [
    ("issue run", "refs/heads/main", "claude/issue-12-20260924-1500"),
    ("closed PR", "refs/heads/feature", "claude/pr-7-20260924-1500"),
    ("merged PR", "refs/heads/feature", "claude/pr-8-20260924-1500"),
    ("PR closed between the gate and the prepare", "refs/heads/feature", "claude/pr-9-20260924-1500"),
])
@pytest.mark.parametrize("rc,ok", [(2, True), (0, False), (128, False)])
def test_a_branch_the_prepare_created_must_be_absent_on_origin(w, case, recorded, new, rc, ok):
    (w["opt"] / "run" / "head").write_text(recorded + "\n")
    checkout(w, new)
    r = launch(w, *BASE, LS_REMOTE_RC=str(rc))
    log = stub_log(w)
    assert f"ls-remote refs/heads/{new} token={JOB} count=2" in log, (case, log)
    assert (r.returncode == 0) is ok, (case, r.stderr)
    assert ("agent-ns-launch" in log) is ok
    if rc == 0:
        assert "already exists on origin" in r.stderr
    if rc == 128:
        assert "exit 128" in r.stderr


def test_the_fixed_clock_fallback_collision_refuses(w):
    # setupBranch's fallback name equals its first name within one minute,
    # and both exist on origin: the lookup finds the ref, the launch refuses.
    checkout(w, "claude/issue-12-20260924-1500")
    r = launch(w, *BASE, LS_REMOTE_RC="0")
    assert r.returncode == 1 and "already exists on origin" in r.stderr


def test_an_open_pr_follow_up_and_a_detached_review_do_no_lookup(w):
    assert launch(w, *BASE, LS_REMOTE_RC="0").returncode == 0
    sha = git("rev-parse", "HEAD", cwd=w["ws"]).stdout.strip()
    git("checkout", "-q", "--detach", cwd=w["ws"])
    (w["opt"] / "run" / "head").write_text(f"detached:{sha}\n")
    assert launch(w, *BASE, LS_REMOTE_RC="0").returncode == 0
    assert "ls-remote" not in stub_log(w)


def test_a_moved_detached_head_refuses(w):
    (w["opt"] / "run" / "head").write_text("detached:" + "0" * 40 + "\n")
    git("checkout", "-q", "--detach", cwd=w["ws"])
    r = launch(w, *BASE)
    assert r.returncode == 1 and "neither the checkout the launcher recorded" in r.stderr


# --- agent_ns.py ----------------------------------------------------------------


def make_handoff(sections):
    out = [agent_ns.HANDOFF_MAGIC]
    for name, items in sections:
        out += [name.encode(), str(len(items)).encode(), *items]
    return b"\0".join(out) + b"\0"


GOOD = [("argv", [b"-p"]), ("env", [b"HOME=/h", b"A_B=1=2"]), ("unreachable", [b"/x"]),
        ("settings", [b"{}"]), ("wif-config", [])]


def test_parse_handoff_reads_the_sections():
    h = agent_ns.parse_handoff(make_handoff(GOOD))
    assert h["argv"] == [b"-p"] and h["env-map"] == {b"HOME": b"/h", b"A_B": b"1=2"} and h["wif-config"] == []


@pytest.mark.parametrize("data,why", [
    (make_handoff(GOOD)[:-1], "NUL-terminated"),
    (b"other\0", "magic"),
    (make_handoff(GOOD[:-1]), "lacks wif-config"),
    (make_handoff(GOOD + [("argv", [])]), "unknown or repeated"),
    (make_handoff([("argv", [b"-p"]), ("bogus", [])] + GOOD[1:]), "unknown or repeated"),
    (make_handoff([("env", [b"lower=1"])] + [s for s in GOOD if s[0] != "env"]), "NAME=VALUE"),
    (make_handoff([("env", [b"A=1", b"A=2"])] + [s for s in GOOD if s[0] != "env"]), "twice"),
    (make_handoff([("settings", [])] + [s for s in GOOD if s[0] != "settings"]), "exactly one settings"),
    (make_handoff([("wif-config", [b"a", b"b"])] + GOOD[:-1]), "more than one WIF"),
    (make_handoff([("unreachable", [b"rel"])] + [s for s in GOOD if s[0] != "unreachable"]), "not absolute"),
    (agent_ns.HANDOFF_MAGIC + b"\0argv\0" + b"9" * 6 + b"\0", "count out of range"),
])
def test_parse_handoff_refuses(data, why):
    with pytest.raises(agent_ns.Refused, match=why):
        agent_ns.parse_handoff(data)


def test_bind_plan_per_grant_mode():
    assert agent_ns.bind_plan("workspace", "/h/r/work/a/a", "/h/r/work/_temp") == [
        ("/h/r/work/a/a", True), ("/h/r/work/_temp/claude-agent", True),
        ("/h/r/work/_temp/claude-workload-identity", False)]
    assert agent_ns.bind_plan("none", "/h/r/work/a/a", "/h/r/work/_temp") == [
        ("/h/r/work/a/a", False), ("/h/r/work/_temp/claude-agent", True), ("/h/r/work/_temp/scratch", True),
        ("/h/r/work/_temp/claude-workload-identity", False)]
    agent_ns.check_plan(agent_ns.bind_plan("none", "/h/r/w", "/h/r/t"), "/h/r")
    for bad in ("/elsewhere/w", "/h/r/../x", "/h/r"):
        with pytest.raises(agent_ns.Refused):
            agent_ns.check_plan([(bad, True)], "/h/r")


def test_posix_acl_encoding_sorts_and_marks_undefined_ids():
    data = agent_ns.posix_acl([(agent_ns.ACL_OTHER, 0, None), (agent_ns.ACL_USER, 4, 1234),
                               (agent_ns.ACL_USER_OBJ, 6, None), (agent_ns.ACL_MASK, 4, None),
                               (agent_ns.ACL_GROUP_OBJ, 0, None)])
    import struct
    assert data[:4] == struct.pack("<I", 2)
    entries = [struct.unpack("<HHI", data[i:i + 8]) for i in range(4, len(data), 8)]
    assert entries == [(1, 6, 0xFFFFFFFF), (2, 4, 1234), (4, 0, 0xFFFFFFFF), (0x10, 4, 0xFFFFFFFF), (0x20, 0, 0xFFFFFFFF)]


def fake_proc(tmp_path, procs):
    """procs: {pid: (comm, ppid, exe)}."""
    proc = tmp_path / "proc"
    for pid, (comm, ppid, exe) in procs.items():
        d = proc / str(pid)
        d.mkdir(parents=True)
        fields = ["S", str(ppid)] + ["0"] * 17 + [str(1000 + pid)]
        (d / "stat").write_bytes(f"{pid} ({comm}) ".encode() + " ".join(fields).encode())
        os.symlink(exe, d / "exe")
    return str(proc)


CHAIN = {10: ("Runner.Worker", 1, "/runner/bin/Runner.Worker"), 20: ("bash", 10, "/usr/bin/bash"),
         30: ("bun", 20, "/home/runner/.bun/bin/bun"), 40: ("sudo", 30, "/usr/bin/sudo")}


def test_the_action_process_check_accepts_bun_under_runner_worker(tmp_path):
    proc = fake_proc(tmp_path, CHAIN)
    assert agent_ns.check_action_process(30, os.getuid(), 40, proc) == 1030


@pytest.mark.parametrize("change,pid,uid_delta,why", [
    ({40: ("bash", 30, "/usr/bin/bash")}, 30, 0, "not sudo"),
    ({}, 20, 0, "sudo's parent is 30"),
    ({30: ("node", 20, "/usr/bin/node")}, 30, 0, "not bun"),
    ({}, 30, 1, "does not run as uid"),
    ({10: ("systemd", 1, "/sbin/init")}, 30, 0, "no Runner.Worker"),
])
def test_the_action_process_check_refuses(tmp_path, change, pid, uid_delta, why):
    proc = fake_proc(tmp_path, {**CHAIN, **change})
    with pytest.raises(agent_ns.Refused, match=why):
        agent_ns.check_action_process(pid, os.getuid() + uid_delta, 40, proc)


def test_read_run_takes_one_line_and_refuses_more(tmp_path):
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "grant").write_text("none\n")
    assert agent_ns.read_run(str(tmp_path), "grant", require_root=False) == "none"
    (tmp_path / "run" / "two").write_text("a\nb\n")
    with pytest.raises(agent_ns.Refused, match="more than one line"):
        agent_ns.read_run(str(tmp_path), "two", require_root=False)
    with pytest.raises(agent_ns.Refused, match="root-owned"):
        agent_ns.read_run(str(tmp_path), "grant")  # the tests are not root
    os.symlink(tmp_path / "run" / "grant", tmp_path / "run" / "link")
    with pytest.raises(OSError):
        agent_ns.read_run(str(tmp_path), "link", require_root=False)


def test_read_handoff_wants_a_private_single_link_file_of_the_owner(tmp_path):
    d = tmp_path / "h"
    d.mkdir(mode=0o700)
    f = d / "handoff"
    f.write_bytes(make_handoff(GOOD))
    f.chmod(0o600)
    uid = os.getuid()
    assert agent_ns.read_handoff(str(f), uid) == make_handoff(GOOD)
    with pytest.raises(agent_ns.Refused, match="uid"):
        agent_ns.read_handoff(str(f), uid + 1)
    f.chmod(0o644)
    with pytest.raises(agent_ns.Refused, match="0600"):
        agent_ns.read_handoff(str(f), uid)
    f.chmod(0o600)
    os.link(f, d / "second")
    with pytest.raises(agent_ns.Refused, match="single-link"):
        agent_ns.read_handoff(str(f), uid)
    os.unlink(d / "second")
    os.symlink(f, d / "link")
    with pytest.raises(OSError):
        agent_ns.read_handoff(str(d / "link"), uid)
    d.chmod(0o755)
    with pytest.raises(agent_ns.Refused, match="0700 directory"):
        agent_ns.read_handoff(str(f), uid)
    with pytest.raises(agent_ns.Refused, match="absolute"):
        agent_ns.read_handoff("handoff", uid)


@pytest.mark.skipif(not hasattr(os, "waitstatus_to_exitcode"), reason="Python 3.9+")
def test_exit_codes_map_signals_to_128_plus():
    assert agent_ns.exit_code(3 << 8) == 3
    assert agent_ns.exit_code(9) == 137


def test_the_agent_config_dir_name_is_the_same_in_all_three_places():
    name = agent_ns.AGENT_CONFIG_DIR_NAME
    assert f'agent_config_dir="$agent_home/{name}"' in WRAPPER.read_text()
    assert f'sudo rm -rf "/home/$user/{name}"' in RECLAIM_SH.read_text()


def test_the_launch_chain_is_the_designed_one():
    text = AGENT_NS.read_text()
    assert '"setpriv", "--pdeathsig", "KILL", "--",' in text
    assert 'UNSHARE, "--pid", "--fork", "--kill-child", "--mount", "--propagation", "private", "--mount-proc", "--",' in text
    assert '"--inh-caps=-all", "--bounding-set=-all"' in text and '"--init-groups"' in text
    assert "os.pidfd_open(action_pid)" in text and "os.dup2(pidfd, 3, inheritable=True)" in text
    assert AGENT_NS.read_text().startswith("#!/usr/bin/python3 -I\n")


# --- the composite --------------------------------------------------------------


def test_the_composite_checks_the_path_then_pins_its_own():
    steps = composite_steps(ACTION)
    assert len(steps) == 2
    assert "uses: meridianlabs-ai/agents/.github/actions/assert-runner-only-path@main" in steps[0]
    assert "user: claude-agent" in steps[0] and "protect" not in steps[0]
    body = composite_runs(ACTION)[0]
    code = [line for line in body.splitlines() if line.strip() and not line.strip().startswith("#")]
    assert code[:2] == ["set -euo pipefail", 'export PATH="$SYSTEM_PATH"']
    assert "    default: /usr/sbin:/usr/bin:/sbin:/bin\n" in ACTION.read_text()
    assert "value: /opt/meridian-agent/bin/claude" in ACTION.read_text()


def test_the_composite_installs_root_owned_and_records_what_the_wrapper_trusts():
    body = composite_runs(ACTION)[0]
    for line in ('sudo install -o root -g root -m 755 "$SOURCE_DIR/claude" "$opt/bin/claude"',
                 'sudo install -o root -g root -m 755 "$SOURCE_DIR/agent_ns.py" "$opt/bin/agent-ns-launch"',
                 'sudo install -o root -g root -m 755 "$SOURCE_DIR/agent_ns.py" "$opt/bin/agent-ns-init"',
                 'sudo install -o root -g root -m 644 "$SOURCE_DIR/check-isolation.sh" "$opt/bin/check-isolation.sh"',
                 'sudo env -i HOME="$opt/claude" PATH="$SYSTEM_PATH" bash "$installer" "$version"',
                 'sudo chown -R root:root "$opt/claude"'):
        assert line in body, line
    recorded = set(re.findall(r"^record ([a-z-]+) ", body, re.M))
    wrapper_reads = set(re.findall(r"read_run ([a-z-]+)\)", WRAPPER.read_text()))
    ns_reads = set(re.findall(r'read_run\(root, "([a-z-]+)"\)', AGENT_NS.read_text()))
    assert wrapper_reads | ns_reads <= recorded, (wrapper_reads | ns_reads) - recorded
    # The pre-action check runs as the user, and the kill comes last.
    assert body.index("--phase pre") < body.index('sudo pkill -KILL -u "$user"')


@pytest.mark.parametrize("text,version", [
    ('  const claudeCodeVersion = "2.1.281";\n', "2.1.281"),
    ('const claudeCodeVersion = "2.1.281";\nconst claudeCodeVersion = "2.1.282";\n', None),
    ('const claudeCodeVersion = "latest";\n', None),
    ('const claudeCodeVersion = "2.1.281-beta";\n', None),
    ('const claudeCodeVersion = process.env.V;\n', None),
])
def test_the_pinned_version_is_read_fail_closed(tmp_path, text, version):
    body = composite_runs(ACTION)[0]
    pattern = re.search(r"^pattern='(.*)'$", body, re.M).group(1)
    f = tmp_path / "run.ts"
    f.write_text("export function x() {\n" + text + "}\n")
    count = subprocess.run(["grep", "-cE", pattern, str(f)], capture_output=True, text=True).stdout.strip()
    got = subprocess.run(["sed", "-nE", f"s/{pattern}/\\1/p", str(f)], capture_output=True, text=True).stdout.strip()
    if version is None:
        assert count != "1"
    else:
        assert count == "1" and got == version


def test_the_scripts_are_executable_and_parse():
    for f in (WRAPPER, AGENT_NS, CHECK):
        assert os.access(f, os.X_OK), f
    for f in (WRAPPER, CHECK):
        subprocess.run(["bash", "-n", str(f)], check=True)


@pytest.mark.parametrize("args,why", [([], "--phase pre|namespace"), (["--phase"], "has no value"),
                                      (["--phase", "x"], "--phase pre|namespace"), (["--bogus", "1"], "unknown argument")])
def test_the_isolation_check_refuses_bad_arguments(args, why):
    r = subprocess.run(["bash", str(CHECK), *args], capture_output=True, text=True, check=False)
    assert r.returncode == 2 and why in r.stdout


def test_the_isolation_check_fails_as_anyone_but_the_agent_user():
    r = subprocess.run(["bash", str(CHECK), "--phase", "pre"], capture_output=True, text=True, check=False)
    assert r.returncode == 1 and "not claude-agent" in r.stdout


# --- reclaim.sh: the WIF ACL and the agent's config dir -------------------------


def test_reclaim_cleans_up_after_the_kill_and_before_the_refusals():
    text = RECLAIM_SH.read_text()
    cleanup = text.index('if [ "$user" = claude-agent ]; then')
    assert text.index('sudo pkill -KILL -u "$user"') < cleanup < text.index('if [ -L "$gitdir" ]')
    assert '"system.posix_acl_access"' in text[cleanup:] and 'sudo chmod go-rwx "$f"' in text[cleanup:]


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX ACL xattrs")
def test_reclaim_removes_the_wif_acl_and_the_group_bits_it_showed(tmp_path):
    wif = tmp_path / "claude-workload-identity"
    wif.mkdir(mode=0o700)
    token = wif / "identity-token"
    token.write_text("jwt")
    token.chmod(0o600)
    uid = os.getuid()
    try:
        os.setxattr(wif, "system.posix_acl_access", agent_ns.posix_acl([
            (agent_ns.ACL_USER_OBJ, 7, None), (agent_ns.ACL_USER, 1, uid + 1),
            (agent_ns.ACL_GROUP_OBJ, 0, None), (agent_ns.ACL_MASK, 1, None), (agent_ns.ACL_OTHER, 0, None)]))
    except OSError as e:
        pytest.skip(f"no POSIX ACLs here: {e}")
    assert stat.S_IMODE(wif.stat().st_mode) == 0o710
    # Only the cleanup block, with a sudo that runs the command as us.
    text = RECLAIM_SH.read_text()
    start = text.index('if [ "$user" = claude-agent ]; then')
    block = text[start:text.index("\nfi\n", start) + 4].replace('"/home/$user/', f'"{tmp_path}/home/')
    r = subprocess.run(["bash", "-c", f'set -euo pipefail\nsudo() {{ "$@"; }}\nuser=claude-agent\n{block}'],
                       env={**os.environ, "RUNNER_TEMP": str(tmp_path)}, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    with pytest.raises(OSError):
        os.getxattr(wif, "system.posix_acl_access")
    assert stat.S_IMODE(wif.stat().st_mode) == 0o700 and stat.S_IMODE(token.stat().st_mode) == 0o600


# --- the hosted runs ---------------------------------------------------------------


def test_the_canary_runs_the_launcher_per_grant_mode_without_the_action():
    text = (ROOT / ".github" / "workflows" / "engine-isolation-canary.yml").read_text()
    job = text[text.index("\n  claude-launcher:\n"):]
    assert "grant: [workspace, none]" in job
    # claude-code-action is named (so the runner downloads it) and never run.
    assert "        if: false\n        uses: anthropics/claude-code-action@v1\n" in job
    order = ["uses: ./.github/actions/create-codex-user", "uses: ./.github/actions/reclaim-codex-workspace",
             "uses: ./.github/actions/claude-agent-launcher", "uses: ./.github/actions/assert-runner-only-path",
             "A launch from outside the action is refused", "Post-agent reclaim"]
    assert [job.index(o) for o in order] == sorted(job.index(o) for o in order)
    assert '      - ".github/actions/claude-agent-launcher/**"\n' in text


def test_the_path_smoke_runs_for_both_agent_users():
    wf = (ROOT / ".github" / "workflows" / "codex-path-smoke.yml").read_text()
    assert "user: [codex, claude-agent]" in wf and "SMOKE_USER: ${{ matrix.user }}" in wf
    smoke = (ROOT / "tests" / "codex_path_smoke.sh").read_text()
    assert 'U=${SMOKE_USER:-codex}' in smoke and 'if [ "$U" = codex ]; then CODEX_HOME_DIR' in smoke
