#!/usr/bin/env bash
# The codex user's codex home, as codex-action's runner-side bootstrap
# expects it (create-codex-user/action.yml, steps 5 and 6): the 755 home
# and `.codex` dir the action reads config.toml back from before appending
# its provider block, the named network-on permission profile, and the
# server-info file the action's skipped bootstrap would have pre-touched for
# the proxy. Run by the composite on creation and again in `reset-home`
# mode after provisioning ran as codex, so the home is exactly this at the
# moment codex-action starts — whatever a process running as codex did to it
# in between. Takes the user; runs everything through sudo.
set -euo pipefail
user="$1"
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
echo "codex home $codex_home: config.toml written (workspace_net profile), $GITHUB_RUN_ID.json pre-touched"
