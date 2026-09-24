#!/usr/bin/env bash
# The codex user's codex home, as codex-action's runner-side bootstrap
# expects it (create-codex-user/action.yml, steps 5 and 6): the 755 home
# and `.codex` dir the action reads config.toml back from before appending
# its provider block, the named network-on permission profile, and the
# server-info file the action's skipped bootstrap would have pre-touched for
# the proxy. Run by the composite on creation and again in `reset-home`
# mode after provisioning ran as codex, so the home is exactly this at the
# moment codex-action starts — whatever a process running as codex did to it
# in between. Takes the user and, optionally, the provisioning composite's
# `bin` directories (colon-separated, absolute; the reset-home mode passes
# them), which go on the PATH codex gives the commands it runs (below).
# Runs everything through sudo.
set -euo pipefail
user="$1"
bin="${2:-}"
# The pinned bubblewrap's two directories (pin-bwrap.sh), first on that PATH.
pin=/usr/lib/codex-bwrap
pin_copy=/var/lib/codex-bwrap
home="/home/$user"
codex_home="$home/.codex"
# Whatever is there goes (rm -rf on a symlink removes the link, not what it
# points at); the directory comes back codex-owned and 755.
sudo rm -rf "$codex_home"
sudo chmod 755 "$home"
sudo install -d -o "$user" -g "$user" -m 755 "$codex_home"
# The server-info file the action's skipped bootstrap would have pre-touched
# for the proxy (root-owned, 666 until the action locks it down).
sudo touch "$codex_home/$GITHUB_RUN_ID.json"
sudo chmod 666 "$codex_home/$GITHUB_RUN_ID.json"
# A NAMED profile, not `[sandbox_workspace_write] network_access`:
# codex-action selects the profile explicitly (`-c
# default_permissions=":workspace"`), and codex deliberately ignores the
# legacy [sandbox_workspace_write] table for an explicit builtin selection
# (core/src/config/mod.rs: "explicitly selecting `:workspace` intentionally
# ignores those legacy settings") — the 2026-09-09 attempt wrote that key
# and changed nothing: trio still died on setsockopt in every 2026-09-10
# round while the detector reported the key present. Named profiles may
# `extends` a builtin (core/src/config/permissions.rs,
# extensible_builtin_parent_profile) and their `[..network] enabled = true`
# compiles straight to NetworkSandboxPolicy::Enabled
# (compile_network_sandbox_policy), so this inherits :workspace's filesystem
# rules, pins the checkout as a workspace root, and turns the seccomp
# network filter off. codex-action accepts a custom profile name (only
# three unrelated config roots are restricted for custom profiles).
printf '%s\n' \
  '# Added by the meridianlabs-ai/agents workflow (design/codex-engine.md → Network).' \
  '[permissions.workspace_net]' \
  'extends = ":workspace"' \
  '[permissions.workspace_net.workspace_roots]' \
  '"." = true' \
  '[permissions.workspace_net.network]' \
  'enabled = true' \
  | sudo -u "$user" tee "$codex_home/config.toml" >/dev/null
# The provisioned tools on the PATH of every command codex runs
# (executed-paths follow-up, option B; decision: Ransom, 2026-09-23).
# codex-action starts codex as `sudo -u <user> -- codex exec`, so codex
# inherits sudo's reset PATH (secure_path), which has none of the
# provisioning's bin directories: a tool named by absolute path runs, but one
# that finds another by name does not (ts-mono's turbo-based `pnpm check`:
# "Unable to find package manager binary", pnpm being only in
# ~codex/.local/bin). codex builds each command's environment from
# `shell_environment_policy`, whose `set` table overrides what codex
# inherited; codex-action rejects that key in `codex-args`, so it goes in
# this file like the profile above. The value is the pinned bubblewrap's
# two directories, then the bin directories, then the PATH sudo gives the user,
# probed the way codex-action launches codex. The pin comes first because
# codex's sandbox runs the first `bwrap` on this PATH outside the command's
# working directory, and the bin directories are codex-writable: without it
# a planted `bwrap` would run codex's commands unsandboxed. It is two
# directories in disjoint trees so that one survives any working directory
# (pin-bwrap.sh, which the reset-home mode runs first; this refuses without
# them). Only
# codex's commands see the PATH: nothing here touches the job PATH
# (GITHUB_PATH) the runner-side steps resolve through. Written as a TOML
# literal string, so a character one cannot hold (a single quote, a control
# character) is refused rather than escaped, and so is an entry that is not
# an absolute directory (an empty one would mean the working directory).
if [ -n "$bin" ]; then
  if [ ! -L "$pin/bwrap" ] || [ -L "$pin_copy/bwrap" ] || [ ! -f "$pin_copy/bwrap" ]; then
    echo "::error::no pinned bwrap at $pin/bwrap and $pin_copy/bwrap (pin-bwrap.sh runs first); refusing to put codex-writable directories on codex's command PATH" >&2
    exit 1
  fi
  base=$(sudo -u "$user" -- /usr/bin/printenv PATH) || base=""
  if [ -z "$base" ]; then
    echo "::error::could not read the PATH sudo gives $user" >&2
    exit 1
  fi
  path="$pin:$pin_copy:$bin:$base"
  if [[ "$path" == *"'"* ]] || ! [[ "$path" =~ ^[[:print:]]+$ ]]; then
    echo "::error::the codex command PATH has a character a TOML literal string cannot hold: $path" >&2
    exit 1
  fi
  if [[ ":$path:" == *::* ]] || [[ ":$path:" =~ :[^/] ]]; then
    echo "::error::the codex command PATH has an empty or relative entry: $path" >&2
    exit 1
  fi
  printf '%s\n' \
    '[shell_environment_policy.set]' \
    "PATH = '$path'" \
    | sudo -u "$user" tee -a "$codex_home/config.toml" >/dev/null
fi
sudo chmod 644 "$codex_home/config.toml"
# What codex-action's runner-side writeProxyConfig will read and re-write:
# a regular file with exactly this content, owned by the user.
if [ -L "$codex_home/config.toml" ] || [ ! -f "$codex_home/config.toml" ]; then
  echo "::error::$codex_home/config.toml is not a regular file after writing it" >&2
  exit 1
fi
if [ -L "$codex_home" ] || [ ! -d "$codex_home" ]; then
  echo "::error::$codex_home is not a directory after creating it" >&2
  exit 1
fi
echo "codex home $codex_home: config.toml written (workspace_net profile${bin:+, command PATH $path}), $GITHUB_RUN_ID.json pre-touched"
