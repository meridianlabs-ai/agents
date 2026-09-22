set -euo pipefail
[ ! -e pyproject.toml ] || { echo "::error::this fixture must have no pyproject.toml"; exit 1; }
nv=$($AS_CODEX node --version)
major=${nv#v}; major=${major%%.*}
[ "$major" -ge 22 ] || { echo "::error::expected Node >= 22 (engines), got $nv"; exit 1; }
pv=$($AS_CODEX /home/codex/.local/bin/pnpm --version)
[ "$pv" = "11.22.0" ] || { echo "::error::expected pnpm 11.22.0 from packageManager, got $pv"; exit 1; }
$AS_CODEX node_modules/.bin/prettier --version
[ "$(sha256sum <pnpm-lock.yaml)" = "$LOCK_BEFORE" ] || { echo "::error::pnpm install changed the lockfile despite --frozen-lockfile"; exit 1; }
echo "ts-mono-like: Node $nv, pnpm $pv via corepack in ~codex/.local/bin, frozen install, prettier runs as codex"
