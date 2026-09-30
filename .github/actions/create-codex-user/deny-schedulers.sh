#!/usr/bin/env bash
# Deny cron and at to the agent user (Claude Security finding 4773340,
# criterion 1). Run as root by create-codex-user's create mode, right after
# the user exists. One argument: the user (`codex` or `claude-agent`); a
# second, the directory holding the files (the tests'; default /etc).
#
# How Debian's crontab and at (the hosted Ubuntu image's) read the files:
# when `<tool>.allow` exists, only the users it lists may use the tool and
# `<tool>.deny` is not read; otherwise a user `<tool>.deny` lists is
# refused. So the user is appended to `cron.deny` and `at.deny` (each
# created when missing), and the step fails if an allow file lists the
# user, since the deny would then not apply. A fresh system user is never
# in an allow file the image ships, so that refusal is a tripwire.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 022
user="${1:-}"
etc="${2:-/etc}"
case "$user" in
  codex|claude-agent) ;;
  *) echo "::error::deny-schedulers: user must be codex or claude-agent, not '$user'"; exit 1 ;;
esac
for tool in cron at; do
  allow="$etc/$tool.allow"
  deny="$etc/$tool.deny"
  if [ -e "$allow" ] && grep -qE "^[[:space:]]*${user}[[:space:]]*\$" "$allow"; then
    echo "::error::deny-schedulers: $allow lists $user, so $deny would not apply"
    exit 1
  fi
  touch "$deny"
  # A last line without its newline would join the name onto it.
  if [ -s "$deny" ] && [ -n "$(tail -c 1 "$deny")" ]; then printf '\n' >>"$deny"; fi
  grep -qxF -- "$user" "$deny" || printf '%s\n' "$user" >>"$deny"
  echo "$tool denied to $user ($deny)"
done
