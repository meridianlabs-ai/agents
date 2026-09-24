#!/usr/bin/env bash
# The reclaim itself (reclaim-codex-workspace/action.yml, steps 1-9 of its
# description): kill every process of the agent user, refuse a redirected
# git dir or a new embedded repository, take `.git` back and revoke the
# group's write on it and on the workspace root, restore the pre-agent
# config, move hooks aside and pin core.hooksPath/core.fsmonitor. A script
# next to the composite rather than its step body so the Claude jobs' agent
# launcher can run it too (design/executed-paths-residual.md → Pre-agent
# reclaim); the composite's step runs it. Safe to run twice in a job (the
# Claude jobs reclaim before and after the agent): each run restores the
# snapshot afresh and appends the pins once.
#
# Environment: AGENT_USER (`codex`, the default, or `claude-agent`),
# SNAPSHOT and EMBEDDED_SNAPSHOT (the pre-agent `.git/config` copy and
# embedded-`.git` list; default to the paths `create-codex-user` writes),
# SYSTEM_PATH (the root-owned system directories every command resolves
# through), GITHUB_WORKSPACE and RUNNER_TEMP. Runs no git command itself.
set -euo pipefail
# 0. System tools only, whatever GITHUB_PATH prepended: nothing below may
#    resolve through a directory the agent user could write.
export PATH="${SYSTEM_PATH:-/usr/sbin:/usr/bin:/sbin:/bin}"
user="${AGENT_USER:-codex}"
case "$user" in
  codex|claude-agent) ;;
  *) echo "::error::reclaim: user must be codex or claude-agent, not '$user'"; exit 1 ;;
esac
SNAPSHOT="${SNAPSHOT:-$RUNNER_TEMP/git-config.pre-codex}"
EMBEDDED_SNAPSHOT="${EMBEDDED_SNAPSHOT:-$RUNNER_TEMP/embedded-git.pre-codex}"
gitdir="$GITHUB_WORKSPACE/.git"
# The snapshots are the runner's own files from before the grant; if
# one is missing the checks below would either leave the agent's config
# in place for every later step or wave a nested repo through, so
# fail closed rather than fall through.
if [ ! -f "$SNAPSHOT" ]; then
  echo "::error::pre-$user git config snapshot not found at $SNAPSHOT — refusing to reuse the $user-written .git/config."
  exit 1
fi
if [ ! -f "$EMBEDDED_SNAPSHOT" ]; then
  echo "::error::pre-$user embedded-repository snapshot not found at $EMBEDDED_SNAPSHOT — refusing to run git over a tree that may hold a nested .git."
  exit 1
fi
# First: nothing may still be running as the agent user while .git is put
# back — a lingering background process could re-tamper the config
# after the restore below and before the landing step's credentialed
# push. pkill scans /proc once per call and exits 1 when it found
# nothing to kill (0 = killed something, 2/3 = pkill itself failed),
# so repeat until a pass comes up empty — bounded: a survivor that
# keeps forking faster than that is a red step, not a silent one.
clear=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  rc=0
  sudo pkill -KILL -u "$user" || rc=$?
  if [ "$rc" -eq 1 ]; then clear=1; break; fi
  if [ "$rc" -ne 0 ]; then
    echo "::error::pkill -u $user failed (exit $rc) — cannot tell whether $user processes survive; refusing to reclaim the workspace."
    exit 1
  fi
  sleep 0.2
done
if [ "$clear" -ne 1 ]; then
  echo "::error::processes were still running as $user after repeated kills — refusing to reclaim the workspace under them."
  exit 1
fi
# The Claude agent's launch (claude-agent-launcher's agent-ns-launch) gave
# the agent user named-user ACL entries on the action's Workload Identity dir
# and token file, and its own copy of the WIF profile dir in its home
# (design/executed-paths-residual.md → After the agent). Take both back now
# that nothing runs as the agent: the access ACL goes, and with it the group
# bits its mask showed (the action creates both with none), and the copy —
# which holds the credential cache the CLI wrote — is deleted (rm -rf on a
# symlink removes the link). The action's own step end deletes the WIF dir
# too; either may already be gone, and before the agent (the pre-agent
# reclaim) neither exists yet.
if [ "$user" = claude-agent ]; then
  wif="$RUNNER_TEMP/claude-workload-identity"
  for f in "$wif/identity-token" "$wif"; do
    if [ -e "$f" ] && [ ! -L "$f" ]; then
      sudo /usr/bin/python3 -I -c '
