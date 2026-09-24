#!/bin/bash
# The Claude agent user's isolation check, adapted from
# meridianlabs-ai/actions .github/actions/isolated-agent/check_isolation.sh
# (design/executed-paths-residual.md → The launcher, → The agent namespace,
# setup step 6), without its model-broker probes. Runs AS THE AGENT USER and
# fails closed: any probe that finds a way out exits non-zero, and the agent
# never starts. Every probe is negative — it reads no secret.
#
# Two phases:
#
#   --phase pre        the launcher, before the action step, as the user
#                      with plain `sudo -n -u claude-agent` and no namespace:
#                      only what exists before the action (no sudo, no
#                      Docker, Yama ptrace_scope 1 or more).
#   --phase namespace  agent-ns-init, inside the finished agent namespace,
#                      with the CLI's own privilege drop, cwd and
#                      environment, immediately before the CLI starts: the
#                      above, plus no process outside the namespace in /proc
#                      and no readable environ or mem of another user's
#                      process; no connectable .NET diagnostic socket;
#                      nothing under the runner's home but the binds (and
#                      the directories leading to them), and none of the
#                      paths the wrapper named (--unreachable) readable or
#                      writable; no descriptor or cwd leading back to the
#                      original tree; and the grant mode's expectations.
#
# Arguments (namespace phase): --grant workspace|none, --runner-home DIR,
# --bind DIR (each of the four binds), --workspace DIR, --landing DIR,
# --wif DIR, --scratch DIR (none mode), --unreachable PATH (repeatable).
set -uo pipefail

status=0
fail() { echo "::error::agent isolation: $*"; status=1; }

phase="" grant="" runner_home="" workspace="" landing="" wif="" scratch=""
binds=() unreachable=()
while [ $# -gt 0 ]; do
  [ $# -ge 2 ] || { echo "::error::agent isolation: $1 has no value"; exit 2; }
  case "$1" in
    --phase) phase=$2 ;;
    --grant) grant=$2 ;;
    --runner-home) runner_home=$2 ;;
    --bind) binds+=("$2") ;;
    --workspace) workspace=$2 ;;
    --landing) landing=$2 ;;
    --wif) wif=$2 ;;
    --scratch) scratch=$2 ;;
    --unreachable) unreachable+=("$2") ;;
    *) echo "::error::agent isolation: unknown argument $1"; exit 2 ;;
  esac
  shift 2
done
case "$phase" in pre|namespace) ;; *) echo "::error::agent isolation: --phase pre|namespace"; exit 2 ;; esac

me=$(id -un)
[ "$me" = claude-agent ] || fail "running as '$me', not claude-agent"
[ "$(id -u)" != 0 ] || fail "running as root"

# No privilege escalation.
if sudo -n true >/dev/null 2>&1; then fail "the agent user can sudo"; fi
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  fail "the agent user can reach the Docker daemon"
fi
# The daemon's socket itself, whether or not a docker CLI is on PATH.
for sock in /var/run/docker.sock /run/docker.sock; do
  [ -S "$sock" ] || continue
  if /usr/bin/python3 -I - "$sock" <<'PY'
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(5)
try:
    s.connect(sys.argv[1])
    sys.exit(0)
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
  then fail "the Docker socket $sock is connectable"; fi
done

# Yama ptrace scope 1+ (behind the uid boundary and, in the namespace, the
# private /proc).
scope=$(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo missing)
case "$scope" in 1|2|3) ;; *) fail "kernel.yama.ptrace_scope is '$scope'; 1 or stricter is required" ;; esac

if [ "$phase" = pre ]; then
  [ "$status" = 0 ] && echo "agent isolation (pre-action) holds: '$me' cannot sudo or reach Docker, ptrace_scope $scope"
  exit "$status"
fi

for v in grant runner_home workspace landing wif; do
  [ -n "${!v}" ] || { echo "::error::agent isolation: --${v//_/-} is required in the namespace phase"; exit 2; }
done
case "$grant" in workspace|none) ;; *) echo "::error::agent isolation: --grant workspace|none"; exit 2 ;; esac
[ "$grant" = workspace ] || [ -n "$scratch" ] || { echo "::error::agent isolation: --scratch is required in none mode"; exit 2; }

