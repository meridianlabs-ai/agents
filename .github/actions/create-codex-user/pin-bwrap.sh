#!/usr/bin/env bash
# Pin the bubblewrap codex's sandbox uses (decision: Ransom, 2026-09-24).
# Run by the composite's `reset-home` mode, as the runner through sudo,
# after the kill loop and before `codex-home.sh` writes the provisioning's
# bin directories into codex's command PATH.
#
# codex's Linux sandbox helper runs the first `bwrap` on the command's PATH
# that is not under the command's working directory, and falls back to the
# bubblewrap bundled with codex only when there is none
# (codex-rs/sandboxing/src/bwrap.rs `find_system_bwrap_in_path`,
# codex-rs/linux-sandbox/README.md). The command PATH now leads with
# directories the codex user can write (`~codex/.local/bin`, which the
# provisioning's build backend writes unsandboxed, and the workspace's
# `.venv/bin` and `node_modules/.bin`, which codex's own sandboxed commands
# write), and the hosted image ships no bubblewrap, so a planted `bwrap`
# there would run every later command outside the sandbox. So: install the
# distribution's bubblewrap (root-owned `/usr/bin/bwrap`), and re-create two
# root-owned directories that `codex-home.sh` puts FIRST on the command
# PATH: `$pin`, holding only a `bwrap` link to it, and `$pin_copy`, holding
# only a root-owned copy of it. Two, in trees that share no directory but
# `/`, because codex canonicalises each candidate before the cwd test: the
# link counts as `/usr/bin/bwrap`, so a command working in `/usr` or
# `/usr/bin` would skip it and fall through to the writable directories
# (review round 1 of #163). No working directory other than `/` contains
# both `/usr/bin/bwrap` and `/var/lib/codex-bwrap/bwrap`, and at `/` codex
# excludes nothing. So whatever the command's cwd, the first `bwrap` codex
# accepts is one of the two, and neither codex nor anything it runs can
# replace or shadow them.
#
# Idempotent: the install is skipped when the package's binary is already
# there, and both directories are re-created every run.
set -euo pipefail
bwrap=/usr/bin/bwrap
pin=/usr/lib/codex-bwrap
pin_copy=/var/lib/codex-bwrap
user="${1:-codex}"

if [ ! -x "$bwrap" ]; then
  # `install` alone first (the image's package lists are usually current);
  # a failure refreshes the lists and retries, bounded.
  apt_install() {
    sudo env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 \
      install -y --no-install-recommends bubblewrap
  }
  if ! apt_install; then
    for attempt in 1 2 3; do
      if sudo env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 update && apt_install; then
        break
      fi
      if [ "$attempt" = 3 ]; then
        echo "::error::could not install bubblewrap after 3 attempts; refusing to put codex-writable directories on codex's command PATH without a pinned bwrap" >&2
        exit 1
      fi
      sleep $((attempt * 10))
    done
  fi
fi

# Root owns it and nobody else can write it (octal mode, group/other write).
root_only() {
  local uid mode
  read -r uid mode < <(stat -c '%u %a' "$1")
  [ "$uid" = 0 ] && (( (8#$mode & 8#022) == 0 ))
}

# The binary the link names must be the package's: a root-owned regular
# file no one else can write.
if [ -L "$bwrap" ] || [ ! -f "$bwrap" ] || ! root_only "$bwrap"; then
  echo "::error::$bwrap is not a root-owned regular file writable by root alone" >&2
  exit 1
fi

sudo rm -rf "$pin" "$pin_copy"
sudo install -d -o root -g root -m 755 "$pin" "$pin_copy"
sudo ln -s "$bwrap" "$pin/bwrap"
sudo install -o root -g root -m 755 "$bwrap" "$pin_copy/bwrap"

# Every hop from / to each directory is root's alone, so the codex user can
# neither replace what is there nor add a second `bwrap` next to it.
for dir in "$pin" "$pin_copy"; do
  hop="$dir"
  while :; do
    if [ -L "$hop" ] || [ ! -d "$hop" ] || ! root_only "$hop"; then
      echo "::error::$hop is not a root-owned directory writable by root alone" >&2
      exit 1
    fi
    [ "$hop" = / ] && break
    hop=$(dirname "$hop")
  done
  if sudo -u "$user" test -w "$dir"; then
    echo "::error::$user can write $dir" >&2
    exit 1
  fi
  entries=$(ls -A "$dir")
  if [ "$entries" != bwrap ]; then
    echo "::error::$dir must hold only bwrap (has: $entries)" >&2
    exit 1
  fi
done
if [ "$(readlink "$pin/bwrap")" != "$bwrap" ]; then
  echo "::error::$pin/bwrap is not a link to $bwrap" >&2
  exit 1
fi
if [ -L "$pin_copy/bwrap" ] || [ ! -f "$pin_copy/bwrap" ] || ! root_only "$pin_copy/bwrap" \
   || ! cmp -s "$bwrap" "$pin_copy/bwrap"; then
  echo "::error::$pin_copy/bwrap is not a root-owned copy of $bwrap" >&2
  exit 1
fi
echo "bwrap pinned: $pin/bwrap -> $bwrap and $pin_copy/bwrap (a copy; $("$bwrap" --version))"
