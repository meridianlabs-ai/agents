#!/usr/bin/python3 -I
"""The Claude agent's namespace: `agent-ns-launch` and `agent-ns-init`.

design/executed-paths-residual.md → The launcher (step 5) and the wrapper,
The agent namespace. One file, installed root-owned by the
`claude-agent-launcher` composite as both
/opt/meridian-agent/bin/agent-ns-launch and /opt/meridian-agent/bin/agent-ns-init;
it runs as whichever its file name says. Stdlib only, run with `-I`, so no
environment variable, working directory or user site reaches the imports.

agent-ns-launch <handoff> <action pid>
    Host side, root, `sudo`'s child: the wrapper (`claude`, which the SDK
    spawned as the CLI) execs `sudo -n agent-ns-launch`. It checks that
    <action pid> is this job's claude-code-action process — `bun`, owned by
    the invoking user, `sudo`'s parent, with `Runner.Worker` among its
    ancestors — and opens a pidfd on it. It reads the handoff (runner-only,
    written by the wrapper) and does the root-side preparation: the flag
    settings file, the Workload Identity ACL entries, the agent's own copy of
    the WIF profile dir and, in `workspace` grant mode, the re-grant of
    `.git` and the workspace root. Then it execs
    `setpriv --pdeathsig KILL -- unshare --pid --fork --kill-child --mount
    --propagation private --mount-proc -- python3 -I agent-ns-init <handoff>`
    with the pidfd as fd 3.

agent-ns-init <handoff>
    The namespace's PID 1, root. It builds the agent's view — the four bind
    mounts staged, tmpfs over the runner's home and /tmp, the binds put back
    at their own paths, the staging detached — closes every other
    descriptor, runs the isolation check as the agent user inside the
    finished namespace and, if it passes, starts the CLI as its only child
    with every privilege dropped. It exits when the CLI does (the kernel then
    kills whatever else runs in the namespace) and kills the namespace when
    the action process behind the pidfd is gone.

Nothing here takes input from the agent: the bind list comes from the grant
mode the launcher recorded in the root-owned run directory, and the handoff
is the runner's own file.
"""

import ctypes
import errno
import json
import os
import pwd
import select
import signal
import stat
import struct
import subprocess
import sys
import time

AGENT_USER = "claude-agent"
# The agent's own ANTHROPIC_CONFIG_DIR (a copy of the action's WIF profile
# dir, so the CLI's credential cache is the agent's). The wrapper names it in
# the env it builds and reclaim.sh deletes it after the agent; keep the three
# in step (tests/test_claude_agent_launcher.py checks them).
AGENT_CONFIG_DIR_NAME = ".anthropic-config"
HANDOFF_MAGIC = b"meridian-agent-handoff-1"
HANDOFF_SECTIONS = ("argv", "env", "unreachable", "settings", "wif-config")
MAX_HANDOFF_BYTES = 8 << 20
MAX_SECTION_ITEMS = 4096
SYSTEM_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
SETPRIV = "/usr/bin/setpriv"
UNSHARE = "/usr/bin/unshare"
PYTHON = "/usr/bin/python3"
BASH = "/bin/bash"
WIF_DIR_NAME = "claude-workload-identity"
WIF_TOKEN_NAME = "identity-token"
# How long the isolation check may take before PID 1 gives up on it.
CHECK_TIMEOUT_S = 120
POLL_S = 0.1


class Refused(Exception):
    """A launch precondition failed; the message says which."""


def log(msg):
    sys.stderr.write(f"{os.path.basename(sys.argv[0])}: {msg}\n")
    sys.stderr.flush()


# --- the root-owned layout ----------------------------------------------------


def install_root():
    """/opt/meridian-agent: the directory above this file's `bin`."""
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def read_run(root, name, *, require_root=True):
    """One value the launcher recorded in <root>/run/<name>: a single line,
    from a regular root-owned file nobody else can write (checked when
    require_root, i.e. always outside the tests)."""
    path = os.path.join(root, "run", name)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"{path} is not a regular file")
        if require_root and (st.st_uid != 0 or st.st_mode & 0o022):
            raise Refused(f"{path} is not root-owned and closed to others")
        data = os.read(fd, 65536)
    finally:
        os.close(fd)
    text = data.decode()
    if text.endswith("\n"):
        text = text[:-1]
    if "\n" in text or "\0" in text:
        raise Refused(f"{path} holds more than one line")
    return text