# /proc is the namespace's own: PID 1 is agent-ns-init (root), every other
# process is the agent's. A host process — the sudo/unshare chain, the
# action's bun, Runner.Worker, a step's curl — would show up here.
foreign=0
for d in /proc/[0-9]*; do
  p=${d#/proc/}
  owner=$(stat -c %U "$d" 2>/dev/null) || continue
  if [ "$p" = 1 ]; then
    [ "$owner" = root ] || fail "PID 1 of the namespace runs as '$owner', not root"
    tr '\0' ' ' <"$d/cmdline" 2>/dev/null | grep -q 'agent-ns-init' || fail "PID 1 is not agent-ns-init — this is not the agent namespace"
  elif [ "$owner" != "$me" ]; then
    fail "process $p ($owner: $(tr '\0' ' ' <"$d/cmdline" 2>/dev/null | head -c 80)) from outside the agent is visible in /proc"
  fi
  [ "$owner" = "$me" ] && continue
  foreign=$((foreign + 1))
  for name in environ mem; do
    if cat "$d/$name" >/dev/null 2>&1; then fail "$d/$name ($owner) is readable by the agent"; fi
  done
  if ls "$d/cwd/" >/dev/null 2>&1; then fail "$d/cwd ($owner) is readable by the agent"; fi
done
[ "$foreign" -ge 1 ] || fail "no process outside the agent's user is visible (not even PID 1); cannot confirm the boundary"

# .NET diagnostic sockets: none connectable. /tmp is private, so none should
# exist by path; the network namespace is shared, so every such socket the
# host has is listed in /proc/net/unix — each is tried by its path.
if ! /usr/bin/python3 -I - <<'PY'
import socket, sys
bad = []
paths = set()
try:
    for line in open("/proc/net/unix").read().splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 8 and "dotnet-diagnostic" in parts[7]:
            paths.add(parts[7])
except OSError:
    pass
import glob
paths.update(glob.glob("/tmp/dotnet-diagnostic-*-socket"))
for p in sorted(paths):
    if p.startswith("@"):
        continue
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect(p)
        bad.append(p)
    except OSError:
        pass
    finally:
        s.close()
for p in bad:
    print(f"::error::agent isolation: the .NET diagnostic socket {p} is connectable")
sys.exit(1 if bad else 0)
PY
then status=1; fi

# The runner's home holds only the binds and the directories leading to
# them. A step script, a command file, `_actions`, the runner's install
# directory or a sentinel the job planted would be listed here.
is_bind_or_inside() {
  local p=$1 b
  for b in "${binds[@]}"; do
    case "$p" in "$b"|"$b"/*) return 0 ;; esac
  done
  return 1
}
is_bind_ancestor() {
  local p=$1 b
  for b in "${binds[@]}"; do
    case "$b" in "$p"/*) return 0 ;; esac
  done
  return 1
}
[ "${#binds[@]}" -ge 1 ] || { echo "::error::agent isolation: no --bind given"; exit 2; }
prune=()
for b in "${binds[@]}"; do prune+=(-path "$b" -o); done
while IFS= read -r -d '' p; do
  is_bind_or_inside "$p" && continue
  if is_bind_ancestor "$p"; then
    [ -d "$p" ] && [ ! -L "$p" ] || fail "$p leads to a bind but is not a plain directory"
    continue
  fi
  fail "$p is visible under the runner's home outside the binds"
done < <(find "$runner_home" -mindepth 1 \( "${prune[@]}" -false \) -prune -o -print0 2>/dev/null)

for p in ${unreachable[@]+"${unreachable[@]}"}; do
  if [ -r "$p" ] || [ -w "$p" ]; then fail "$p is reachable by the agent"; fi
done

# No descriptor or working directory leads back to the original tree: the
# cwd is the workspace bind, and every open descriptor that is a directory
# or a file resolves into a bind (or is a pipe/socket/tty).
cwd=$(pwd -P)
[ "$cwd" = "$workspace" ] || fail "the working directory is '$cwd', not the workspace bind $workspace"
[ "$(readlink /proc/self/cwd)" = "$workspace" ] || fail "/proc/self/cwd is '$(readlink /proc/self/cwd)', not $workspace"
for fd in /proc/self/fd/*; do
  t=$(readlink "$fd" 2>/dev/null) || continue
  case "$t" in
    pipe:*|socket:*|anon_inode:*|/dev/*|/proc/*) continue ;;
  esac
  case "$t" in
    "$runner_home"|"$runner_home"/*) is_bind_or_inside "$t" || is_bind_ancestor "$t" || fail "descriptor ${fd##*/} leads to $t" ;;
  esac
done

# The grant mode.
[ -d "$landing" ] && [ -w "$landing" ] || fail "the landing dir $landing is not writable"
[ -d "$wif" ] || fail "the WIF dir $wif is missing"
[ -r "$wif/identity-token" ] || fail "the identity token in $wif is not readable (the ACL grant did not hold)"
if [ "$grant" = workspace ]; then
  [ -w "$workspace" ] || fail "workspace mode, but the workspace root is not writable"
  [ -w "$workspace/.git" ] || fail "workspace mode, but .git is not writable"
else
  probe_name=".agent-isolation-probe.$$"
  if ( : >"$workspace/$probe_name" ) 2>/dev/null; then
    rm -f "$workspace/$probe_name"; fail "none mode, but the agent can create an entry in the workspace root"
  fi
  if ( : >"$workspace/.git/$probe_name" ) 2>/dev/null; then
    rm -f "$workspace/.git/$probe_name"; fail "none mode, but the agent can write .git"
  fi
  if mv "$workspace/.git" "$workspace/.git$probe_name" 2>/dev/null; then
    mv "$workspace/.git$probe_name" "$workspace/.git"; fail "none mode, but the agent can rename .git"
  fi
  tracked=$(find "$workspace" -maxdepth 2 -type f -not -path "$workspace/.git/*" -print -quit 2>/dev/null)
  if [ -n "$tracked" ] && ( : >>"$tracked" ) 2>/dev/null; then fail "none mode, but the agent can write $tracked"; fi
  [ -d "$scratch" ] && [ -w "$scratch" ] || fail "none mode, but the scratch copy $scratch is not writable"
fi

if [ "$status" = 0 ]; then
  echo "agent isolation holds in the namespace: '$me', no sudo or Docker, ptrace_scope $scope, only the namespace's processes in /proc, no .NET socket, nothing under $runner_home but ${#binds[@]} binds, ${#unreachable[@]} runner paths unreachable, grant mode $grant as expected"
fi
exit "$status"
