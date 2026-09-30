#!/usr/bin/env bash
# Remove every cron and at entry the agent user owns, and fail if there was
# one (Claude Security finding 4773340, criterion 1). Run by the runner
# right after each kill of the agent user's processes: the Claude launcher's
# pre-launch kill, the codex `reset-home` kill and the reclaim's kill. A
# `pkill -u` loop leaves the scheduler's entries in place, and the root
# cron or at daemon would start an agent-uid process on the host again
# after the kill, outside any namespace. `create-codex-user` denies cron and
# at to the agent users and every agent-user command starts with
# no_new_privs, so no entry should ever exist; one that does means that
# denial was bypassed, and the job fails here after the purge.
#
# What counts as the user's (Debian's cron and at, as on the hosted Ubuntu
# image): in the crontab spool, a file named after the user (cron runs it
# as that user when root or the user owns it) or owned by the user; in the
# at spool, a job file owned by the user (atd runs a job as its file's
# owner, and unlinks the file when it starts the job, so a running job is
# the kill's). The spools are root's to write, so the removal goes through
# sudo. After a removal, every process of the user is killed again, since
# the daemon may have started one between the caller's kill and the purge.
#
# Environment: AGENT_USER (`codex` or `claude-agent`), SYSTEM_PATH (the
# root-owned system directories every command resolves through),
# CRON_SPOOL and AT_SPOOL (the tests' spools; default to Debian's). Runs no
# git command.
set -euo pipefail
export PATH="${SYSTEM_PATH:-/usr/sbin:/usr/bin:/sbin:/bin}"
user="${AGENT_USER:-}"
case "$user" in
  codex|claude-agent) ;;
  *) echo "::error::purge-agent-schedules: user must be codex or claude-agent, not '$user'"; exit 1 ;;
esac
cron_spool="${CRON_SPOOL:-/var/spool/cron/crontabs}"
at_spool="${AT_SPOOL:-/var/spool/cron/atjobs}"
found=()
list=$(mktemp)
trap 'rm -f "$list"' EXIT
for spool in "$cron_spool" "$at_spool"; do
  # Not installed: nothing to purge there.
  sudo test -d "$spool" || continue
  uid=$(id -u "$user")
  if [ "$spool" = "$cron_spool" ]; then
    match=('(' -name "$user" -o -uid "$uid" ')')
  else
    match=(-uid "$uid")
  fi
  # Into a file first, so a failing find fails the script (the runner's
  # own file: only the find needs root).
  # shellcheck disable=SC2024
  sudo find "$spool" -mindepth 1 -maxdepth 1 "${match[@]}" -print0 >"$list"
  while IFS= read -r -d '' f; do
    found+=("$f")
  done <"$list"
done
[ "${#found[@]}" -eq 0 ] && exit 0
for f in "${found[@]}"; do
  sudo rm -rf -- "$f"
  echo "::error::purge-agent-schedules: removed $f, a scheduler entry of $user"
done
# The daemon may have started a process from an entry before its removal.
clear=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  rc=0
  sudo pkill -KILL -u "$user" || rc=$?
  if [ "$rc" -eq 1 ]; then clear=1; break; fi
  [ "$rc" -eq 0 ] || { echo "::error::purge-agent-schedules: pkill -u $user failed (exit $rc)"; exit 1; }
  sleep 0.2
done
[ "$clear" = 1 ] || echo "::error::purge-agent-schedules: processes were still running as $user after repeated kills"
echo "::error::$user had ${#found[@]} cron or at entries, which create-codex-user denies it; removed them and failed the step."
exit 1