import errno, os, sys
try:
    os.removexattr(sys.argv[1], "system.posix_acl_access", follow_symlinks=False)
except OSError as e:
    if e.errno != errno.ENODATA:
        raise
' "$f"
      sudo chmod go-rwx "$f"
    fi
  done
  sudo rm -rf "/home/$user/.anthropic-config"
fi
# Refuse BEFORE the chown: a .git that the agent turned into a symlink
# would otherwise have its target re-owned first. -L on commondir
# too — -e is false for a dangling symlink.
if [ -L "$gitdir" ] || [ ! -d "$gitdir" ] || [ -e "$gitdir/commondir" ] || [ -L "$gitdir/commondir" ]; then
  echo "::error::$user left a redirected git dir (.git is not a plain directory, or .git/commondir exists) — refusing to run git through it."
  exit 1
fi
# Refuse an embedded repository too: a nested .git/config is executed
# by the landing step's git status / git add (is_submodule_modified
# spawns a child git inside it) and is not covered by the restore or
# the env pins. `find` runs as root so a directory the agent made
# unreadable to runner cannot hide one; -P (the default) does not
# follow symlinks, and -name matches a gitfile or symlink named .git
# as well as a directory. Same find, same sort as the snapshot.
sudo find "$GITHUB_WORKSPACE" -mindepth 2 -name .git | LC_ALL=C sort >"$RUNNER_TEMP/embedded-git.post-codex"
if ! cmp -s "$EMBEDDED_SNAPSHOT" "$RUNNER_TEMP/embedded-git.post-codex"; then
  echo "::error::$user left an embedded git repository in the tree — refusing to run git over it (a nested .git/config is executed by the landing step and is not covered by the restore):"
  diff "$EMBEDDED_SNAPSHOT" "$RUNNER_TEMP/embedded-git.post-codex" || true
  exit 1
fi
sudo chown -R runner "$gitdir"
# Revoke the agent group's write grant on .git so a survivor of the
# kill has no write path into it either (chown -R does not follow
# symlinks under .git — GNU default -P — and neither does chmod -R),
# and on the workspace root so it cannot rename .git away and
# replace it wholesale (a same-parent rename needs write on the
# parent only). Root only, not recursive: runner owns it, nothing
# runner-side changes, and the tree keeps its grant.
sudo chmod -R g-w "$gitdir"
sudo chmod g-w "$GITHUB_WORKSPACE"
cp --remove-destination "$SNAPSHOT" "$gitdir/config"
# Hooks are FILES, so the restore does not reach them, and the chown
# above leaves anything the agent wrote in .git/hooks runner-owned and
# executable. Not only `git commit` runs them: `git fetch` runs
# reference-transaction when it updates a tracking ref (the agent can
# delete the local one so the next fetch deterministically does),
# and `git status` runs post-index-change whenever it writes the
# refreshed index — both in steps that carry the machine-account
# token. Move the whole directory aside (mv moves a symlink, not its
# target; nothing runner-side relies on hooks — the landing step
# already points hooksPath at a non-existent path) and list what
# the agent left there, names only, for the log.
if [ -e "$gitdir/hooks" ] || [ -L "$gitdir/hooks" ]; then
  rm -rf "$RUNNER_TEMP/git-hooks.post-codex"
  mv -T "$gitdir/hooks" "$RUNNER_TEMP/git-hooks.post-codex"
  if [ -d "$RUNNER_TEMP/git-hooks.post-codex" ] && [ ! -L "$RUNNER_TEMP/git-hooks.post-codex" ]; then
    find "$RUNNER_TEMP/git-hooks.post-codex" -mindepth 1 ! -name '*.sample' -printf "::warning::.git/hooks/%P found after $user, moved aside (hooks are never run after $user)\n"
  fi
fi
# And pin the two keys a config restore cannot cover into the
# restored config itself, so EVERY later git in the job — the loops'
# hand-back and Surface fetches, claude.yml's open-PR fetch, not just
# the guard and landing steps that also pin them by env — runs
# hook-free and never consults an fsmonitor command the index's
# extension might name. Appended after the cp so the snapshot stays
# a faithful copy of checkout's config.
printf '[core]\n\thooksPath = %s\n\tfsmonitor = false\n' "$RUNNER_TEMP/no-hooks" >>"$gitdir/config"
echo "reclaimed .git from $user ($user processes killed, no embedded repository, group write revoked on .git and the workspace root, hooks moved aside) and restored the pre-$user config with core.hooksPath/core.fsmonitor pinned."
