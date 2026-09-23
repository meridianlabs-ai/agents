#!/bin/bash
# Hosted-runner smoke test for "no root for the agent uid" (Claude Security
# finding 4629153, criterion 2; design/architecture.md → No root for the
# agent uid). The unit tests in test_agent_root_boundary.py stub sudo, id and
# the kernel files; `.github/workflows/root-boundary-smoke.yml` runs this
# with the real ones on a stock ubuntu-latest runner, one mode per step:
#
#   before   (as runner, before the drop) the control: the runner's sudo
#            works, the docker socket answers, a Runner.Worker is found and
#            root can open its memory — so the refusals checked later are
#            refusals, not probes that never worked. Records the image's
#            defaults (Yama scope, groups, sudoers files).
#   runner   (as runner, after this revision's drop-runner-root composite)
#            `sudo -n true` and `sudo -n -l` fail, the docker socket refuses
#            the connection, opening Runner.Worker's /proc/<pid>/mem fails,
#            and pkexec (when installed) does not run anything either.
#   sandbox  (as runner, after the drop) unprivileged bubblewrap, which the
#            reviewer's Claude Code sandbox runs on, still starts.
#   codex    (as runner, which keeps its sudo in the codex jobs, after
#            create-codex-user) the same probes run as the codex user, the
#            way codex-action starts it (`sudo -u codex`, groups from
#            /etc/group), and fail.
#   report   disclosure, not a check: root-owned world-writable directories
#            on root's and systemd's default search path, root's timers and
#            running services — the routes the composite does not cover
#            unless a root process executes from such a directory.
#
# By hand on a disposable Ubuntu box with passwordless sudo, from the repo
# root: `bash tests/root_boundary_smoke.sh before`, then the composite's
# body, then `... runner`. It changes sudo for good: never on a machine you
# keep.
set -uo pipefail

mode=${1:?mode: before | runner | sandbox | codex | report}
say() { printf '\n==> %s\n' "$*"; }
fail() { echo "::error::root boundary smoke ($mode): $*" >&2; exit 1; }
sock=/var/run/docker.sock

# The Runner.Worker that runs this job (one per job on a hosted runner).
worker_pid() {
  local pids
  pids=$(pgrep -x Runner.Worker || true)
  [ "$(printf '%s\n' "$pids" | grep -c .)" -eq 1 ] || fail "expected one Runner.Worker, found: ${pids:-none}"
  printf '%s\n' "$pids"
}
# Each probe prints what happened and returns 0 when the access WORKED.
# $1 is a command prefix to run it as another user ("" = as this user).
probe_sudo() { local out; out=$($1 sudo -n true 2>&1); local rc=$?; echo "sudo -n true: exit $rc ${out:+($out)}"; return $rc; }
probe_sudo_list() { local out; out=$($1 sudo -n -l 2>&1); local rc=$?; echo "sudo -n -l: exit $rc"; printf '%s\n' "$out" | sed 's/^/    /'; return $rc; }
probe_docker() {
  local out; out=$($1 curl -sS --max-time 10 --unix-socket "$sock" http://localhost/_ping 2>&1); local rc=$?
  echo "docker socket $sock /_ping: exit $rc (${out:-no output})"
  [ "$rc" -eq 0 ] && [ "$out" = OK ]
}
probe_mem() {
  local out; out=$($1 python3 -c '
import sys
try:
    open(f"/proc/{sys.argv[1]}/mem", "rb").close()
    print("opened")
except OSError as e:
    print(f"refused: {e}")
    sys.exit(1)
' "$2" 2>&1); local rc=$?
  echo "open /proc/$2/mem (Runner.Worker): $out"; return $rc
}

case "$mode" in
before)
  say "identity and image defaults"
  id; echo "groups in /etc/group: $(id -nG "$(id -un)")"
  echo "kernel.yama.ptrace_scope = $(cat /proc/sys/kernel/yama/ptrace_scope)"
  sudo -n ls -l /etc/sudoers.d/ || true
  sudo -n grep -rn -e "$(id -un)" -e '^%' /etc/sudoers /etc/sudoers.d/ 2>/dev/null | grep -v '^\s*#' || true
  sudo -n passwd -S "$(id -un)" || true
  ls -l "$(readlink -f "$sock")" 2>/dev/null || echo "no docker socket"
  command -v pkexec || echo "no pkexec"
  say "control: every route works before the drop"
  probe_sudo "" || fail "the runner's sudo does not work before the drop: the 'after' check would prove nothing"
  probe_sudo_list "" || fail "sudo -n -l fails before the drop"
  probe_docker "" || fail "the docker socket does not answer the runner before the drop"
  pid=$(worker_pid) || exit 1
  probe_mem "" "$pid" || echo "(the image's Yama scope already refuses the runner; root is the route)"
  probe_mem "sudo -n" "$pid" || fail "root cannot open Runner.Worker's memory either: the probe is broken"
  echo "control passed: sudo, the docker socket and (through root) Runner.Worker's memory are all reachable before the drop."
  ;;
runner)
  say "from the runner, after drop-runner-root"
  id; echo "kernel.yama.ptrace_scope = $(cat /proc/sys/kernel/yama/ptrace_scope)"
  ls -l "$(readlink -f "$sock")" 2>/dev/null || true
  if probe_sudo ""; then fail "sudo -n true succeeds after the drop"; fi
  if probe_sudo_list ""; then fail "sudo -n -l lists rules after the drop"; fi
  if probe_docker ""; then fail "the docker socket answers the runner after the drop"; fi
  pid=$(worker_pid) || exit 1
  if probe_mem "" "$pid"; then fail "the runner opens Runner.Worker's memory after the drop"; fi
  if command -v pkexec >/dev/null; then
    if timeout 20 pkexec --disable-internal-agent true </dev/null >/dev/null 2>&1; then fail "pkexec ran a command as root"; fi
    echo "pkexec: refused"
  fi
  # For the record, not a check: Yama restricts attach-mode access (mem,
  # ptrace, process_vm_readv), not read-mode (environ, cmdline, status).
  if [ -r "/proc/$pid/environ" ] && tr '\0' '\n' <"/proc/$pid/environ" >/dev/null 2>&1; then
    echo "note: /proc/$pid/environ (Runner.Worker) is still readable by the runner — the service environment, not the job's secrets: $(tr '\0' '\n' <"/proc/$pid/environ" | cut -d= -f1 | tr '\n' ' ')"
  fi
  echo "passed: no sudo, no docker socket, no Runner.Worker memory for the runner."
  ;;
