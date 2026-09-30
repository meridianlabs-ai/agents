#!/bin/bash
# Hosted-runner smoke test for "the agent users cannot schedule host-side
# work" (Claude Security finding 4773340, criterion 1;
# design/untrusted-agent-job.md → What stays in the untrusted job). The
# unit tests stub sudo and the spools; the engine isolation canary's
# `scheduler-boundary` job runs this with the real cron and at on a stock
# ubuntu-latest runner, one mode per step. U is the agent user (`codex` or
# `claude-agent`).
#
#   image          (runner) installs and starts cron and at, so both are
#                  exercised whatever the image ships, and records their
#                  modes and the allow and deny files.
#   layers U       (runner, after create-codex-user) each layer alone: the
#                  deny refuses a plain `sudo -u U` crontab and at;
#                  no_new_privs alone, the deny lifted for a moment, refuses
#                  them too (the setgid crontab cannot write its spool);
#                  with neither, U can (the positive control), and those
#                  entries are removed again.
#   attempt WHERE  (as U) tries crontab, at and direct writes into both
#                  spools, and prints what happened, one `key=value` line
#                  each. Always exits 0: `check` judges the result.
#   check U FILE   (runner) FILE, an `attempt` report, shows no_new_privs
#                  set and every attempt refused; then `quiet U`.
#   plant U        (runner) what a bypassed deny would leave: the deny
#                  lifted, root installs a crontab for U and U queues an at
#                  job to run now and one in five minutes. Waits until cron
#                  and atd have each started a U process on the host, so
#                  the purge after it has something to find. The deny stays
#                  lifted, so only the purge stops the daemons.
#   quiet U        (runner) no cron or at entry of U (atq included), no U
#                  process now, and
#                  still none after the next minute boundary, when cron
#                  would have started one; then the deny is put back.
set -uo pipefail

mode=${1:?mode: image | layers U | attempt WHERE | check U FILE | plant U | quiet U}
say() { printf '\n==> %s\n' "$*"; }
fail() { echo "::error::scheduler boundary smoke ($mode): $*" >&2; exit 1; }
cron_spool=/var/spool/cron/crontabs
at_spool=/var/spool/cron/atjobs

need_user() {
  case "${1:-}" in codex|claude-agent) ;; *) fail "user must be codex or claude-agent, not '${1:-}'" ;; esac
}
lift_deny() { sudo sed -i "/^$1\$/d" /etc/cron.deny /etc/at.deny; }
restore_deny() {
  local f
  for f in /etc/cron.deny /etc/at.deny; do
    sudo grep -qxF "$1" "$f" || printf '%s\n' "$1" | sudo tee -a "$f" >/dev/null
  done
}
# U's pending at jobs, as atd lists them (whoever owns the file).
at_jobs() { sudo atq | awk -v u="$1" '$NF == u {print $1}'; }
# U's entries: the crontab named U or owned by U, at jobs owned by U.
entries() {
  sudo find "$cron_spool" -mindepth 1 -maxdepth 1 \( -name "$1" -o -user "$1" \) -print
  sudo find "$at_spool" -mindepth 1 -maxdepth 1 -user "$1" -print
}