# --- the handoff --------------------------------------------------------------


def parse_handoff(data):
    """The wrapper's handoff: NUL-terminated fields — the magic, then for each
    section its name, its item count and the items. Every section appears
    exactly once; `settings` has one item and `wif-config` zero or one.
    Returns {section: [bytes, ...]}."""
    if len(data) > MAX_HANDOFF_BYTES:
        raise Refused("handoff too large")
    if not data.endswith(b"\0"):
        raise Refused("handoff is not NUL-terminated")
    fields = data[:-1].split(b"\0")
    if not fields or fields[0] != HANDOFF_MAGIC:
        raise Refused("handoff has the wrong magic")
    out = {}
    i = 1
    while i < len(fields):
        name = fields[i].decode("ascii", "replace")
        if name not in HANDOFF_SECTIONS or name in out:
            raise Refused(f"handoff section {name!r} unknown or repeated")
        if i + 1 >= len(fields) or not fields[i + 1].isdigit():
            raise Refused(f"handoff section {name!r} has no count")
        count = int(fields[i + 1])
        if count > MAX_SECTION_ITEMS or i + 2 + count > len(fields):
            raise Refused(f"handoff section {name!r} count out of range")
        out[name] = fields[i + 2:i + 2 + count]
        i += 2 + count
    missing = [s for s in HANDOFF_SECTIONS if s not in out]
    if missing:
        raise Refused(f"handoff lacks {', '.join(missing)}")
    if len(out["settings"]) != 1:
        raise Refused("handoff must carry exactly one settings document")
    if len(out["wif-config"]) > 1:
        raise Refused("handoff carries more than one WIF profile")
    env = {}
    for item in out["env"]:
        key, sep, value = item.partition(b"=")
        if not sep or not key or not all(c in b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for c in key) or key[:1].isdigit():
            raise Refused(f"handoff env entry {key[:40]!r} is not NAME=VALUE")
        if key in env:
            raise Refused(f"handoff env names {key.decode()} twice")
        env[key] = value
    out["env-map"] = env
    for p in out["unreachable"]:
        if not p.startswith(b"/"):
            raise Refused("handoff unreachable path is not absolute")
    return out