sandbox)
  say "unprivileged bubblewrap after the drop (the review sandbox's primitive)"
  # shellcheck disable=SC2016  # expanded inside the sandbox
  out=$(bwrap --new-session --die-with-parent --unshare-pid --unshare-net --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp \
    -- /bin/sh -c 'echo "inside the sandbox as $(id -un), pid $$"' 2>&1) || fail "bwrap did not start after the drop: $out"
  echo "$out"
  ;;
codex)
  say "as the codex user (created by create-codex-user), the runner keeping its sudo"
  id codex
  as="sudo -n -u codex -H"
  if probe_sudo "$as"; then fail "codex has sudo"; fi
  if probe_sudo_list "$as"; then fail "sudo -n -l lists rules for codex"; fi
  if probe_docker "$as"; then fail "the docker socket answers codex"; fi
  pid=$(worker_pid) || exit 1
  if probe_mem "$as" "$pid"; then fail "codex opens Runner.Worker's memory"; fi
  probe_sudo "" >/dev/null || fail "the runner lost its sudo in the codex job (the reclaim needs it)"
  echo "passed: codex has no sudo, no docker socket and no Runner.Worker memory; the runner kept its sudo."
  ;;
report)
  say "root-owned world-writable directories on the default search paths (disclosure)"
  for d in $(printf '%s' "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin" | tr ':' '\n' | awk '!seen[$0]++'); do
    [ -d "$d" ] || continue
    m=$(stat -c '%A %U:%G' "$d")
    case "$m" in ????????w*) echo "WORLD-WRITABLE $m $d" ;; *) echo "ok $m $d" ;; esac
  done
  say "root-owned, world-writable, non-sticky directories on the root filesystem (first 40)"
  timeout 180 find / -xdev -type d -user root -perm -0002 ! -perm -1000 2>/dev/null | head -40
  say "root's timers and running services during the job"
  systemctl list-timers --all --no-pager 2>&1 | head -40
  systemctl list-units --type=service --state=running --no-pager --no-legend 2>&1 | awk '{print $1}' | head -60
  echo "systemd's default service PATH: $(systemctl show --property=DefaultEnvironment 2>/dev/null)"
  ;;
*) fail "unknown mode" ;;
esac