case "$mode" in
  image)
    say "cron and at on the image, before"
    for t in crontab at atd cron; do echo "$t: $(command -v "$t" || echo 'not installed')"; done
    ls -l /etc/cron.allow /etc/cron.deny /etc/at.allow /etc/at.deny 2>&1
    say "installing and starting cron and at"
    sudo apt-get update -q >/dev/null || fail "apt-get update failed"
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -yq cron at >/dev/null || fail "installing cron and at failed"
    sudo systemctl start cron atd || fail "starting cron and atd failed"
    systemctl is-active cron atd || fail "cron or atd is not running"
    stat -c '%A %U:%G %n' /usr/bin/crontab /usr/bin/at "$cron_spool" "$at_spool"
    ls -l /etc/cron.allow /etc/cron.deny /etc/at.allow /etc/at.deny 2>&1
    exit 0
    ;;

  layers)
    U=${2:-}; need_user "$U"
    say "the deny files"
    for f in /etc/cron.deny /etc/at.deny; do
      sudo grep -qxF "$U" "$f" || fail "$f does not list $U"
      echo "$f lists $U"
    done
    for f in /etc/cron.allow /etc/at.allow; do
      [ ! -e "$f" ] || fail "$f exists, so the deny file is not read"
    done
    say "the deny alone (plain sudo -u, no no_new_privs)"
    out=$(printf '* * * * * true\n' | sudo -u "$U" crontab - 2>&1) && fail "crontab accepted $U's crontab past the deny"
    echo "crontab: $out"
    grep -q "not allowed" <<<"$out" || fail "crontab refused, but not for the deny"
    out=$(echo true | sudo -u "$U" at now + 10 minutes 2>&1) && fail "at queued $U's job past the deny"
    echo "at: $out"
    grep -q "permission" <<<"$out" || fail "at refused, but not for the deny"
    say "no_new_privs alone (the deny lifted for a moment)"
    lift_deny "$U"
    out=$(printf '* * * * * true\n' | sudo -u "$U" /usr/bin/setpriv --no-new-privs -- crontab - 2>&1) && { restore_deny "$U"; fail "crontab under no_new_privs installed $U's crontab"; }
    echo "crontab under no_new_privs: $out"
    out=$(echo true | sudo -u "$U" /usr/bin/setpriv --no-new-privs -- at now + 10 minutes 2>&1) && { restore_deny "$U"; fail "at under no_new_privs queued $U's job"; }
    echo "at under no_new_privs: $out"
    say "positive control: with neither, $U can"
    ok=1
    printf '* * * * * true\n' | sudo -u "$U" crontab - || ok=0
    echo true | sudo -u "$U" at now + 10 minutes || ok=0
    sudo crontab -u "$U" -r 2>/dev/null || true
    for j in $(at_jobs "$U"); do sudo atrm "$j"; done
    restore_deny "$U"
    [ "$ok" = 1 ] || fail "positive control missing: with no deny and no no_new_privs $U could not use crontab or at, so the refusals above prove nothing"
    [ -z "$(entries "$U")" ] && [ -z "$(at_jobs "$U")" ] || fail "the positive control's entries were not removed: $(entries "$U") $(at_jobs "$U")"
    echo "each layer refuses on its own; the positive control's entries are gone"
    ;;

  attempt)
    where=${2:?where}
    me=$(id -un)
    echo "where=$where user=$me"
    echo "nnp=$(sed -n 's/^NoNewPrivs:[[:space:]]*//p' /proc/self/status)"
    out=$(printf '* * * * * sleep 603\n' | crontab - 2>&1) && echo "crontab=installed" || echo "crontab=refused ($(tr '\n' ' ' <<<"$out"))"
    out=$(echo 'sleep 604' | at now 2>&1) && echo "at=queued" || echo "at=refused ($(tr '\n' ' ' <<<"$out"))"
    ( printf '* * * * * sleep 605\n' >"$cron_spool/$me" ) 2>/dev/null && echo "cron-spool=written" || echo "cron-spool=refused"
    ( printf 'sleep 606\n' >"$at_spool/a00001$me" ) 2>/dev/null && echo "at-spool=written" || echo "at-spool=refused"
    exit 0
    ;;

  check)
    U=${2:-}; need_user "$U"
    file=${3:?report file}
    say "what $U's attempt reached ($file)"
    cat "$file" || fail "no report at $file"
    grep -qx "user=$U" <<<"$(sed -n 's/^where=[^ ]* //p' "$file")" || fail "the attempt did not run as $U"
    grep -qx "nnp=1" "$file" || fail "the attempt ran without no_new_privs"
    for k in crontab at cron-spool at-spool; do
      grep -q "^$k=refused" "$file" || fail "$k was not refused"
    done
    exec bash "$0" quiet "$U"
    ;;

  plant)
    U=${2:-}; need_user "$U"
    say "planting what a bypassed deny would leave for $U"
    # crontab refuses a denied user even to root, and the deny stays lifted
    # until `quiet` has run, so that only the purge stops the daemons.
    lift_deny "$U"
    printf '* * * * * sleep 601\n' | sudo crontab -u "$U" - || fail "root could not install $U's crontab"
    echo 'sleep 602' | sudo -u "$U" at now 2>&1 || fail "$U could not queue an at job with the deny lifted"
    # And one still pending when the purge runs (atd unlinks a job's file
    # when it starts it).
    echo 'sleep 607' | sudo -u "$U" at now + 5 minutes 2>&1 || fail "$U could not queue a pending at job"
    entries "$U"
    [ -n "$(at_jobs "$U")" ] || fail "atq lists no pending job of $U"
    # cron reads a changed crontab at a minute boundary: up to two of them.
    for _ in $(seq 1 75); do
      if pgrep -u "$U" -f 'sleep 601' >/dev/null && pgrep -u "$U" -f 'sleep 602' >/dev/null; then
        pgrep -u "$U" -a
        echo "cron and atd each started a $U process on the host"
        exit 0
      fi
      sleep 2
    done
    pgrep -u "$U" -a
    sudo journalctl -u cron -u atd --since "-4min" --no-pager | tail -20
    fail "positive control missing: cron and atd did not both start a $U process within 150s"
    ;;

  quiet)
    U=${2:-}; need_user "$U"
    say "no scheduler entry and no process of $U"
    left=$(entries "$U")
    [ -z "$left" ] || fail "$U still has scheduler entries: $left"
    [ -z "$(at_jobs "$U")" ] || fail "atq still lists jobs of $U: $(at_jobs "$U")"
    if pgrep -u "$U" -a; then fail "a $U process runs on the host"; fi
    # Past the next minute boundary, when cron would start a job.
    wait=$((75 - 10#$(date +%S)))
    echo "waiting ${wait}s, past the next minute boundary"
    sleep "$wait"
    if pgrep -u "$U" -a; then fail "a $U process appeared on the host after the purge"; fi
    [ -z "$(entries "$U")" ] || fail "$U has scheduler entries again: $(entries "$U")"
    restore_deny "$U"
    echo "$U has no scheduler entry and no process on the host, past a minute boundary"
    ;;

  *) fail "unknown mode" ;;
esac