def read_handoff(path, owner_uid):
    """The handoff file, opened without following a link: a regular file of
    the invoking user, closed to everyone else, in a directory of theirs that
    is closed too."""
    if not os.path.isabs(path):
        raise Refused("handoff path is not absolute")
    parent = os.path.dirname(path)
    dst = os.lstat(parent)
    if not stat.S_ISDIR(dst.st_mode) or dst.st_uid != owner_uid or dst.st_mode & 0o077:
        raise Refused(f"handoff directory {parent} is not a 0700 directory of uid {owner_uid}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != owner_uid or st.st_mode & 0o077 or st.st_nlink != 1:
            raise Refused("handoff is not a single-link 0600 file of the invoking user")
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_HANDOFF_BYTES:
                raise Refused("handoff too large")
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


# --- the action process -------------------------------------------------------


def proc_stat(pid, proc="/proc"):
    """(comm, ppid, starttime) from /proc/<pid>/stat; comm may hold spaces
    and parentheses, so split at the last ')'."""
    with open(f"{proc}/{pid}/stat", "rb") as f:
        data = f.read()
    lpar, rpar = data.index(b"("), data.rindex(b")")
    comm = data[lpar + 1:rpar].decode("utf-8", "replace")
    rest = data[rpar + 2:].split()
    return comm, int(rest[1]), int(rest[19])


def check_action_process(action_pid, invoker_uid, launch_ppid, proc="/proc"):
    """The pid the wrapper named is the claude-code-action process of this
    job: its exe is `bun`, it runs as the invoking user, it is the parent of
    the `sudo` that runs this launch, and `Runner.Worker` is among its
    ancestors. Returns its start time, which the caller compares after
    opening the pidfd so a recycled pid is not taken for it."""
    sudo_comm, sudo_ppid, _ = proc_stat(launch_ppid, proc)
    if sudo_comm != "sudo":
        raise Refused(f"launched by {sudo_comm!r}, not sudo")
    if sudo_ppid != action_pid:
        raise Refused(f"sudo's parent is {sudo_ppid}, not the named action process {action_pid}")
    exe = os.readlink(f"{proc}/{action_pid}/exe")
    if os.path.basename(exe) != "bun":
        raise Refused(f"the action process {action_pid} runs {exe}, not bun")
    if os.stat(f"{proc}/{action_pid}").st_uid != invoker_uid:
        raise Refused(f"the action process {action_pid} does not run as uid {invoker_uid}")
    _, ppid, start = proc_stat(action_pid, proc)
    seen = []
    for _ in range(16):
        if ppid <= 1:
            break
        comm, next_ppid, _ = proc_stat(ppid, proc)
        seen.append(comm)
        if comm == "Runner.Worker":
            return start
        ppid = next_ppid
    raise Refused(f"no Runner.Worker above the action process {action_pid} (ancestors: {seen})")


# --- root-side preparation ----------------------------------------------------


def posix_acl(entries):
    """A system.posix_acl_access xattr value: version 2, then (tag, perm, id)
    per entry, sorted as the kernel requires (by tag, then id)."""
    undefined = 0xFFFFFFFF
    data = struct.pack("<I", 2)
    for tag, perm, ident in sorted(entries, key=lambda e: (e[0], e[2] if e[2] is not None else undefined)):
        data += struct.pack("<HHI", tag, perm, undefined if ident is None else ident)
    return data


ACL_USER_OBJ, ACL_USER, ACL_GROUP_OBJ, ACL_MASK, ACL_OTHER = 0x01, 0x02, 0x04, 0x10, 0x20


def grant_wif(wif_dir, invoker_uid, agent_uid):
    """Named-user ACL entries for the agent on the action's WIF dir (search)
    and its identity-token (read). The action creates both fresh (0700,
    0600, the invoking user's) every step and deletes them at its end, and
    this sets the whole access ACL: owner bits as they are, owning group and
    other nothing, the agent's entry and a mask equal to it — so no other
    principal's effective rights change, as isolated-agent's grant_traverse
    keeps the mask (reclaim.sh removes the ACL and the group bits it shows).
    Refused if either is not what the action creates."""
    dfd = os.open(wif_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        st = os.fstat(dfd)
        if st.st_uid != invoker_uid or st.st_mode & 0o077 not in (0, 0o010):
            raise Refused(f"{wif_dir} is not the action's 0700 directory")
        tfd = os.open(WIF_TOKEN_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        try:
            tst = os.fstat(tfd)
            if not stat.S_ISREG(tst.st_mode) or tst.st_uid != invoker_uid or tst.st_nlink != 1 or tst.st_mode & 0o037:
                raise Refused(f"{wif_dir}/{WIF_TOKEN_NAME} is not the action's 0600 token file")
            os.setxattr(dfd, "system.posix_acl_access", posix_acl([
                (ACL_USER_OBJ, (st.st_mode >> 6) & 7, None), (ACL_USER, 1, agent_uid),
                (ACL_GROUP_OBJ, 0, None), (ACL_MASK, 1, None), (ACL_OTHER, 0, None)]))
            os.setxattr(tfd, "system.posix_acl_access", posix_acl([
                (ACL_USER_OBJ, (tst.st_mode >> 6) & 7, None), (ACL_USER, 4, agent_uid),
                (ACL_GROUP_OBJ, 0, None), (ACL_MASK, 4, None), (ACL_OTHER, 0, None)]))
        finally:
            os.close(tfd)
    finally:
        os.close(dfd)


def write_agent_config(home, profile, agent):
    """The agent's own copy of the WIF profile dir: <home>/.anthropic-config/
    configs/default.json, agent-owned, 0700/0600, built from scratch through
    directory descriptors that never follow a link. No agent process is alive
    (the launcher killed them all and the CLI has not started)."""
    subprocess.run(["/usr/bin/rm", "-rf", "--", os.path.join(home, AGENT_CONFIG_DIR_NAME)], check=True)
    hfd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(hfd).st_uid != agent.pw_uid:
            raise Refused(f"{home} is not the agent's home")
        os.mkdir(AGENT_CONFIG_DIR_NAME, 0o700, dir_fd=hfd)
        cfd = os.open(AGENT_CONFIG_DIR_NAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=hfd)
        try:
            os.mkdir("configs", 0o700, dir_fd=cfd)
            sfd = os.open("configs", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cfd)
            try:
                ffd = os.open("default.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=sfd)
                try:
                    os.write(ffd, profile)
                    os.fchown(ffd, agent.pw_uid, agent.pw_gid)
                finally:
                    os.close(ffd)
                os.fchown(sfd, agent.pw_uid, agent.pw_gid)
            finally:
                os.close(sfd)
            os.fchown(cfd, agent.pw_uid, agent.pw_gid)
        finally:
            os.close(cfd)
    finally:
        os.close(hfd)


def write_settings(root, document):
    """The flag settings file the wrapper names with --settings: root-owned
    0644, replaced atomically."""
    json.loads(document)  # a parse failure refuses the launch
    run = os.path.join(root, "run")
    tmp = os.path.join(run, ".settings.json.tmp")
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        os.write(fd, document)
        os.fchmod(fd, 0o644)
    finally:
        os.close(fd)
    os.rename(tmp, os.path.join(run, "settings.json"))


def regrant_workspace(workspace):
    """`workspace` grant mode: give the agent write on `.git` and the
    workspace root again, which the pre-agent reclaim revoked, so it can
    commit. The action's prepare ran its git as the runner in between; this
    runs after the wrapper's last runner-side git."""
    gitdir = os.path.join(workspace, ".git")
    st = os.lstat(gitdir)
    if not stat.S_ISDIR(st.st_mode):
        raise Refused(f"{gitdir} is not a directory")
    for cmd in (["/usr/bin/chgrp", "-R", AGENT_USER, "--", gitdir],
                ["/usr/bin/chmod", "-R", "g+rwX", "--", gitdir],
                ["/usr/bin/find", gitdir, "-type", "d", "-exec", "/usr/bin/chmod", "g+s", "{}", "+"],
                ["/usr/bin/chmod", "g+w", "--", workspace]):
        subprocess.run(cmd, check=True, env={"PATH": SYSTEM_PATH})


# --- agent-ns-launch ----------------------------------------------------------


def launch(argv):
    if len(argv) != 2:
        raise Refused("usage: agent-ns-launch <handoff> <action pid>")
    if os.geteuid() != 0:
        raise Refused("must run as root (through sudo)")
    handoff_path, pid_text = argv
    if not pid_text.isdigit():
        raise Refused("the action pid is not a number")
    action_pid = int(pid_text)
    invoker_uid = int(os.environ.get("SUDO_UID", "-1"))
    if invoker_uid <= 0:
        raise Refused("no invoking user (SUDO_UID)")
    start = check_action_process(action_pid, invoker_uid, os.getppid())
    pidfd = os.pidfd_open(action_pid)
    if proc_stat(action_pid)[2] != start:
        raise Refused("the action process changed while its pidfd was opened")

    root = install_root()
    grant = read_run(root, "grant")
    if grant not in ("workspace", "none"):
        raise Refused(f"unknown grant mode {grant!r}")
    workspace = read_run(root, "workspace")
    runner_temp = read_run(root, "runner-temp")
    handoff = parse_handoff(read_handoff(handoff_path, invoker_uid))
    agent = pwd.getpwnam(AGENT_USER)

    write_settings(root, handoff["settings"][0])
    wif_dir = os.path.join(runner_temp, WIF_DIR_NAME)
    # A missing WIF dir is agent-ns-init's to refuse (its bind is required).
    if os.path.lexists(wif_dir):
        grant_wif(wif_dir, invoker_uid, agent.pw_uid)
    if handoff["wif-config"]:
        write_agent_config(agent.pw_dir, handoff["wif-config"][0], agent)
    if grant == "workspace":
        regrant_workspace(workspace)

    if pidfd != 3:
        os.dup2(pidfd, 3, inheritable=True)
        os.close(pidfd)
    else:
        os.set_inheritable(3, True)
    os.execve(SETPRIV, [
        "setpriv", "--pdeathsig", "KILL", "--",
        UNSHARE, "--pid", "--fork", "--kill-child", "--mount", "--propagation", "private", "--mount-proc", "--",
        PYTHON, "-I", os.path.join(root, "bin", "agent-ns-init"), handoff_path,
    ], {"PATH": SYSTEM_PATH})


# --- agent-ns-init ------------------------------------------------------------


libc = ctypes.CDLL(None, use_errno=True)
MS_RDONLY, MS_NOSUID, MS_NODEV, MS_REMOUNT, MS_BIND = 1, 2, 4, 32, 4096
MNT_DETACH = 2


def mount(source, target, fstype, flags, data=None):
    enc = (lambda s: s.encode() if isinstance(s, str) else s)
    if libc.mount(enc(source), enc(target), enc(fstype), ctypes.c_ulong(flags), enc(data)) != 0:
        e = ctypes.get_errno()
        raise OSError(e, f"mount {source} -> {target}: {os.strerror(e)}")


def umount_detach(target):
    if libc.umount2(target.encode(), MNT_DETACH) != 0:
        e = ctypes.get_errno()
        raise OSError(e, f"umount {target}: {os.strerror(e)}")


def bind_plan(grant, workspace, runner_temp):
    """(path, writable) for each directory the agent sees under the runner's
    home, fixed by the grant mode: the workspace (read-only in `none` mode),
    the landing dir, the review scratch copy (`none` mode only) and the WIF
    dir (read-only). Every one is required."""
    plan = [(workspace, grant == "workspace"),
            (os.path.join(runner_temp, "claude-agent"), True)]
    if grant == "none":
        plan.append((os.path.join(runner_temp, "scratch"), True))
    plan.append((os.path.join(runner_temp, WIF_DIR_NAME), False))
    return plan


def check_plan(plan, runner_home):
    """Every bind lies strictly below the runner's home (the tmpfs covers it,
    and the bind is what puts the directory back) and is written normalised."""
    home = runner_home.rstrip("/")
    for path, _ in plan:
        if os.path.normpath(path) != path or not path.startswith(home + "/"):
            raise Refused(f"bind {path} is not a normalised path below {home}")


def build_view(plan, runner_home, stage_parent="/run"):
    """Steps 2-4 of The agent namespace: stage each bind, cover the runner's
    home and /tmp with tmpfs, put each staged directory back at its own path,
    detach the staging."""
    stage = os.path.join(stage_parent, f"agent-ns.{os.getpid()}.{time.monotonic_ns()}")
    os.mkdir(stage, 0o700)
    staged = []
    for n, (path, writable) in enumerate(plan):
        fd = os.open(path, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            point = os.path.join(stage, str(n))
            os.mkdir(point, 0o700)
            mount(f"/proc/self/fd/{fd}", point, None, MS_BIND)
        finally:
            os.close(fd)
        staged.append((point, path, writable))
    mount("tmpfs", runner_home, "tmpfs", MS_NOSUID | MS_NODEV, "mode=0755")
    mount("tmpfs", "/tmp", "tmpfs", MS_NOSUID | MS_NODEV, "mode=1777")
    for point, path, writable in staged:
        os.makedirs(path, 0o755, exist_ok=True)
        mount(point, path, None, MS_BIND)
        mount(None, path, None, MS_REMOUNT | MS_BIND | MS_NOSUID | MS_NODEV | (0 if writable else MS_RDONLY))
    for point, _, _ in staged:
        umount_detach(point)
        os.rmdir(point)
    os.rmdir(stage)


def close_other_fds(keep):
    for name in os.listdir("/proc/self/fd"):
        fd = int(name)
        if fd not in keep:
            try:
                os.close(fd)
            except OSError as e:
                if e.errno != errno.EBADF:
                    raise


def exit_code(status):
    code = os.waitstatus_to_exitcode(status)
    return code if code >= 0 else 128 - code


def drop_and_exec(cwd, argv, env):
    """In a forked child: the agent user, no capabilities, `cwd` freshly
    resolved, the pidfd closed."""
    try:
        os.close(3)
        os.chdir(cwd)
        os.execve(SETPRIV, ["setpriv", "--reuid", AGENT_USER, "--regid", AGENT_USER, "--init-groups",
                            "--inh-caps=-all", "--bounding-set=-all", "--", *argv], env)
    except BaseException as e:  # noqa: BLE001 — the child must never return
        log(f"exec {argv[0]!r} failed: {e}")
    os._exit(127)


def supervise(child, pidfd, timeout=None):
    """Reap until `child` exits and return its exit code; as PID 1 every
    orphan in the namespace is ours to reap too. If the action process
    behind the pidfd exits first, kill everything in the namespace and
    return None. `timeout` bounds the wait (the isolation check)."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                pid = 0
            if pid == 0:
                break
            if pid == child:
                return exit_code(status)
        ready, _, _ = select.select([pidfd], [], [], POLL_S)
        if ready:
            log("the action process is gone; killing the namespace")
            os.kill(-1, signal.SIGKILL)
            return None
        if deadline is not None and time.monotonic() > deadline:
            log("timed out; killing the namespace")
            os.kill(-1, signal.SIGKILL)
            return None


def init(argv):
    if len(argv) != 1:
        raise Refused("usage: agent-ns-init <handoff>")
    if os.getpid() != 1 or os.geteuid() != 0:
        raise Refused("must run as root as the PID namespace's init")
    # 1. No reference to the old tree through the working directory.
    os.chdir("/")
    os.umask(0o022)
    if not os.readlink("/proc/self/fd/3").startswith("anon_inode:[pidfd]"):
        raise Refused("fd 3 is not the action process's pidfd")
    handoff_path = argv[0]
    root = install_root()
    grant = read_run(root, "grant")
    if grant not in ("workspace", "none"):
        raise Refused(f"unknown grant mode {grant!r}")
    workspace = read_run(root, "workspace")
    runner_temp = read_run(root, "runner-temp")
    runner_home = read_run(root, "runner-home")
    cli = read_run(root, "cli")
    # agent-ns-launch checked the handoff against sudo's invoking user; the
    # environment is gone here, so check it against its owner, which must be
    # neither root nor the agent.
    owner = os.stat(handoff_path, follow_symlinks=False).st_uid
    if owner in (0, pwd.getpwnam(AGENT_USER).pw_uid):
        raise Refused("the handoff is not the runner's")
    handoff = parse_handoff(read_handoff(handoff_path, owner))
    os.unlink(handoff_path)
    env = handoff["env-map"]
    plan = bind_plan(grant, workspace, runner_temp)
    check_plan(plan, runner_home)
    for path, _ in plan:
        if not os.path.isdir(path) or os.path.islink(path):
            raise Refused(f"{path} is missing (or not a plain directory); refusing to start without its bind")
    # 2-4. The view.
    build_view(plan, runner_home)
    # 5. Nothing inherited but stdio and the pidfd.
    close_other_fds({0, 1, 2, 3})

    # 6. The isolation check, as the agent, exactly as the CLI will run.
    check = [BASH, os.path.join(root, "bin", "check-isolation.sh"), "--phase", "namespace",
             "--grant", grant, "--runner-home", runner_home]
    for path, _ in plan:
        check += ["--bind", path]
    check += ["--workspace", workspace, "--landing", os.path.join(runner_temp, "claude-agent"),
              "--wif", os.path.join(runner_temp, WIF_DIR_NAME)]
    if grant == "none":
        check += ["--scratch", os.path.join(runner_temp, "scratch")]
    for p in handoff["unreachable"]:
        check += ["--unreachable", os.fsdecode(p)]
    pid = os.fork()
    if pid == 0:
        os.dup2(2, 1)  # the check's output never reaches the SDK's stdout
        drop_and_exec(workspace, check, env)
    rc = supervise(pid, 3, CHECK_TIMEOUT_S)
    if rc != 0:
        raise Refused(f"the isolation check failed (exit {rc}); the CLI was not started")

    # 7. The CLI, as the namespace's only child.
    pid = os.fork()
    if pid == 0:
        drop_and_exec(workspace, [cli, *[os.fsdecode(a) for a in handoff["argv"]]], env)
    rc = supervise(pid, 3)
    return 137 if rc is None else rc


def main():
    name = os.path.basename(sys.argv[0])
    try:
        if name == "agent-ns-launch":
            launch(sys.argv[1:])
        elif name == "agent-ns-init":
            sys.exit(init(sys.argv[1:]))
        else:
            raise Refused(f"run as agent-ns-launch or agent-ns-init, not {name}")
    except (Refused, OSError, ValueError, KeyError, subprocess.CalledProcessError) as e:
        log(f"refusing to launch the agent: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
